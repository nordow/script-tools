import argparse
import copy
import importlib
import logging
import os
import random
import signal
import sys
import tomllib
from datetime import datetime
from http.cookies import SimpleCookie
from logging import config
from threading import Event
from typing import Any

import pytz
from apscheduler import events
from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.schedulers.base import BaseScheduler
from apscheduler.triggers.cron import CronTrigger
from selenium import webdriver
from selenium.webdriver.chrome.webdriver import WebDriver
from selenium.webdriver.common.by import By
from selenium.webdriver.support import expected_conditions as EC
from selenium.webdriver.support.ui import WebDriverWait


__all__ = ["Bot"]


_SYSTEM = "System"

_ACTION_PROCESS = "Process"
_ACTION_EXECUTION = "Execution"


_logger = logging.getLogger(__name__)


def _format_message(sender, action, message, root: bool = False) -> str:
    return f"{f'<{sender}>' if root else f'[{sender}]':<16} - {action:<9} | {message}"

def _format_fstring(fstring: str, **kwargs) -> str:
    return eval(f'f{repr(fstring)}', kwargs)


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
    __users: dict[str, User]

    def __init__(self, conf: dict[str, Any], preview: bool) -> None:
        self.__conf = conf
        self.__preview = preview
        self.__users = None

    def init(self) -> None:

        def load_page(driver: WebDriver, cookies: str) -> None:
            app_xpath = '//div[@id="app"]'

            driver.get("https://weibo.com")
            WebDriverWait(driver, 30).until(EC.presence_of_element_located((By.XPATH, app_xpath)))

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
            WebDriverWait(driver, 30).until(EC.presence_of_element_located((By.XPATH, home_wrap_xpath)))

        def send_post(driver: WebDriver, args: dict[str, Any], preview: bool) -> None:

            def normalize_template(template) -> dict[str, Any]:
                if isinstance(template, str):
                    return {
                        "text": template,
                        "images": [],
                        "videos": []
                    }
                elif isinstance(template, dict):
                    return {
                        "text": template["text"],
                        "images": template.get("images", []),
                        "videos": template.get("videos", [])
                    }
                else:
                    raise TypeError(f"Wrong type of template; got '{type(template).__name__}', expected '{str.__name__}' or '{dict.__name__}[{str.__name__}, Any]'")

            job_id: str = args["job_id"]

            timezone: str | None = args["timezone"]
            envs: dict[str, Any] = args["envs"]

            select: str = args["select"]
            templates: list[str | dict[str, Any]] = args["templates"]

            if not templates:
                raise ValueError(f"Wrong value of templates for [{job_id}]; got {templates}, expected [...]")

            match select:
                case "random":
                    template: str | dict[str, Any] = random.choice(templates)
                case _:
                    raise ValueError(f"Wrong value of select for [{job_id}]; got {repr(select)}, expected 'random'")

            template = normalize_template(template)
            mods = {
                "random": importlib.import_module("random"),
                # "requests": importlib.import_module("requests")
            }
            vars = {
                "now": datetime.now(pytz.timezone(timezone) if timezone is not None else None)
            }

            text = _format_fstring(template["text"], mods = mods, vars = vars, envs = envs)

            _logger.info(
                _format_message(
                    sender = job_id,
                    action = _ACTION_PROCESS,
                    message = f'{repr(template["text"])} -> {repr(text)}'
                )
            )

            if text.isspace():
                raise ValueError(f"Wrong value of formatted text for [{job_id}]; got {repr(text)}, expected not whitespace")

            if preview:
                return

            text_textarea_xpath = '//div[@id="homeWrap"]/div[1]/div/div[1]/div/textarea'
            send_button_xpath = '//div[@id="homeWrap"]/div[1]/div/div[4]/div/div[5]/button'
            # file_input_xpath = '//div[@id="homeWrap"]/div[1]/div/div[2]/div/div/div[1]/div/div/input'

            text_textarea = driver.find_element(By.XPATH, text_textarea_xpath)
            send_button = driver.find_element(By.XPATH, send_button_xpath)
            # file_input = driver.find_element(By.XPATH, file_input_xpath)

            text_textarea.clear()
            text_textarea.send_keys(text)
            send_button.click()

            WebDriverWait(driver, 10).until_not(EC.element_to_be_clickable((By.XPATH, send_button_xpath)))

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
            jobs_conf: dict[str, dict[str, Any]] = user_conf.get("jobs", conf["default"].get("jobs", {}))
            drivers: dict[str, WebDriver] = {}
            scheduler = BackgroundScheduler()

            for job_name, job_conf in jobs_conf.items():
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
                job_select: str = job_conf.get("select", "random")
                job_templates: list[str | dict[str, Any]] = job_conf.get("templates", [])

                scheduler.add_job(send_post, FullCronTrigger.from_cron(job_cron, timezone, job_jitter), kwargs = {
                    "args": {
                        "job_id": job_id,

                        "timezone": timezone,
                        "envs": envs,

                        "select": job_select,
                        "templates": job_templates,
                    },

                    "preview": preview,
                    "driver": driver
                }, id = job_id)

            scheduler.add_listener(
                lambda event: _logger.info(
                    _format_message(
                        sender = event.job_id,
                        action = _ACTION_EXECUTION,
                        message = "Success!"
                    )
                ),
                events.EVENT_JOB_EXECUTED
            )
            scheduler.add_listener(
                lambda event: _logger.warning(
                    _format_message(
                        sender = event.job_id,
                        action = _ACTION_EXECUTION,
                        message = f"The job scheduled for '{event.scheduled_run_time:%Y-%m-%d %H:%M:%S}' has missed!"
                    )
                ),
                events.EVENT_JOB_MISSED
            )
            scheduler.add_listener(
                lambda event: _logger.warning(
                    _format_message(
                        sender = event.job_id,
                        action = _ACTION_EXECUTION,
                        message = f"The job scheduled for {[f'{scheduled_run_time:%Y-%m-%d %H:%M:%S}' for scheduled_run_time in event.scheduled_run_times]} has skipped!"
                    )
                ),
                events.EVENT_JOB_MAX_INSTANCES
            )
            scheduler.add_listener(
                lambda event: _logger.error(
                    _format_message(
                        sender = event.job_id,
                        action = _ACTION_EXECUTION,
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
            action = _ACTION_EXECUTION,
            message = f"Oops, an error occurred! -> {repr(value)}",
            root = True
        )
    )

    _logger.info(
        _format_message(
            sender = _SYSTEM,
            action = _ACTION_EXECUTION,
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
            action = _ACTION_EXECUTION,
            message = f"Preview {'On' if preview else 'Off'}!",
            root = True
        )
    )

    bot = Bot(conf, preview)

    _logger.debug(
        _format_message(
            sender = _SYSTEM,
            action = _ACTION_EXECUTION,
            message = "Init...",
            root = True
        )
    )

    bot.init()

    _logger.debug(
        _format_message(
            sender = _SYSTEM,
            action = _ACTION_EXECUTION,
            message = "Init OK!",
            root = True
        )
    )

    _logger.debug(
        _format_message(
            sender = _SYSTEM,
            action = _ACTION_EXECUTION,
            message = "Start...",
            root = True
        )
    )

    bot.start()

    _logger.debug(
        _format_message(
            sender = _SYSTEM,
            action = _ACTION_EXECUTION,
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
            action = _ACTION_EXECUTION,
            message = "Stop...",
            root = True
        )
    )

    bot.stop()
    
    _logger.debug(
        _format_message(
            sender = _SYSTEM,
            action = _ACTION_EXECUTION,
            message = "Stop OK!",
            root = True
        )
    )

    _logger.debug(
        _format_message(
            sender = _SYSTEM,
            action = _ACTION_EXECUTION,
            message = "Uninit...",
            root = True
        )
    )

    bot.uninit()
    
    _logger.debug(
        _format_message(
            sender = _SYSTEM,
            action = _ACTION_EXECUTION,
            message = "Uninit OK!",
            root = True
        )
    )

    _logger.info(
        _format_message(
            sender = _SYSTEM,
            action = _ACTION_EXECUTION,
            message = "Bye!",
            root = True
        )
    )
