#!/usr/bin/env python3
from __future__ import annotations

import asyncio
import contextlib
import html
import io
import json
import os
import re
import sys
from pathlib import Path
from typing import Any
from urllib.parse import quote, urlencode, urlparse, urlunparse

import httpx
from mcp.server.fastmcp import FastMCP
from mcp.server.fastmcp.utilities.types import Image


PLUGIN_ROOT = Path(__file__).resolve().parents[1]
SOURCE_DIR = Path(__file__).resolve().parent / "sources"
DATA_DIR = PLUGIN_ROOT / "data"
DATA_DIR.mkdir(exist_ok=True)
sys.path.insert(0, str(SOURCE_DIR))

import ozonbp as ozon  # noqa: E402
import wb_bp as wb  # noqa: E402


ozon.COOKIES_FILE = str(DATA_DIR / "ozon_cookies.json")
wb.COOKIES_FILE = str(DATA_DIR / "wb_cookies.json")

_ozon_load_cookies = ozon.load_cookies
_ozon_save_cookies = ozon.save_cookies
_wb_load_cookies = wb.load_cookies
_wb_save_cookies = wb.save_cookies


def _load_ozon_cookies(path: str | None = None) -> list[dict] | None:
    return _ozon_load_cookies(path or ozon.COOKIES_FILE)


def _save_ozon_cookies(cookies: list[dict], path: str | None = None) -> None:
    _ozon_save_cookies(cookies, path or ozon.COOKIES_FILE)


def _load_wb_cookies(path: str | None = None) -> list[dict] | None:
    return _wb_load_cookies(path or wb.COOKIES_FILE)


def _save_wb_cookies(cookies: list[dict], path: str | None = None) -> None:
    _wb_save_cookies(cookies, path or wb.COOKIES_FILE)


ozon.load_cookies = _load_ozon_cookies
ozon.save_cookies = _save_ozon_cookies
wb.load_cookies = _load_wb_cookies
wb.save_cookies = _save_wb_cookies

server = FastMCP(
    name="Marketplaces",
    instructions=(
        "Use these tools to search Ozon and Wildberries, inspect product cards "
        "with photos, and fetch reviews. Search is paginated; call again with "
        "the returned next_page to keep browsing."
    ),
    json_response=True,
)

_oz_cookies: dict[str, str] | None = None
_wb_cookies: dict[str, str] | None = None
_oz_lock = asyncio.Lock()
_wb_lock = asyncio.Lock()


def _json_text(payload: dict[str, Any]) -> dict[str, Any]:
    return payload


def _ok_source(source: str) -> str:
    value = (source or "").strip().lower()
    aliases = {
        "wildberries": "wb",
        "wildberry": "wb",
        "вб": "wb",
        "wb": "wb",
        "oz": "ozon",
        "озон": "ozon",
        "ozon": "ozon",
        "all": "all",
        "both": "all",
    }
    if value not in aliases:
        raise ValueError("source must be one of: all, wb, ozon")
    return aliases[value]


def _limit(value: int, *, low: int, high: int) -> int:
    return max(low, min(high, int(value)))


