#!/usr/bin/env python3
"""Open a real Chrome window on ozon.ru so the user can log in by hand.

We never solve the anti-bot challenge programmatically: the window stays open,
the user signs in and clears whatever check Ozon shows, and as soon as the
account cookies appear they are written to ~/.ozon/cookies.json and the
window closes.
"""
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

with contextlib.suppress(ImportError):
    import fcntl

PLUGIN_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_DATA = Path(os.environ.get("OZON_HOME") or (Path.home() / ".ozon"))
DEFAULT_PROFILE = Path.home() / ".ozon" / "browser-profile"

LOGIN_URL = "https://www.ozon.ru/my/main"
COOKIE_DOMAINS = ("ozon.ru", "ozone.ru")
LOGIN_COOKIES = {
    "__Secure-access-token",
    "__Secure-refresh-token",
    "__Secure-user-id",
    "ozonIdAuthResponseToken",
}
BLOCKED_MARKERS = (
    "antibot",
    "anti-bot",
    "challenge-data",
    "runchallenge",
    "доступ ограничен",
    "/abt/result",
    "profilemenuanonymous",
    "войти или зарегистрироваться",
    "войдите, чтобы делать покупки",
)

_profile_lock_handle: Any | None = None


def _patch_cookie_model() -> None:
    """Let a cookie without `sameParty` parse — Chrome stopped sending that field.

    nodriver's generated CDP model still reads it as required, so every cookie
    read raises KeyError deep inside nodriver's background listener. That kills
    the CDP channel: the read may or may not have won the race, but nothing after
    it ever completes, so the browser window sits on screen forever and the login
    looks like it failed even when the cookies were already saved.
    """
    from nodriver.cdp import network

    original = network.Cookie.from_json
    if getattr(original, "_ozon_patched", False):
        return

    def from_json(json: dict[str, Any]) -> Any:
        return original({"sameParty": False, **json})

    from_json._ozon_patched = True  # type: ignore[attr-defined]
    network.Cookie.from_json = staticmethod(from_json)


def browser_candidates() -> list[str]:
    candidates = [os.environ.get("OZON_BROWSER") or os.environ.get("MARKETPLACES_BROWSER")]
    if sys.platform == "darwin":
        candidates += [
            "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
            str(Path.home() / "Applications/Google Chrome.app/Contents/MacOS/Google Chrome"),
            "/Applications/Chromium.app/Contents/MacOS/Chromium",
            "/Applications/Brave Browser.app/Contents/MacOS/Brave Browser",
            "/Applications/Microsoft Edge.app/Contents/MacOS/Microsoft Edge",
        ]
    elif sys.platform.startswith("win"):
        for base in (
            os.environ.get("PROGRAMFILES"),
            os.environ.get("PROGRAMFILES(X86)"),
            os.environ.get("LOCALAPPDATA"),
        ):
            if base:
                candidates += [
                    str(Path(base) / "Google/Chrome/Application/chrome.exe"),
                    str(Path(base) / "Microsoft/Edge/Application/msedge.exe"),
                    str(Path(base) / "BraveSoftware/Brave-Browser/Application/brave.exe"),
                ]
    candidates += [
        shutil.which(name)
        for name in (
            "google-chrome",
            "google-chrome-stable",
            "chromium",
            "chromium-browser",
            "brave-browser",
            "microsoft-edge",
            "msedge",
        )
    ]
    out: list[str] = []
    for candidate in candidates:
        cleaned = (candidate or "").strip().strip('"').strip("'")
        if cleaned and Path(cleaned).exists() and cleaned not in out:
            out.append(cleaned)
    return out


def _acquire_profile_lock(profile: Path, timeout_sec: int = 120) -> None:
    global _profile_lock_handle
    if _profile_lock_handle is not None or "fcntl" not in globals():
        return
    profile.mkdir(parents=True, exist_ok=True)
    lock_path = profile / ".ozon-profile.lock"
    handle = lock_path.open("a+")
    deadline = time.monotonic() + max(1, int(timeout_sec))
    while True:
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            _profile_lock_handle = handle
            return
        except BlockingIOError:
            if time.monotonic() >= deadline:
                handle.close()
                raise TimeoutError(f"Timed out waiting for the login profile lock: {lock_path}")
            time.sleep(0.25)


def _release_profile_lock() -> None:
    global _profile_lock_handle
    handle, _profile_lock_handle = _profile_lock_handle, None
    if handle is None or "fcntl" not in globals():
        return
    with contextlib.suppress(Exception):
        fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
    with contextlib.suppress(Exception):
        handle.close()


def _serialize(cookie: Any) -> dict[str, Any]:
    get = cookie.get if isinstance(cookie, dict) else (lambda k, d=None: getattr(cookie, k, d))
    item = {
        "name": get("name", ""),
        "value": get("value", ""),
        "domain": get("domain", ""),
        "path": get("path", "/"),
    }
    expires = get("expires", None)
    if expires and expires > 0:
        item["expires"] = expires
    if get("secure", False):
        item["secure"] = True
    if get("http_only", False) or get("httpOnly", False):
        item["httpOnly"] = True
    same_site = get("same_site", None) or get("sameSite", None)
    if same_site:
        value = str(getattr(same_site, "value", same_site))
        if value and value != "None":
            item["sameSite"] = value
    return item


