"""抖音 URL 常量与构造函数。"""
from urllib.parse import quote

DOUYIN_HOME = "https://www.douyin.com"


def make_search_url(keyword: str) -> str:
    return f"https://www.douyin.com/search/{quote(keyword)}?type=video"


def make_user_url(sec_uid: str) -> str:
    return f"https://www.douyin.com/user/{sec_uid}"
