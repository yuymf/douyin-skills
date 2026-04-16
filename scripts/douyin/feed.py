"""抖音首页推荐流提取。

核心策略（按优先级）：
1. JS fetch 拦截：导航到首页前注入 JS Hook，拦截浏览器自然发出的
   /aweme/v1/web/tab/feed/ 或 /aweme/v2/web/module/feed/ 请求响应。
   浏览器自带 cookie、msToken、a_bogus，无需逆向签名算法。
2. 页面内主动 fetch：在首页上下文中借用已有认证状态调用 tab/feed API。
3. 页面变量提取：从 RENDER_DATA / __INITIAL_STATE__ 提取 SSR 数据。

参考：
- yidai2024/douyin-api-capture: Playwright 拦截确认 feed 端点
- Johnserf-Seed/f2: PostFeed 模型和 TAB_FEED 端点定义
- NanmiCoder/MediaCrawler: 通用爬虫架构
"""
from __future__ import annotations

import json
import logging
import time

from .cdp import Page
from .errors import NoResultsError, RateLimitError
from .human import sleep_random
from .rate_guard import AdaptiveThrottle, check_page_risk, raise_if_risky, get_throttle
from .types import Video
from .urls import DOUYIN_HOME

logger = logging.getLogger(__name__)

# ============================================================================
# JS Hook：拦截 fetch / XHR 响应，捕获 feed API 返回的 aweme_list
# 注入到 Page.addScriptToEvaluateOnNewDocument，在页面加载前生效
# ============================================================================
_FEED_INTERCEPT_JS = """
(() => {
    window.__FEED_CAPTURED__ = [];

    // 匹配推荐流 API 端点
    function isFeedURL(url) {
        return url.includes('/web/tab/feed/')
            || url.includes('/web/module/feed/')
            || url.includes('/web/channel/feed/');
    }

    // 从 JSON 中提取 aweme 列表（过滤直播间 type=101）
    function extractAwemes(data) {
        if (!data || typeof data !== 'object') return [];
        let candidates = [];
        // v1 tab/feed 返回 aweme_list
        if (Array.isArray(data.aweme_list) && data.aweme_list.length > 0) {
            candidates = data.aweme_list;
        }
        // v2 module/feed 返回 data[]，每项包含 aweme
        if (candidates.length === 0 && Array.isArray(data.data)) {
            for (const item of data.data) {
                if (item.aweme) candidates.push(item.aweme);
                else if (item.aweme_info) candidates.push(item.aweme_info);
            }
        }
        // 过滤直播间和无效条目
        return candidates.filter(a => a.aweme_id && a.aweme_type !== 101 && a.desc);
    }

    // Hook fetch
    const _origFetch = window.fetch;
    window.fetch = async function(...args) {
        const resp = await _origFetch.apply(this, args);
        try {
            const url = typeof args[0] === 'string' ? args[0]
                      : (args[0]?.url || '');
            if (isFeedURL(url)) {
                const cloned = resp.clone();
                cloned.json().then(json => {
                    const awemes = extractAwemes(json);
                    if (awemes.length > 0) {
                        window.__FEED_CAPTURED__.push(...awemes);
                    }
                }).catch(() => {});
            }
        } catch(e) {}
        return resp;
    };

    // Hook XMLHttpRequest
    const _origOpen = XMLHttpRequest.prototype.open;
    const _origSend = XMLHttpRequest.prototype.send;
    XMLHttpRequest.prototype.open = function(method, url, ...rest) {
        this.__feedURL = url;
        return _origOpen.call(this, method, url, ...rest);
    };
    XMLHttpRequest.prototype.send = function(...args) {
        if (this.__feedURL && isFeedURL(this.__feedURL)) {
            this.addEventListener('load', function() {
                try {
                    const data = JSON.parse(this.responseText);
                    const awemes = extractAwemes(data);
                    if (awemes.length > 0) {
                        window.__FEED_CAPTURED__.push(...awemes);
                    }
                } catch(e) {}
            });
        }
        return _origSend.apply(this, args);
    };
})();
"""

