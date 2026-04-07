"""CDP WebSocket 客户端（Browser, Page, Element），对应 Go browser/browser.go + go-rod API。

通过原生 WebSocket 与 Chrome DevTools Protocol 通信，实现浏览器自动化控制。
"""

from __future__ import annotations

import json
import logging
import random
import time
from typing import Any

import requests
import websockets.sync.client as ws_client

from .errors import CDPError, ElementNotFoundError
from .stealth import STEALTH_JS, build_ua_override

logger = logging.getLogger(__name__)


class CDPClient:
    """底层 CDP WebSocket 通信客户端。"""

    def __init__(self, ws_url: str) -> None:
        self._ws = ws_client.connect(ws_url, max_size=50 * 1024 * 1024)
        self._id = 0
        self._callbacks: dict[int, Any] = {}

    def send(self, method: str, params: dict | None = None) -> dict:
        """发送 CDP 命令并等待结果。"""
        self._id += 1
        msg: dict[str, Any] = {"id": self._id, "method": method}
        if params:
            msg["params"] = params
        self._ws.send(json.dumps(msg))
        return self._wait_for(self._id)

    def _wait_for(self, msg_id: int, timeout: float = 30.0) -> dict:
        """等待指定 id 的响应。"""
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            try:
                raw = self._ws.recv(timeout=max(0.1, deadline - time.monotonic()))
            except TimeoutError:
                break
            data = json.loads(raw)
            if data.get("id") == msg_id:
                if "error" in data:
                    raise CDPError(f"CDP 错误: {data['error']}")
                return data.get("result", {})
        raise CDPError(f"等待 CDP 响应超时 (id={msg_id})")

    def close(self) -> None:
        import contextlib

        with contextlib.suppress(Exception):
            self._ws.close()


class Page:
    """CDP 页面对象，封装常用操作。"""

    def __init__(self, cdp: CDPClient, target_id: str, session_id: str) -> None:
        self._cdp = cdp
        self.target_id = target_id
        self.session_id = session_id
        self._ws = cdp._ws
        self._id_counter = 1000

    def _send_session(self, method: str, params: dict | None = None) -> dict:
        """向 session 发送命令。"""
        self._id_counter += 1
        msg: dict[str, Any] = {
            "id": self._id_counter,
            "method": method,
            "sessionId": self.session_id,
        }
        if params:
            msg["params"] = params
        self._ws.send(json.dumps(msg))
        return self._wait_session(self._id_counter)

    def _wait_session(self, msg_id: int, timeout: float = 60.0) -> dict:
        """等待 session 响应。"""
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            try:
                raw = self._ws.recv(timeout=max(0.1, deadline - time.monotonic()))
            except TimeoutError:
                break
            data = json.loads(raw)
            if data.get("id") == msg_id:
                if "error" in data:
                    raise CDPError(f"CDP 错误: {data['error']}")
                return data.get("result", {})
        raise CDPError(f"等待 session 响应超时 (id={msg_id})")

    def navigate(self, url: str) -> None:
        """导航到指定 URL。"""
        logger.info("导航到: %s", url)
        self._send_session("Page.navigate", {"url": url})

    def wait_for_load(self, timeout: float = 60.0) -> None:
        """等待页面加载完成（通过轮询 document.readyState）。"""
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            try:
                state = self.evaluate("document.readyState")
                if state == "complete":
                    return
            except CDPError:
                pass
            time.sleep(0.5)
        logger.warning("等待页面加载超时")

    def wait_dom_stable(self, timeout: float = 10.0, interval: float = 0.5) -> None:
        """等待 DOM 稳定（连续两次 DOM 快照一致）。"""
        last_html = ""
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            try:
                html = self.evaluate("document.body ? document.body.innerHTML.length : 0")
                if html == last_html and html != "":
                    return
                last_html = html
            except CDPError:
                pass
            time.sleep(interval)

    def evaluate(self, expression: str, timeout: float = 30.0) -> Any:
        """执行 JavaScript 表达式并返回结果。"""
        result = self._send_session(
            "Runtime.evaluate",
            {
                "expression": expression,
                "returnByValue": True,
                "awaitPromise": False,
            },
        )
        if "exceptionDetails" in result:
            raise CDPError(f"JS 执行异常: {result['exceptionDetails']}")
        remote_obj = result.get("result", {})
        return remote_obj.get("value")

    def evaluate_function(self, function_body: str, *args: Any) -> Any:
        """执行 JavaScript 函数并返回结果。"""
        result = self._send_session(
            "Runtime.evaluate",
            {
                "expression": f"({function_body})()",
                "returnByValue": True,
                "awaitPromise": False,
            },
        )
        if "exceptionDetails" in result:
            raise CDPError(f"JS 函数执行异常: {result['exceptionDetails']}")
        remote_obj = result.get("result", {})
        return remote_obj.get("value")

    def query_selector(self, selector: str) -> str | None:
        """查找单个元素，返回 objectId 或 None。"""
        result = self._send_session(
            "Runtime.evaluate",
            {
                "expression": f"document.querySelector({json.dumps(selector)})",
                "returnByValue": False,
            },
        )
        remote_obj = result.get("result", {})
        if remote_obj.get("subtype") == "null" or remote_obj.get("type") == "undefined":
            return None
        return remote_obj.get("objectId")

    def has_element(self, selector: str) -> bool:
        """检查元素是否存在。"""
        return self.evaluate(f"document.querySelector({json.dumps(selector)}) !== null") is True

    def scroll_by(self, x: int, y: int) -> None:
        """滚动页面。"""
        self.evaluate(f"window.scrollBy({x}, {y})")

    def scroll_to_bottom(self) -> None:
        """滚动到页面底部。"""
        self.evaluate("window.scrollTo(0, document.body.scrollHeight)")

    def get_scroll_top(self) -> int:
        """获取当前滚动位置。"""
        result = self.evaluate(
            "window.pageYOffset || document.documentElement.scrollTop"
            " || document.body.scrollTop || 0"
        )
        return int(result) if result else 0

    def get_viewport_height(self) -> int:
        """获取视口高度。"""
        result = self.evaluate("window.innerHeight")
        return int(result) if result else 768

    def mouse_move(self, x: float, y: float) -> None:
        """移动鼠标。"""
        self._send_session(
            "Input.dispatchMouseEvent",
            {"type": "mouseMoved", "x": x, "y": y},
        )

    def mouse_click(self, x: float, y: float, button: str = "left") -> None:
        """在指定坐标点击。"""
        self._send_session(
            "Input.dispatchMouseEvent",
            {"type": "mousePressed", "x": x, "y": y, "button": button, "clickCount": 1},
        )
        self._send_session(
            "Input.dispatchMouseEvent",
            {"type": "mouseReleased", "x": x, "y": y, "button": button, "clickCount": 1},
        )

    def inject_stealth(self) -> None:
        """注入反检测脚本。"""
        self._send_session(
            "Page.addScriptToEvaluateOnNewDocument",
            {"source": STEALTH_JS},
        )


