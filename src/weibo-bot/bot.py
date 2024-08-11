import argparse
import copy
import importlib
import json
import logging
import os
import random
import re
import signal
import sys
import tomllib
import urllib.request
from collections.abc import Callable
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
_EVENT_NOTIFICATION = "Notification"


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
    return f"{f'<{sender}>' if root else f'[{sender}]':<16} - {event:<12} | {message}"

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


class PreviewException(Exception):
    pass


class Validator:
    __id: str

    def __init__(self, id: str) -> None:
        self.__id = id

    @property
    def id(self) -> str:
        return self.__id

    def validate(self, value) -> Any:
        pass


class UserNameValidator(Validator):

    def __init__(self, id: str) -> None:
        super().__init__(id)

    def validate(self, value) -> str:
        if re.search(r"^[A-Za-z0-9_-]+$", value):
            return value
        else:
            raise ValueError(f"Wrong value of user name @{self.id}; got {repr(value)}, expected ^[A-Za-z0-9_-]+$")


class CookieValidator(Validator):

    def __init__(self, id: str) -> None:
        super().__init__(id)

    def validate(self, value) -> dict[str, Any]:
        if isinstance(value, str):
            return {
                "source": "string",
                "type": "header",
                "value": value
            }
        elif isinstance(value, dict):
            return {
                "source": value.get("source"),
                "type": value["type"],
                "value": value["value"]
            }
        else:
            raise TypeError(f"Wrong type of cookies @{self.id}; got '{type(value).__name__}', expected '{str.__name__}' or '{dict.__name__}[{str.__name__}, Any]'")


class ModValidator(Validator):

    def __init__(self, id: str) -> None:
        super().__init__(id)

    def validate(self, value) -> dict[str, Any]:
        if isinstance(value, str):
            return {
                "type": "module",
                "value": value
            }
        elif isinstance(value, dict):
            return {
                "type": value["type"],
                "value": value["value"]
            }
        else:
            raise TypeError(f"Wrong type of mod @{self.id}; got '{type(value).__name__}', expected '{str.__name__}' or '{dict.__name__}[{str.__name__}, Any]'")


class JobNameValidator(Validator):

    def __init__(self, id: str) -> None:
        super().__init__(id)

    def validate(self, value) -> str:
        if re.search(r"^[A-Za-z0-9_-]+$", value):
            return value
        else:
            raise ValueError(f"Wrong value of job name @{self.id}; got {repr(value)}, expected ^[A-Za-z0-9_-]+$")


class CommandValidator(Validator):

    def __init__(self, id: str) -> None:
        super().__init__(id)

    def validate(self, value) -> list[str] | None:
        (commands, group) = value

        if commands is None:
            return None

        if (group_commands := commands.get(group)) is None:
            return None

        if isinstance(group_commands, str):
            return [group_commands]
        elif isinstance(group_commands, list):
            return group_commands
        else:
            raise TypeError(f"Wrong type of {group} commands @{self.id}; got '{type(group_commands).__name__}', expected '{str.__name__}' or '{list.__name__}[{str.__name__}]'")


class TemplateValidator(Validator):

    def __init__(self, id: str) -> None:
        super().__init__(id)

    def validate(self, value) -> dict[str, Any]:
        if isinstance(value, str):
            return {
                "text": value,
                "images": []
            }
        elif isinstance(value, dict):
            return {
                "text": value["text"],
                "images": value.get("images", [])
            }
        else:
            raise TypeError(f"Wrong type of template @{self.id}; got '{type(value).__name__}', expected '{str.__name__}' or '{dict.__name__}[{str.__name__}, Any]'")


class CookieParser:
    __id: str

    def __init__(self, id: str) -> None:
        self.__id = id

    @property
    def id(self) -> str:
        return self.__id

    def parse(self, value: str, type: str | None = None, source: str | None = None) -> list[dict[str, Any]]:
        cookies_str: str

        match source:
            case None | "string":
                cookies_str = value
            case "file":
                with open(value, "r") as f:
                    cookies_str = f.read()
            case _:
                raise ValueError(f"Wrong value of cookies source @{self.id}; got {repr(source)}, expected 'string' or 'file'")

        cookies: list[dict[str, Any]]

        match type:
            case None | "header":
                cookies = [{
                    "domain": ".weibo.com",
                    "name": key,
                    "value": morsel.value,
                    # "expiry": null,
                    "path": "/",
                    "httpOnly": False,
                    "hostOnly": False,
                    "secure": False
                } for key, morsel in SimpleCookie(cookies_str).items()]
            case "json":
                cookies = json.loads(cookies_str)
            case _:
                raise ValueError(f"Wrong value of cookies type @{self.id}; got {repr(type)}, expected 'header' or 'json'")

        for cookie in cookies:
            if isinstance(cookie.get("expiry"), float):
                cookie["expiry"] = int(cookie["expiry"])

        return cookies


