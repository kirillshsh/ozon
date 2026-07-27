#!/usr/bin/env python3
"""Transport layer for the Ozon MCP server.

Everything network-facing lives here: the saved-cookie jar, the curl_cffi
session that impersonates the same Chrome the cookies were captured in, the
composer-api JSON fetcher with retries/backoff/cache, and image downloading.

Design notes (learned from the anti-bot behaviour of ozon.ru):
  * requests go to /api/composer-api.bx/page/json/v2?url=<page path> — the same
    endpoint the SPA uses, so the JSON is exactly what the page renders;
  * requests are serialised (never parallel) and spaced by a small random
    delay — parallel bursts reliably trigger 403;
  * every response's Set-Cookie is merged back into the jar and persisted, so
    rolling anti-bot/session cookies stay fresh without a browser round trip;
  * a 403 with a challengeURL means the session is dead: the caller must run the
    visible browser login again. We never try to solve the challenge ourselves.
"""
from __future__ import annotations

import asyncio
import contextlib
import io
import json
import os
import random
import re
import time
from pathlib import Path
from typing import Any
from urllib.parse import quote, urlparse

PLUGIN_ROOT = Path(__file__).resolve().parents[1]
# The session belongs to the machine's user, not to this copy of the plugin, so it
# lives in the home directory and survives reinstalls and upgrades.
DATA_DIR = Path(os.environ.get("OZON_HOME") or (Path.home() / ".ozon"))
COOKIES_FILE = Path(os.environ.get("OZON_COOKIES_FILE") or (DATA_DIR / "cookies.json"))
_LEGACY_COOKIES = PLUGIN_ROOT / "data" / "ozon_cookies.json"
if not COOKIES_FILE.exists() and _LEGACY_COOKIES.exists():
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    COOKIES_FILE.write_bytes(_LEGACY_COOKIES.read_bytes())

BASE = "https://www.ozon.ru"
COMPOSER = BASE + "/api/composer-api.bx/page/json/v2?url="
ENTRYPOINT = BASE + "/api/entrypoint-api.bx/page/json/v2?url="

# Chrome build the login profile runs; curl_cffi impersonates the same one.
CHROME_MAJOR = "136"
IMPERSONATE = f"chrome{CHROME_MAJOR}"
USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    f"(KHTML, like Gecko) Chrome/{CHROME_MAJOR}.0.0.0 Safari/537.36"
)

# Ozon hard-blocks non-Russian datacentre IPs before it even looks at cookies, so
# running this anywhere outside Russia needs a Russian exit. Value is a proxy URL:
# http://user:pass@host:port, or socks5://host:port.
PROXY = (os.environ.get("OZON_PROXY") or "").strip()

CACHE_TTL = float(os.environ.get("OZON_CACHE_TTL") or 180)
MIN_GAP = 0.35  # seconds between two requests
MAX_GAP = 0.9
RETRIES = 3

# Cookies worth writing back when Ozon rotates them mid-session.
SESSION_COOKIES = {"abt_data", "abt_att", "xcid", "rfuid", "SSRT", "sess_id", "guest"}

CHALLENGE_MARKERS = (
    "challenge-data",
    "AntiBot Challenge",
    "runChallenge",
    "/abt/result",
    "challengeURL",
    "доступ ограничен",
)


class OzonChallenge(RuntimeError):
    """Session is dead — only a visible browser login can fix it."""


class OzonBlocked(RuntimeError):
    """Ozon refused this IP outright. Re-logging in cannot help; the address has to change.

    Ozon's two 403s look alike but mean opposite things. A solvable one carries
    `challengeURL` (incident id `fab_chlg_*`); a hard block carries `blockURL`
    plus `timeoutSec` (incident id `fab_nmk_*`) and is what a non-Russian
    datacentre IP gets before any cookie is even looked at.
    """


class OzonHTTPError(RuntimeError):
    pass


# ── cookies ──────────────────────────────────────────────────────────────────


