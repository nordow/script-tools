import argparse
import copy
import datetime
import functools
import importlib
import json
import logging
import os
import random
import re
import signal
import sys
import time
import tomllib
import urllib.parse
import urllib.request
from _thread import LockType
from abc import ABC, abstractmethod
from collections.abc import Callable
from dependency_injector import containers, providers
from enum import auto, Enum, Flag
from http.cookies import SimpleCookie
from logging import config
from threading import Event, Lock
from typing import Any

import qrcode
import qrcode.constants
from apscheduler import events
from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.schedulers.base import BaseScheduler
from apscheduler.triggers.cron import CronTrigger
from RestrictedPython import compile_restricted, safe_builtins
from RestrictedPython.Eval import (default_guarded_getitem,
                                   default_guarded_getiter)
from RestrictedPython.Guards import guarded_iter_unpack_sequence
from selenium import webdriver
from selenium.common import exceptions as EX
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


def sync(lock):
    if isinstance(lock, LockType):

        def decorator(func):

            @functools.wraps(func)
            def wrapper(*args, **kwargs):
                with lock:
                    return func(*args, **kwargs)

            return wrapper

        return decorator

    else:
        func = lock
        lock = Lock()

        @functools.wraps(func)
        def wrapper(*args, **kwargs):
            with lock:
                return func(*args, **kwargs)

        return wrapper


