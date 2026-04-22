"""抖音搜索功能实现。

搜索方式：在首页搜索框输入关键词并按回车搜索，模拟人类操作。
避免直接导航到搜索 URL（会触发抖音反爬风控）。

数据提取策略（CSR 时代）：
抖音搜索结果页自 2024 末起改为 CSR（客户端渲染），搜索结果不再注入
`#RENDER_DATA`，而是通过 `/aweme/v1/web/general/search/stream/` 流式端点
（chunked transfer-encoding）动态加载。

核心策略：在页面加载前注入 fetch hook，拦截 stream 端点响应，
按 NDJSON 逐行解析（每行是一个 JSON 对象，包含一批 aweme_info）。
浏览器自带 cookie/msToken/a_bogus，无需逆向签名。
"""
from __future__ import annotations

import json
import logging
import random
import time

from .cdp import Page
from .errors import ElementNotFoundError, NoResultsError
from .human import sleep_random
from .rate_guard import raise_if_risky
from .types import Video
from .urls import DOUYIN_HOME

logger = logging.getLogger(__name__)

# ─── 搜索框相关选择器（抖音首页实际 DOM 结构） ──────────────────────────────

# 抖音首页顶部搜索输入框（多个候选，按优先级尝试）
_SEARCH_INPUT_CANDIDATES = [
    'input[data-e2e="searchbar-input"]',
    'input[data-e2e="search-input"]',
    '#douyin-header input[type="search"]',
    '#douyin-header input[type="text"]',
    'input[placeholder*="搜索"]',
    '.search-input-container input',
]

# 搜索按钮候选
_SEARCH_BUTTON_CANDIDATES = [
    'button[data-e2e="searchbar-button"]',
    'button[data-e2e="search-button"]',
    '#douyin-header .search-icon',
    '#douyin-header button[type="submit"]',
    '.search-input-container button',
]

# ─── JS Hook：拦截搜索 stream 端点响应 ──────────────────────────────────────
# 抖音搜索端点 /aweme/v1/web/general/search/stream/ 返回类似 NDJSON 的流式数据：
#   每行一个 JSON 对象，对象中 data 数组每项 {type, aweme_info}
# 响应原文本中保留 HTTP chunked 标记（十六进制长度 + CRLF），需按 \n 切行后
# 尝试 JSON.parse 每一行（非 JSON 行自动忽略）。
_SEARCH_INTERCEPT_JS = """
(() => {
    if (window.__SEARCH_HOOK_INSTALLED__) return;
    window.__SEARCH_HOOK_INSTALLED__ = true;
    window.__SEARCH_CAPTURED__ = [];

    function isSearchURL(url) {
        return url.includes('/aweme/v1/web/general/search/stream/')
            || url.includes('/aweme/v1/web/general/search/single/')
            || url.includes('/aweme/v1/web/search/item/');
    }

    // 从 stream/single 响应中提取 aweme_info 列表
    // 兼容三种格式：
    //   1) 单个 JSON 对象（/single/）
    //   2) NDJSON：每行一个 JSON 对象（/stream/）
    //   3) 带 chunked 分块标记的文本（长度 + CRLF + JSON + CRLF...）
    function parseResponseText(text) {
        const awemes = [];
        const seen = new Set();

        function collectFromObj(obj) {
            if (!obj || typeof obj !== 'object') return;
            const arr = obj.data || obj.aweme_list;
            if (!Array.isArray(arr)) return;
            for (const item of arr) {
                const a = item.aweme_info || item.aweme || item;
                if (a && a.aweme_id && a.aweme_type !== 101 && !seen.has(a.aweme_id)) {
                    seen.add(a.aweme_id);
                    awemes.push(a);
                }
            }
        }

        // 尝试 1：整体 JSON
        try {
            collectFromObj(JSON.parse(text));
            if (awemes.length > 0) return awemes;
        } catch(e) {}

        // 尝试 2：按 \\n 切行逐行 JSON（NDJSON 或 chunked 文本）
        const lines = text.split('\\n');
        for (let line of lines) {
            line = line.trim();
            if (!line || line.length < 2) continue;
            // 跳过 chunked 十六进制长度标记（纯十六进制字符）
            if (/^[0-9a-fA-F]+$/.test(line)) continue;
            // 必须以 { 开头
            if (line[0] !== '{') continue;
            try {
                collectFromObj(JSON.parse(line));
            } catch(e) {}
        }
        return awemes;
    }

    const _origFetch = window.fetch;
    window.fetch = async function(...args) {
        const resp = await _origFetch.apply(this, args);
        try {
            const url = typeof args[0] === 'string' ? args[0]
                      : (args[0]?.url || '');
            if (isSearchURL(url)) {
                const cloned = resp.clone();
                cloned.text().then(text => {
                    const awemes = parseResponseText(text);
                    if (awemes.length > 0) {
                        window.__SEARCH_CAPTURED__.push(...awemes);
                    }
                }).catch(() => {});
            }
        } catch(e) {}
        return resp;
    };

    // XHR 兜底
    const _origOpen = XMLHttpRequest.prototype.open;
    const _origSend = XMLHttpRequest.prototype.send;
    XMLHttpRequest.prototype.open = function(method, url, ...rest) {
        this.__searchURL = url;
        return _origOpen.call(this, method, url, ...rest);
    };
    XMLHttpRequest.prototype.send = function(...args) {
        if (this.__searchURL && isSearchURL(this.__searchURL)) {
            this.addEventListener('load', function() {
                try {
                    const awemes = parseResponseText(this.responseText || '');
                    if (awemes.length > 0) {
                        window.__SEARCH_CAPTURED__.push(...awemes);
                    }
                } catch(e) {}
            });
        }
        return _origSend.apply(this, args);
    };
})();
"""