class Browser:
    """Chrome 浏览器 CDP 控制器。"""

    def __init__(self, host: str = "127.0.0.1", port: int = 9333) -> None:
        self.host = host
        self.port = port
        self.base_url = f"http://{host}:{port}"
        self._cdp: CDPClient | None = None
        self._chrome_version: str | None = None

    def connect(self) -> None:
        """连接到 Chrome DevTools。"""
        resp = requests.get(f"{self.base_url}/json/version", timeout=5)
        resp.raise_for_status()
        info = resp.json()
        ws_url = info["webSocketDebuggerUrl"]

        browser_str = info.get("Browser", "")
        if "/" in browser_str:
            self._chrome_version = browser_str.split("/", 1)[1]

        logger.info("连接到 Chrome: %s (version=%s)", ws_url, self._chrome_version)
        self._cdp = CDPClient(ws_url)

    def _setup_page(self, page: Page) -> Page:
        """为 Page 对象注入 stealth、UA、viewport，并启用必要的 CDP domain。"""
        import contextlib

        page.inject_stealth()
        page._send_session(
            "Emulation.setUserAgentOverride",
            build_ua_override(self._chrome_version),
        )
        page._send_session(
            "Emulation.setDeviceMetricsOverride",
            {
                "width": random.randint(1366, 1920),
                "height": random.randint(768, 1080),
                "deviceScaleFactor": 1,
                "mobile": False,
            },
        )
        for perm in ("geolocation", "notifications", "midi", "camera", "microphone"):
            with contextlib.suppress(CDPError):
                assert self._cdp is not None
                self._cdp.send(
                    "Browser.setPermission",
                    {"permission": {"name": perm}, "setting": "denied"},
                )
        page._send_session("Page.enable")
        page._send_session("DOM.enable")
        page._send_session("Runtime.enable")
        return page

    def get_or_create_page(self) -> Page:
        """复用现有空白 tab，找不到时才新建。"""
        if not self._cdp:
            self.connect()
        assert self._cdp is not None

        import contextlib

        resp = requests.get(f"{self.base_url}/json", timeout=5)
        targets = resp.json()

        for target in targets:
            if target.get("type") == "page" and target.get("url") in (
                "about:blank",
                "chrome://newtab/",
            ):
                target_id = target["id"]
                with contextlib.suppress(Exception):
                    result = self._cdp.send(
                        "Target.attachToTarget",
                        {"targetId": target_id, "flatten": True},
                    )
                    session_id = result.get("sessionId")
                    if session_id:
                        return self._setup_page(Page(self._cdp, target_id, session_id))

        return self.new_page()

    def new_page(self, url: str = "about:blank") -> Page:
        """创建新页面。"""
        if not self._cdp:
            self.connect()
        assert self._cdp is not None

        result = self._cdp.send("Target.createTarget", {"url": url})
        target_id = result["targetId"]
        result = self._cdp.send(
            "Target.attachToTarget",
            {"targetId": target_id, "flatten": True},
        )
        session_id = result["sessionId"]
        return self._setup_page(Page(self._cdp, target_id, session_id))

    def close(self) -> None:
        """关闭连接。"""
        if self._cdp:
            self._cdp.close()
            self._cdp = None
