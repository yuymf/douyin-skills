"""抖音 CDP CLI 入口。退出码: 0=成功, 1=未登录, 2=错误"""
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

    headless = _should_use_headless()
    proc = launch_chrome(port=args.port, headless=headless, user_data_dir=DEFAULT_PROFILE_DIR)
    wait_for_chrome(args.port)
    try:
        _, page = _get_page(args.host, args.port)
        result = fn(page)
        print(json.dumps(result, ensure_ascii=False))
        return 0
    except Exception as e:
        print(json.dumps({"success": False, "error": str(e)}, ensure_ascii=False))
        return 2
    finally:
        if proc:
            proc.terminate()


def cmd_check_login(args) -> int:
    def _fn(page):
        page.navigate("https://www.douyin.com")
        page.wait_for_load()
        is_login = page.evaluate(
            "location.href.includes('/login') || !!document.querySelector('.login-guide')"
        )
        return {"success": True, "logged_in": not is_login}
    return _run(args, _fn)


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


def main() -> None:
    parser = argparse.ArgumentParser(prog="douyin-cli")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=DEFAULT_PORT)
    sub = parser.add_subparsers(dest="command")

    sub.add_parser("check-login")

    p_user = sub.add_parser("user-posts")
    p_user.add_argument("--sec-uid", required=True)
    p_user.add_argument("--count", type=int, default=10)

    p_search = sub.add_parser("search-videos")
    p_search.add_argument("--keyword", required=True)
    p_search.add_argument("--count", type=int, default=10)

    args = parser.parse_args()
    if not args.command:
        parser.print_help()
        sys.exit(2)

    logging.basicConfig(level=logging.WARNING)
    dispatch = {
        "check-login": cmd_check_login,
        "user-posts": cmd_user_posts,
        "search-videos": cmd_search_videos,
    }
    sys.exit(dispatch[args.command](args))


if __name__ == "__main__":
    main()