class ModImporter:
    __id: str

    def __init__(self, id: str) -> None:
        self.__id = id

    @property
    def id(self) -> str:
        return self.__id

    def import_single(self, value: str, type: str | None = None, context: Callable[[], dict[str, Any]] | dict[str, Any] | None = None) -> Any:
        if callable(context):
            context = context()

        mod: Any

        match type:
            case None | "module":
                mod = importlib.import_module(value)
            case "expression":
                mod = _safe_eval(value, context)
            case _:
                raise ValueError(f"Wrong value of type @{self.id}; got {repr(type)}, expected 'module' or 'expression'")

        return mod

    def import_multi(self, items: dict[str, dict[str, Any]], context: Callable[[dict[str, Any]], dict[str, Any]] | dict[str, Any] | None = None) -> dict[str, Any]:
        mods: dict[str, Any] = {}

        if callable(context):
            context = context(mods)

        for name, item in items.items():
            type: str = item.get("type")
            value: str = item["value"]

            mod: Any

            match type:
                case None | "module":
                    mod = importlib.import_module(value)
                case "expression":
                    mod = _safe_eval(value, context)
                case _:
                    raise ValueError(f"Wrong value of type for mod {repr(name)} @{self.id}; got {repr(type)}, expected 'module' or 'expression'")

            mods[name] = mod

        return mods


class TemplateSelector:
    __id: str

    def __init__(self, id: str) -> None:
        self.__id = id

    @property
    def id(self) -> str:
        return self.__id

    def select(self, templates, mode: str | None = None) -> Any:
        if not templates:
            raise ValueError(f"Wrong value of select templates @{self.id}; got {repr(templates)}, expected [templates]{{1,}}")

        template: Any

        match mode:
            case None | "random":
                template = random.choice(templates)
            case _:
                raise ValueError(f"Wrong value of select mode @{self.id}; got {repr(mode)}, expected 'random'")

        return template


class Poster:
    __id: str
    __driver: WebDriver
    __preview: bool

    def __init__(self, id: str) -> None:
        self.__id = id
        self.__preview = False

        options = webdriver.ChromeOptions()

        options.add_argument("--no-sandbox")
        options.add_argument("--headless")
        options.add_argument("--incognito")
        options.add_argument("--disable-gpu")
        options.add_argument("--disable-dev-shm-usage")
        options.add_argument(f"--user-data-dir={os.path.expanduser('~/.config/google-chrome')}")
        options.add_argument(f"--profile-directory={id}")

        driver = webdriver.Chrome(options = options)

        driver.maximize_window()

        self.__driver = driver

    @property
    def id(self) -> str:
        return self.__id

    @property
    def preview(self) -> bool:
        return self.__preview

    @preview.setter
    def preview(self, value: bool) -> None:
        self.__preview = value

    def with_preview(self, value: bool):
        self.preview = value

        return self

    def with_cookies(self, value: list[dict[str, Any]]):
        driver = self.__driver

        app_xpath = '//div[@id="app"]'

        current = driver.current_window_handle
        current_index = driver.window_handles.index(current)

        driver.execute_script("window.open('https://weibo.com', '_blank')")
        driver.switch_to.window(driver.window_handles[current_index + 1])

        try:
            loading_wait = WebDriverWait(driver, 30)

            loading_wait.until(EC.presence_of_element_located((By.XPATH, app_xpath)))

            driver.delete_all_cookies()

            for cookie in value:
                driver.add_cookie(cookie)

            home_wrap_xpath = '//div[@id="homeWrap"]'

            driver.refresh()
            loading_wait.until(EC.presence_of_element_located((By.XPATH, home_wrap_xpath)))

        finally:
            driver.close()
            driver.switch_to.window(current)

        return self

    def send(self, **kwargs) -> None:
        text: str | None = kwargs.get("text")
        images: list[str] | None = kwargs.get("images")

        if not images and (not text or text.isspace()):
            raise ValueError(f"Wrong value of text @{self.id}; got {repr(text)}, expected not empty and not whitespace, if there is no images")

        driver = self.__driver
        preview = self.__preview

        if preview:
            raise PreviewException(f"Preview over @{self.id}")

        current = driver.current_window_handle
        current_index = driver.window_handles.index(current)

        driver.execute_script("window.open('https://weibo.com', '_blank')")
        driver.switch_to.window(driver.window_handles[current_index + 1])

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
                        files.append((os.path.abspath(image), False) if os.path.isfile(image) else (urllib.request.urlretrieve(image)[0], True))

                    file_input.send_keys("\n".join((file[0] for file in files)))
                    execution_wait.until(EC.element_to_be_clickable((By.XPATH, send_button_xpath)))

                    upload_wait = WebDriverWait(driver, 60)

                    item_div_xpath = '//div[@id="homeWrap"]/div[1]/div/div[2]/div/div/div'
                    cover_img_xpath = '//div[@id="homeWrap"]/div[1]/div/div[2]/div/div/div[{index}]/div/div/img'

                    file_div_list: list[WebElement] = execution_wait.until(EC.presence_of_all_elements_located((By.XPATH, item_div_xpath)))[:-1]

                    if (files_diff := len(files) - len(file_div_list)) > 0:
                        raise ValueError(f"Find {files_diff} unacceptable image(s) @{self.id}; got {images}, expected [images]{{0,18}} or [images and videos]{{0,9}}, accepted {repr(file_input_accept)}")

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
            driver.switch_to.window(current)

    def dispose(self) -> None:
        driver = self.__driver

        if driver is None:
            return

        driver.quit()

        self.__driver = None


