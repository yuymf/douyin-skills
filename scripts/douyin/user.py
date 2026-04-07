"""抖音用户主页视频列表提取。"""
from __future__ import annotations

import json
import logging
import time

from .cdp import Page
from .errors import NoResultsError
from .human import sleep_random
from .types import Video
from .urls import make_user_url

logger = logging.getLogger(__name__)

# 从 RENDER_DATA 提取（旧方案，作为降级回退）
_EXTRACT_USER_VIDEOS_JS = """
(() => {
    const el = document.getElementById('RENDER_DATA');
    if (el) {
        try {
            const raw = decodeURIComponent(el.textContent || '');
            const data = JSON.parse(raw);
            // 遍历常见路径（页面版本不同路径会变）
            for (const key of Object.keys(data)) {
                const list = data[key]?.aweme?.awemeList
                    || data[key]?.userPost?.awemeList
                    || data[key]?.post?.data;
                if (Array.isArray(list) && list.length > 0) {
                    return JSON.stringify(list);
                }
            }
        } catch(e) {}
    }
    return '';
})()
"""

# 通过页面内 fetch 直接调用 aweme/post API（借用页面已有的 cookie 和 ms_token）
_FETCH_USER_POSTS_JS = """
(async (secUid, count) => {
    try {
        // 从页面 JS 上下文里找已有的参数（ms_token、device_id 等）
        const searchParams = new URLSearchParams(location.search);
        const apiBase = 'https://www.douyin.com/aweme/v1/web/aweme/post/';
        const params = new URLSearchParams({
            device_platform: 'webapp',
            aid: '6383',
            channel: 'channel_pc_web',
            sec_user_id: secUid,
            max_cursor: '0',
            locate_query: 'false',
            show_live_replay_strategy: '1',
            need_time_list: '1',
            time_list_query: '0',
            whale_cut_token: '',
            cut_version: '1',
            reduce_similar: 'false',
            cursor: '0',
            count: String(count),
            publish_video_strategy_type: '2',
            update_version_code: '170400',
            pc_client_type: '1',
            version_code: '190500',
            version_name: '19.5.0',
        });
        const resp = await fetch(apiBase + '?' + params.toString(), {
            credentials: 'include',
            headers: {
                'Referer': location.href,
            }
        });
        if (!resp.ok) return '';
        const data = await resp.json();
        const list = data.aweme_list || data.awemeList || [];
        return list.length > 0 ? JSON.stringify(list) : '';
    } catch(e) {
        return '';
    }
})(arguments[0], arguments[1])
"""


def _fetch_via_page_js(page: Page, sec_uid: str, count: int) -> list[Video]:
    """通过页面内 fetch 调用 aweme/post API（借用页面 cookie 和认证状态）。"""
    result = page._send_session(
        "Runtime.evaluate",
        {
            "expression": f"""
(async () => {{
    try {{
        const secUid = {json.dumps(sec_uid)};
        const count = {count};
        const apiBase = 'https://www.douyin.com/aweme/v1/web/aweme/post/';
        const params = new URLSearchParams({{
            device_platform: 'webapp',
            aid: '6383',
            channel: 'channel_pc_web',
            sec_user_id: secUid,
            max_cursor: '0',
            locate_query: 'false',
            show_live_replay_strategy: '1',
            need_time_list: '1',
            time_list_query: '0',
            whale_cut_token: '',
            cut_version: '1',
            reduce_similar: 'false',
            cursor: '0',
            count: String(count),
            publish_video_strategy_type: '2',
            update_version_code: '170400',
            pc_client_type: '1',
            version_code: '190500',
            version_name: '19.5.0',
        }});
        const resp = await fetch(apiBase + '?' + params.toString(), {{
            credentials: 'include',
            headers: {{ 'Referer': location.href }}
        }});
        if (!resp.ok) return '';
        const data = await resp.json();
        const list = data.aweme_list || data.awemeList || [];
        return list.length > 0 ? JSON.stringify(list) : '';
    }} catch(e) {{
        return '';
    }}
}})()
""",
            "returnByValue": True,
            "awaitPromise": True,
        },
    )
    value = result.get("result", {}).get("value", "")
    if not value:
        raise NoResultsError(f"页面内 fetch 返回空: {sec_uid}")
    raw_list = json.loads(value)
    if not isinstance(raw_list, list) or len(raw_list) == 0:
        raise NoResultsError(f"页面内 fetch 返回空列表: {sec_uid}")
    return _parse_aweme_list(raw_list[:count])


def _parse_aweme_list(raw_list: list) -> list[Video]:
    """从 aweme list 解析 Video 对象列表。"""
    return [Video.from_dict(item) for item in raw_list]


def list_user_posts(page: Page, sec_uid: str, count: int = 10) -> list[Video]:
    """获取用户主页的视频列表。

    主路径：导航到用户主页后，通过页面内 fetch 调用 aweme/post API（借用页面 cookie）。
    降级路径：从 RENDER_DATA 提取（兼容旧版页面结构）。

    Args:
        page: CDP 页面对象。
        sec_uid: 抖音用户的 sec_uid（URL 中的那段）。
        count: 期望获取的视频数量（最多）。

    Raises:
        NoResultsError: 无法提取视频数据。
    """
    url = make_user_url(sec_uid)
    page.navigate(url)
    page.wait_for_load()
    page.wait_dom_stable()
    # 等页面 JS 完成初始化和 cookie 注入
    sleep_random(2500, 4000)

    # 主路径：页面内 fetch（借用 cookie/ms_token，绕过跨域和签名问题）
    try:
        videos = _fetch_via_page_js(page, sec_uid, count)
        logger.info("用户 %s 通过页面内 fetch 提取到 %d 个视频", sec_uid, len(videos))
        return videos
    except NoResultsError as e:
        logger.warning("页面内 fetch 失败，降级到 RENDER_DATA: %s", e)

    # 降级路径：RENDER_DATA
    result = page.evaluate(_EXTRACT_USER_VIDEOS_JS)
    if not result:
        page.evaluate("window.scrollBy(0, 500)")
        sleep_random(1500, 3000)
        result = page.evaluate(_EXTRACT_USER_VIDEOS_JS)

    if not result:
        raise NoResultsError(f"无法从用户主页提取视频数据: {sec_uid}")

    raw_list = json.loads(result)
    if not isinstance(raw_list, list):
        raise NoResultsError(f"数据格式异常: {type(raw_list)}")

    videos = [Video.from_dict(item) for item in raw_list[:count]]
    logger.info("用户 %s 通过 RENDER_DATA 提取到 %d 个视频", sec_uid, len(videos))
    return videos
