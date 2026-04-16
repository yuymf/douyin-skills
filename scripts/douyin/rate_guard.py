"""风控检测与自适应节流。

提供三层防护：
1. 页面风控检测：识别验证码弹窗、登录重定向、403 等页面级风控信号
2. API 响应风控检测：识别 HTTP 状态码异常和返回数据中的风控标记
3. 自适应退避：根据连续失败次数动态增加请求间隔

长时间运行的关键策略：
- 连续失败 → 指数退避（基础间隔 × 2^n，封顶 5 分钟）
- 检测到验证码 → 长暂停 + 抛出 RateLimitError 让上层决策
- 请求间隔加入高斯噪声，避免机械化固定间隔
"""
from __future__ import annotations

import logging
import random
import time

from .cdp import Page
from .errors import RateLimitError
from .human import sleep_random

logger = logging.getLogger(__name__)


# ─── 页面风控检测 ──────────────────────────────────────────────────────────

# 在页面中检测风控信号的 JS（验证码弹窗、登录强制跳转等）
_DETECT_RISK_CONTROL_JS = """
(() => {
    const signals = [];

    // 1. 验证码/滑块弹窗
    const captchaSelectors = [
        '#captcha_container',
        '.captcha_verify_container',
        '.verify-wrap',
        '[class*="captcha"]',
        '[class*="verify"]',
        '[id*="captcha"]',
        'iframe[src*="captcha"]',
        'iframe[src*="verify"]',
    ];
    for (const sel of captchaSelectors) {
        const el = document.querySelector(sel);
        if (el && el.offsetHeight > 0) {
            signals.push('captcha:' + sel);
            break;
        }
    }

    // 2. 登录强制跳转
    if (location.href.includes('/login') || location.href.includes('login_redirect')) {
        signals.push('login_redirect');
    }
    const loginGuide = document.querySelector('.login-guide, .login-panel, [class*="login-modal"]');
    if (loginGuide && loginGuide.offsetHeight > 0) {
        signals.push('login_modal');
    }

    // 3. 访问频率提示（"操作太频繁"、"请稍后再试" 等）
    const bodyText = document.body?.innerText || '';
    if (/操作太频繁|请稍后再试|访问频率|频率限制|rate.?limit/i.test(bodyText.slice(0, 2000))) {
        signals.push('rate_limit_text');
    }

    // 4. 空白页/错误页
    if (document.title.includes('验证') || document.title.includes('安全')) {
        signals.push('security_page');
    }

    return signals.length > 0 ? JSON.stringify(signals) : '';
})()
"""

# 在 JS fetch 响应中检测风控信号的代码片段（嵌入到 _FETCH_TAB_FEED_JS 中）
_DETECT_API_RISK_JS = """
// 检查 API 响应中的风控标记
function detectApiRisk(resp, data) {
    const risks = [];
    if (resp.status === 403) risks.push('http_403');
    if (resp.status === 429) risks.push('http_429');
    if (resp.status >= 500) risks.push('http_' + resp.status);
    if (data && typeof data === 'object') {
        // 抖音 API 常见风控码
        const code = data.status_code ?? data.code;
        if (code === 2154 || code === 9) risks.push('api_rate_limit');
        if (code === 8 || code === 2) risks.push('api_blocked');
        if (data.filter_list?.length > 0) risks.push('content_filtered');
    }
    return risks;
}
"""


def check_page_risk(page: Page) -> list[str]:
    """检测页面上的风控信号。

    Returns:
        风控信号列表（空 = 安全）。
        可能的值：captcha:xxx, login_redirect, login_modal,
                  rate_limit_text, security_page
    """
    try:
        result = page.evaluate(_DETECT_RISK_CONTROL_JS)
        if not result:
            return []
        import json
        signals = json.loads(result)
        if signals:
            logger.warning("检测到页面风控信号: %s", signals)
        return signals
    except Exception:
        return []


