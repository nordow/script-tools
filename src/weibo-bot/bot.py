import argparse
import copy
import importlib
import logging
import os
import random
import signal
import sys
import tomllib
import urllib.request
from http.cookies import SimpleCookie
from logging import config
from threading import Event
from typing import Any

from apscheduler import events
from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.schedulers.base import BaseScheduler
from apscheduler.triggers.cron import CronTrigger
from RestrictedPython import compile_restricted, safe_builtins
from RestrictedPython.Eval import (default_guarded_getitem,
                                   default_guarded_getiter)
from selenium import webdriver
from selenium.webdriver.chrome.webdriver import WebDriver
from selenium.webdriver.common.by import By
from selenium.webdriver.common.keys import Keys
from selenium.webdriver.remote.webelement import WebElement
from selenium.webdriver.support import expected_conditions as EC
from selenium.webdriver.support.ui import WebDriverWait


__all__ = ["Bot"]


_SYSTEM = "System"

_EVENT_PROCESS = "Process"
_EVENT_EXECUTION = "Execution"


_logger = logging.getLogger(__name__)


def _safe_eval(expr: str, globals: dict[str, Any] | None = None, locals: dict[str, Any] | None = None) -> Any:
    code = compile_restricted(expr, mode = "eval")

    return eval(code, {
        "__builtins__": {
            **safe_builtins,

            "_getitem_": default_guarded_getitem,
            "_getiter_": default_guarded_getiter
        },

        **(globals if globals is not None else {})
    }, locals)

def _format_fstring(fstring: str, **kwargs) -> str:
    return _safe_eval(f'f{repr(fstring)}', kwargs)

def _format_message(sender, event, message, root: bool = False) -> str:
    return f"{f'<{sender}>' if root else f'[{sender}]':<16} - {event:<9} | {message}"

def _try_delete_file(path: str) -> bool:
    try:
        os.remove(path)

        return True
    except OSError:
        return False


class FullCronTrigger(CronTrigger):

    @classmethod
    def from_cron(cls, expr, timezone = None, jitter = None):
        values = expr.split()

        match len(values):
            case 5:
                return cls(minute = values[0], hour = values[1], day = values[2], month = values[3],
                           day_of_week = values[4], timezone = timezone, jitter = jitter)
            case 6:
                return cls(second = values[0], minute = values[1], hour = values[2], day = values[3],
                           month = values[4], day_of_week = values[5], timezone = timezone, jitter = jitter)
            case 7:
                return cls(second = values[0], minute = values[1], hour = values[2], day = values[3],
                           month = values[4], day_of_week = values[5], year = values[6], timezone = timezone, jitter = jitter)
            case _:
                raise ValueError(f"Wrong number of fields; got {len(values)}, expected 5 or 6 or 7")


class User:
    drivers: dict[str, WebDriver]
    scheduler: BaseScheduler

    def __init__(self, drivers: dict[str, WebDriver], scheduler: BaseScheduler) -> None:
        self.drivers = drivers
        self.scheduler = scheduler


