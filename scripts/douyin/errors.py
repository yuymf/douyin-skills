"""错误类型定义。"""


class CDPError(Exception):
    """CDP 通信错误。"""


class ElementNotFoundError(CDPError):
    """元素未找到。"""


class LoginRequiredError(Exception):
    """需要登录。"""


class NoResultsError(Exception):
    """未获取到数据。"""