def load_cookie_list() -> list[dict[str, Any]]:
    try:
        data = json.loads(COOKIES_FILE.read_text(encoding="utf-8"))
    except Exception:
        return []
    return data if isinstance(data, list) else []


def save_cookie_list(cookies: list[dict[str, Any]]) -> None:
    COOKIES_FILE.parent.mkdir(parents=True, exist_ok=True)
    tmp = COOKIES_FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps(cookies, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(tmp, COOKIES_FILE)
    with contextlib.suppress(Exception):
        COOKIES_FILE.chmod(0o600)


def cookies_to_dict(cookies: list[dict[str, Any]]) -> dict[str, str]:
    """Flatten to name->value. On duplicates prefer the .ozon.ru copy."""
    out: dict[str, str] = {}
    for cookie in cookies:
        name = cookie.get("name")
        if not name:
            continue
        if name in out and ".ozone." in cookie.get("domain", ""):
            continue
        out[name] = cookie.get("value", "")
    return out


def cookie_names() -> list[str]:
    return sorted({str(cookie.get("name") or "") for cookie in load_cookie_list()} - {""})


def cookies_mtime() -> float:
    try:
        return COOKIES_FILE.stat().st_mtime
    except OSError:
        return 0.0


def _proxy_kwargs() -> dict[str, str]:
    return {"proxy": PROXY} if PROXY else {}


class Session:
    """One shared cookie jar + serialised, throttled access to ozon.ru."""

    def __init__(self) -> None:
        self._jar: dict[str, str] = {}
        self._jar_mtime = 0.0
        self._lock = asyncio.Lock()
        self._last_request = 0.0
        self._cache: dict[str, tuple[float, Any]] = {}

    # -- jar -----------------------------------------------------------------
    def jar(self) -> dict[str, str]:
        mtime = cookies_mtime()
        if not self._jar or mtime > self._jar_mtime:
            self._jar = cookies_to_dict(load_cookie_list())
            self._jar_mtime = mtime
        return dict(self._jar)

    def _persist(self, updates: dict[str, str]) -> None:
        """Merge fresh Set-Cookie values back into data/ozon_cookies.json.

        Only session-bearing cookies are kept: ones already in the jar plus the
        __Secure-* family and Ozon's own anti-bot names. Ozon also sets throwaway
        cookies (devtools flags, banners) that would only bloat the file.
        """
        updates = {
            key: value
            for key, value in updates.items()
            if value
            and self._jar.get(key) != value
            and (key in self._jar or key.startswith("__Secure-") or key in SESSION_COOKIES)
        }
        if not updates:
            return
        self._jar.update(updates)
        stored = load_cookie_list()
        seen = set()
        for cookie in stored:
            name = cookie.get("name")
            if name in updates and name not in seen:
                cookie["value"] = updates[name]
                seen.add(name)
        for name, value in updates.items():
            if name not in seen:
                stored.append({"name": name, "value": value, "domain": ".ozon.ru", "path": "/"})
        save_cookie_list(stored)
        self._jar_mtime = cookies_mtime()

    def _harvest(self, response: Any) -> None:
        with contextlib.suppress(Exception):
            self._persist({str(k): str(v) for k, v in dict(response.cookies).items()})

    # -- cache ---------------------------------------------------------------
    def cache_get(self, key: str) -> Any:
        hit = self._cache.get(key)
        if hit and time.monotonic() - hit[0] < CACHE_TTL:
            return hit[1]
        self._cache.pop(key, None)
        return None

    def cache_put(self, key: str, value: Any) -> None:
        self._cache[key] = (time.monotonic(), value)
        if len(self._cache) > 256:
            oldest = sorted(self._cache.items(), key=lambda kv: kv[1][0])[:64]
            for key, _ in oldest:
                self._cache.pop(key, None)

    def cache_clear(self) -> None:
        self._cache.clear()

    # -- http ----------------------------------------------------------------
    def _headers(self, referer: str, *, json_api: bool) -> dict[str, str]:
        headers = {
            "User-Agent": USER_AGENT,
            "Accept-Language": "ru-RU,ru;q=0.9,en-US;q=0.8,en;q=0.7",
            "Sec-Ch-Ua": (
                f'"Chromium";v="{CHROME_MAJOR}", "Google Chrome";v="{CHROME_MAJOR}", '
                '"Not.A/Brand";v="99"'
            ),
            "Sec-Ch-Ua-Mobile": "?0",
            "Sec-Ch-Ua-Platform": '"macOS"',
            "Referer": referer or BASE + "/",
        }
        if json_api:
            headers.update(
                {
                    "Accept": "application/json,text/plain,*/*",
                    "Sec-Fetch-Dest": "empty",
                    "Sec-Fetch-Mode": "cors",
                    "Sec-Fetch-Site": "same-origin",
                    "x-o3-app-name": "dweb_client",
                    "x-o3-app-version": "release_3-2-2026",
                }
            )
        else:
            headers.update(
                {
                    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
                    "Sec-Fetch-Dest": "document",
                    "Sec-Fetch-Mode": "navigate",
                    "Sec-Fetch-Site": "same-origin" if referer else "none",
                    "Upgrade-Insecure-Requests": "1",
                }
            )
        return headers

    async def _throttle(self) -> None:
        gap = random.uniform(MIN_GAP, MAX_GAP)
        wait = self._last_request + gap - time.monotonic()
        if wait > 0:
            await asyncio.sleep(wait)
        self._last_request = time.monotonic()

    async def raw_get(self, url: str, *, referer: str = "", json_api: bool = True) -> Any:
        """One throttled GET. Serialised: Ozon 403s on parallel card fetches."""
        from curl_cffi.requests import AsyncSession

        async with self._lock:
            await self._throttle()
            async with AsyncSession(impersonate=IMPERSONATE, **_proxy_kwargs()) as client:
                response = await client.get(
                    url,
                    headers=self._headers(referer, json_api=json_api),
                    cookies=self.jar(),
                    allow_redirects=True,
                    timeout=40,
                )
            self._harvest(response)
            return response

    async def page_json(
        self,
        path: str,
        *,
        referer: str = "",
        cache: bool = True,
        entrypoint: bool = False,
    ) -> dict[str, Any]:
        """Fetch one storefront page as the SPA does and return its JSON."""
        path = path if path.startswith("/") else "/" + path
        key = ("e:" if entrypoint else "c:") + path
        if cache:
            hit = self.cache_get(key)
            if hit is not None:
                return hit
        url = (ENTRYPOINT if entrypoint else COMPOSER) + quote(path, safe="")
        referer = referer or BASE + path.split("?")[0]

        last: Exception | None = None
        for attempt in range(RETRIES):
            try:
                response = await self.raw_get(url, referer=referer, json_api=True)
            except Exception as exc:  # network/TLS hiccup
                last = exc
                await asyncio.sleep(2**attempt + random.random())
                continue
            body = response.text or ""
            if response.status_code == 200:
                try:
                    data = response.json()
                except Exception as exc:
                    last = exc
                    await asyncio.sleep(2**attempt + random.random())
                    continue
                if cache:
                    self.cache_put(key, data)
                return data
            if response.status_code in {401, 403} or is_challenge(body):
                if is_ip_block(body):
                    raise OzonBlocked(
                        "Ozon blocked this IP address outright (HTTP "
                        f"{response.status_code}, incident {_incident(body)}). This is not a "
                        "session problem — logging in again will not help. Route the "
                        "requests through a Russian IP: set OZON_PROXY to a proxy URL "
                        "(http://user:pass@host:port or socks5://…) and retry."
                    )
                raise OzonChallenge(
                    "Ozon session is not valid anymore (HTTP "
                    f"{response.status_code}). Run ozon_refresh_cookies to log in "
                    "again in a visible browser."
                )
            if response.status_code in {404, 410}:
                raise OzonHTTPError(f"Ozon returned HTTP {response.status_code} for {path}")
            last = OzonHTTPError(f"Ozon returned HTTP {response.status_code}: {body[:200]}")
            await asyncio.sleep(2**attempt + random.random())
        raise OzonHTTPError(str(last) if last else f"Ozon request failed: {path}")


SESSION = Session()


def is_challenge(text: str) -> bool:
    head = (text or "")[:8000]
    return any(marker in head for marker in CHALLENGE_MARKERS)


def is_ip_block(text: str) -> bool:
    """A hard IP ban rather than a solvable challenge — see OzonBlocked."""
    head = (text or "")[:8000]
    if "challengeURL" in head:
        return False
    return "blockURL" in head or "fab_nmk_" in head


def _incident(text: str) -> str:
    match = re.search(r'"incidentId":\s*"([^"]+)"', text or "")
    return match.group(1) if match else "unknown"


# ── images ───────────────────────────────────────────────────────────────────


def abs_url(url: str) -> str:
    url = (url or "").strip()
    if not url:
        return ""
    if url.startswith("//"):
        return "https:" + url
    if url.startswith("/"):
        return BASE + url
    return url


def _fmt(url: str, content_type: str) -> str:
    ctype = (content_type or "").split(";")[0].strip().lower()
    if ctype.startswith("image/"):
        fmt = ctype.split("/", 1)[1]
        return "jpeg" if fmt == "jpg" else fmt
    suffix = Path(urlparse(url).path).suffix.lower().lstrip(".")
    return {"jpg": "jpeg"}.get(suffix, suffix or "jpeg")


async def fetch_image(url: str, *, max_side: int = 800, max_bytes: int = 8_000_000) -> tuple[bytes, str] | None:
    """Download one image and downscale it so MCP output stays small."""
    url = abs_url(url)
    if not url:
        return None
    try:
        from curl_cffi.requests import AsyncSession

        async with AsyncSession(impersonate=IMPERSONATE, **_proxy_kwargs()) as client:
            response = await client.get(
                url,
                headers={"User-Agent": USER_AGENT, "Referer": BASE + "/", "Accept": "image/*,*/*"},
                timeout=30,
            )
        if response.status_code != 200 or len(response.content) > max_bytes:
            return None
        data, fmt = response.content, _fmt(url, response.headers.get("content-type", ""))
        try:
            from PIL import Image as PILImage

            with PILImage.open(io.BytesIO(data)) as img:
                img.thumbnail((max_side, max_side))
                if img.mode not in {"RGB", "L"}:
                    img = img.convert("RGB")
                buf = io.BytesIO()
                img.save(buf, format="JPEG", quality=82)
                return buf.getvalue(), "jpeg"
        except Exception:
            return (data, fmt) if fmt in {"png", "jpeg", "gif", "webp"} else None
    except Exception:
        return None


# ── misc helpers ─────────────────────────────────────────────────────────────


def product_id_from_url(url: str) -> str:
    match = re.search(r"/product/(?:[^/?#]*-)?(\d+)(?:[/?#]|$)", url or "")
    return match.group(1) if match else ""


def normalize_product_url(url: str = "", product_id: str = "") -> str:
    raw = (url or "").strip()
    if raw:
        if raw.startswith("/"):
            raw = BASE + raw
        parsed = urlparse(raw)
        if "ozon.ru" not in parsed.netloc:
            raise ValueError(f"Not an Ozon URL: {url}")
        path = parsed.path if parsed.path.endswith("/") else parsed.path + "/"
        return f"https://www.ozon.ru{path}"
    pid = (product_id or "").strip()
    if not pid:
        raise ValueError("Pass an Ozon product url or product_id.")
    return f"{BASE}/product/{pid}/"


def path_of(url: str) -> str:
    parsed = urlparse(url)
    path = parsed.path or "/"
    return path + (("?" + parsed.query) if parsed.query else "")
