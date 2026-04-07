"""抖音搜索功能实现。"""
from __future__ import annotations

import json
import logging

from .cdp import Page
from .errors import NoResultsError
from .human import sleep_random
from .types import Video
from .urls import make_search_url

logger = logging.getLogger(__name__)

_EXTRACT_SEARCH_JS = """
(() => {
    const el = document.getElementById('RENDER_DATA');
    if (el) {
        try {
            const raw = decodeURIComponent(el.textContent || '');
            const data = JSON.parse(raw);
            for (const key of Object.keys(data)) {
                const itemList = data[key]?.search?.itemList
                    || data[key]?.searchResult?.itemList;
                if (Array.isArray(itemList) && itemList.length > 0) {
                    // 搜索结果是 {type, aweme} 结构，展开 aweme
                    return JSON.stringify(itemList.map(i => i.aweme || i).filter(Boolean));
                }
            }
        } catch(e) {}
    }
    return '';
})()
"""


def search_videos(page: Page, keyword: str, count: int = 10) -> list[Video]:
    """搜索抖音视频。

    Args:
        page: CDP 页面对象。
        keyword: 搜索关键词。
        count: 期望返回的视频数量。

    Raises:
        NoResultsError: 无法获取搜索结果。
    """
    url = make_search_url(keyword)
    page.navigate(url)
    page.wait_for_load()
    page.wait_dom_stable()
    sleep_random(2000, 4000)

    result = page.evaluate(_EXTRACT_SEARCH_JS)
    if not result:
        raise NoResultsError(f"搜索 '{keyword}' 未返回结果")

    raw_list = json.loads(result)
    if not isinstance(raw_list, list):
        raise NoResultsError(f"搜索结果格式异常: {type(raw_list)}")

    videos = [Video.from_dict(item) for item in raw_list[:count]]
    logger.info("搜索 '%s' 获取到 %d 个视频", keyword, len(videos))
    return videos
