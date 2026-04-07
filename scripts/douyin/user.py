"""抖音用户主页视频列表提取。"""
from __future__ import annotations

import json
import logging

from .cdp import Page
from .errors import NoResultsError
from .human import sleep_random
from .types import Video
from .urls import make_user_url

logger = logging.getLogger(__name__)

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


def list_user_posts(page: Page, sec_uid: str, count: int = 10) -> list[Video]:
    """获取用户主页的视频列表。

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
    sleep_random(2000, 4000)

    result = page.evaluate(_EXTRACT_USER_VIDEOS_JS)
    if not result:
        # 尝试滚动触发懒加载后重试
        page.evaluate("window.scrollBy(0, 500)")
        sleep_random(1500, 3000)
        result = page.evaluate(_EXTRACT_USER_VIDEOS_JS)

    if not result:
        raise NoResultsError(f"无法从用户主页提取视频数据: {sec_uid}")

    raw_list = json.loads(result)
    if not isinstance(raw_list, list):
        raise NoResultsError(f"数据格式异常: {type(raw_list)}")

    videos = [Video.from_dict(item) for item in raw_list[:count]]
    logger.info("用户 %s 提取到 %d 个视频", sec_uid, len(videos))
    return videos