def _safe_eval(expr: str, globals: dict[str, Any] | None = None, locals: dict[str, Any] | None = None) -> Any:
    code = compile_restricted(expr, mode = "eval")

    return eval(code, {
        "__builtins__": {
            **safe_builtins,

            "_getitem_": default_guarded_getitem,
            "_getiter_": default_guarded_getiter,
            "_iter_unpack_sequence_": guarded_iter_unpack_sequence
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

def _input_yes_no(prompt, default: bool | None = None) -> bool:
    while True:
        user_input = input(prompt).strip().lower()

        if not user_input:
            if default is None:
                continue

            else:
                user_input = "y" if default else "n"

        match user_input:
            case "n" | "no":
                return False
            case "y" | "yes":
                return True
            case _:
                print("Invalid input, please enter 'yes' or 'no'")

def _raise_exception(exception) -> None:
    raise exception


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
                "value": value,
                "mode": "interactive"
            }
        elif isinstance(value, dict):
            return {
                "source": value.get("source"),
                "type": value["type"],
                "value": value["value"],
                "mode": value.get("mode")
            }
        else:
            raise TypeError(f"Wrong type of cookies @{self.id}; got {type(value)}, expected <{dict[str, Any] | str}>")


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
            raise TypeError(f"Wrong type of mod @{self.id}; got {type(value)}, expected <{dict[str, Any] | str}>")


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
            raise TypeError(f"Wrong type of {group} commands @{self.id}; got {type(group_commands)}, expected <{list[str] | str}>")


class TemplateValidator(Validator):

    def __init__(self, id: str) -> None:
        super().__init__(id)

    def validate(self, value) -> dict[str, Any]:
        if isinstance(value, str):
            return {
                "text": value,
                "images": [],
                "options": {}
            }
        elif isinstance(value, dict):
            return {
                "text": value["text"],
                "images": value.get("images", []),
                "options": value.get("options", {})
            }
        else:
            raise TypeError(f"Wrong type of template @{self.id}; got {type(value)}, expected <{dict[str, Any] | str}>")


class FeatureValidator(Validator):

    def __init__(self, id: str) -> None:
        super().__init__(id)

    def validate(self, value) -> dict[str, Any]:
        if isinstance(value, bool):
            return {
                "enabled": value
            }
        elif isinstance(value, dict):
            return {
                "enabled": value.get("enabled", True),

                **value
            }
        else:
            raise TypeError(f"Wrong type of feature @{self.id}; got {type(value)}, expected <{dict[str, Any] | bool}>")


class _InternalCookiePolicy(Flag):
    NONE = 0
    OVERRIDE = auto()
    SANITIZE = auto()


class CookieMode(Enum):
    INTERACTIVE = auto()
    FALLBACK = auto()
    OVERRIDE = auto()
    MIGRATE = auto()


class CookieProvider:
    __value: list[dict[str, Any]] | None
    __mode: CookieMode
    __options: dict[str, Any] | None

    def __init__(self, value: list[dict[str, Any]] | None, mode: CookieMode, options: dict[str, Any] | None = None) -> None:
        self.__value = value
        self.__mode = mode
        self.__options = options

    @property
    def value(self) -> list[dict[str, Any]] | None:
        return self.__value

    @property
    def mode(self) -> CookieMode:
        return self.__mode

    @property
    def options(self) -> dict[str, Any] | None:
        return self.__options

    @property
    def live(self) -> bool:
        return self.__value is None


class CookieParser:
    __id: str

    def __init__(self, id: str) -> None:
        self.__id = id

    @property
    def id(self) -> str:
        return self.__id

    def parse(self, value: str, type: str | None = None, source: str | None = None, mode: str | None = None) -> CookieProvider:
        actual_value: str

        match source:
            case None | "string":
                actual_value = value
            case "file":
                with open(value, "r") as f:
                    actual_value = f.read()
            case _:
                raise ValueError(f"Wrong value of cookies source @{self.id}; got {repr(source)}, expected in {repr(['string', 'file'])}")

        cookies: list[dict[str, Any]] | None
        options: dict[str, Any] | None

        match type:
            case None | "header":
                cookies, options = [{
                    "domain": ".weibo.com",
                    "name": key,
                    "value": morsel.value,
                    # "expiry": null,
                    "path": "/",
                    "httpOnly": False,
                    "hostOnly": False,
                    "secure": False
                } for key, morsel in SimpleCookie(actual_value).items()], None
            case "json":
                cookies, options = [cookie for cookie in json.loads(actual_value) if isinstance(cookie, dict)], None
            case "live":
                cookies, options = None, json.loads(actual_value)
            case _:
                raise ValueError(f"Wrong value of cookies type @{self.id}; got {repr(type)}, expected in {repr(['header', 'json', 'live'])}")

        if cookies is not None:
            for cookie in cookies:
                if isinstance(cookie.get("expiry"), float):
                    cookie["expiry"] = int(cookie["expiry"])

        actual_mode: CookieMode

        match mode:
            case None | "interactive":
                actual_mode = CookieMode.INTERACTIVE
            case "fallback":
                actual_mode = CookieMode.FALLBACK
            case "override":
                actual_mode = CookieMode.OVERRIDE
            case "migrate":
                actual_mode = CookieMode.MIGRATE
            case _:
                raise ValueError(f"Wrong value of cookies mode @{self.id}; got {repr(mode)}, expected in {repr(['interactive', 'fallback', 'override', 'migrate'])}")

        return CookieProvider(cookies, actual_mode, options)


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
                raise ValueError(f"Wrong value of type @{self.id}; got {repr(type)}, expected in {repr(['module', 'expression'])}")

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
                    raise ValueError(f"Wrong value of type for mod {repr(name)} @{self.id}; got {repr(type)}, expected in {repr(['module', 'expression'])}")

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
                raise ValueError(f"Wrong value of select mode @{self.id}; got {repr(mode)}, expected in {repr(['random'])}")

        return template


class PathProvider:
    __id: str

    __folders: list[str]

    __base_dir: str

    def __init__(self, id: str) -> None:
        self.__id = id

        self.__folders = [
            "assets",
            "cache",
            "data",
            "logs",
            "mods",
            "res"
        ]

        self.__base_dir = os.path.abspath(os.path.dirname(__file__))

    @property
    def id(self) -> str:
        return self.__id

    def get(self, folder: str) -> str:
        folders = self.__folders

        if folder not in folders:
            raise ValueError(f"Wrong value of folder @{self.id}; got {repr(folder)}, expected in {repr(folders)}")

        folder_path = os.path.join(self.__base_dir, folder)

        os.makedirs(folder_path, exist_ok = True)

        return folder_path

    def __getitem__(self, key: str) -> str:
        return self.get(key)


class Engine:
    __id: str
    __driver: WebDriver
    __props: dict[str, Any]

    __lock: Lock

    __disposed: bool

    def __init__(self, id: str) -> None:
        self.__id = id
        self.__props = {}
        self.__lock = Lock()

        options = webdriver.ChromeOptions()

        options.add_argument("--no-sandbox")
        options.add_argument("--headless")
        options.add_argument("--window-size=1920,1080")
        # options.add_argument("--incognito")
        options.add_argument("--disable-gpu")
        options.add_argument("--disable-dev-shm-usage")
        options.add_argument(f"--user-data-dir={os.path.expanduser('~/.config/google-chrome')}")
        options.add_argument(f"--profile-directory={id}")

        driver = webdriver.Chrome(options = options)

        # driver.maximize_window()

        self.__driver = driver

        self.__disposed = False

    @property
    def id(self) -> str:
        return self.__id

    @property
    def driver(self) -> WebDriver:
        return self.__driver

    @property
    def props(self) -> dict[str, Any]:
        return self.__props

    @property
    def lock(self) -> Lock:
        return self.__lock

    def dispose(self) -> None:
        if self.__disposed:
            return

        driver = self.__driver

        driver.quit()

        self.__driver = None

        self.__disposed = True


class CDPClient(ABC):

    @abstractmethod
    def send_cdp(self, method: str, params: dict):
        pass


class Console(CDPClient):
    __engine: Engine

    def __init__(self, engine: Engine) -> None:
        self.__engine = engine

        self.__send_cdp_sync()

    @property
    def id(self) -> str:
        return self.__engine.id

    def __send_cdp_sync(self): self.send_cdp = sync(self.__engine.lock)(self.send_cdp)
    def send_cdp(self, method: str, params: dict):
        engine = self.__engine

        driver = engine.driver

        return driver.execute_cdp_cmd(method, params)


class BrowserTab:
    __engine: Engine
    __handle: str

    def __init__(self, engine: Engine, handle: str) -> None:
        self.__engine = engine
        self.__handle = handle

        self.__execute_sync()

    def __execute_sync(self): self.__execute = sync(self.__engine.lock)(self.__execute)
    def __execute(self, method: Callable[[WebDriver], Any]) -> Any:
        engine = self.__engine
        handle = self.__handle

        driver = engine.driver

        driver.switch_to.window(handle)

        return method(driver)

    def focus(self) -> None:
        self.__execute(lambda driver: None)

    def get_user_agent(self) -> str:
        return self.__execute(lambda driver: driver.execute_script("return navigator.userAgent"))

    def get_url(self) -> str:
        return self.__execute(lambda driver: driver.current_url)

    def get_title(self) -> str:
        return self.__execute(lambda driver: driver.title)

    def get_cookie(self, name: str) -> dict | None:
        return self.__execute(lambda driver: driver.get_cookie(name))

    def get_cookies(self) -> list[dict]:
        return self.__execute(lambda driver: driver.get_cookies())

    def close(self) -> None:
        self.__execute(lambda driver: driver.close())


class Browser(CDPClient):
    __engine: Engine

    def __init__(self, engine: Engine) -> None:
        self.__engine = engine

        self.__send_cdp_sync()
        self.__open_tab_sync()

    @property
    def id(self) -> str:
        return self.__engine.id

    def __send_cdp_sync(self): self.send_cdp = sync(self.__engine.lock)(self.send_cdp)
    def send_cdp(self, method: str, params: dict):
        engine = self.__engine

        driver = engine.driver

        return driver.execute_cdp_cmd(method, params)

    def __open_tab_sync(self): self.open_tab = sync(self.__engine.lock)(self.open_tab)
    def open_tab(self, url: str) -> BrowserTab:
        engine = self.__engine

        driver = engine.driver

        current = driver.current_window_handle
        current_index = driver.window_handles.index(current)

        driver.execute_script("window.open('about:blank', '_blank')")

        new_handle = driver.window_handles[current_index + 1]

        driver.switch_to.window(new_handle)

        try:
            driver.get(url)

            return BrowserTab(engine, new_handle)

        except:
            driver.close()
            driver.switch_to.window(current)

            raise


class FeatureProvider:

    class __Container(containers.DeclarativeContainer):
        config = providers.Configuration()

        engine = providers.Resource(
            lambda id: (
                (yield (engine := Engine(id))),
                engine.dispose()
            ),
            id = config.id)

        browser = providers.Singleton(Browser, engine = engine)

    __id: str

    __container: __Container
    __features: dict[str, Callable[[__Container], Any]]

    __disposed: bool

    def __init__(self, id: str, conf: dict[str, dict[str, Any]]) -> None:
        self.__id = id

        container = self.__Container()

        container.config.from_dict({
            "id": id,
            "features": conf
        })

        self.__container = container
        self.__features = {
            "browser": (lambda container: container.browser())
        }
        self.__features = {
            key: (lambda container: value(container)
                  if hasattr(container.config.features, key) and getattr(container.config.features, key).enabled()
                  else _raise_exception(RuntimeError(f"@{container.config.id()} feature {repr(key)} is not enabled")))
                for key, value in self.__features.items()
        }

        self.__disposed = False

    @property
    def id(self) -> str:
        return self.__id

    def get(self, key: str) -> Any:
        container = self.__container
        features = self.__features

        if not (feature := features.get(key, None)):
            raise RuntimeError(f"@{container.config.id()} feature {repr(key)} is not supported")

        return feature(container)

    def __getitem__(self, key: str) -> Any:
        return self.get(key)

    def dispose(self) -> None:
        if self.__disposed:
            return

        container = self.__container

        container.shutdown_resources()

        self.__features = None
        self.__container = None

        self.__disposed = True


class FeatureBuilder:
    __id: str

    __features: dict[str, dict[str, Any]]

    def __init__(self, id: str) -> None:
        self.__id = id

        self.__features = {}

    @property
    def id(self) -> str:
        return self.__id

    def add_single(self, key: str, value: dict[str, Any] | bool):
        features = self.__features

        features[key] = FeatureValidator(f"{self.id}.{key}").validate(value)

        return self

    def add_multi(self, items: dict[str, dict[str, Any] | bool]):
        features = self.__features

        for key, value in items.items():
            features[key] = FeatureValidator(f"{self.id}.{key}").validate(value)

        return self

    def build(self) -> FeatureProvider:
        features = self.__features

        return FeatureProvider(self.id, features)


class Poster:
    __engine: Engine
    __preview: bool

    def __init__(self, engine: Engine, preview: bool) -> None:
        self.__engine = engine
        self.__preview = preview

        self.__with_cookies_sync()
        self.__send_sync()

    @property
    def id(self) -> str:
        return self.__engine.id

    @property
    def preview(self) -> bool:
        return self.__preview

    @preview.setter
    def preview(self, value: bool) -> None:
        self.__preview = value

    def with_preview(self, value: bool):
        self.preview = value

        return self

    def __with_cookies_sync(self): self.with_cookies = sync(self.__engine.lock)(self.with_cookies)
    def with_cookies(self, provider: CookieProvider):
        engine = self.__engine

        driver = engine.driver

        profile_id: str
        profile_name: str

        app_xpath = '//div[@id="app"]'
        home_wrap_xpath = '//div[@id="homeWrap"]'

        current = driver.current_window_handle
        current_index = driver.window_handles.index(current)

        driver.execute_script("window.open('https://weibo.com', '_blank')")
        driver.switch_to.window(driver.window_handles[current_index + 1])

        try:
            loading_wait = WebDriverWait(driver, 30)

            loading_wait.until(EC.presence_of_element_located((By.XPATH, app_xpath)))

            cookie_policy: _InternalCookiePolicy

            profile_a_xpath = '//div[@role="navigation"]/div/div/div[2]/div/div[1]/a[5]'
            # profile_div_xpath = '//div[@role="navigation"]/div/div/div[2]/div/div[1]/a[5]/div/div/div'

            match provider.mode:
                case CookieMode.INTERACTIVE:
                    current_profile_a: WebElement = loading_wait.until(EC.presence_of_element_located((By.XPATH, profile_a_xpath)))
                    current_profile_a_href: str | None = current_profile_a.get_attribute("href")

                    if current_profile_a_href is not None:
                        # current_profile_div: WebElement = loading_wait.until(EC.presence_of_element_located((By.XPATH, profile_div_xpath)))
                        # current_profile_div_title: str = current_profile_div.get_attribute("title")
                        current_profile_a_title: str = current_profile_a.get_attribute("title")

                        profile_id = re.search(r"/u/([0-9]+)$", current_profile_a_href).group(1)
                        profile_name = current_profile_a_title # current_profile_div_title

                        print(f"@{self.id} -> ({profile_id}, '{profile_name}')")

                        if _input_yes_no("[override] do you want to override the current cookies? (y/n) "):
                            if _input_yes_no("[migrate] do you want to sanitize the current cookies? (y/n) "):
                                cookie_policy = _InternalCookiePolicy.OVERRIDE | _InternalCookiePolicy.SANITIZE
                            else:
                                cookie_policy = _InternalCookiePolicy.OVERRIDE
                        else:
                            cookie_policy = _InternalCookiePolicy.NONE

                    else:
                        print(f"@{self.id} -> None, [fallback] as default")

                        cookie_policy = _InternalCookiePolicy.OVERRIDE

                case CookieMode.FALLBACK:
                    fallback_profile_a: WebElement = loading_wait.until(EC.presence_of_element_located((By.XPATH, profile_a_xpath)))
                    fallback_profile_a_href: str | None = fallback_profile_a.get_attribute("href")

                    if fallback_profile_a_href is not None:
                        # fallback_profile_div: WebElement = loading_wait.until(EC.presence_of_element_located((By.XPATH, profile_div_xpath)))
                        # fallback_profile_div_title: str = fallback_profile_div.get_attribute("title")
                        fallback_profile_a_title: str = fallback_profile_a.get_attribute("title")

                        profile_id = re.search(r"/u/([0-9]+)$", fallback_profile_a_href).group(1)
                        profile_name = fallback_profile_a_title # fallback_profile_div_title

                        cookie_policy = _InternalCookiePolicy.NONE

                    else:
                        cookie_policy = _InternalCookiePolicy.OVERRIDE

                case CookieMode.OVERRIDE:
                    cookie_policy = _InternalCookiePolicy.OVERRIDE
                case CookieMode.MIGRATE:
                    cookie_policy = _InternalCookiePolicy.OVERRIDE | _InternalCookiePolicy.SANITIZE

                # never
                case _:
                    raise ValueError(f"Wrong value of provider.mode @{self.id}; got {repr(provider.mode)}, expected in {repr(['interactive', 'fallback', 'override', 'migrate'])}")

            if _InternalCookiePolicy.SANITIZE in cookie_policy:
                driver.get("https://login.sina.com.cn/sso/logout.php?entry=miniblog&r=")

                loading_wait.until(EC.all_of(
                    EC.url_contains("https://passport.weibo.com/sso/signin"),
                    EC.presence_of_element_located((By.XPATH, app_xpath))))

            if _InternalCookiePolicy.OVERRIDE in cookie_policy:
                driver.get("https://weibo.com")

                loading_wait.until(EC.presence_of_element_located((By.XPATH, app_xpath)))

                driver.delete_all_cookies()

                if provider.live:
                    scanning_wait = WebDriverWait(driver, (provider.options if provider.options is not None else {}).get("qrcode", {}).get("expires", 300))

                    driver.get("https://passport.weibo.com/sso/signin?url=https%3A%2F%2Fweibo.com")

                    qrcode_img_xpath = '//div[@id="app"]/div/div/div[2]/div[1]/div[2]/div/img'

                    qrcode_img: WebElement = loading_wait.until(EC.presence_of_element_located((By.XPATH, qrcode_img_xpath))) \
                                                if loading_wait.until(EC.text_to_be_present_in_element_attribute((By.XPATH, qrcode_img_xpath), "src", "http")) \
                                                else None
                    qrcode_img_src: str = qrcode_img.get_attribute("src")

                    qrcode_content = urllib.parse.parse_qs(urllib.parse.urlparse(qrcode_img_src).query)["data"][0]

                    qr = qrcode.QRCode(
                        version = 1,
                        error_correction = qrcode.constants.ERROR_CORRECT_L,
                        box_size = 10,
                        border = 2
                    )

                    qr.add_data(qrcode_content)

                    print(f"@{self.id}, expires at '{(datetime.datetime.now() + datetime.timedelta(seconds = scanning_wait._timeout)):%Y-%m-%d %H:%M:%S}'")
                    qr.print_ascii(invert = True)

                    scanning_wait.until(EC.presence_of_element_located((By.XPATH, home_wrap_xpath)))

                else:
                    for cookie in provider.value:
                        driver.add_cookie(cookie)

                    driver.refresh()
                    loading_wait.until(EC.presence_of_element_located((By.XPATH, home_wrap_xpath)))

                new_profile_a: WebElement = loading_wait.until(EC.presence_of_element_located((By.XPATH, profile_a_xpath)))
                new_profile_a_href: str = new_profile_a.get_attribute("href")

                # new_profile_div: WebElement = loading_wait.until(EC.presence_of_element_located((By.XPATH, profile_div_xpath)))
                # new_profile_div_title: str = new_profile_div.get_attribute("title")
                new_profile_a_title: str = new_profile_a.get_attribute("title")

                profile_id = re.search(r"/u/([0-9]+)$", new_profile_a_href).group(1)
                profile_name = new_profile_a_title # new_profile_div_title

        finally:
            driver.close()
            driver.switch_to.window(current)

        _logger.info(
            _format_message(
                sender = _SYSTEM,
                event = _EVENT_EXECUTION,
                message = f"@{self.id} Init OK! -> ({profile_id}, '{profile_name}')",
                root = True
            )
        )

        return self

    def __send_sync(self): self.send = sync(self.__engine.lock)(self.send)
    def send(self, **kwargs) -> None:
        text: str = kwargs.get("text", "")
        images: list[str] = kwargs.get("images", [])
        options: dict[str, Any] = kwargs.get("options", {})

        behavior: str | None = options.get("behavior")

        match behavior:
            case None | "origin":
                if options.get("stopic") is not None:
                    self.__send_origin_with_stopic(text, images, options)
                else:
                    self.__send_origin(text, images, options)
            case "repost":
                self.__send_repost(text, images, options)
            case "comment":
                self.__send_comment(text, images, options)
            case _:
                raise ValueError(f"Wrong value of behavior @{self.id}; got {repr(behavior)}, expected in {repr(['origin', 'repost', 'comment'])}")

    def __send_origin(self, text: str, images: list[str], options: dict[str, Any]) -> None:

        def non_presence_of_element_located(locator):
            """ An expectation for checking that an element is not present on the DOM
            of a page.
            locator - used to find the element
            returns False if the element is present on the DOM, True otherwise.
            """

            def _predicate(driver):
                try:
                    driver.find_element(*locator)
                    return False
                except EX.NoSuchElementException:
                    return True

            return _predicate

        if not images and (not text or text.isspace()):
            raise ValueError(f"Wrong value of text @{self.id}; got {repr(text)}, expected not empty and not whitespace, if there is no images")

        engine = self.__engine
        preview = self.__preview

        driver = engine.driver

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
            execution_wait.until(EC.element_attribute_to_include((By.XPATH, send_button_xpath), "disabled"))

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
                    loading_svg_xpath = '//div[@id="homeWrap"]/div[1]/div/div[2]/div/div/div[{index}]/div/div/svg'
                    cover_img_xpath = '//div[@id="homeWrap"]/div[1]/div/div[2]/div/div/div[{index}]/div/div/img'

                    file_div_list: list[WebElement] = execution_wait.until(EC.presence_of_all_elements_located((By.XPATH, item_div_xpath)))[:-1]

                    if (files_diff := len(files) - len(file_div_list)) > 0:
                        raise ValueError(f"Find {files_diff} unacceptable image(s) @{self.id}; got {images}, expected [images]{{0,18}} or [images and videos]{{0,9}}, accepted {repr(file_input_accept)}")

                    for index in range(len(file_div_list)):
                        upload_wait.until(EC.all_of(
                            non_presence_of_element_located((By.XPATH, loading_svg_xpath.format(index = 1 + index))),
                            EC.presence_of_element_located((By.XPATH, cover_img_xpath.format(index = 1 + index)))))

                finally:
                    for path in (file[0] for file in files if file[1]):
                        _try_delete_file(path)

            text_textarea.send_keys(text)
            execution_wait.until(EC.element_to_be_clickable((By.XPATH, send_button_xpath)))

            # send_button.click()
            driver.execute_script("arguments[0].click();", send_button)
            execution_wait.until(EC.element_attribute_to_include((By.XPATH, send_button_xpath), "disabled"))

        finally:
            driver.close()
            driver.switch_to.window(current)

    def __send_origin_with_stopic(self, text: str, images: list[str], options: dict[str, Any]) -> None:

        def element_located_text_to_be(locator, text_):
            """ An expectation for checking if the given text is the expected value in the
            specified element.
            locator, text
            """

            def _predicate(driver):
                try:
                    element_text = driver.find_element(*locator).text
                    return text_ == element_text
                except EX.StaleElementReferenceException:
                    return False

            return _predicate

        def non_presence_of_element_located(locator):
            """ An expectation for checking that an element is not present on the DOM
            of a page.
            locator - used to find the element
            returns False if the element is present on the DOM, True otherwise.
            """

            def _predicate(driver):
                try:
                    driver.find_element(*locator)
                    return False
                except EX.NoSuchElementException:
                    return True

            return _predicate

        if not images and (not text or text.isspace()):
            raise ValueError(f"Wrong value of text @{self.id}; got {repr(text)}, expected not empty and not whitespace, if there is no images")

        stopic: dict[str, Any]
        stopic_value: dict[str, Any] | str = options.get("stopic")

        if isinstance(stopic_value, str):
            if not stopic_value or stopic_value.isspace():
                raise ValueError(f"Wrong value of stopic @{self.id}; got {repr(stopic_value)}, expected not empty and not whitespace")

            stopic = {
                "title": stopic_value,
                "sync": None
            }

        elif isinstance(stopic_value, dict):
            stopic_title = stopic_value["title"]

            if isinstance(stopic_title, str):
                if not stopic_title or stopic_title.isspace():
                    raise ValueError(f"Wrong value of stopic.title @{self.id}; got {repr(stopic_title)}, expected not empty and not whitespace")

                stopic = {
                    "title": stopic_title,
                    "sync": bool(stopic_sync) if (stopic_sync := stopic_value.get("sync")) is not None else None
                }

            else:
                raise TypeError(f"Wrong type of stopic.title @{self.id}; got {type(stopic_title)}, expected {str}")
        else:
            raise TypeError(f"Wrong type of stopic @{self.id}; got {type(stopic_value)}, expected <{dict[str, Any] | str}>")

        engine = self.__engine
        preview = self.__preview

        driver = engine.driver

        if preview:
            raise PreviewException(f"Preview over @{self.id}")

        current = driver.current_window_handle
        current_index = driver.window_handles.index(current)

        driver.execute_script("window.open('https://weibo.com', '_blank')")
        driver.switch_to.window(driver.window_handles[current_index + 1])

        try:
            element_wait = WebDriverWait(driver, 30)
            execution_wait = WebDriverWait(driver, 15)

            stopic_list_loading_delay = 0.5

            vellipsis_div_xpath = '//div[@id="homeWrap"]/div[1]/div[1]/div[4]/div/div[1]/div/div[6]/div/span/div'
            stopic_div_xpath = '//div[@id="homeWrap"]/div[1]/div[1]/div[4]/div/div[1]/div/div[6]/div/div/div/div[span/*[local-name()="svg" and namespace-uri()="http://www.w3.org/2000/svg"]/*[@*[local-name()="href" and namespace-uri()="http://www.w3.org/1999/xlink"]="#woo_svg_nav_stopic"]]'
            stopic_modal_div_xpath = '/html/body/div[2]'
            stopic_select_div_xpath = '/html/body/div[2]/div[1]/div/div[2]/div[2]/div[1]/div[1]'
            stopic_search_input_xpath = '/html/body/div[2]/div[1]/div/div[2]/div[2]/div[1]/div[2]/div/div/div[1]/div/input'
            stopic_list_none_span_xpath = '/html/body/div[2]/div[1]/div/div[2]/div[2]/div[1]/div[2]/div/div/div[4]/div/span'
            stopic_list_scroller_div_xpath = '//div[@id="scroller"]'
            stopic_item_div_xpath = '//div[@id="scroller"]/div[2]/div[{index}]/div/div' # elements div[{index}]/div/div
            stopic_item_text_div_xpath = '//div[@id="scroller"]/div[2]/div/div/div/div[2]/div[1]' # elements div[{index}]/div/div/div[2]/div[1]
            stopic_title_span_xpath = '/html/body/div[2]/div[1]/div/div[1]/div/div[1]/span'
            sync_label_xpath = '/html/body/div[2]/div[1]/div/div[2]/div[2]/div[2]/label'
            sync_input_xpath = '/html/body/div[2]/div[1]/div/div[2]/div[2]/div[2]/label/input'
            text_textarea_xpath = '/html/body/div[2]/div[1]/div/div[2]/div[1]/textarea'
            send_button_xpath = '/html/body/div[2]/div[1]/div/div[2]/div[3]/div[2]/div[5]/button'

            # 1
            driver.execute_script("arguments[0].click();", element_wait.until(EC.element_to_be_clickable((By.XPATH, vellipsis_div_xpath))))

            # 2
            driver.execute_script("arguments[0].click();", element_wait.until(EC.element_to_be_clickable((By.XPATH, stopic_div_xpath))))
            execution_wait.until(EC.presence_of_element_located((By.XPATH, stopic_modal_div_xpath)))

            # 3
            driver.execute_script("arguments[0].click();", element_wait.until(EC.element_to_be_clickable((By.XPATH, stopic_select_div_xpath))))

            # 4
            stopic_search_input: WebElement = element_wait.until(EC.presence_of_element_located((By.XPATH, stopic_search_input_xpath)))

            # stopic_search_input.clear()
            stopic_search_input.send_keys(Keys.CONTROL, "A")
            stopic_search_input.send_keys(Keys.DELETE)

            stopic_search_input.send_keys(stopic["title"] + " ") # this space is necessary!
            time.sleep(stopic_list_loading_delay)
            execution_wait.until(EC.any_of(
                EC.presence_of_element_located((By.XPATH, stopic_list_scroller_div_xpath)),
                EC.presence_of_element_located((By.XPATH, stopic_list_none_span_xpath))))

            stopic_search_input.send_keys(Keys.BACKSPACE) # refresh
            time.sleep(stopic_list_loading_delay)
            execution_wait.until(EC.any_of(
                EC.presence_of_element_located((By.XPATH, stopic_list_scroller_div_xpath)),
                EC.presence_of_element_located((By.XPATH, stopic_list_none_span_xpath))))

            # 5
            stopic_item_text_div_list: list[WebElement] | WebElement = element_wait.until(EC.any_of(
                EC.presence_of_all_elements_located((By.XPATH, stopic_item_text_div_xpath)),
                EC.presence_of_element_located((By.XPATH, stopic_list_none_span_xpath))))

            stopic_item_div: WebElement | None = None

            if isinstance(stopic_item_text_div_list, list):
                for index in range(len(stopic_item_text_div_list)):
                    stopic_item_text_div = stopic_item_text_div_list[index]

                    if stopic_item_text_div.text == stopic["title"]:
                        stopic_item_div = element_wait.until(EC.presence_of_element_located((By.XPATH, stopic_item_div_xpath.format(index = 1 + index))))

                        break

            if stopic_item_div is None:
                raise ValueError(f"Cannot find a stopic by stopic.title {repr(stopic['title'])} @{self.id}, please check the stopic options")

            driver.execute_script("arguments[0].click();", execution_wait.until(EC.element_to_be_clickable(stopic_item_div)))
            execution_wait.until(element_located_text_to_be((By.XPATH, stopic_title_span_xpath), stopic["title"]))

            # 6
            if stopic["sync"] is not None:
                sync_input: WebElement = element_wait.until(EC.presence_of_element_located((By.XPATH, sync_input_xpath)))

                if stopic["sync"] != sync_input.is_selected():
                    driver.execute_script("arguments[0].click();", execution_wait.until(EC.element_to_be_clickable((By.XPATH, sync_label_xpath))))
                    execution_wait.until(EC.element_selection_state_to_be(sync_input, stopic["sync"]))

            # 7
            text_textarea: WebElement = element_wait.until(EC.presence_of_element_located((By.XPATH, text_textarea_xpath)))
            send_button: WebElement = element_wait.until(EC.presence_of_element_located((By.XPATH, send_button_xpath)))

            # text_textarea.clear()
            text_textarea.send_keys(Keys.CONTROL, "A")
            text_textarea.send_keys(Keys.DELETE)
            execution_wait.until(EC.element_attribute_to_include((By.XPATH, send_button_xpath), "disabled"))

            # 8
            if images:
                file_input_xpath = '/html/body/div[2]/div[1]/div/div[2]/div[3]/div[1]/div/div/div[{index}]/div/div/input'

                file_input: WebElement = element_wait.until(EC.presence_of_element_located((By.XPATH, file_input_xpath.format(index = 1))))
                file_input_accept: str = file_input.get_attribute("accept")

                files: list[tuple[str, bool]] = []

                try:
                    for image in images:
                        files.append((os.path.abspath(image), False) if os.path.isfile(image) else (urllib.request.urlretrieve(image)[0], True))

                    file_input.send_keys("\n".join((file[0] for file in files)))
                    execution_wait.until(EC.element_to_be_clickable((By.XPATH, send_button_xpath)))

                    upload_wait = WebDriverWait(driver, 60)

                    item_div_xpath = '/html/body/div[2]/div[1]/div/div[2]/div[3]/div[1]/div/div/div'
                    loading_svg_xpath = '/html/body/div[2]/div[1]/div/div[2]/div[3]/div[1]/div/div/div[{index}]/div/div[1]/svg'
                    cover_img_xpath = '/html/body/div[2]/div[1]/div/div[2]/div[3]/div[1]/div/div/div[{index}]/div/div[1]/img'

                    file_div_list: list[WebElement] = execution_wait.until(EC.presence_of_all_elements_located((By.XPATH, item_div_xpath)))[:-1]

                    if (files_diff := len(files) - len(file_div_list)) > 0:
                        raise ValueError(f"Find {files_diff} unacceptable image(s) @{self.id}; got {images}, expected [images]{{0,18}}, accepted {repr(file_input_accept)}")

                    for index in range(len(file_div_list)):
                        upload_wait.until(EC.all_of(
                            non_presence_of_element_located((By.XPATH, loading_svg_xpath.format(index = 1 + index))),
                            EC.presence_of_element_located((By.XPATH, cover_img_xpath.format(index = 1 + index)))))

                finally:
                    for path in (file[0] for file in files if file[1]):
                        _try_delete_file(path)

            # 9
            text_textarea.send_keys(text)
            execution_wait.until(EC.element_to_be_clickable((By.XPATH, send_button_xpath)))

            # send_button.click()
            driver.execute_script("arguments[0].click();", send_button)
            execution_wait.until(non_presence_of_element_located((By.XPATH, stopic_modal_div_xpath)))

        finally:
            driver.close()
            driver.switch_to.window(current)

    def __send_repost(self, text: str, images: list[str], options: dict[str, Any]) -> None:

        def text_to_be_not_equal_to_element_attribute(locator, attribute_, text_):
            """
            An expectation for checking if the given text is not equal to the element's attribute.
            locator, attribute, text
            """

            def _predicate(driver):
                try:
                    if not EC.element_attribute_to_include(locator, attribute_)(driver):
                        return False
                    element_text = driver.find_element(*locator).get_attribute(attribute_)
                    return text_ != element_text
                except EX.StaleElementReferenceException:
                    return False

            return _predicate

        quote: dict[str, Any] = options.get("quote", {})

        if not re.search(r"^[0-9]+$", quote["uid"]):
            raise ValueError(f"Wrong value of quote.uid @{self.id}; got {repr(quote['uid'])}, expected ^[0-9]+$")

        if not re.search(r"^[A-Za-z0-9]+$", quote["bid"]):
            raise ValueError(f"Wrong value of quote.bid @{self.id}; got {repr(quote['bid'])}, expected ^[A-Za-z0-9]+$")

        engine = self.__engine
        preview = self.__preview

        driver = engine.driver

        if preview:
            raise PreviewException(f"Preview over @{self.id}")

        current = driver.current_window_handle
        current_index = driver.window_handles.index(current)

        driver.execute_script(f"window.open('https://weibo.com/{quote['uid']}/{quote['bid']}#repost', '_blank')")
        driver.switch_to.window(driver.window_handles[current_index + 1])

        try:
            element_wait = WebDriverWait(driver, 30)
            execution_wait = WebDriverWait(driver, 15)

            text_textarea_xpath = '//div[@id="composerEle"]/div[2]/div/div[1]/div/textarea'
            send_button_xpath = '//div[@id="composerEle"]/div[2]/div/div[3]/div/button'

            text_textarea: WebElement = element_wait.until(EC.presence_of_element_located((By.XPATH, text_textarea_xpath)))
            send_button: WebElement = element_wait.until(EC.presence_of_element_located((By.XPATH, send_button_xpath)))

            if not options.get("keep_quote", True):
                tool_div_xpath = '//div[@id="composerEle"]/div[2]/div/div[3]/div/div'

                # text_textarea.clear()
                text_textarea.send_keys(Keys.CONTROL, "A")
                text_textarea.send_keys(Keys.DELETE)
                execution_wait.until(lambda driver: len(driver.find_elements(By.XPATH, tool_div_xpath)) == 3)

            if options.get("comment", False):
                comment_checkbox_xpath = '//div[@id="composerEle"]/div[2]/div/div[3]/div/div[2]/label/input'
                comment_span_xpath = '//div[@id="composerEle"]/div[2]/div/div[3]/div/div[2]/label/span[1]'

                comment_checkbox: WebElement = element_wait.until(EC.presence_of_element_located((By.XPATH, comment_checkbox_xpath)))

                # comment_checkbox.click()
                driver.execute_script("arguments[0].click();", comment_checkbox)
                execution_wait.until(EC.text_to_be_present_in_element_attribute((By.XPATH, comment_span_xpath), "class", "woo-checkbox-checked"))

            text_textarea.send_keys(text)
            execution_wait.until(EC.element_to_be_clickable((By.XPATH, send_button_xpath)))

            repost_a_xpath = '//div[@id="scroller"]/div[1]/div[1]/div/div/div/div/div[2]/div[2]/div[1]/a'

            repost_a: WebElement | None = repost_a_list[0] if (repost_a_list := driver.find_elements(By.XPATH, repost_a_xpath)) else None
            repost_a_href: str | None = repost_a.get_attribute("href") if repost_a is not None else None

            # send_button.click()
            driver.execute_script("arguments[0].click();", send_button)
            execution_wait.until(text_to_be_not_equal_to_element_attribute((By.XPATH, repost_a_xpath), "href", repost_a_href))

        finally:
            driver.close()
            driver.switch_to.window(current)

    def __send_comment(self, text: str, images: list[str], options: dict[str, Any]) -> None:
        quote: dict[str, Any] = options.get("quote", {})

        if not re.search(r"^[0-9]+$", quote["uid"]):
            raise ValueError(f"Wrong value of quote.uid @{self.id}; got {repr(quote['uid'])}, expected ^[0-9]+$")

        if not re.search(r"^[A-Za-z0-9]+$", quote["bid"]):
            raise ValueError(f"Wrong value of quote.bid @{self.id}; got {repr(quote['bid'])}, expected ^[A-Za-z0-9]+$")

        if not text or text.isspace():
            raise ValueError(f"Wrong value of text @{self.id}; got {repr(text)}, expected not empty and not whitespace")

        engine = self.__engine
        preview = self.__preview

        driver = engine.driver

        if preview:
            raise PreviewException(f"Preview over @{self.id}")

        current = driver.current_window_handle
        current_index = driver.window_handles.index(current)

        driver.execute_script(f"window.open('https://weibo.com/{quote['uid']}/{quote['bid']}#comment', '_blank')")
        driver.switch_to.window(driver.window_handles[current_index + 1])

        try:
            element_wait = WebDriverWait(driver, 30)
            execution_wait = WebDriverWait(driver, 15)

            text_textarea_xpath = '//div[@id="composerEle"]/div[2]/div/div[1]/div/textarea'
            send_button_xpath = '//div[@id="composerEle"]/div[2]/div/div[3]/div/button'

            text_textarea: WebElement = element_wait.until(EC.presence_of_element_located((By.XPATH, text_textarea_xpath)))
            send_button: WebElement = element_wait.until(EC.presence_of_element_located((By.XPATH, send_button_xpath)))

            tool_div_xpath = '//div[@id="composerEle"]/div[2]/div/div[3]/div/div'

            # text_textarea.clear()
            text_textarea.send_keys(Keys.CONTROL, "A")
            text_textarea.send_keys(Keys.DELETE)
            execution_wait.until(lambda driver: len(driver.find_elements(By.XPATH, tool_div_xpath)) == 2)

            if options.get("repost", False):
                repost_checkbox_xpath = '//div[@id="composerEle"]/div[2]/div/div[3]/div/div[2]/label/input'
                repost_span_xpath = '//div[@id="composerEle"]/div[2]/div/div[3]/div/div[2]/label/span[1]'

                repost_checkbox: WebElement = element_wait.until(EC.presence_of_element_located((By.XPATH, repost_checkbox_xpath)))

                # repost_checkbox.click()
                driver.execute_script("arguments[0].click();", repost_checkbox)
                execution_wait.until(EC.text_to_be_present_in_element_attribute((By.XPATH, repost_span_xpath), "class", "woo-checkbox-checked"))

            text_textarea.send_keys(text)
            execution_wait.until(EC.element_to_be_clickable((By.XPATH, send_button_xpath)))

            # send_button.click()
            driver.execute_script("arguments[0].click();", send_button)
            execution_wait.until(EC.element_attribute_to_include((By.XPATH, send_button_xpath), "disabled"))

        finally:
            driver.close()
            driver.switch_to.window(current)


class User:
    __name: str
    __preview: bool

    __paths: PathProvider
    __features: FeatureProvider
    __engine: Engine
    __console: Console
    __scheduler: BaseScheduler

    __disposed: bool

    def __init__(self, name: str, conf: dict[str, Any], default_conf: dict[str, Any], preview: bool) -> None:

        @sync
        def send_post(poster: Poster, kwargs: dict[str, Any]) -> bool:

            def eval_vars(vars: dict[str, Any], bins: dict[str, Any], envs: dict[str, Any], mods: dict[str, Any]) -> dict[str, Any]:
                evaluated_vars: dict[str, Any] = {}

                for var_name, var_expr in vars.items():
                    evaluated_var = _safe_eval(var_expr, {
                        "bins": bins,
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

            job_id: str = kwargs["id"]

            bins: dict[str, Any] = kwargs["bins"]
            envs: dict[str, Any] = kwargs["envs"]
            mods: dict[str, Any] = kwargs["mods"]
            vars: dict[str, Any] = kwargs["vars"]

            select: str | None = kwargs["select"]
            commands: dict[str, Any] | None = kwargs["commands"]
            templates: list[dict[str, Any] | str] | None = kwargs["templates"]

            job_kwargs = {
                "bins": bins,
                "envs": envs,
                "mods": mods,
                "vars": eval_vars(vars, bins, envs, mods)
            }

            real: bool

            try:
                execute_commands(commands, "pre", job_kwargs, job_id)

                template_conf = TemplateSelector(job_id).select(templates, select)

                template = TemplateValidator(job_id).validate(template_conf)
                template_text = template["text"]
                template_images = template["images"]
                template_options = template["options"]

                text = _format_fstring(template_text, **job_kwargs)
                images = [_format_fstring(template_image, **job_kwargs) for template_image in template_images] if isinstance(template_images, list) else [*(evaluated_images if (evaluated_images := _safe_eval(template_images, job_kwargs)) is not None else [])]
                options = template_options if isinstance(template_options, dict) else { **(evaluated_options if (evaluated_options := _safe_eval(template_options, job_kwargs)) is not None else {}) }

                _logger.info(
                    _format_message(
                        sender = job_id,
                        event = _EVENT_PROCESS,
                        message = f'{repr(template_conf)} -> {repr(text if not images and not options else { "text": text, "images": images } if not options else { "text": text, "options": options } if not images else { "text": text, "images": images, "options": options })}'
                    )
                )

                try:
                    poster.send(text = text, images = images, options = options)
                except PreviewException:
                    real = False
                else:
                    real = True

            except:
                execute_commands(commands, "fail", job_kwargs, job_id)

                raise

            else:
                execute_commands(commands, "success", job_kwargs, job_id)

            finally:
                execute_commands(commands, "post", job_kwargs, job_id)

            return real

        self.__name = name
        self.__preview = preview

        UserNameValidator(name).validate(name)

        paths: PathProvider = None
        features: FeatureProvider = None
        engine: Engine = None
        console: Console = None
        scheduler: BaseScheduler = None

        try:
            timezone: str | None = conf.get("timezone", default_conf.get("timezone"))
            cookies: CookieProvider = CookieParser(name).parse(**(CookieValidator(name).validate(conf.get("cookies", default_conf["cookies"]))))

            features = FeatureBuilder(name).add_multi(conf.get("features", default_conf["features"])).build()
            paths = PathProvider(name)
            engine = Engine(name)
            console = Console(engine)

            bins: dict[str, Any] = {
                "features": features,
                "paths": paths,
                "console": console
            }
            envs: dict[str, Any] = copy.deepcopy({
                **(default_conf.get("envs", {})),
                **(conf.get("envs", {}))
            })
            mods: dict[str, Any] = ModImporter(name).import_multi({key: ModValidator(f"{name}.{key}").validate(value) for key, value in {
                **(default_conf.get("mods", {})),
                **(conf.get("mods", {}))
            }.items()}, lambda mods: { "bins": bins, "envs": envs, "mods": mods })
            vars: dict[str, Any] = copy.deepcopy({
                **(default_conf.get("vars", {})),
                **(conf.get("vars", {}))
            })
            jobs: dict[str, dict[str, Any]] = conf.get("jobs", default_conf.get("jobs", {}))
            poster = Poster(engine, preview).with_cookies(cookies)
            scheduler = BackgroundScheduler()

            for job_name, job_conf in jobs.items():
                JobNameValidator(name).validate(job_name)

                job_id = f"{name}.{job_name}"

                job_cron: str = job_conf["cron"]
                job_jitter: int | None = job_conf.get("jitter")
                job_select: str | None = job_conf.get("select")
                job_commands: dict[str, Any] | None = job_conf.get("commands")
                job_templates: list[dict[str, Any] | str] | None = job_conf.get("templates")

                scheduler.add_job(send_post, FullCronTrigger.from_cron(job_cron, timezone, job_jitter), kwargs = {
                    "kwargs": {
                        "id": job_id,

                        "bins": bins,
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

            self.__paths = paths
            self.__features = features
            self.__engine = engine
            self.__console = console
            self.__scheduler = scheduler

        except:
            engine and engine.dispose()
            features and features.dispose()

            raise

        self.__disposed = False

    @property
    def name(self) -> str:
        return self.__name

    @property
    def preview(self) -> bool:
        return self.__preview

    def launch(self, paused: bool = False) -> None:
        self.__scheduler.start(paused = paused)

    def start(self) -> None:
        self.__scheduler.resume()

    def stop(self) -> None:
        self.__scheduler.pause()

    def dispose(self) -> None:
        if self.__disposed:
            return

        features = self.__features
        engine = self.__engine
        scheduler = self.__scheduler

        scheduler.running and scheduler.shutdown(wait = False)
        engine.dispose()
        features.dispose()

        self.__scheduler = None
        self.__console = None
        self.__engine = None
        self.__features = None
        self.__paths = None

        self.__disposed = True


class Bot:
    __conf: dict[str, Any]
    __preview: bool
    __users: dict[str, User] | None

    def __init__(self, conf: dict[str, Any], preview: bool) -> None:
        self.__conf = conf
        self.__preview = preview
        self.__users = None

    def init(self) -> None:
        conf = self.__conf
        preview = self.__preview
        users: dict[str, User] = {}

        default_conf = conf["default"]

        for user_name, user_conf in conf.items():
            users[user_name] = User(user_name, user_conf, default_conf, preview)

        for user in users.values():
            user.launch(paused = True)

        self.__users = users

    def start(self) -> None:
        users = self.__users

        for user in users.values():
            user.start()

    def stop(self) -> None:
        users = self.__users

        for user in users.values():
            user.stop()

    def uninit(self) -> None:
        users = self.__users

        if users is None:
            return

        for user in users.values():
            user.dispose()

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