class Bot:
    __conf: dict[str, Any]
    __preview: bool
    __users: dict[str, User] | None

    def __init__(self, conf: dict[str, Any], preview: bool) -> None:
        self.__conf = conf
        self.__preview = preview
        self.__users = None

    def init(self) -> None:

        def import_mods(mods: dict[str, Any], envs: dict[str, Any], user_name: str) -> dict[str, Any]:

            def normalize_mod(mod, mod_id: str) -> dict[str, Any]:
                if isinstance(mod, str):
                    return {
                        "type": "module",
                        "value": mod
                    }
                elif isinstance(mod, dict):
                    return {
                        "type": mod["type"],
                        "value": mod["value"]
                    }
                else:
                    raise TypeError(f"Wrong type of mod '{mod_id}'; got '{type(mod).__name__}', expected '{str.__name__}' or '{dict.__name__}[{str.__name__}, Any]'")

            imported_mods: dict[str, Any] = {}

            for mod_name, mod_conf in mods.items():
                mod_id = f"{user_name}.{mod_name}"

                mod = normalize_mod(mod_conf, mod_id)
                mod_type: str = mod["type"]
                mod_value: str = mod["value"]

                imported_mod: Any

                match mod_type:
                    case "module":
                        imported_mod = importlib.import_module(mod_value)
                    case "expression":
                        imported_mod = _safe_eval(mod_value, {
                            "envs": envs,
                            "mods": imported_mods
                        })
                    case _:
                        raise ValueError(f"Wrong value of type for mod '{mod_id}'; got {repr(mod_type)}, expected 'module' or 'expression'")

                imported_mods[mod_name] = imported_mod

            return imported_mods

        def load_page(driver: WebDriver, cookies: str) -> None:
            app_xpath = '//div[@id="app"]'

            driver.get("https://weibo.com")

            loading_wait = WebDriverWait(driver, 30)

            loading_wait.until(EC.presence_of_element_located((By.XPATH, app_xpath)))

            for key, morsel in SimpleCookie(cookies).items():
                cookie = {
                    "domain": ".weibo.com",
                    "name": key,
                    "value": morsel.value,
                    "expires": "",
                    "path": "/",
                    "httpOnly": False,
                    "HostOnly": False,
                    "Secure": False
                }

                driver.add_cookie(cookie)

            home_wrap_xpath = '//div[@id="homeWrap"]'

            driver.refresh()
            loading_wait.until(EC.presence_of_element_located((By.XPATH, home_wrap_xpath)))

            driver.execute_script("window.open('', '_blank')")
            driver.close()
            driver.switch_to.window(driver.window_handles[-1])

        def send_post(driver: WebDriver, args: dict[str, Any], preview: bool) -> None:

            def eval_vars(vars: dict[str, Any], envs: dict[str, Any], mods: dict[str, Any]) -> dict[str, Any]:
                evaluated_vars: dict[str, Any] = {}

                for var_name, var_expr in vars.items():
                    evaluated_var = _safe_eval(var_expr, {
                        "envs": envs,
                        "mods": mods,
                        "vars": evaluated_vars
                    })

                    evaluated_vars[var_name] = evaluated_var

                return evaluated_vars

            def execute_commands(commands: dict[str, Any] | None, group: str, kwargs: dict[str, Any] | None, job_id: str) -> None:

                def normalize_commands(commands: dict[str, Any] | None, group: str, job_id: str) -> list[str] | None:
                    if commands is None:
                        return None

                    if (group_commands := commands.get(group)) is None:
                        return None

                    if isinstance(group_commands, str):
                        return [group_commands]
                    elif isinstance(group_commands, list):
                        return group_commands
                    else:
                        raise TypeError(f"Wrong type of {group} commands for [{job_id}]; got '{type(group_commands).__name__}', expected '{str.__name__}' or '{list.__name__}[{str.__name__}]'")

                if not (group_commands := normalize_commands(commands, group, job_id)):
                    return

                for command in group_commands:
                    _safe_eval(command, kwargs)

            def normalize_template(template, job_id: str) -> dict[str, Any]:
                if isinstance(template, str):
                    return {
                        "text": template,
                        "images": []
                    }
                elif isinstance(template, dict):
                    return {
                        "text": template["text"],
                        "images": template.get("images", [])
                    }
                else:
                    raise TypeError(f"Wrong type of template for [{job_id}]; got '{type(template).__name__}', expected '{str.__name__}' or '{dict.__name__}[{str.__name__}, Any]'")

            def convert_url_to_path(url: str) -> tuple[str, bool]:
                if os.path.isfile(url):
                    return (os.path.abspath(url), False)

                return (urllib.request.urlretrieve(url)[0], True)

            job_id: str = args["job_id"]

            envs: dict[str, Any] = args["envs"]
            mods: dict[str, Any] = args["mods"]
            vars: dict[str, Any] = args["vars"]

            select: str | None = args["select"]
            commands: dict[str, Any] | None = args["commands"]
            templates: list[str | dict[str, Any]] | None = args["templates"]

            if not templates:
                raise ValueError(f"Wrong value of templates for [{job_id}]; got {templates}, expected [templates]{{1,}}")

            template_conf: str | dict[str, Any]

            match select:
                case None | "random":
                    template_conf = random.choice(templates)
                case _:
                    raise ValueError(f"Wrong value of select for [{job_id}]; got {repr(select)}, expected 'random'")

            template = normalize_template(template_conf, job_id)
            template_text = template["text"]
            template_images = template["images"]

            job_kwargs = {
                "envs": envs,
                "mods": mods,
                "vars": eval_vars(vars, envs, mods)
            }

            execute_commands(commands, "pre", job_kwargs, job_id)

            text = _format_fstring(template_text, **job_kwargs)
            images = [_format_fstring(template_image, **job_kwargs) for template_image in template_images]

            _logger.info(
                _format_message(
                    sender = job_id,
                    event = _EVENT_PROCESS,
                    message = f'{repr(template_conf)} -> {repr(text if not images else { "text": text, "images": images })}'
                )
            )

            if not images and text.isspace():
                raise ValueError(f"Wrong value of formatted text for [{job_id}]; got {repr(text)}, expected not whitespace, if there is no images")

            if not preview:
                driver.execute_script("window.open('https://weibo.com', '_blank')")
                driver.switch_to.window(driver.window_handles[-1])

                try:
                    element_wait = WebDriverWait(driver, 30)
                    execution_wait = WebDriverWait(driver, 15)

                    text_textarea_xpath = '//div[@id="homeWrap"]/div[1]/div/div[1]/div/textarea'
                    send_button_xpath = '//div[@id="homeWrap"]/div[1]/div/div[4]/div/div[5]/button'

                    text_textarea: WebElement = element_wait.until(EC.presence_of_element_located((By.XPATH, text_textarea_xpath)))
                    send_button: WebElement = element_wait.until(EC.presence_of_element_located((By.XPATH, send_button_xpath)))

                    # text_textarea.clear()
                    text_textarea.send_keys(Keys.CONTROL, "A")
                    text_textarea.send_keys(Keys.DELETE)
                    execution_wait.until_not(EC.element_to_be_clickable((By.XPATH, send_button_xpath)))

                    if images:
                        file_input_xpath = '//div[@id="homeWrap"]/div[1]/div/div[2]/div/div/div[{index}]/div/div/input'

                        file_input: WebElement = element_wait.until(EC.presence_of_element_located((By.XPATH, file_input_xpath.format(index = 1))))
                        file_input_accept: str = file_input.get_attribute("accept")

                        files: list[tuple[str, bool]] = []

                        try:
                            for image in images:
                                files.append(convert_url_to_path(image))

                            file_input.send_keys("\n".join((file[0] for file in files)))
                            execution_wait.until(EC.element_to_be_clickable((By.XPATH, send_button_xpath)))

                            upload_wait = WebDriverWait(driver, 60)

                            item_div_xpath = '//div[@id="homeWrap"]/div[1]/div/div[2]/div/div/div'
                            cover_img_xpath = '//div[@id="homeWrap"]/div[1]/div/div[2]/div/div/div[{index}]/div/div/img'

                            file_div_list: list[WebElement] = execution_wait.until(EC.presence_of_all_elements_located((By.XPATH, item_div_xpath)))[:-1]

                            if (files_diff := len(files) - len(file_div_list)) > 0:
                                raise ValueError(f"Find {files_diff} unacceptable formatted image(s) for [{job_id}]; got {images}, expected [images]{{0,18}} or [images and videos]{{0,9}}, accepted {repr(file_input_accept)}")

                            for index in range(len(file_div_list)):
                                upload_wait.until(EC.presence_of_element_located((By.XPATH, cover_img_xpath.format(index = 1 + index))))

                        finally:
                            for path in (file[0] for file in files if file[1]):
                                _try_delete_file(path)

                    text_textarea.send_keys(text)
                    execution_wait.until(EC.element_to_be_clickable((By.XPATH, send_button_xpath)))

                    # send_button.click()
                    driver.execute_script("arguments[0].click();", send_button)
                    execution_wait.until_not(EC.element_to_be_clickable((By.XPATH, send_button_xpath)))

                finally:
                    driver.close()
                    driver.switch_to.window(driver.window_handles[-1])

            execute_commands(commands, "post", job_kwargs, job_id)

        conf = self.__conf
        preview = self.__preview
        users: dict[str, User] = {}

        for user_name, user_conf in conf.items():
            timezone: str | None = user_conf.get("timezone", conf["default"].get("timezone"))
            cookies: str = user_conf.get("cookies", conf["default"]["cookies"])
            envs: dict[str, Any] = copy.deepcopy({
                **(conf["default"].get("envs", {})),
                **(user_conf.get("envs", {}))
            })
            mods: dict[str, Any] = import_mods({
                **(conf["default"].get("mods", {})),
                **(user_conf.get("mods", {}))
            }, envs, user_name)
            vars: dict[str, Any] = copy.deepcopy({
                **(conf["default"].get("vars", {})),
                **(user_conf.get("vars", {}))
            })
            jobs: dict[str, dict[str, Any]] = user_conf.get("jobs", conf["default"].get("jobs", {}))
            drivers: dict[str, WebDriver] = {}
            scheduler = BackgroundScheduler()

            for job_name, job_conf in jobs.items():
                job_id = f"{user_name}.{job_name}"

                options = webdriver.ChromeOptions()

                options.add_argument("--no-sandbox")
                options.add_argument("--headless")
                options.add_argument("--incognito")
                options.add_argument("--disable-gpu")
                options.add_argument("--disable-dev-shm-usage")
                options.add_argument(f"--user-data-dir={os.path.expanduser('~/.config/google-chrome')}")
                options.add_argument(f"--profile-directory={job_id}")

                driver = webdriver.Chrome(options = options)

                driver.maximize_window()

                load_page(driver, cookies)

                drivers[job_name] = driver

                job_cron: str = job_conf["cron"]
                job_jitter: int | None = job_conf.get("jitter")
                job_select: str | None = job_conf.get("select")
                job_commands: dict[str, Any] | None = job_conf.get("commands")
                job_templates: list[str | dict[str, Any]] | None = job_conf.get("templates")

                scheduler.add_job(send_post, FullCronTrigger.from_cron(job_cron, timezone, job_jitter), kwargs = {
                    "args": {
                        "job_id": job_id,

                        "envs": envs,
                        "mods": mods,
                        "vars": vars,

                        "select": job_select,
                        "commands": job_commands,
                        "templates": job_templates,
                    },

                    "preview": preview,
                    "driver": driver
                }, id = job_id)

            scheduler.add_listener(
                lambda event: _logger.info(
                    _format_message(
                        sender = event.job_id,
                        event = _EVENT_EXECUTION,
                        message = "Success!"
                    )
                ),
                events.EVENT_JOB_EXECUTED
            )
            scheduler.add_listener(
                lambda event: _logger.warning(
                    _format_message(
                        sender = event.job_id,
                        event = _EVENT_EXECUTION,
                        message = f"The job scheduled for '{event.scheduled_run_time:%Y-%m-%d %H:%M:%S}' has missed!"
                    )
                ),
                events.EVENT_JOB_MISSED
            )
            scheduler.add_listener(
                lambda event: _logger.warning(
                    _format_message(
                        sender = event.job_id,
                        event = _EVENT_EXECUTION,
                        message = f"The job scheduled for {[f'{scheduled_run_time:%Y-%m-%d %H:%M:%S}' for scheduled_run_time in event.scheduled_run_times]} has skipped!"
                    )
                ),
                events.EVENT_JOB_MAX_INSTANCES
            )
            scheduler.add_listener(
                lambda event: _logger.error(
                    _format_message(
                        sender = event.job_id,
                        event = _EVENT_EXECUTION,
                        message = f"Oops, an error occurred! -> {repr(event.exception)}"
                    )
                ),
                events.EVENT_JOB_ERROR
            )

            users[user_name] = User(drivers, scheduler)

        for user in users.values():
            user.scheduler.start(paused = True)

        self.__users = users

    def start(self) -> None:
        users = self.__users

        for user in users.values():
            user.scheduler.resume()

    def stop(self) -> None:
        users = self.__users

        for user in users.values():
            user.scheduler.pause()

    def uninit(self) -> None:
        users = self.__users

        for user in users.values():
            user.scheduler.shutdown(wait = False)

            for driver in user.drivers.values():
                driver.quit()

        self.__users = None


