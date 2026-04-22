"""抖音 URL 常量与构造函数。"""
from urllib.parse import quote

DOUYIN_HOME = "https://www.douyin.com"


def make_search_url(keyword: str) -> str:
    """构造搜索 URL（已弃用）。

    .. deprecated::
        搜索功能已改为通过首页搜索框模拟人类输入，
        不再直接导航到搜索 URL（避免触发风控）。
        保留此函数仅供可能的外部引用兼容。
    """
    return f"https://www.douyin.com/search/{quote(keyword)}?type=video"


def make_user_url(sec_uid: str) -> str:
    return f"https://www.douyin.com/user/{sec_uid}"