def search_videos(page: Page, keyword: str, count: int = 10) -> list[Video]:
    """搜索抖音视频。

    通过首页搜索框输入关键词并触发搜索，模拟人类操作。
    避免直接导航到搜索 URL（会触发抖音反爬风控）。

    数据通过 fetch hook 从 /aweme/v1/web/general/search/stream/ 响应中提取。

    Args:
        page: CDP 页面对象。
        keyword: 搜索关键词。
        count: 期望返回的视频数量。

    Raises:
        NoResultsError: 无法获取搜索结果。
        RateLimitError: 检测到风控限流。
    """
    # 0. 预注入拦截 hook（在所有后续新文档加载前生效）
    page._send_session(
        "Page.addScriptToEvaluateOnNewDocument",
        {"source": _SEARCH_INTERCEPT_JS},
    )

    # 1. 导航到抖音首页（搜索入口）
    page.navigate(DOUYIN_HOME)
    page.wait_for_load()
    page.wait_dom_stable()

    # 即时注入兜底（SPA 场景下新文档注入可能未生效）
    page.evaluate(_SEARCH_INTERCEPT_JS)

    sleep_random(1500, 3000)

    # 2. 首页风控检测（首页也可能触发验证码）
    raise_if_risky(page)

    # 3. 定位搜索框
    search_input = _find_search_input(page)
    logger.info("定位到搜索框: %s", search_input)

    # 4. 点击搜索框聚焦
    page.click_element(search_input)
    sleep_random(300, 800)

    # 5. 模拟人类输入关键词
    _type_keyword_human(page, keyword, search_input)
    sleep_random(500, 1200)

    # 6. 按回车触发搜索
    page.press_key("Enter")
    sleep_random(2000, 4000)

    # 7. 等待搜索结果页加载
    page.wait_for_load()
    page.wait_dom_stable()

    # 8. 搜索结果页风控检测
    raise_if_risky(page)

    # 9. 等待拦截器收集到搜索 API 响应
    videos = _wait_for_captured(page, count, timeout=20.0)
    if not videos:
        raise NoResultsError(f"搜索 '{keyword}' 未返回结果")

    logger.info("搜索 '%s' 获取到 %d 个视频", keyword, len(videos))
    return videos[:count]


# ─── 内部辅助函数 ──────────────────────────────────────────────────────────


