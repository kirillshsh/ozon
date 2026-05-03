#!/usr/bin/env python3
from __future__ import annotations

import argparse
import asyncio
import contextlib
import json
import os
import shutil
import sys
import tempfile
import time
from pathlib import Path
from typing import Any

import nodriver as uc


PLUGIN_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_DATA = PLUGIN_ROOT / "data"
DEFAULT_PROFILE = Path.home() / ".codex" / "marketplaces-browser-profile"

MARKETS = {
    "ozon": {
        "url": "https://www.ozon.ru/",
        "cookie_file": "ozon_cookies.json",
        "domains": ("ozon.ru", "ozone.ru"),
        "login_cookies": {
            "__Secure-access-token",
            "__Secure-refresh-token",
            "__Secure-user-id",
            "ozonIdAuthResponseToken",
        },
    },
    "wb": {
        "url": "https://www.wildberries.ru/",
        "cookie_file": "wb_cookies.json",
        "domains": ("wildberries.ru", "wb.ru"),
        "login_cookies": {
            "wbx-refresh",
            "wbx-access",
            "WILDAUTHNEW_V3",
        },
    },
}


def _browser_candidates() -> list[str]:
    candidates = [
        os.environ.get("MARKETPLACES_BROWSER"),
        os.environ.get("MARKETPLACE_PRODUCTS_BROWSER"),
    ]
    if sys.platform == "darwin":
        candidates.extend(
            [
                "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
                str(
                    Path.home()
                    / "Applications/Google Chrome.app/Contents/MacOS/Google Chrome"
                ),
                "/Applications/Chromium.app/Contents/MacOS/Chromium",
                "/Applications/Brave Browser.app/Contents/MacOS/Brave Browser",
                "/Applications/Microsoft Edge.app/Contents/MacOS/Microsoft Edge",
            ]
        )
    elif sys.platform.startswith("win"):
        for base in (
            os.environ.get("PROGRAMFILES"),
            os.environ.get("PROGRAMFILES(X86)"),
            os.environ.get("LOCALAPPDATA"),
        ):
            if base:
                candidates.extend(
                    [
                        str(Path(base) / "Google/Chrome/Application/chrome.exe"),
                        str(Path(base) / "Microsoft/Edge/Application/msedge.exe"),
                        str(
                            Path(base)
                            / "BraveSoftware/Brave-Browser/Application/brave.exe"
                        ),
                    ]
                )
    candidates.extend(
        filter(
            None,
            [
                shutil.which("google-chrome"),
                shutil.which("google-chrome-stable"),
                shutil.which("chromium"),
                shutil.which("chromium-browser"),
                shutil.which("brave-browser"),
                shutil.which("microsoft-edge"),
                shutil.which("msedge"),
            ],
        )
    )
    out: list[str] = []
    for candidate in candidates:
        if not candidate:
            continue
        cleaned = candidate.strip().strip('"').strip("'")
        if Path(cleaned).exists() and cleaned not in out:
            out.append(cleaned)
    return out


def _serialize_cookie(cookie: Any) -> dict[str, Any]:
    item = {
        "name": cookie.name,
        "value": cookie.value,
        "domain": cookie.domain,
        "path": cookie.path,
    }
    if getattr(cookie, "expires", None) and cookie.expires > 0:
        item["expires"] = cookie.expires
    if getattr(cookie, "secure", False):
        item["secure"] = True
    if getattr(cookie, "http_only", False):
        item["httpOnly"] = True
    same_site = getattr(cookie, "same_site", None)
    if same_site:
        value = str(getattr(same_site, "value", same_site))
        if value and value != "None":
            item["sameSite"] = value
    return item


def _filter_cookies(cookies: list[dict[str, Any]], market: str) -> list[dict[str, Any]]:
    domains = MARKETS[market]["domains"]
    return [
        cookie
        for cookie in cookies
        if any(domain in cookie.get("domain", "") for domain in domains)
    ]


def _is_logged_in(cookies: list[dict[str, Any]], market: str) -> bool:
    names = {cookie.get("name") for cookie in cookies}
    return bool(names & MARKETS[market]["login_cookies"])


