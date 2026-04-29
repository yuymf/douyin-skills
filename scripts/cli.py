"""抖音 CDP CLI 入口。退出码: 0=成功, 1=未登录, 2=错误, 3=风控限流"""
from __future__ import annotations

import argparse
import json
import logging
import os
import sys

DEFAULT_PORT = 9333
DEFAULT_PROFILE_DIR = os.path.join(os.path.expanduser("~"), ".douyin", "chrome-profile")


def _should_use_headless() -> bool:
    if os.getenv("CI") or os.getenv("E2E_MOCK_DOUYIN"):
        return True
    sys.path.insert(0, os.path.dirname(__file__))
    from chrome_launcher import has_display
    return not has_display()


def _get_page(host: str, port: int):
    """获取 CDP Page 实例（附加到第一个 page target）。"""
    import requests
    from douyin.cdp import CDPClient, Page

    targets = requests.get(f"http://{host}:{port}/json", timeout=5).json()
    target = next((t for t in targets if t.get("type") == "page"), None)
    if not target:
        raise RuntimeError("没有可用的 page target")
    cdp = CDPClient(target["webSocketDebuggerUrl"])
    result = cdp.send("Target.attachToTarget", {"targetId": target["id"], "flatten": True})
    page = Page(cdp, target["id"], result.get("sessionId", ""))
    return cdp, page


def _run(args, fn):
    """通用执行框架：启动 Chrome → 执行 → 关闭。"""
    sys.path.insert(0, os.path.dirname(__file__))
    from chrome_launcher import launch_chrome, wait_for_chrome
    from douyin.errors import RateLimitError

    headless = _should_use_headless()
    proc = launch_chrome(port=args.port, headless=headless, user_data_dir=DEFAULT_PROFILE_DIR)
    wait_for_chrome(args.port)
    try:
        _, page = _get_page(args.host, args.port)
        result = fn(page)
        print(json.dumps(result, ensure_ascii=False))
        return 0
    except RateLimitError as e:
        # 风控限流：返回结构化信息，退出码 3
        print(json.dumps({
            "success": False,
            "error": str(e),
            "rate_limited": True,
            "retry_after": e.retry_after,
            "reason": e.reason,
        }, ensure_ascii=False))
        return 3
    except Exception as e:
        print(json.dumps({"success": False, "error": str(e)}, ensure_ascii=False))
        return 2
    finally:
        if proc:
            proc.terminate()


def cmd_check_login(args) -> int:
    _CHECK_LOGIN_JS = r"""
    (() => {
        const debug = {};

        // 1. 明确被强制跳转到登录页 → 未登录
        debug.href = location.href;
        if (location.href.includes('/login') || location.href.includes('login_redirect')) {
            return {logged_in: false, reason: 'login_redirect', debug};
        }

        // 2. 有可见的登录弹窗/引导 → 未登录
        const loginSelectors = '.login-guide, .login-panel, [class*="login-modal"], [class*="LoginModal"]';
        const loginEl = document.querySelector(loginSelectors);
        if (loginEl && loginEl.offsetHeight > 0) {
            return {logged_in: false, reason: 'login_modal', debug};
        }

        // 3. 检查 cookie：sessionid 必须存在且非空
        const cookies = document.cookie;
        debug.cookies_snippet = cookies.slice(0, 200);
        const sessionMatch = cookies.match(/(?:^|;\s*)sessionid=([^;]+)/);
        const hasSession = sessionMatch && sessionMatch[1].length > 0;
        debug.has_sessionid = hasSession;

        // 4. 检查页面内嵌的用户数据（抖音 __RENDER_DATA__ 或 localStorage）
        let hasUserInfo = false;
        try {
            const rd = window.__RENDER_DATA__;
            if (rd && typeof rd === 'object') {
                const uid = rd?.app?.user?.id || rd?.user?.id;
                debug.render_data_uid = uid;
                if (uid) hasUserInfo = true;
            }
        } catch(e) {}
        try {
            const ls = localStorage.getItem('user_info') || localStorage.getItem('userInfo');
            debug.localStorage_user = !!ls;
            if (ls) hasUserInfo = true;
        } catch(e) {}

        // 5. 检查导航栏登录按钮文本（未登录时显示"登录"）
        //    抖音首页右上角：未登录显示登录按钮，已登录显示头像
        const navButtons = document.querySelectorAll('button, [role="button"], a');
        for (const btn of navButtons) {
            const text = (btn.textContent || '').trim();
            if (text === '登录') {
                debug.login_button_found = true;
                return {logged_in: false, reason: 'login_button_visible', debug};
            }
        }

        // 6. 登录后才有的导航元素
        const loggedSelectors = [
            '[data-e2e="avatar-icon"]',
            '[data-e2e="user-avatar"]',
            '.avatar-icon',
            '.sidebar-user-info',
        ];
        for (const sel of loggedSelectors) {
            const el = document.querySelector(sel);
            if (el && el.offsetHeight > 0) {
                debug.logged_selector = sel;
                return {logged_in: true, reason: 'logged_element:' + sel, debug};
            }
        }

        // 综合判定
        if (hasSession || hasUserInfo) {
            return {logged_in: true, reason: 'cookie_or_render_data', debug};
        }

        return {logged_in: false, reason: 'no_login_evidence', debug};
    })()
    """

    def _fn(page):
        page.navigate("https://www.douyin.com")
        page.wait_for_load()
        # 等待页面 JS 渲染完成
        import time
        time.sleep(3)
        result = page.evaluate(_CHECK_LOGIN_JS)
        logged_in = result.get("logged_in", False) if isinstance(result, dict) else bool(result)
        out = {"success": True, "logged_in": logged_in}
        if args.debug and isinstance(result, dict):
            out["reason"] = result.get("reason")
            out["debug"] = result.get("debug")
        return out
    return _run(args, _fn)