async def _all_cookies(browser: Any) -> list[dict[str, Any]]:
    with contextlib.suppress(Exception):
        raw = await asyncio.wait_for(browser.cookies.get_all(), timeout=5.0)
        cookies = [_serialize(cookie) for cookie in raw]
        if cookies:
            return cookies
    tabs = browser.tabs
    if hasattr(tabs, "__await__"):
        tabs = await tabs
    for tab in tabs:
        with contextlib.suppress(Exception):
            raw = await asyncio.wait_for(tab.send(uc.cdp.storage.get_cookies()), timeout=5.0)
            cookies = [_serialize(cookie) for cookie in raw]
            if cookies:
                return cookies
    return []


def _ozon_cookies(cookies: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [c for c in cookies if any(d in c.get("domain", "") for d in COOKIE_DOMAINS)]


def _logged_in(cookies: list[dict[str, Any]]) -> bool:
    return bool({c.get("name") for c in cookies} & LOGIN_COOKIES)


async def _page_usable(tab: Any) -> bool:
    # Every read is timed out: a window the user closed leaves a CDP call that
    # never answers, and an untimed await there hangs the whole login — silently,
    # with the profile lock still held, so the next attempt cannot open a window
    # either.
    async def read(expression: str) -> str:
        return str(await asyncio.wait_for(tab.evaluate(expression), timeout=10))

    try:
        title = await read("document.title || ''")
        html = await read("document.documentElement.innerHTML.substring(0, 30000)")
        body = await read("document.body?.innerText.substring(0, 10000) || ''")
    except Exception:
        return False
    text = f"{title}\n{html}\n{body}".lower()
    return not any(marker in text for marker in BLOCKED_MARKERS)


def _save(cookies: list[dict[str, Any]], data_dir: Path) -> Path:
    data_dir.mkdir(parents=True, exist_ok=True)
    target = data_dir / "cookies.json"
    target.write_text(json.dumps(cookies, ensure_ascii=False, indent=2), encoding="utf-8")
    with contextlib.suppress(Exception):
        target.chmod(0o600)
    print(f"[saved] {target} ({len(cookies)} cookies)", flush=True)
    return target


async def login(
    *,
    data_dir: Path = DEFAULT_DATA,
    profile: Path = DEFAULT_PROFILE,
    timeout_minutes: float = 15.0,
    headless: bool = False,
) -> Path:
    _patch_cookie_model()
    profile.mkdir(parents=True, exist_ok=True)
    _acquire_profile_lock(profile)
    try:
        paths = browser_candidates()
        kwargs: dict[str, Any] = {
            "headless": headless,
            "sandbox": False,
            "lang": "ru-RU",
            "user_data_dir": str(profile),
            "browser_args": [
                "--no-first-run",
                "--no-default-browser-check",
                "--disable-dev-shm-usage",
                "--lang=ru-RU",
            ],
        }
        if paths:
            kwargs["browser_executable_path"] = paths[0]
            print(f"[browser] using {paths[0]}", flush=True)
        try:
            browser = await uc.start(**kwargs)
        except Exception as exc:
            temp = Path(tempfile.mkdtemp(prefix="ozon-login-"))
            print(f"[browser] profile {profile} failed ({exc}); retrying with {temp}", flush=True)
            kwargs["user_data_dir"] = str(temp)
            browser = await uc.start(**kwargs)

        try:
            tab = await browser.get(LOGIN_URL)
            print(f"[login] opened {LOGIN_URL}", flush=True)
            print("Log in to Ozon in the opened window and pass any anti-bot check.", flush=True)
            print("The window closes by itself once the account cookies appear.", flush=True)
            deadline = time.time() + timeout_minutes * 60
            hinted = 0.0
            while True:
                if browser.stopped:
                    raise RuntimeError(
                        "The login window closed before the Ozon cookies appeared — "
                        "run ozon_refresh_cookies again and leave the window open."
                    )
                if time.time() > deadline:
                    raise TimeoutError("Ozon login timed out.")
                cookies = _ozon_cookies(await _all_cookies(browser))
                if _logged_in(cookies) and await _page_usable(tab):
                    target = _save(cookies, data_dir)
                    # Closing the last tab takes the browser — and the CDP channel —
                    # with it, so the reply to close() may never come. Waiting for it
                    # forever leaves the window on screen with the cookies already
                    # saved, which reads as "login failed" when it in fact succeeded.
                    with contextlib.suppress(Exception):
                        await asyncio.wait_for(tab.close(), timeout=5)
                    return target
                if time.time() - hinted > 15:
                    print("[login] waiting for Ozon login…", flush=True)
                    hinted = time.time()
                await asyncio.sleep(2)
        finally:
            with contextlib.suppress(Exception):
                browser.stop()
    finally:
        _release_profile_lock()


async def main() -> None:
    parser = argparse.ArgumentParser(description="Log in to Ozon and save cookies.")
    parser.add_argument("--data", type=Path, default=DEFAULT_DATA)
    parser.add_argument("--profile", type=Path, default=DEFAULT_PROFILE)
    parser.add_argument("--timeout-minutes", type=float, default=15.0)
    parser.add_argument("--headless", action="store_true")
    args = parser.parse_args()
    await login(
        data_dir=args.data,
        profile=args.profile,
        timeout_minutes=args.timeout_minutes,
        headless=args.headless,
    )
    print("[done] Ozon cookies saved", flush=True)


if __name__ == "__main__":
    asyncio.run(main())