# ============================================================================
# 在页面已有上下文中主动调用 tab/feed API（借用浏览器 cookie + a_bogus）
# ============================================================================
_FETCH_TAB_FEED_JS = """
(async (count, refreshIndex) => {
    try {
        const params = new URLSearchParams({
            device_platform: 'webapp',
            aid: '6383',
            channel: 'channel_pc_web',
            count: String(count),
            refresh_index: String(refreshIndex),
            video_type_select: '1',
            aweme_pc_rec_raw_data: encodeURIComponent('{"is_client":"false"}'),
            version_code: '170400',
            version_name: '17.4.0',
            cookie_enabled: 'true',
            screen_width: String(window.screen.width || 1920),
            screen_height: String(window.screen.height || 1080),
            browser_language: navigator.language || 'zh-CN',
            browser_platform: navigator.platform || 'MacIntel',
            browser_name: 'Chrome',
            browser_online: 'true',
            platform: 'PC',
        });
        const url = 'https://www.douyin.com/aweme/v1/web/tab/feed/?' + params.toString();
        const resp = await fetch(url, {
            credentials: 'include',
            headers: { 'Referer': 'https://www.douyin.com/' }
        });

        // 返回结构化结果（含 HTTP 状态和风控信息）
        const result = { status: resp.status, risk: [], videos: [] };

        if (resp.status === 403) { result.risk.push('http_403'); return JSON.stringify(result); }
        if (resp.status === 429) { result.risk.push('http_429'); return JSON.stringify(result); }
        if (resp.status >= 500)  { result.risk.push('http_' + resp.status); return JSON.stringify(result); }
        if (!resp.ok) return JSON.stringify(result);

        const text = await resp.text();
        try {
            const data = JSON.parse(text);
            // 检测 API 风控码
            const code = data.status_code ?? data.code;
            if (code === 2154 || code === 9) result.risk.push('api_rate_limit');
            if (code === 8 || code === 2) result.risk.push('api_blocked');

            const list = data.aweme_list || [];
            // 过滤直播间（aweme_type=101）和无效条目
            result.videos = list.filter(item =>
                item.aweme_id && item.aweme_type !== 101 && item.desc
            );
        } catch(e) {}
        return JSON.stringify(result);
    } catch(e) {
        return JSON.stringify({ status: 0, risk: ['fetch_error'], videos: [] });
    }
})
"""

# ============================================================================
# 从页面全局变量/SSR 数据中提取
# ============================================================================
_EXTRACT_FROM_PAGE_VARS_JS = """
(() => {
    // 1. 从 __INITIAL_STATE__ 提取
    try {
        const state = window.__INITIAL_STATE__;
        if (state) {
            for (const key of Object.keys(state)) {
                const entry = state[key];
                if (entry && typeof entry === 'object') {
                    const list = entry?.awemeList || entry?.list;
                    if (Array.isArray(list) && list.length > 0 && list[0]?.aweme_id) {
                        return JSON.stringify(list);
                    }
                }
            }
        }
    } catch(e) {}

    // 2. 从 RENDER_DATA 提取（递归搜索 aweme_id）
    try {
        const el = document.getElementById('RENDER_DATA');
        if (el) {
            const raw = decodeURIComponent(el.textContent || '');
            const data = JSON.parse(raw);
            const found = [];
            function search(obj, depth) {
                if (depth > 6 || found.length >= 30) return;
                if (Array.isArray(obj)) {
                    for (const item of obj) search(item, depth + 1);
                } else if (obj && typeof obj === 'object') {
                    if (obj.aweme_id || obj.awemeId) found.push(obj);
                    for (const v of Object.values(obj)) {
                        if (found.length >= 30) return;
                        search(v, depth + 1);
                    }
                }
            }
            search(data, 0);
            if (found.length > 0) return JSON.stringify(found);
        }
    } catch(e) {}

    return '';
})()
"""