def raise_if_risky(page: Page) -> None:
    """如果检测到页面风控，抛出 RateLimitError。"""
    signals = check_page_risk(page)
    if not signals:
        return

    reason = ", ".join(signals)
    if any("captcha" in s for s in signals):
        raise RateLimitError(
            f"检测到验证码弹窗: {reason}",
            retry_after=120,
            reason="captcha",
        )
    if any("login" in s for s in signals):
        raise RateLimitError(
            f"被强制跳转到登录页: {reason}",
            retry_after=60,
            reason="login_redirect",
        )
    if any("rate_limit" in s for s in signals):
        raise RateLimitError(
            f"页面提示频率限制: {reason}",
            retry_after=180,
            reason="rate_limit",
        )
    raise RateLimitError(
        f"页面风控信号: {reason}",
        retry_after=60,
        reason="page_risk",
    )


# ─── 自适应退避 ────────────────────────────────────────────────────────────

class AdaptiveThrottle:
    """自适应请求节流器。

    根据连续失败次数动态调整请求间隔：
    - 成功时恢复基础间隔
    - 失败时指数退避（base × 2^n），封顶 max_backoff_s
    - 每次请求间隔加入高斯噪声（±30%），避免机械化特征
    """

    def __init__(
        self,
        base_interval_ms: int = 1500,
        max_backoff_s: float = 300.0,  # 5 分钟封顶
    ) -> None:
        self.base_interval_ms = base_interval_ms
        self.max_backoff_s = max_backoff_s
        self._consecutive_failures = 0
        self._last_request_time = 0.0

    def record_success(self) -> None:
        """记录成功请求，重置退避计数。"""
        if self._consecutive_failures > 0:
            logger.info("风控退避: 请求成功，重置退避计数 (%d → 0)",
                        self._consecutive_failures)
        self._consecutive_failures = 0

    def record_failure(self) -> None:
        """记录失败请求，增加退避计数。"""
        self._consecutive_failures += 1
        logger.warning("风控退避: 连续失败 %d 次", self._consecutive_failures)

    @property
    def consecutive_failures(self) -> int:
        return self._consecutive_failures

    def should_abort(self, max_consecutive_failures: int = 5) -> bool:
        """是否应该中止当前操作（连续失败太多次）。"""
        return self._consecutive_failures >= max_consecutive_failures

    def wait(self) -> None:
        """等待适当的间隔后再发起下一次请求。

        间隔计算：base × 2^failures + gaussian_noise
        """
        if self._consecutive_failures == 0:
            # 正常模式：基础间隔 + 随机噪声
            base_ms = self.base_interval_ms
            noise_ms = random.gauss(0, base_ms * 0.3)
            delay_ms = max(500, base_ms + noise_ms)
        else:
            # 退避模式：指数递增
            backoff_s = min(
                self.max_backoff_s,
                (self.base_interval_ms / 1000.0) * (2 ** self._consecutive_failures),
            )
            noise_s = random.gauss(0, backoff_s * 0.2)
            delay_s = max(1.0, backoff_s + noise_s)
            delay_ms = delay_s * 1000
            logger.info("风控退避: 等待 %.1f 秒 (failures=%d)",
                        delay_ms / 1000, self._consecutive_failures)

        # 保证最小间隔不低于上次请求
        now = time.monotonic()
        elapsed_ms = (now - self._last_request_time) * 1000
        remaining_ms = delay_ms - elapsed_ms
        if remaining_ms > 0:
            time.sleep(remaining_ms / 1000.0)

        self._last_request_time = time.monotonic()

    def wait_after_risk(self, retry_after: int = 0) -> None:
        """检测到风控后的长暂停。

        Args:
            retry_after: RateLimitError 建议的等待秒数（0=使用默认）。
        """
        wait_s = retry_after if retry_after > 0 else 60
        # 加入 ±20% 噪声
        noise = random.gauss(0, wait_s * 0.2)
        actual_s = max(30, wait_s + noise)
        logger.warning("风控暂停: 等待 %.0f 秒后继续", actual_s)
        time.sleep(actual_s)
        self._last_request_time = time.monotonic()


# 全局节流器实例（跨函数共享状态）
_global_throttle = AdaptiveThrottle()


def get_throttle() -> AdaptiveThrottle:
    """获取全局节流器实例。"""
    return _global_throttle
