#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Export cookies from a fresh Microsoft Edge profile after the user signs in."""

import json
import sys
import time
from pathlib import Path

from playwright.sync_api import sync_playwright


def is_douyin_cookie(cookie):
    domain = str(cookie.get("domain") or "").lower().lstrip(".")
    return domain == "douyin.com" or domain.endswith(".douyin.com")


def has_session(cookies):
    return any(
        cookie.get("name") in ("sessionid", "sessionid_ss") and cookie.get("value")
        for cookie in cookies
    )


def output_path():
    if getattr(sys, "frozen", False):
        folder = Path(sys.executable).resolve().parent
    else:
        folder = Path(__file__).resolve().parent
    return folder / "douyin_cookies.json"


def main():
    output = output_path()
    if output.exists():
        answer = input(f"已有 {output.name}，要覆盖吗？输入 y 确认：").strip().lower()
        if answer != "y":
            print("已取消，没有修改原文件。")
            return 1

    with sync_playwright() as playwright:
        browser = None
        context = None
        try:
            browser = playwright.chromium.launch(channel="msedge", headless=False)
            context = browser.new_context()
            page = context.new_page()
            page.goto("https://www.douyin.com/", wait_until="domcontentloaded", timeout=60000)
            print("请在弹出的抖音窗口中登录自己的账号；检测到登录后会自动导出。")
            deadline = time.monotonic() + 300
            cookies = []
            while time.monotonic() < deadline and not page.is_closed():
                cookies = [cookie for cookie in context.cookies() if is_douyin_cookie(cookie)]
                if has_session(cookies):
                    break
                time.sleep(2)

            if not has_session(cookies):
                print("5 分钟内没有检测到登录状态，未导出 Cookie。")
                return 3

            output.write_text(json.dumps(cookies, ensure_ascii=False, indent=2), encoding="utf-8")
            print(f"已保存 {len(cookies)} 项 Cookie：{output}")
            print("请回到网站的「手动输入 Cookie」页签导入，导入后删除这个 JSON 文件。")
            return 0
        except Exception as error:
            print("无法启动 Microsoft Edge。请确认 Edge 已安装并更新后重试。")
            print(f"错误：{error}")
            return 2
        finally:
            if context is not None:
                try:
                    context.close()
                except Exception:
                    pass
            if browser is not None:
                try:
                    browser.close()
                except Exception:
                    pass


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except KeyboardInterrupt:
        print("已取消，没有导出 Cookie。")
        raise SystemExit(130)