def _find_search_input(page: Page, timeout: float = 10.0) -> str:
    """在候选选择器中找到实际存在的搜索框。

    按优先级遍历候选选择器，返回第一个匹配的选择器字符串。
    如果全部不匹配，等待一段时间后重试。

    Raises:
        ElementNotFoundError: 超时仍未找到搜索框。
    """
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        for selector in _SEARCH_INPUT_CANDIDATES:
            if page.has_element(selector):
                return selector
        time.sleep(0.5)

    raise ElementNotFoundError(
        f"抖音首页搜索框未找到，尝试过: {_SEARCH_INPUT_CANDIDATES}"
    )


def _type_keyword_human(page: Page, keyword: str, selector: str) -> None:
    """模拟人类逐字输入关键词。

    策略：
    1. 先用 CDP Input.dispatchKeyEvent 逐字符输入（触发完整键盘事件链，最真实）
    2. 兜底用 React nativeInputValueSetter 确保 React 状态更新

    字符间随机 60~200ms 延迟，模拟真实打字节奏。
    """
    # 方法1：CDP 逐字符键盘输入（触发 keyDown/keyUp，isTrusted=true）
    for char in keyword:
        page._send_session(
            "Input.dispatchKeyEvent",
            {"type": "keyDown", "text": char},
        )
        page._send_session(
            "Input.dispatchKeyEvent",
            {"type": "keyUp", "text": char},
        )
        # 人类打字节奏：字符间 60~200ms，偶尔稍长停顿
        if random.random() < 0.1:
            sleep_random(250, 500)  # 10% 概率长停顿（模拟思考）
        else:
            sleep_random(60, 200)

    sleep_random(200, 500)

    # 方法2：兜底确保 React 状态同步
    # 如果 CDP 键盘事件未触发 React 更新，用 nativeInputValueSetter 补一刀
    page.evaluate(
        f"""
        (() => {{
            const input = document.querySelector({json.dumps(selector)});
            if (!input) return;
            // 检查当前值是否已正确
            if (input.value === {json.dumps(keyword)}) return;
            // React nativeInputValueSetter 兜底
            const setter = Object.getOwnPropertyDescriptor(
                window.HTMLInputElement.prototype, 'value'
            )?.set;
            if (setter) {{
                setter.call(input, {json.dumps(keyword)});
                input.dispatchEvent(new Event('input', {{ bubbles: true }}));
                input.dispatchEvent(new Event('change', {{ bubbles: true }}));
            }}
        }})()
        """
    )


def _wait_for_captured(page: Page, count: int, timeout: float = 20.0) -> list[Video]:
    """等待拦截器收集到足够的搜索结果。

    在超时前每 0.5s 轮询一次 window.__SEARCH_CAPTURED__。
    一旦累计数量 >= count 或时间耗尽，返回已收集到的视频列表。
    """
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        raw = page.evaluate(
            "window.__SEARCH_CAPTURED__ && window.__SEARCH_CAPTURED__.length > 0"
            " ? JSON.stringify(window.__SEARCH_CAPTURED__) : ''"
        )
        if raw:
            try:
                items = json.loads(raw)
            except json.JSONDecodeError:
                items = []
            if isinstance(items, list) and len(items) >= count:
                return _to_videos(items)
        time.sleep(0.5)

    # 超时：返回目前已收集到的（可能为空）
    raw = page.evaluate(
        "window.__SEARCH_CAPTURED__ ? JSON.stringify(window.__SEARCH_CAPTURED__) : '[]'"
    )
    try:
        items = json.loads(raw) if raw else []
    except json.JSONDecodeError:
        items = []
    if isinstance(items, list):
        return _to_videos(items)
    return []


def _to_videos(raw_list: list) -> list[Video]:
    """将 aweme_info 字典列表转为 Video 对象列表（过滤无效项）。"""
    videos: list[Video] = []
    for item in raw_list:
        if not isinstance(item, dict):
            continue
        try:
            videos.append(Video.from_dict(item))
        except Exception:  # noqa: BLE001 - 字段异常时跳过该项
            continue
    return videos