class User:
    posters: dict[str, Poster]
    scheduler: BaseScheduler

    def __init__(self, posters: dict[str, Poster], scheduler: BaseScheduler) -> None:
        self.posters = posters
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

        def send_post(poster: Poster, kwargs: dict[str, Any]) -> bool:

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
                if not (group_commands := CommandValidator(job_id).validate((commands, group))):
                    return

                for command in group_commands:
                    _safe_eval(command, kwargs)

            job_id: str = poster.id

            envs: dict[str, Any] = kwargs["envs"]
            mods: dict[str, Any] = kwargs["mods"]
            vars: dict[str, Any] = kwargs["vars"]

            select: str | None = kwargs["select"]
            commands: dict[str, Any] | None = kwargs["commands"]
            templates: list[dict[str, Any] | str] | None = kwargs["templates"]

            job_kwargs = {
                "envs": envs,
                "mods": mods,
                "vars": eval_vars(vars, envs, mods)
            }

            try:
                execute_commands(commands, "pre", job_kwargs, job_id)

                template_conf = TemplateSelector(job_id).select(templates, select)

                template = TemplateValidator(job_id).validate(template_conf)
                template_text = template["text"]
                template_images = template["images"]

                text = _format_fstring(template_text, **job_kwargs)
                images = [_format_fstring(template_image, **job_kwargs) for template_image in template_images] if isinstance(template_images, list) else [template_image for template_image in _safe_eval(template_images, job_kwargs)]

                _logger.info(
                    _format_message(
                        sender = job_id,
                        event = _EVENT_PROCESS,
                        message = f'{repr(template_conf)} -> {repr(text if not images else { "text": text, "images": images })}'
                    )
                )

                try:
                    poster.send(text = text, images = images)
                except PreviewException:
                    return False
                else:
                    return True

            finally:
                execute_commands(commands, "post", job_kwargs, job_id)

        conf = self.__conf
        preview = self.__preview
        users: dict[str, User] = {}

        for user_name, user_conf in conf.items():
            UserNameValidator(user_name).validate(user_name)

            timezone: str | None = user_conf.get("timezone", conf["default"].get("timezone"))
            cookies: list[dict[str, Any]] = CookieParser(user_name).parse(**(CookieValidator(user_name).validate(user_conf.get("cookies", conf["default"]["cookies"]))))
            envs: dict[str, Any] = copy.deepcopy({
                **(conf["default"].get("envs", {})),
                **(user_conf.get("envs", {}))
            })
            mods: dict[str, Any] = ModImporter(user_name).import_multi({key: ModValidator(f"{user_name}.{key}").validate(value) for key, value in {
                **(conf["default"].get("mods", {})),
                **(user_conf.get("mods", {}))
            }.items()}, lambda mods: { "envs": envs, "mods": mods })
            vars: dict[str, Any] = copy.deepcopy({
                **(conf["default"].get("vars", {})),
                **(user_conf.get("vars", {}))
            })
            jobs: dict[str, dict[str, Any]] = user_conf.get("jobs", conf["default"].get("jobs", {}))
            posters: dict[str, Poster] = {}
            scheduler = BackgroundScheduler()

            for job_name, job_conf in jobs.items():
                JobNameValidator(user_name).validate(job_name)

                job_id = f"{user_name}.{job_name}"

                poster = Poster(job_id).with_preview(preview).with_cookies(cookies)

                posters[job_name] = poster

                job_cron: str = job_conf["cron"]
                job_jitter: int | None = job_conf.get("jitter")
                job_select: str | None = job_conf.get("select")
                job_commands: dict[str, Any] | None = job_conf.get("commands")
                job_templates: list[dict[str, Any] | str] | None = job_conf.get("templates")

                scheduler.add_job(send_post, FullCronTrigger.from_cron(job_cron, timezone, job_jitter), kwargs = {
                    "kwargs": {
                        "envs": envs,
                        "mods": mods,
                        "vars": vars,

                        "select": job_select,
                        "commands": job_commands,
                        "templates": job_templates,
                    },

                    "poster": poster
                }, id = job_id)

            scheduler.add_listener(
                lambda event: (_logger.info(
                    _format_message(
                        sender = event.job_id,
                        event = _EVENT_EXECUTION,
                        message = "Success!" if event.retval else "Preview over!"
                    )
                ), _logger.info(
                    _format_message(
                        sender = event.job_id,
                        event = _EVENT_NOTIFICATION,
                        message = f"The next job is scheduled for '{scheduler.get_job(event.job_id).next_run_time:%Y-%m-%d %H:%M:%S}'"
                    )
                )),
                events.EVENT_JOB_EXECUTED
            )
            scheduler.add_listener(
                lambda event: (_logger.warning(
                    _format_message(
                        sender = event.job_id,
                        event = _EVENT_EXECUTION,
                        message = f"The job scheduled for '{event.scheduled_run_time:%Y-%m-%d %H:%M:%S}' has missed!"
                    )
                ), _logger.info(
                    _format_message(
                        sender = event.job_id,
                        event = _EVENT_NOTIFICATION,
                        message = f"The next job is scheduled for '{scheduler.get_job(event.job_id).next_run_time:%Y-%m-%d %H:%M:%S}'"
                    )
                )),
                events.EVENT_JOB_MISSED
            )
            scheduler.add_listener(
                lambda event: (_logger.warning(
                    _format_message(
                        sender = event.job_id,
                        event = _EVENT_EXECUTION,
                        message = f"The job scheduled for {[f'{scheduled_run_time:%Y-%m-%d %H:%M:%S}' for scheduled_run_time in event.scheduled_run_times]} has skipped!"
                    )
                ), _logger.info(
                    _format_message(
                        sender = event.job_id,
                        event = _EVENT_NOTIFICATION,
                        message = f"The next job is scheduled for '{scheduler.get_job(event.job_id).next_run_time:%Y-%m-%d %H:%M:%S}'"
                    )
                )),
                events.EVENT_JOB_MAX_INSTANCES
            )
            scheduler.add_listener(
                lambda event: (_logger.error(
                    _format_message(
                        sender = event.job_id,
                        event = _EVENT_EXECUTION,
                        message = f"Oops, an error occurred! -> {repr(event.exception)}"
                    )
                ), _logger.info(
                    _format_message(
                        sender = event.job_id,
                        event = _EVENT_NOTIFICATION,
                        message = f"The next job is scheduled for '{scheduler.get_job(event.job_id).next_run_time:%Y-%m-%d %H:%M:%S}'"
                    )
                )),
                events.EVENT_JOB_ERROR
            )

            users[user_name] = User(posters, scheduler)

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

            for poster in user.posters.values():
                poster.dispose()

        self.__users = None


if __name__ == '__main__':
    parser = argparse.ArgumentParser()

    parser.add_argument("-c", "--configuration", default = "bot.toml", help = "defines the bot configuration")

    preview_group = parser.add_mutually_exclusive_group()

    preview_group.add_argument("-p", "--preview", action = "store_true", help = "defines whether to preview the post (default)")
    preview_group.add_argument("-r", "--real", action = "store_true", help = "defines whether to really send the post")

    args = parser.parse_args()

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

    conf: dict[str, Any]
    preview: bool

    with open(args.configuration, "rb") as f:
        conf = tomllib.load(f)

    preview = args.preview or not args.real

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