def _parse_aweme_list(raw_list: list) -> list[Video]:
    """从 aweme list 解析 Video 对象列表。"""
    videos = []
    for item in raw_list:
        if not isinstance(item, dict):
            continue
        try:
            videos.append(Video.from_dict(item))
        except Exception:
            continue
    return videos


def _collect_intercepted(page: Page) -> list[Video]:
    """从 __FEED_CAPTURED__ 中收集拦截到的推荐流数据。"""
    result = page.evaluate(
        "window.__FEED_CAPTURED__ && window.__FEED_CAPTURED__.length > 0"
        " ? JSON.stringify(window.__FEED_CAPTURED__) : ''"
    )
    if not result:
        return []
    try:
        raw_list = json.loads(result)
    except json.JSONDecodeError:
        return []
    if not isinstance(raw_list, list):
        return []
    return _parse_aweme_list(raw_list)


def _fetch_via_page_js(page: Page, count: int, refresh_index: int) -> tuple[list[Video], list[str]]:
    """在页面内通过 fetch 调用 tab/feed API。

    Returns:
        (视频列表, 风控信号列表)。风控信号非空表示检测到异常。
    """
    result = page._send_session(
        "Runtime.evaluate",
        {
            "expression": f"({_FETCH_TAB_FEED_JS})({count}, {refresh_index})",
            "returnByValue": True,
            "awaitPromise": True,
        },
    )
    value = result.get("result", {}).get("value", "")
    if not value:
        return [], []
    try:
        data = json.loads(value)
    except json.JSONDecodeError:
        return [], []

    # 解析结构化响应
    risk_signals = data.get("risk", [])
    raw_videos = data.get("videos", [])

    if risk_signals:
        logger.warning("API 风控信号: %s (status=%s)", risk_signals, data.get("status"))

    if not isinstance(raw_videos, list):
        return [], risk_signals

    return _parse_aweme_list(raw_videos), risk_signals


def _extract_from_page_vars(page: Page) -> list[Video]:
    """从页面全局变量提取推荐视频（__INITIAL_STATE__ / RENDER_DATA）。"""
    result = page.evaluate(_EXTRACT_FROM_PAGE_VARS_JS)
    if not result:
        return []
    try:
        raw_list = json.loads(result)
    except json.JSONDecodeError:
        return []
    if not isinstance(raw_list, list):
        return []
    return _parse_aweme_list(raw_list)