def _save_cookie_file(
    market: str,
    cookies: list[dict[str, Any]],
    data_dirs: list[Path],
) -> None:
    file_name = MARKETS[market]["cookie_file"]
    for data_dir in data_dirs:
        data_dir.mkdir(parents=True, exist_ok=True)
        target = data_dir / file_name
        target.write_text(
            json.dumps(cookies, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        print(f"[saved] {market}: {target} ({len(cookies)} cookies)", flush=True)


async def _all_cookies(browser: Any) -> list[dict[str, Any]]:
    raw = await browser.cookies.get_all()
    return [_serialize_cookie(cookie) for cookie in raw]


async def main() -> None:
    parser = argparse.ArgumentParser(description="Login to Ozon/WB and save cookies.")
    parser.add_argument("--source", choices=["all", "ozon", "wb"], default="all")
    parser.add_argument("--source-data", type=Path, default=DEFAULT_DATA)
    parser.add_argument("--cache-data", type=Path, default=None)
    parser.add_argument("--profile", type=Path, default=DEFAULT_PROFILE)
    parser.add_argument("--timeout-minutes", type=float, default=20.0)
    args = parser.parse_args()

    markets = ["ozon", "wb"] if args.source == "all" else [args.source]
    data_dirs = [args.source_data]
    if args.cache_data:
        data_dirs.append(args.cache_data)
    for data_dir in data_dirs:
        data_dir.mkdir(parents=True, exist_ok=True)
    args.profile.mkdir(parents=True, exist_ok=True)

    browser_paths = _browser_candidates()
    browser_path = browser_paths[0] if browser_paths else None
    if browser_path:
        print(f"[browser] using {browser_path}", flush=True)
    else:
        print("[browser] no explicit browser found; nodriver will auto-detect", flush=True)

    browser_kwargs = {
        "headless": False,
        "sandbox": False,
        "lang": "ru-RU",
        "user_data_dir": str(args.profile),
        "browser_args": [
            "--no-first-run",
            "--no-default-browser-check",
            "--disable-dev-shm-usage",
            "--lang=ru-RU",
        ],
    }
    if browser_path:
        browser_kwargs["browser_executable_path"] = browser_path

    try:
        browser = await uc.start(**browser_kwargs)
    except Exception as exc:
        temp_profile = Path(tempfile.mkdtemp(prefix="marketplaces-login-"))
        print(
            f"[browser] profile {args.profile} failed ({exc}); retrying with {temp_profile}",
            flush=True,
        )
        browser_kwargs["user_data_dir"] = str(temp_profile)
        browser = await uc.start(**browser_kwargs)
    tabs: dict[str, Any] = {}
    done: set[str] = set()
    try:
        for index, market in enumerate(markets):
            tab = await browser.get(
                MARKETS[market]["url"],
                new_window=index > 0,
            )
            tabs[market] = tab
            print(
                f"[login] {market}: opened {MARKETS[market]['url']}",
                flush=True,
            )
            await tab.sleep(1)

        print("", flush=True)
        names = " and ".join("Ozon" if market == "ozon" else "Wildberries" for market in markets)
        print(f"Log in to {names} in the opened browser window(s).", flush=True)
        print("Each window closes automatically after login cookies appear.", flush=True)
        print("The installer continues after both marketplaces are logged in.", flush=True)
        print("", flush=True)

        deadline = time.time() + args.timeout_minutes * 60
        last_hint = 0.0
        while len(done) < len(markets):
            if time.time() > deadline:
                missing = ", ".join(market for market in markets if market not in done)
                raise TimeoutError(f"login timeout for: {missing}")

            serialized = await _all_cookies(browser)
            for market in markets:
                if market in done:
                    continue
                market_cookies = _filter_cookies(serialized, market)
                if not _is_logged_in(market_cookies, market):
                    continue
                _save_cookie_file(market, market_cookies, data_dirs)
                done.add(market)
                print(f"[login] {market}: login detected, closing window", flush=True)
                with contextlib.suppress(Exception):
                    await tabs[market].close()

            now = time.time()
            if now - last_hint > 15:
                waiting = ", ".join(market for market in markets if market not in done)
                if waiting:
                    print(f"[login] waiting for: {waiting}", flush=True)
                last_hint = now
            await asyncio.sleep(2)
    finally:
        browser.stop()

    print("[done] marketplace cookies saved", flush=True)


if __name__ == "__main__":
    asyncio.run(main())
