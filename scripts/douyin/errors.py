"""错误类型定义。"""


class CDPError(Exception):
    """CDP 通信错误。"""


class ElementNotFoundError(CDPError):
    """元素未找到。"""


class LoginRequiredError(Exception):
    """需要登录。"""


class NoResultsError(Exception):
    """未获取到数据。"""


class RateLimitError(Exception):
    """被风控限流（验证码/403/429/频率限制）。

    Attributes:
        retry_after: 建议等待秒数（0 表示未知）。
        reason: 触发原因描述。
    """

    def __init__(self, message: str, *, retry_after: int = 0, reason: str = "") -> None:
        super().__init__(message)
        self.retry_after = retry_after
        self.reason = reason