if __name__ == '__main__':
    os.makedirs("logs", exist_ok = True)

    config.dictConfig({
        "version": 1,
        "handlers": {
            "console": {
                "class": "logging.StreamHandler",
                "level": "DEBUG",
                "formatter": "simple",
                "stream": "ext://sys.stdout"
            },
            "file": {
                "class": "logging.handlers.RotatingFileHandler",
                "level": "INFO",
                "formatter": "simple",
                "filename": "logs/bot.log",
                "maxBytes": 10 * 1024 * 1024,
                "backupCount": 5
            }
        },
        "formatters": {
            "simple": {
                "format": "[%(asctime)s] - %(levelname)-8s: %(message)s",
                "datefmt": "%Y-%m-%d %H:%M:%S"
            }
        },
        "loggers": {
            __name__: {
                "level": "DEBUG",
                "handlers": ["console", "file"]
            }
        }
    })

    sys.excepthook = lambda type, value, traceback: _logger.error(
        _format_message(
            sender = _SYSTEM,
            event = _EVENT_EXECUTION,
            message = f"Oops, an error occurred! -> {repr(value)}",
            root = True
        )
    )

    _logger.info(
        _format_message(
            sender = _SYSTEM,
            event = _EVENT_EXECUTION,
            message = "Welcome!",
            root = True
        )
    )

    parser = argparse.ArgumentParser()

    parser.add_argument("-c", "--configuration", default = "bot.toml", help = "defines the bot configuration")
    parser.add_argument("-p", "--preview", action = "store_true", help = "defines whether to preview the post")

    args = parser.parse_args()

    conf: dict[str, Any]
    preview: bool

    with open(args.configuration, "rb") as f:
        conf = tomllib.load(f)

    preview = args.preview

    _logger.info(
        _format_message(
            sender = _SYSTEM,
            event = _EVENT_EXECUTION,
            message = f"Preview {'On' if preview else 'Off'}!",
            root = True
        )
    )

    bot = Bot(conf, preview)

    _logger.debug(
        _format_message(
            sender = _SYSTEM,
            event = _EVENT_EXECUTION,
            message = "Init...",
            root = True
        )
    )

    bot.init()

    _logger.debug(
        _format_message(
            sender = _SYSTEM,
            event = _EVENT_EXECUTION,
            message = "Init OK!",
            root = True
        )
    )

    _logger.debug(
        _format_message(
            sender = _SYSTEM,
            event = _EVENT_EXECUTION,
            message = "Start...",
            root = True
        )
    )

    bot.start()

    _logger.debug(
        _format_message(
            sender = _SYSTEM,
            event = _EVENT_EXECUTION,
            message = "Start OK!",
            root = True
        )
    )

    event = Event()

    signal.signal(signal.SIGTERM, (lambda signum, frame: event.set()))
    signal.signal(signal.SIGINT, (lambda signum, frame: signal.raise_signal(signal.SIGTERM)))

    event.wait()

    _logger.debug(
        _format_message(
            sender = _SYSTEM,
            event = _EVENT_EXECUTION,
            message = "Stop...",
            root = True
        )
    )

    bot.stop()
    
    _logger.debug(
        _format_message(
            sender = _SYSTEM,
            event = _EVENT_EXECUTION,
            message = "Stop OK!",
            root = True
        )
    )

    _logger.debug(
        _format_message(
            sender = _SYSTEM,
            event = _EVENT_EXECUTION,
            message = "Uninit...",
            root = True
        )
    )

    bot.uninit()
    
    _logger.debug(
        _format_message(
            sender = _SYSTEM,
            event = _EVENT_EXECUTION,
            message = "Uninit OK!",
            root = True
        )
    )

    _logger.info(
        _format_message(
            sender = _SYSTEM,
            event = _EVENT_EXECUTION,
            message = "Bye!",
            root = True
        )
    )