def fetch_home_feed(page: Page, count: int = 20, refresh_index: int = 0) -> list[Video]:
    """获取抖音首页推荐流。

    三级策略（按优先级）：
    1. JS 拦截：注入 fetch/XHR hook → 导航首页 → 等待浏览器自然请求
       feed API → 收集响应中的 aweme_list（最可靠，无需签名逆向）
    2. 页面内 fetch：在首页上下文中借用 cookie 和 a_bogus 主动调用
       /aweme/v1/web/tab/feed/ 接口
    3. 页面变量：从 RENDER_DATA / __INITIAL_STATE__ 提取 SSR 数据

    Args:
        page: CDP 页面对象。
        count: 期望返回的视频数量（默认 20）。
        refresh_index: 翻页索引（0=首次请求，递增翻页）。

    Raises:
        NoResultsError: 无法获取推荐流数据。
        RateLimitError: 检测到风控限流。
    """
    throttle = get_throttle()
    # ── 策略 1：JS 拦截 feed API 响应 ─────────────────────────────────
    logger.info("推荐流: 注入 fetch/XHR 拦截 hook")
    try:
        # 双重注入：addScriptToEvaluateOnNewDocument（预注入）+ Runtime.evaluate（即时注入）
        page._send_session(
            "Page.addScriptToEvaluateOnNewDocument",
            {"source": _FEED_INTERCEPT_JS},
        )

        # 导航到首页
        page.navigate(DOUYIN_HOME)
        page.wait_for_load()

        # 即时注入兜底（确保 SPA 路由切换场景也能生效）
        page.evaluate(_FEED_INTERCEPT_JS)
        sleep_random(3000, 5000)

        # 风控检测：检查页面是否出现验证码/登录弹窗
        raise_if_risky(page)

        # 等待 feed API 自然触发（首页加载时浏览器会自动请求推荐流）
        deadline = time.monotonic() + 15.0
        while time.monotonic() < deadline:
            videos = _collect_intercepted(page)
            if videos:
                logger.info("推荐流: 拦截到 %d 个视频", len(videos))
                throttle.record_success()
                return videos[:count]
            # 滚动触发加载更多
            page.evaluate("window.scrollBy(0, 400)")
            sleep_random(1500, 2500)

        logger.info("推荐流: 拦截超时，未捕获到 feed 数据")
    except RateLimitError:
        raise  # 风控直接上抛
    except Exception as e:
        logger.warning("推荐流: JS 拦截失败: %s", e)

    # ── 策略 2：页面内主动 fetch tab/feed API ─────────────────────────
    logger.info("推荐流: 降级到页面内 fetch tab/feed")
    try:
        # 确保在抖音页面上下文中
        current_url = page.evaluate("location.href") or ""
        if "douyin.com" not in current_url:
            page.navigate(DOUYIN_HOME)
            page.wait_for_load()
            sleep_random(2000, 4000)
            # 导航后再次检测风控
            raise_if_risky(page)

        # 多轮 fetch 累积结果（每轮递增 refresh_index 翻页）
        # tab/feed API 每次只返回 1-3 条视频（网页端沉浸式模式）
        all_videos: list[Video] = []
        seen_ids: set[str] = set()
        max_rounds = min(15, max(5, count * 2))  # 每轮约 1-2 条，需要更多轮
        empty_rounds = 0  # 连续空轮次计数

        for r in range(max_rounds):
            # 自适应等待：根据退避状态调整间隔
            throttle.wait()

            batch, risk_signals = _fetch_via_page_js(page, 10, refresh_index + r)

            # 风控信号处理
            if risk_signals:
                throttle.record_failure()
                if any(s in ("http_403", "http_429", "api_rate_limit", "api_blocked")
                       for s in risk_signals):
                    logger.warning("推荐流: API 风控触发 (%s)，中止多轮请求",
                                   risk_signals)
                    # 被限流了，不要继续请求，用已有数据返回
                    break
                # 其他信号（如 5xx），短暂等待后继续
                sleep_random(3000, 6000)
                continue

            if batch:
                throttle.record_success()
                empty_rounds = 0
                for v in batch:
                    if v.aweme_id and v.aweme_id not in seen_ids:
                        seen_ids.add(v.aweme_id)
                        all_videos.append(v)
            else:
                empty_rounds += 1
                # 连续 3 轮空结果，可能被静默限流
                if empty_rounds >= 3:
                    logger.warning("推荐流: 连续 %d 轮空结果，疑似静默限流",
                                   empty_rounds)
                    throttle.record_failure()
                    break

            if len(all_videos) >= count:
                break

        if all_videos:
            logger.info("推荐流: 页面内 fetch 获取到 %d 个视频（%d 轮）",
                        len(all_videos), r + 1)
            return all_videos[:count]
    except RateLimitError:
        raise  # 风控直接上抛
    except Exception as e:
        logger.warning("推荐流: 页面内 fetch 失败: %s", e)

    # ── 策略 3：页面变量提取 ──────────────────────────────────────────
    logger.info("推荐流: 降级到页面变量提取")
    try:
        videos = _extract_from_page_vars(page)
        if videos:
            logger.info("推荐流: 从页面变量提取到 %d 个视频", len(videos))
            return videos[:count]

        # 滚动后再试
        for _ in range(2):
            page.evaluate("window.scrollBy(0, 600)")
            sleep_random(1500, 3000)
            videos = _extract_from_page_vars(page)
            if videos:
                logger.info("推荐流: 滚动后从页面变量提取到 %d 个视频", len(videos))
                return videos[:count]
    except Exception as e:
        logger.warning("推荐流: 页面变量提取失败: %s", e)

    raise NoResultsError("无法获取抖音首页推荐流数据")