def cmd_login(args) -> int:
    """打开 Chrome 让用户手动登录抖音，登录后 cookie 自动保存在 profile 中。"""
    import time

    sys.path.insert(0, os.path.dirname(__file__))
    from chrome_launcher import launch_chrome, wait_for_chrome

    # 必须有界面，不能用 headless
    proc = launch_chrome(port=args.port, headless=False, user_data_dir=DEFAULT_PROFILE_DIR)
    wait_for_chrome(args.port)

    cdp = None
    try:
        cdp, page = _get_page(args.host, args.port)
        # 导航到抖音首页，用户点击右上角"登录"按钮即可
        page.navigate("https://www.douyin.com")
        page.wait_for_load()

        print("浏览器已打开抖音首页，请点击右上角「登录」按钮完成登录。", file=sys.stderr)
        print("登录成功后请回到终端按 Enter 确认...", file=sys.stderr)

        # 等待用户在终端按 Enter
        input()

        # 尝试验证登录状态（可能因页面跳转导致 CDP 断连，需容错）
        logged_in = False
        for attempt in range(3):
            try:
                # 重新获取 page 连接（之前的可能已失效）
                cdp2, page2 = _get_page(args.host, args.port)
                page2.navigate("https://www.douyin.com")
                page2.wait_for_load()
                time.sleep(3)

                _CHECK_JS = r"""
                (() => {
                    const sessionMatch = document.cookie.match(/(?:^|;\s*)sessionid=([^;]+)/);
                    return sessionMatch && sessionMatch[1].length > 0;
                })()
                """
                logged_in = bool(page2.evaluate(_CHECK_JS))
                try:
                    cdp2.close()
                except Exception:
                    pass
                break
            except Exception:
                time.sleep(2)
                continue

        if logged_in:
            print(json.dumps({"success": True, "logged_in": True}, ensure_ascii=False))
            return 0
        else:
            print(json.dumps({"success": True, "logged_in": False}, ensure_ascii=False))
            return 1
    except Exception as e:
        print(json.dumps({"success": False, "error": str(e)}, ensure_ascii=False))
        return 2
    finally:
        # 关闭 CDP 连接，不杀 Chrome 进程，保留登录态
        try:
            if cdp:
                cdp.close()
        except Exception:
            pass


def cmd_user_posts(args) -> int:
    from douyin.user import list_user_posts

    def _fn(page):
        videos = list_user_posts(page, args.sec_uid, count=args.count)
        return {
            "success": True,
            "videos": [
                {
                    "aweme_id": v.aweme_id,
                    "desc": v.desc,
                    "create_time": v.create_time,
                    "is_top": v.is_top,
                    "author": v.author.nickname,
                    "digg_count": v.stats.digg_count,
                }
                for v in videos
            ],
        }
    return _run(args, _fn)


def cmd_search_videos(args) -> int:
    from douyin.search import search_videos

    def _fn(page):
        videos = search_videos(page, args.keyword, count=args.count)
        return {
            "success": True,
            "videos": [
                {
                    "aweme_id": v.aweme_id,
                    "desc": v.desc,
                    "create_time": v.create_time,
                    "author": v.author.nickname,
                    "digg_count": v.stats.digg_count,
                }
                for v in videos
            ],
        }
    return _run(args, _fn)


def cmd_fetch_feed(args) -> int:
    from douyin.feed import fetch_home_feed

    def _fn(page):
        videos = fetch_home_feed(page, count=args.count, refresh_index=args.refresh_index)
        return {
            "success": True,
            "videos": [
                {
                    "aweme_id": v.aweme_id,
                    "desc": v.desc,
                    "create_time": v.create_time,
                    "author": v.author.nickname,
                    "digg_count": v.stats.digg_count,
                }
                for v in videos
            ],
        }
    return _run(args, _fn)


def main() -> None:
    parser = argparse.ArgumentParser(prog="douyin-cli")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=DEFAULT_PORT)
    sub = parser.add_subparsers(dest="command")

    p_check = sub.add_parser("check-login")
    p_check.add_argument("--debug", action="store_true", help="输出调试信息")

    sub.add_parser("login", help="打开浏览器手动登录抖音")

    p_user = sub.add_parser("user-posts")
    p_user.add_argument("--sec-uid", required=True)
    p_user.add_argument("--count", type=int, default=10)

    p_search = sub.add_parser("search-videos")
    p_search.add_argument("--keyword", required=True)
    p_search.add_argument("--count", type=int, default=10)

    p_feed = sub.add_parser("fetch-feed")
    p_feed.add_argument("--count", type=int, default=20)
    p_feed.add_argument("--refresh-index", type=int, default=0)

    args = parser.parse_args()
    if not args.command:
        parser.print_help()
        sys.exit(2)

    logging.basicConfig(level=logging.WARNING)
    dispatch = {
        "check-login": cmd_check_login,
        "login": cmd_login,
        "user-posts": cmd_user_posts,
        "search-videos": cmd_search_videos,
        "fetch-feed": cmd_fetch_feed,
    }
    sys.exit(dispatch[args.command](args))


if __name__ == "__main__":
    main()