def _dedupe(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    seen: set[str] = set()
    out: list[dict[str, Any]] = []
    for item in items:
        key = "|".join(
            str(item.get(part) or "")
            for part in ("source", "id", "url", "title")
        )
        if not key.strip("|") or key in seen:
            continue
        seen.add(key)
        out.append(item)
    return out


def _as_abs_url(url: str) -> str:
    if not url:
        return ""
    if url.startswith("//"):
        return "https:" + url
    if url.startswith("/"):
        return "https://www.ozon.ru" + url
    return url


def _strip_tags(text: str) -> str:
    text = html.unescape(re.sub(r"<[^>]+>", " ", text or ""))
    return re.sub(r"\s+", " ", text).strip()


def _extract_wb_id(value: str) -> str:
    raw = str(value or "").strip()
    if raw.isdigit():
        return raw
    match = re.search(r"/catalog/(\d+)/", raw)
    return match.group(1) if match else raw


def _extract_ozon_id(url: str) -> str:
    match = re.search(r"/product/(?:[^/?#]*-)?(\d+)(?:[/?#]|$)", url or "")
    return match.group(1) if match else ""


def _normalize_ozon_product_url(url: str = "", product_id: str = "") -> str:
    raw = (url or "").strip()
    if raw:
        if raw.startswith("/"):
            raw = "https://www.ozon.ru" + raw
        parsed = urlparse(raw)
        clean = parsed._replace(query="", fragment="")
        return urlunparse(clean)
    pid = (product_id or "").strip()
    if not pid:
        raise ValueError("Ozon product requires url or product_id.")
    return f"https://www.ozon.ru/product/{pid}/"


def _ozon_reviews_url(product_url: str, page: int) -> str:
    parsed = urlparse(product_url)
    path = parsed.path
    if not path.endswith("/"):
        path += "/"
    if not path.endswith("/reviews/"):
        path += "reviews/"
    query = urlencode({"page": str(max(1, int(page)))})
    return urlunparse(parsed._replace(path=path, query=query, fragment=""))


def _image_format(url: str, content_type: str) -> str:
    ctype = (content_type or "").split(";")[0].strip().lower()
    if ctype.startswith("image/"):
        fmt = ctype.split("/", 1)[1]
        return "jpeg" if fmt == "jpg" else fmt
    suffix = Path(urlparse(url).path).suffix.lower().lstrip(".")
    if suffix in {"png", "jpeg", "jpg", "webp", "gif"}:
        return "jpeg" if suffix == "jpg" else suffix
    return "png"


async def _image_preview(url: str, *, max_bytes: int = 4_000_000) -> Image | None:
    url = _as_abs_url(url)
    if not url:
        return None
    try:
        async with httpx.AsyncClient(
            verify=False,
            timeout=20,
            follow_redirects=True,
            headers={"User-Agent": ozon.USER_AGENT},
        ) as client:
            resp = await client.get(url)
        if resp.status_code != 200 or len(resp.content) > max_bytes:
            return None
        ctype = resp.headers.get("content-type", "")
        if not ctype.startswith("image/") and not urlparse(url).path.lower().endswith(
            (".jpg", ".jpeg", ".png", ".webp", ".gif")
        ):
            return None
        fmt = _image_format(url, ctype)
        data = resp.content
        if fmt not in {"png", "jpeg"}:
            try:
                from PIL import Image as PILImage

                with PILImage.open(io.BytesIO(resp.content)) as img:
                    if img.mode not in {"RGB", "RGBA"}:
                        img = img.convert("RGBA")
                    out = io.BytesIO()
                    img.save(out, format="PNG")
                    data = out.getvalue()
                    fmt = "png"
            except Exception:
                pass
        return Image(data=data, format=fmt)
    except Exception:
        return None


def _image_urls_from_payload(payload: Any, *, limit: int = 10) -> list[str]:
    urls: list[str] = []

    def walk(value: Any) -> None:
        if len(urls) >= limit:
            return
        if isinstance(value, str):
            if value.startswith(("http", "//", "/")) and re.search(
                r"\.(?:jpg|jpeg|png|webp|gif)(?:[?#]|$)",
                value,
                re.I,
            ):
                url = _as_abs_url(value)
                if url not in urls:
                    urls.append(url)
            return
        if isinstance(value, dict):
            for key in ("image", "cover", "preview"):
                if key in value:
                    walk(value[key])
            for key in ("images", "photos", "media"):
                if key in value:
                    walk(value[key])
            for child in value.values():
                walk(child)
        elif isinstance(value, list):
            for item in value:
                walk(item)

    walk(payload)
    return urls[:limit]


async def _image_previews(payload: Any, *, limit: int) -> list[Image]:
    images: list[Image] = []
    for url in _image_urls_from_payload(payload, limit=max(limit * 3, limit)):
        image = await _image_preview(url)
        if image:
            images.append(image)
        if len(images) >= limit:
            break
    return images


def _first_image_from_payload(payload: Any) -> str:
    if isinstance(payload, dict):
        for key in ("image", "cover", "preview"):
            value = payload.get(key)
            if isinstance(value, str) and value:
                return value
        for key in ("images", "photos", "media"):
            value = payload.get(key)
            if isinstance(value, list):
                found = _first_image_from_payload(value)
                if found:
                    return found
        for value in payload.values():
            found = _first_image_from_payload(value)
            if found:
                return found
    elif isinstance(payload, list):
        for item in payload:
            if isinstance(item, str) and item.startswith(("http", "//", "/")):
                return item
            found = _first_image_from_payload(item)
            if found:
                return found
    return ""


async def _ensure_ozon(*, force_browser: bool = False) -> dict[str, str]:
    global _oz_cookies
    async with _oz_lock:
        if _oz_cookies and not force_browser:
            return _oz_cookies
        if force_browser:
            await _run_browser_login("ozon")
        saved = ozon.load_cookies()
        if not saved:
            raise RuntimeError(
                "Ozon cookies are missing. Run `npx github:kirillshsh/marketplaces` "
                "or call marketplace_refresh_cookies(source='ozon')."
            )
        _oz_cookies = ozon.cookies_to_dict(saved)
        return _oz_cookies


async def _ensure_wb(*, force_browser: bool = False) -> dict[str, str]:
    global _wb_cookies
    async with _wb_lock:
        if _wb_cookies and not force_browser:
            return _wb_cookies
        if force_browser:
            await _run_browser_login("wb")
        saved = wb.load_cookies()
        if not saved:
            raise RuntimeError(
                "Wildberries cookies are missing. Run `npx github:kirillshsh/marketplaces` "
                "or call marketplace_refresh_cookies(source='wb')."
            )
        _wb_cookies = wb.cookies_to_dict(saved)
        return _wb_cookies


async def _run_browser_login(source: str) -> None:
    script = PLUGIN_ROOT / "scripts" / "browser_login.py"
    profile = Path.home() / ".codex" / "marketplaces-browser-profile"
    source_data = PLUGIN_ROOT / "data"
    cache_data = PLUGIN_ROOT / "data"
    proc = await asyncio.create_subprocess_exec(
        sys.executable,
        str(script),
        "--source",
        source,
        "--source-data",
        str(source_data),
        "--cache-data",
        str(cache_data),
        "--profile",
        str(profile),
    )
    code = await proc.wait()
    if code != 0:
        raise RuntimeError(f"Browser login failed for {source} with exit code {code}.")


async def _ozon_entrypoint(path: str, cookies: dict[str, str], referer: str = "") -> tuple[list[dict[str, Any]], dict[str, Any]]:
    from curl_cffi.requests import AsyncSession

    api_url = "https://www.ozon.ru/api/entrypoint-api.bx/page/json/v2?url=" + quote(
        path,
        safe="",
    )
    headers = {
        "User-Agent": ozon.USER_AGENT,
        "Accept": "application/json",
        "Accept-Language": "ru-RU,ru;q=0.9,en-US;q=0.8,en;q=0.7",
        "Referer": referer or "https://www.ozon.ru/",
        "Sec-Ch-Ua": '"Not_A Brand";v="8", "Chromium";v="131", "Google Chrome";v="131"',
        "Sec-Ch-Ua-Mobile": "?0",
        "Sec-Ch-Ua-Platform": '"Windows"',
        "Sec-Fetch-Dest": "empty",
        "Sec-Fetch-Mode": "cors",
        "Sec-Fetch-Site": "same-origin",
        "x-o3-app-name": "dweb_client",
        "x-o3-app-version": "release_3-2-2026",
    }
    products: list[dict[str, Any]] = []
    meta: dict[str, Any] = {"entrypoint_url": api_url}
    async with AsyncSession(impersonate="chrome131") as client:
        resp = await client.get(
            api_url,
            headers=headers,
            cookies=cookies,
            allow_redirects=True,
            timeout=30,
        )
    meta["status_code"] = resp.status_code
    if resp.status_code != 200:
        meta["error"] = resp.text[:500]
        return products, meta

    data = resp.json()
    widget_states = data.get("widgetStates", {})
    meta["widgets"] = len(widget_states)
    for value in widget_states.values():
        if not isinstance(value, str):
            continue
        with contextlib.suppress(Exception):
            parsed = json.loads(value)
            if isinstance(parsed, dict):
                items = parsed.get("items", [])
                if any(isinstance(item, dict) and "mainState" in item for item in items):
                    ozon._extract_products_from_state(parsed, products)
    return products, meta


async def _search_ozon_page(query: str, page: int, cookies: dict[str, str]) -> dict[str, Any]:
    params = {"text": query, "from_global": "true"}
    if page > 1:
        params["page"] = str(page)
    search_url = "https://www.ozon.ru/search/?" + urlencode(params)
    resp = await ozon.httpx_get(search_url, cookies)
    if ozon.is_challenge_page(resp.text):
        return {
            "source": "ozon",
            "query": query,
            "page": page,
            "title": "",
            "products": [],
            "error": "cookies_expired",
            "message": "Ozon cookies expired or challenge returned.",
        }

    redirect_url = ""
    redir = re.search(r"location\.replace\([\"'](.+?)[\"']\)", resp.text)
    if redir:
        redirect_url = redir.group(1).replace(r"\u0026", "&").replace(r"\/", "/")
        if not redirect_url.startswith("http"):
            redirect_url = "https://www.ozon.ru" + redirect_url
        if "abt_att=" not in redirect_url:
            redirect_url += ("&" if "?" in redirect_url else "?") + "abt_att=2"
        resp = await ozon.httpx_get(redirect_url, cookies, referer=search_url)

    products = ozon.parse_ozon_products(resp.text)
    entrypoint_meta: dict[str, Any] = {}
    if not products:
        api_url = redirect_url or search_url
        parsed = urlparse(api_url)
        path = parsed.path + (("?" + parsed.query) if parsed.query else "")
        products, entrypoint_meta = await _ozon_entrypoint(path, cookies, api_url)
    if not products:
        path = f"/search/?text={query}&from_global=true&abt_att=1"
        if page > 1:
            path += f"&page={page}"
        products, entrypoint_meta = await _ozon_entrypoint(path, cookies, search_url)

    title_match = re.search(r"<title>(.*?)</title>", resp.text, re.IGNORECASE)
    for product in products:
        product["source"] = "ozon"
        product["image"] = _as_abs_url(product.get("image", ""))
    return {
        "source": "ozon",
        "query": query,
        "page": page,
        "title": title_match.group(1) if title_match else "",
        "products": products,
        "entrypoint": entrypoint_meta,
    }


async def _search_wb_page(query: str, page: int, cookies: dict[str, str]) -> dict[str, Any]:
    products = await wb.search_wb(query, cookies, page=page)
    for product in products:
        product["source"] = "wb"
    return {
        "source": "wb",
        "query": query,
        "page": page,
        "title": f"{query} - Wildberries",
        "products": products,
    }


async def _wb_card(product_id: str, cookies: dict[str, str]) -> dict[str, Any]:
    await wb._load_basket_map()
    pid = int(_extract_wb_id(product_id))
    url = f"{wb._wb_base_url(pid)}/info/ru/card.json"
    async with httpx.AsyncClient(verify=False, follow_redirects=True, timeout=15) as client:
        resp = await client.get(url, headers=wb._wb_headers(), cookies=cookies)
    if resp.status_code != 200:
        return {"_status_code": resp.status_code, "_url": url}
    card = wb.response_json(resp)
    card["_url"] = url
    return card


def _parse_data_state_blocks(html_text: str) -> list[Any]:
    blocks = re.findall(r"data-state='([^']+)'", html_text)
    blocks += re.findall(r'data-state="([^"]+)"', html_text)
    parsed: list[Any] = []
    for block in blocks:
        raw = (
            block.replace("&quot;", '"')
            .replace("&amp;", "&")
            .replace("&lt;", "<")
            .replace("&gt;", ">")
        )
        with contextlib.suppress(Exception):
            parsed.append(json.loads(raw))
    match = re.search(
        r"window\.__NUXT__\.state='(.*?)';window\.__NUXT__\.",
        html_text,
        re.DOTALL,
    )
    if match:
        with contextlib.suppress(Exception):
            parsed.append(json.loads(match.group(1).replace("\\'", "'").replace('\\\\"', '\\"')))
    return parsed


def _collect_image_urls(obj: Any, out: list[str]) -> None:
    if len(out) >= 20:
        return
    if isinstance(obj, str):
        text = _as_abs_url(obj)
        if re.search(r"\.(?:jpg|jpeg|png|webp|gif)(?:[?#]|$)", text, re.I):
            if text not in out:
                out.append(text)
        return
    if isinstance(obj, dict):
        for value in obj.values():
            _collect_image_urls(value, out)
    elif isinstance(obj, list):
        for item in obj:
            _collect_image_urls(item, out)


def _find_first_string(obj: Any, keys: set[str]) -> str:
    if isinstance(obj, dict):
        for key, value in obj.items():
            if str(key).lower() in keys and isinstance(value, str) and value.strip():
                return _strip_tags(value)
        for value in obj.values():
            found = _find_first_string(value, keys)
            if found:
                return found
    elif isinstance(obj, list):
        for item in obj:
            found = _find_first_string(item, keys)
            if found:
                return found
    return ""


def _find_first_number(obj: Any, keys: set[str]) -> float | int | str:
    if isinstance(obj, dict):
        for key, value in obj.items():
            if str(key).lower() in keys and isinstance(value, (int, float, str)):
                text = str(value).strip()
                if re.fullmatch(r"\d+(?:[.,]\d+)?", text):
                    return float(text.replace(",", ".")) if "." in text or "," in text else int(text)
        for value in obj.values():
            found = _find_first_number(value, keys)
            if found != "":
                return found
    elif isinstance(obj, list):
        for item in obj:
            found = _find_first_number(item, keys)
            if found != "":
                return found
    return ""


def _review_candidate(obj: dict[str, Any]) -> dict[str, Any] | None:
    text = _find_first_string(
        obj,
        {
            "text",
            "reviewtext",
            "message",
            "comment",
            "content",
            "description",
            "body",
        },
    )
    pros = _find_first_string(obj, {"advantages", "pros", "positive", "dignity"})
    cons = _find_first_string(obj, {"disadvantages", "cons", "negative", "limitations"})
    rating = _find_first_number(obj, {"rating", "score", "stars", "valuation"})
    if not (text or pros or cons) or rating == "":
        return None
    photos: list[str] = []
    _collect_image_urls(obj, photos)
    return {
        "id": _find_first_string(obj, {"id", "uuid", "reviewid"}),
        "name": _find_first_string(obj, {"author", "authorname", "username", "name"}),
        "rating": rating,
        "date": _find_first_string(obj, {"date", "createdat", "publishedat", "createddate"}),
        "text": text,
        "pros": pros,
        "cons": cons,
        "photos": photos,
    }


def _deep_collect_reviews(obj: Any, out: list[dict[str, Any]], *, limit: int, depth: int = 0) -> None:
    if depth > 14 or len(out) >= limit:
        return
    if isinstance(obj, dict):
        candidate = _review_candidate(obj)
        if candidate:
            key = "|".join(str(candidate.get(k) or "") for k in ("id", "name", "text", "pros", "cons"))
            if key and all(
                key
                != "|".join(str(existing.get(k) or "") for k in ("id", "name", "text", "pros", "cons"))
                for existing in out
            ):
                out.append(candidate)
        for value in obj.values():
            if isinstance(value, str) and len(value) > 50 and value[:1] in "{[":
                with contextlib.suppress(Exception):
                    _deep_collect_reviews(json.loads(value), out, limit=limit, depth=depth + 1)
            _deep_collect_reviews(value, out, limit=limit, depth=depth + 1)
    elif isinstance(obj, list):
        for item in obj:
            _deep_collect_reviews(item, out, limit=limit, depth=depth + 1)


async def _ozon_reviews(product_url: str, cookies: dict[str, str], *, page: int, limit: int) -> dict[str, Any]:
    from curl_cffi.requests import AsyncSession

    reviews_url = _ozon_reviews_url(product_url, page)
    parsed = urlparse(reviews_url)
    path = parsed.path + (("?" + parsed.query) if parsed.query else "")
    api_url = "https://www.ozon.ru/api/entrypoint-api.bx/page/json/v2?url=" + quote(path, safe="")
    headers = {
        "User-Agent": ozon.USER_AGENT,
        "Accept": "application/json",
        "Accept-Language": "ru-RU,ru;q=0.9,en-US;q=0.8,en;q=0.7",
        "Referer": product_url,
        "x-o3-app-name": "dweb_client",
        "x-o3-app-version": "release_3-2-2026",
    }

    reviews: list[dict[str, Any]] = []
    meta: dict[str, Any] = {"reviews_url": reviews_url, "entrypoint_url": api_url}
    with contextlib.suppress(Exception):
        async with AsyncSession(impersonate="chrome131") as client:
            resp = await client.get(api_url, headers=headers, cookies=cookies, allow_redirects=True, timeout=30)
        meta["entrypoint_status"] = resp.status_code
        if resp.status_code == 200:
            data = resp.json()
            meta["entrypoint_widgets"] = len(data.get("widgetStates", {}))
            _deep_collect_reviews(data, reviews, limit=limit)

    if not reviews:
        resp = await ozon.httpx_get(reviews_url, cookies, referer=product_url)
        meta["html_status"] = resp.status_code
        meta["html_challenge"] = ozon.is_challenge_page(resp.text)
        for block in _parse_data_state_blocks(resp.text):
            _deep_collect_reviews(block, reviews, limit=limit)

    return {
        "source": "ozon",
        "url": product_url,
        "reviews_url": reviews_url,
        "page": page,
        "total_returned": len(reviews),
        "reviews": reviews[:limit],
        "meta": meta,
        "note": "Ozon review parsing is best-effort because Ozon changes widget state schemas.",
    }


@server.tool()
async def marketplace_status(check_live: bool = False) -> dict[str, Any]:
    """Return local status for marketplace plugin dependencies and saved cookies."""
    status = {
        "plugin_root": str(PLUGIN_ROOT),
        "source_dir": str(SOURCE_DIR),
        "dependencies": {
            "httpx": True,
            "curl_cffi": False,
            "nodriver": False,
        },
        "cookies": {
            "ozon": {
                "path": ozon.COOKIES_FILE,
                "exists": Path(ozon.COOKIES_FILE).exists(),
            },
            "wb": {
                "path": wb.COOKIES_FILE,
                "exists": Path(wb.COOKIES_FILE).exists(),
            },
        },
        "live": {},
    }
    with contextlib.suppress(Exception):
        import curl_cffi  # noqa: F401

        status["dependencies"]["curl_cffi"] = True
    with contextlib.suppress(Exception):
        import nodriver  # noqa: F401

        status["dependencies"]["nodriver"] = True
    if check_live:
        with contextlib.suppress(Exception):
            saved = ozon.load_cookies()
            status["live"]["ozon"] = bool(saved and await ozon.httpx_check(ozon.cookies_to_dict(saved)))
        with contextlib.suppress(Exception):
            saved = wb.load_cookies()
            status["live"]["wb"] = bool(saved and await wb.httpx_check(wb.cookies_to_dict(saved)))
    return status


@server.tool()
async def marketplace_refresh_cookies(source: str = "all") -> dict[str, Any]:
    """Refresh saved Ozon/WB cookies through the browser challenge flow."""
    source = _ok_source(source)
    result: dict[str, Any] = {"source": source, "refreshed": {}, "errors": {}}
    if source in {"ozon", "all"}:
        try:
            cookies = await _ensure_ozon(force_browser=True)
            result["refreshed"]["ozon"] = len(cookies)
        except Exception as exc:
            result["errors"]["ozon"] = str(exc)
    if source in {"wb", "all"}:
        try:
            cookies = await _ensure_wb(force_browser=True)
            result["refreshed"]["wb"] = len(cookies)
        except Exception as exc:
            result["errors"]["wb"] = str(exc)
    result["ok"] = not result["errors"]
    return result


@server.tool()
async def marketplace_search(
    query: str,
    source: str = "all",
    page: int = 1,
    pages: int = 1,
    limit_per_source: int = 100,
    include_image_preview: bool = True,
    image_preview_limit: int = 1,
    force_refresh_cookies: bool = False,
) -> Any:
    """Search products on Wildberries and/or Ozon. Call again with next_page to keep paging."""
    if not query.strip():
        raise ValueError("query must not be empty.")
    source = _ok_source(source)
    page = _limit(page, low=1, high=100_000)
    pages = _limit(pages, low=1, high=20)
    limit_per_source = _limit(limit_per_source, low=1, high=500)
    image_preview_limit = _limit(image_preview_limit, low=1, high=5)

    products: list[dict[str, Any]] = []
    errors: dict[str, Any] = {}
    page_results: list[dict[str, Any]] = []
    if source in {"ozon", "all"}:
        try:
            cookies = await _ensure_ozon(force_browser=force_refresh_cookies)
            for current_page in range(page, page + pages):
                data = await _search_ozon_page(query, current_page, cookies)
                page_results.append({k: v for k, v in data.items() if k != "products"})
                if data.get("error"):
                    errors["ozon"] = data.get("message") or data.get("error")
                products.extend(data.get("products", []))
        except Exception as exc:
            errors["ozon"] = str(exc)
    if source in {"wb", "all"}:
        try:
            cookies = await _ensure_wb(force_browser=force_refresh_cookies)
            for current_page in range(page, page + pages):
                data = await _search_wb_page(query, current_page, cookies)
                page_results.append({k: v for k, v in data.items() if k != "products"})
                products.extend(data.get("products", []))
        except Exception as exc:
            errors["wb"] = str(exc)

    products = _dedupe(products)[:limit_per_source]
    payload = {
        "ok": bool(products) and not errors,
        "query": query,
        "source": source,
        "page": page,
        "pages": pages,
        "next_page": page + pages,
        "total_returned": len(products),
        "products": products,
        "page_results": page_results,
        "errors": errors,
    }
    images = (
        await _image_previews(products, limit=image_preview_limit)
        if include_image_preview
        else []
    )
    return [_json_text(payload), *images] if images else payload


@server.tool()
async def marketplace_product(
    source: str,
    product_id: str = "",
    url: str = "",
    include_image_preview: bool = True,
    image_preview_limit: int = 4,
    include_raw: bool = False,
    force_refresh_cookies: bool = False,
) -> Any:
    """Fetch product-card details and photos for a WB id/url or an Ozon product URL."""
    source = _ok_source(source)
    if source == "all":
        raise ValueError("source must be wb or ozon for product details.")
    image_preview_limit = _limit(image_preview_limit, low=1, high=10)
    payload: dict[str, Any]
    if source == "wb":
        if not (product_id or url):
            raise ValueError("WB product requires product_id or url.")
        pid = _extract_wb_id(product_id or url)
        cookies = await _ensure_wb(force_browser=force_refresh_cookies)
        variant = await wb.wb_variant_info(pid, cookies)
        details = await wb.wb_product_detail(pid, cookies)
        card = await _wb_card(pid, cookies)
        payload = {
            "ok": True,
            "source": "wb",
            "id": pid,
            "url": f"https://www.wildberries.ru/catalog/{pid}/detail.aspx",
            "imtId": str(card.get("imt_id") or card.get("imtId") or ""),
            "title": variant.get("title") or card.get("imt_name") or "",
            "price": variant.get("price", ""),
            "old_price": variant.get("old_price", ""),
            "discount": variant.get("discount", ""),
            "rating": variant.get("rating", ""),
            "reviews_count": variant.get("reviews", ""),
            "in_stock": variant.get("in_stock", False),
            "color": variant.get("color", ""),
            "description": details.get("description", ""),
            "characteristics": details.get("characteristics", {}),
            "images": details.get("images") or variant.get("images", []),
            "variants": details.get("variants", []),
            "colors": details.get("colors", []),
        }
        if include_raw:
            payload["raw_card"] = card
    else:
        product_url = _normalize_ozon_product_url(url=url, product_id=product_id)
        cookies = await _ensure_ozon(force_browser=force_refresh_cookies)
        product = {
            "url": product_url,
            "title": "",
            "id": product_id or _extract_ozon_id(product_url),
        }
        product = await ozon.fetch_product_details(product, cookies, save_html=False)
        payload = {
            "ok": True,
            "source": "ozon",
            "id": product.get("id") or _extract_ozon_id(product_url),
            "url": product_url,
            "description": product.get("description", ""),
            "characteristics": product.get("characteristics", {}),
            "images": [_as_abs_url(item) for item in product.get("images", [])],
            "note": "Pass this url to marketplace_reviews for Ozon reviews.",
        }
        if include_raw:
            payload["raw_product"] = product

    images = (
        await _image_previews(payload.get("images", payload), limit=image_preview_limit)
        if include_image_preview
        else []
    )
    return [_json_text(payload), *images] if images else payload


@server.tool()
async def marketplace_reviews(
    source: str,
    product_id: str = "",
    url: str = "",
    imt_id: str = "",
    nm_id: str = "",
    page: int = 1,
    limit: int = 30,
    include_review_photo_preview: bool = True,
    review_photo_preview_limit: int = 3,
    force_refresh_cookies: bool = False,
) -> Any:
    """Fetch reviews for WB or Ozon. WB accepts imt_id/nm_id; Ozon accepts product url."""
    source = _ok_source(source)
    if source == "all":
        raise ValueError("source must be wb or ozon for reviews.")
    page = _limit(page, low=1, high=100_000)
    limit = _limit(limit, low=1, high=200)
    review_photo_preview_limit = _limit(review_photo_preview_limit, low=1, high=10)

    if source == "wb":
        pid = _extract_wb_id(product_id or url or nm_id)
        resolved_imt = (imt_id or "").strip()
        if not resolved_imt and pid:
            card = await _wb_card(pid, {})
            if not (card.get("imt_id") or card.get("imtId")):
                cookies = await _ensure_wb(force_browser=force_refresh_cookies)
                card = await _wb_card(pid, cookies)
            resolved_imt = str(card.get("imt_id") or card.get("imtId") or "")
        if not resolved_imt:
            raise ValueError("WB reviews require imt_id, or product_id/url to resolve imt_id.")
        data = await wb.wb_get_reviews(resolved_imt, nm_id or pid)
        reviews = data.get("reviews", [])
        payload = {
            "ok": True,
            "source": "wb",
            "imtId": resolved_imt,
            "nmId": nm_id or pid,
            "page": 1,
            "total_available": data.get("feedbackCount", len(reviews)),
            "valuation": data.get("valuation", ""),
            "distribution": data.get("distribution", {}),
            "total_returned": len(reviews[:limit]),
            "reviews": reviews[:limit],
        }
    else:
        product_url = _normalize_ozon_product_url(url=url, product_id=product_id)
        cookies = await _ensure_ozon(force_browser=force_refresh_cookies)
        payload = await _ozon_reviews(product_url, cookies, page=page, limit=limit)
        payload["ok"] = bool(payload.get("reviews"))

    images = (
        await _image_previews(
            payload.get("reviews", []),
            limit=review_photo_preview_limit,
        )
        if include_review_photo_preview
        else []
    )
    return [_json_text(payload), *images] if images else payload


if __name__ == "__main__":
    os.chdir(PLUGIN_ROOT)
    server.run(transport="stdio")
