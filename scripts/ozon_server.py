#!/usr/bin/env python3
"""Ozon MCP server — a shopper's view of ozon.ru, plus the cart.

Data comes from Ozon's own storefront JSON API (composer-api page/json/v2),
using the cookies of a browser session the user logged in by hand. The only tool
that writes to the account is ozon_cart_add; nothing here places an order,
cancels one, changes favourites or posts reviews.
"""
from __future__ import annotations

import asyncio
import os
import re
import sys
from pathlib import Path
from typing import Any
from urllib.parse import urlencode

PLUGIN_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(Path(__file__).resolve().parent))

from mcp.server.fastmcp import FastMCP  # noqa: E402
from mcp.server.fastmcp.utilities.types import Image  # noqa: E402

import ozon_client as client  # noqa: E402
import ozon_parse as parse  # noqa: E402
from ozon_client import OzonBlocked, OzonChallenge, SESSION  # noqa: E402

DATA_DIR = client.DATA_DIR  # ~/.ozon — cookies and settings belong to the user
LOCATION_FILE = DATA_DIR / "location.json"

server = FastMCP(
    name="ozon",
    instructions=(
        "Access to ozon.ru as the logged-in user: search and catalogue browsing, "
        "full product cards, buyer reviews with photos, product Q&A, own orders, "
        "favourites and cart.\n"
        "Typical flow: ozon_search -> take a product url -> ozon_product / "
        "ozon_reviews / ozon_questions on that url.\n"
        "Everything is read-only except ozon_cart_add, which puts a product in the "
        "user's real cart — ask them before calling it. Nothing here can place or "
        "cancel an order; checkout stays with the user on ozon.ru.\n"
        "Prices: every `price` here is the price WITH an Ozon Card — the number Ozon "
        "headlines and the one its own price filter matches. Crossed-out and no-card "
        "prices are deliberately not reported.\n"
        "Images are never attached unless a tool is explicitly asked for them — use "
        "ozon_review_media or ozon_product(include_images=true).\n"
        "CAPTCHA RULE: if any tool reports session_expired, Ozon is showing a captcha or "
        "asking to log in. Stop the whole Ozon task right there, do not retry and do not "
        "try other Ozon tools. A browser window is opened automatically — tell the user to "
        "look at their screen and pass the check, and wait for them to confirm before "
        "retrying. Only if no window appeared, run ozon_refresh_cookies. If a tool reports "
        "ip_blocked instead, re-logging in is useless — Ozon refuses non-Russian IPs "
        "before it looks at cookies, and the machine needs a Russian exit (OZON_PROXY)."
    ),
    json_response=True,
)


# ── shared helpers ───────────────────────────────────────────────────────────


def _clamp(value: int, low: int, high: int) -> int:
    return max(low, min(high, int(value)))


def _default_city() -> str:
    try:
        import json

        data = json.loads(LOCATION_FILE.read_text(encoding="utf-8"))
        return str(data.get("city") or "") if isinstance(data, dict) else ""
    except Exception:
        return ""


def _save_city(city: str) -> None:
    import json

    DATA_DIR.mkdir(parents=True, exist_ok=True)
    LOCATION_FILE.write_text(
        json.dumps({"city": city}, ensure_ascii=False, indent=2), encoding="utf-8"
    )


def _expired(exc: Exception) -> dict[str, Any]:
    """Turn a refused request into advice that actually fixes it.

    Ozon's two refusals need opposite responses: an expired session needs a new
    login, a blocked IP needs a different exit. Telling the user to re-login from
    a banned address just wastes their time, so keep them apart.
    """
    if isinstance(exc, OzonBlocked):
        return {
            "ok": False,
            "error": "ip_blocked",
            "message": str(exc),
            "next_step": (
                "Do NOT call ozon_refresh_cookies — the session is fine, the address is not. "
                "Ozon blocks non-Russian IPs outright. Tell the user this machine needs a "
                "Russian exit and that OZON_PROXY must point at one."
            ),
            "proxy_configured": bool(client.PROXY),
        }
    opened = _open_login_window()
    return {
        "ok": False,
        "error": "session_expired",
        "message": str(exc),
        "login_window_opened": opened,
        **({"previous_login_error": _login_error} if _login_error else {}),
        "next_step": (
            "STOP all Ozon work and tell the user, in their language, that Ozon is showing a "
            "captcha / asking to log in. A browser window on ozon.ru is already open on their "
            "screen — they must pass the check themselves. Do not retry any Ozon tool until "
            "they say they are done; then retry the tool that failed."
            if opened
            else "A login window is already open — tell the user to pass the captcha there, then retry."
        ),
    }


def _anonymous(data: dict[str, Any], what: str) -> dict[str, Any] | None:
    """Account pages render for logged-out visitors too — with nobody's data in them."""
    if parse.parse_account(data).get("logged_in"):
        return None
    return {
        "ok": False,
        "error": "anonymous_session",
        "message": (
            f"Ozon answered anonymously — {what} need a logged-in session. "
            "Run ozon_refresh_cookies."
        ),
    }


def _open_login_window() -> bool:
    """Pop the login browser at the captcha itself, not one turn later.

    The captcha is the user's job to clear, so the only useful thing the server
    can do is put the window in front of them immediately. Returns False when a
    window is already up — ozon_login.py locks its profile, so a second one would
    just die.
    """
    global _login_task
    if _login_task is not None and not _login_task.done():
        return False
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        return False
    _login_task = loop.create_task(_spawn_login())
    return True


async def _run_login(timeout_minutes: float) -> bool:
    """Run ozon_login.py to completion; True when it saved fresh cookies."""
    global _login_error
    script = Path(__file__).resolve().parent / "ozon_login.py"
    process = await asyncio.create_subprocess_exec(
        sys.executable,
        str(script),
        "--timeout-minutes",
        str(timeout_minutes),
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT,
    )
    stdout, _ = await process.communicate()
    output = (stdout or b"").decode(errors="replace")
    sys.stderr.write(output)
    # A login that died on its own says nothing on screen, so the next refused
    # request is the only place the user hears about it — carry the reason there.
    _login_error = "" if process.returncode == 0 else (output.strip().splitlines() or [""])[-1]
    SESSION.cache_clear()  # fresh cookies land on disk; drop answers from the dead session
    return process.returncode == 0


async def _spawn_login() -> None:
    await _run_login(15)


async def _silent_login() -> bool:
    """Refresh the cookies without asking the user anything.

    The window it opens closes by itself the moment the profile's own login hands
    over new cookies — usually within seconds. Nothing to click, so keep the
    deadline short: past it the profile really is logged out and the long-lived
    window in _expired is what the user needs.
    """
    if _login_task is not None and not _login_task.done():
        return False  # a window is already up; the profile lock is taken
    return await _run_login(1)


_login_task: asyncio.Task[None] | None = None
_login_error: str = ""
SESSION.on_expired = _silent_login


def _mask_proxy(url: str) -> str:
    """Report that a proxy is set without leaking its credentials."""
    if not url:
        return ""
    return re.sub(r"//[^@/]*@", "//***@", url)


def _query(base: str, params: dict[str, Any]) -> str:
    clean = {key: value for key, value in params.items() if str(value or "").strip()}
    return base + ("?" + urlencode(clean) if clean else "")


def _product_path(url: str, suffix: str = "", **params: Any) -> str:
    path = client.path_of(url).split("?")[0]
    if not path.endswith("/"):
        path += "/"
    return _query(path + suffix, params)


async def _images_payload(
    candidates: list[dict[str, Any]],
    *,
    limit: int,
    max_side: int,
) -> tuple[list[dict[str, Any]], list[Image], list[str]]:
    manifest: list[dict[str, Any]] = []
    images: list[Image] = []
    failed: list[str] = []
    seen: set[str] = set()
    for candidate in candidates:
        if len(images) >= limit:
            break
        url = client.abs_url(str(candidate.get("url") or ""))
        if not url or url in seen:
            continue
        seen.add(url)
        fetched = await client.fetch_image(url, max_side=max_side)
        if not fetched:
            failed.append(url)
            continue
        data, fmt = fetched
        images.append(Image(data=data, format=fmt))
        entry = {key: value for key, value in candidate.items() if key != "url"}
        entry["image_index"] = len(images)
        entry["image_url"] = url
        manifest.append(entry)
    return manifest, images, failed


# ── session / status ─────────────────────────────────────────────────────────


@server.tool()
async def ozon_status(check_live: bool = True) -> dict[str, Any]:
    """Check who the Ozon session belongs to and whether it still works.

    Call this first when anything else fails. With check_live=true (default) it
    makes one real request to ozon.ru and reports the logged-in account (name,
    email, user id) plus the delivery city Ozon currently uses. With
    check_live=false it only inspects local files and dependencies — no network.

    A dead session shows up as ok=false with error='session_expired'; the fix is
    ozon_refresh_cookies, which the user has to complete in a browser window."""
    import time

    status: dict[str, Any] = {
        "ok": True,
        "cookies_file": str(client.COOKIES_FILE),
        "cookies_present": client.COOKIES_FILE.exists(),
        "cookie_names": client.cookie_names(),
        "cookies_age_hours": round((time.time() - client.cookies_mtime()) / 3600, 1)
        if client.cookies_mtime()
        else None,
        "default_city": _default_city(),
        "dependencies": {},
        "cache_ttl_seconds": client.CACHE_TTL,
        "proxy": _mask_proxy(client.PROXY),
    }
    for name in ("curl_cffi", "nodriver", "PIL", "httpx"):
        try:
            __import__(name)
            status["dependencies"][name] = True
        except Exception:
            status["dependencies"][name] = False
    if not check_live:
        status["live"] = "not checked (check_live=false)"
        return status
    try:
        data = await SESSION.page_json("/", cache=False)
    except (OzonChallenge, OzonBlocked) as exc:
        return {**status, **_expired(exc)}
    except Exception as exc:
        return {**status, "ok": False, "error": "request_failed", "message": str(exc)}
    account = parse.parse_account(data)
    status["session_alive"] = True
    status["account"] = account
    status["ok"] = bool(account.get("logged_in"))
    if not account.get("logged_in"):
        status["error"] = "anonymous_session"
        status["message"] = (
            "Ozon answered, but the session is anonymous — account tools will not work. "
            "Run ozon_refresh_cookies and log in."
        )
    return status


@server.tool()
async def ozon_refresh_cookies(timeout_minutes: float = 15.0) -> dict[str, Any]:
    """Open a real browser window so the USER can log in to Ozon by hand.

    Use this only when another tool returned error='session_expired' or
    ozon_status reports an anonymous session. It opens a visible Chrome window on
    ozon.ru; the user signs in and passes any anti-bot check themselves — this
    tool never solves a captcha. As soon as the account cookies appear they are
    saved and the window closes. It blocks until then (up to timeout_minutes), so
    tell the user to go look at their screen before calling it."""
    script = Path(__file__).resolve().parent / "ozon_login.py"
    process = await asyncio.create_subprocess_exec(
        sys.executable,
        str(script),
        "--timeout-minutes",
        str(timeout_minutes),
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT,
    )
    stdout, _ = await process.communicate()
    log = (stdout or b"").decode(errors="replace")
    sys.stderr.write(log)
    if process.returncode != 0:
        return {"ok": False, "error": "login_failed", "log": log[-2000:]}
    SESSION.cache_clear()
    return {
        "ok": True,
        "cookie_names": client.cookie_names(),
        "next_step": "Session refreshed. Retry the tool that failed.",
    }


@server.tool()
async def ozon_location(city: str = "", save_default: bool = False) -> dict[str, Any]:
    """Show — or remember — the delivery city that prices and stock refer to.

    Ozon derives the delivery city from the account's own selected address, so
    this tool reports what Ozon currently uses rather than changing it. Passing
    city + save_default=true stores the city locally as a label for later
    answers; to actually deliver elsewhere the user must switch the address in
    their Ozon account. With no arguments it just reports the current state."""
    if save_default and city.strip():
        _save_city(city.strip())
    try:
        data = await SESSION.page_json("/", cache=True)
    except (OzonChallenge, OzonBlocked) as exc:
        return _expired(exc)
    account = parse.parse_account(data)
    return {
        "ok": True,
        "ozon_city": account.get("city", ""),
        "ozon_address": account.get("address", ""),
        "saved_default": _default_city(),
        "note": "Ozon takes the delivery city from the account address; change it on ozon.ru to affect prices.",
    }


# ── search & catalogue ───────────────────────────────────────────────────────


SORT_KEYS = {
    "price": lambda tile: tile.get("price_value") or 0,
    "price_desc": lambda tile: -(tile.get("price_value") or 0),
    "rating": lambda tile: -float(str(tile.get("rating") or 0).replace(",", ".") or 0),
}


def _listing_params(
    sort: str, price_min: int, price_max: int, filters: dict[str, str] | None
) -> tuple[dict[str, Any], str]:
    """Build listing query params.

    Ozon silently drops its own price filter when `sorting` is also present, so
    when both are asked for we keep the filter (correctness) and sort locally.
    The second return value is the sort we still have to apply ourselves.
    """
    params: dict[str, Any] = dict(filters or {})
    local_sort = ""
    if price_min or price_max:
        params["currency_price"] = f"{price_min or 0}.000;{price_max or 9_999_999}.000"
        if sort.strip():
            local_sort = sort.strip()
    elif sort.strip():
        params["sorting"] = sort.strip()
    return params, local_sort


def _apply_local_sort(tiles: list[dict[str, Any]], sort: str) -> tuple[list[dict[str, Any]], str]:
    key = SORT_KEYS.get(sort)
    if not key:
        return tiles, ""
    return sorted(tiles, key=key), (
        f"Ozon ignores its price filter when sorting is requested, so the price filter was applied "
        f"server-side and sort='{sort}' locally over the {len(tiles)} returned products."
    )


async def _collect_tiles(path: str, *, want: int, max_requests: int = 6) -> tuple[list[dict], dict]:
    """Fetch one listing page, then follow its paginator until `want` tiles."""
    tiles: list[dict[str, Any]] = []
    meta: dict[str, Any] = {"requests": 0, "paths": []}
    seen: set[str] = set()
    current = path
    for _ in range(max_requests):
        data = await SESSION.page_json(current)
        meta["requests"] += 1
        meta["paths"].append(current)
        meta.setdefault("filters", parse.parse_filters(data))
        meta.setdefault("sortings", parse.parse_sortings(data))
        meta.setdefault("subcategories", parse.parse_subcategories(data))
        for tile in parse.parse_tiles(data):
            key = tile.get("id") or tile.get("url")
            if key and key not in seen:
                seen.add(key)
                tiles.append(tile)
        if len(tiles) >= want:
            break
        nxt = parse.parse_paginator(data).get("next")
        if not nxt or nxt == current:
            break
        current = nxt
    return tiles[:want], meta


@server.tool()
async def ozon_search(
    query: str,
    page: int = 1,
    limit: int = 36,
    sort: str = "",
    price_min: int = 0,
    price_max: int = 0,
    brand: str = "",
    filters: dict[str, str] | None = None,
) -> dict[str, Any]:
    """Search ozon.ru and return product tiles with prices, rating and links.

    The `url` of every result is what ozon_product / ozon_reviews / ozon_questions
    take as input — pass it through verbatim.

    price on a tile is the price WITH an Ozon Card — what Ozon shows and what its
    own price filter matches. Crossed-out and no-card prices are not reported.

    page: 1-based; each page is a fresh slice from Ozon. limit: how many tiles to
      return (the tool follows Ozon's paginator until it has that many, so
      limit=60 costs about two extra requests).
    sort: '' (relevance), 'price' (cheapest), 'price_desc', 'rating', 'new'.
      Note that Ozon returns only ~8 tiles per request for relevance ordering but
      ~36 for an explicit sort.
    price_min / price_max: rubles, matched against the Ozon Card price. Ozon
      drops its price filter whenever a sort is also requested, so asking for
      both keeps the filter and sorts the returned products locally — the
      response says so in `sort_note`.
    brand: brand filter value.
    filters: any other Ozon filter, as {filter_key: value} — the keys a listing
      supports come back in `available_filters` of this same response."""
    if not query.strip():
        raise ValueError("query must not be empty")
    page = _clamp(page, 1, 100)
    limit = _clamp(limit, 1, 200)
    params, local_sort = _listing_params(sort, price_min, price_max, filters)
    params["text"] = query.strip()
    params["from_global"] = "true"
    if page > 1:
        params["page"] = page
    if brand.strip():
        params["brand"] = brand.strip()

    try:
        tiles, meta = await _collect_tiles(_query("/search/", params), want=limit)
    except (OzonChallenge, OzonBlocked) as exc:
        return _expired(exc)
    tiles, sort_note = _apply_local_sort(tiles, local_sort) if local_sort else (tiles, "")
    return {
        **({"sort_note": sort_note} if sort_note else {}),
        "ok": bool(tiles),
        "query": query,
        "page": page,
        "next_page": page + 1,
        "total_returned": len(tiles),
        "products": tiles,
        "available_filters": meta.get("filters", []),
        "available_sortings": meta.get("sortings", []),
        "price_note": "product.price is the Ozon Card price.",
        "requests_made": meta.get("requests", 0),
    }


@server.tool()
async def ozon_catalog(
    category_url: str = "",
    limit: int = 36,
    page: int = 1,
    sort: str = "",
    price_min: int = 0,
    price_max: int = 0,
    filters: dict[str, str] | None = None,
) -> dict[str, Any]:
    """Browse Ozon's category tree, or list the products inside one category.

    With no arguments it returns Ozon's top-level categories (id, title, url).
    With category_url (an ozon.ru/category/... link from this tool, from a
    product's breadcrumbs, or from `subcategories`) it returns that category's
    products, the filter keys it supports, and `subcategories` — the surrounding
    slice of the tree, where `level` is the depth and `current` marks the
    category being viewed, so you can walk up as well as down.

    Same paging and filter arguments as ozon_search: page, limit, sort
    ('price' | 'price_desc' | 'rating' | 'new'), price_min/price_max in rubles,
    and `filters` for anything listed in available_filters. Combining a price
    range with a sort makes the sort local — see `sort_note` in the response."""
    limit = _clamp(limit, 1, 200)
    page = _clamp(page, 1, 100)
    try:
        if not category_url.strip():
            data = await SESSION.page_json("/")
            return {
                "ok": True,
                "level": "root",
                "categories": parse.parse_catalog_menu(data),
                "next_step": "Call ozon_catalog again with one of these `url` values to open it.",
            }
        params, local_sort = _listing_params(sort, price_min, price_max, filters)
        if page > 1:
            params["page"] = page
        path = _query(client.path_of(category_url).split("?")[0], params)
        tiles, meta = await _collect_tiles(path, want=limit)
    except (OzonChallenge, OzonBlocked) as exc:
        return _expired(exc)
    tiles, sort_note = _apply_local_sort(tiles, local_sort) if local_sort else (tiles, "")
    return {
        **({"sort_note": sort_note} if sort_note else {}),
        "ok": True,
        "level": "category",
        "category_url": client.abs_url(category_url),
        "page": page,
        "next_page": page + 1,
        "subcategories": meta.get("subcategories", []),
        "total_returned": len(tiles),
        "products": tiles,
        "available_filters": meta.get("filters", []),
        "available_sortings": meta.get("sortings", []),
        "price_note": "product.price is the Ozon Card price.",
    }


# ── product card ─────────────────────────────────────────────────────────────


@server.tool()
async def ozon_product(
    url: str = "",
    product_id: str = "",
    include_description: bool = True,
    include_images: bool = False,
    image_limit: int = 4,
) -> Any:
    """Fetch one full product card: every price, spec, variant, photo and the seller.

    Input: a product url from ozon_search (preferred) or a bare numeric
    product_id / SKU.

    Returns price (with an Ozon Card — the only price reported), in_stock and
    stock_left, ALL characteristics, the description and rich-content images,
    brand and breadcrumbs, the seller with their rating and order count, every
    photo and video URL, and the colour/size/volume variants with their own SKU,
    price and url.

    Clothing and shoes additionally return `size_table`: the 'Таблица размеров'
    chart as rows of text (labels plus measurements in cm) and the seller's
    measuring notes, so a size can be matched to real body measurements instead
    of guessed. When the seller uploaded the chart as a picture there is no text
    to read — `size_table.images` holds the URLs, and ozon_review_media
    (image_urls=[...]) will show them. Not every product has a chart at all; the
    key is simply absent then, and `size_note` says what to do with it.

    Which sizes can actually be bought is in `variants`, not in the chart: only a
    variant with availability='inStock' is orderable, and each variant has its own
    SKU to pass to ozon_cart_add.

    include_description=false skips a second request (faster, no specs or text).
    include_images=true additionally attaches the first `image_limit` photos as
    viewable images — leave it off unless the photos must actually be looked at.

    Delivery dates are NOT on the product page: Ozon loads them asynchronously.
    The date shown next to a product in ozon_search results is the real one."""
    try:
        product_url = client.normalize_product_url(url=url, product_id=product_id)
        main = await SESSION.page_json(_product_path(product_url), referer=product_url)
        extra = {}
        if include_description:
            extra = await SESSION.page_json(
                _product_path(product_url, layout_container="pdpPage2column", layout_page_index=2),
                referer=product_url,
            )
        # Only clothing and shoes carry this link, so the extra request is paid for
        # exactly by the products where picking the wrong size is the whole risk.
        size_link = parse.size_table_link(main)
        sizes = await SESSION.page_json(size_link, referer=product_url) if size_link else {}
    except (OzonChallenge, OzonBlocked) as exc:
        return _expired(exc)

    card = parse.parse_product(main, extra)
    size_table = parse.parse_size_table(sizes) if sizes else {}
    if size_table:
        card["size_table"] = size_table
        card["size_note"] = (
            "Sizes come from `variants` (only availability='inStock' can be bought); "
            "`size_table` maps them to body measurements in cm. "
            + (
                "This chart is only a picture — pass size_table.images to "
                "ozon_review_media(image_urls=[...]) to read it, do not guess its numbers."
                if size_table.get("images")
                else "Row 1 of each table is the size labels; the rest are measurements."
            )
        )
    card["url"] = product_url
    card["id"] = card.get("id") or client.product_id_from_url(product_url)
    card["ok"] = bool(card.get("title"))
    card["city"] = parse.parse_account(main).get("city", "")
    card["next_steps"] = {
        "reviews": f"ozon_reviews(url='{product_url}')",
        "questions": f"ozon_questions(url='{product_url}')",
    }
    if not include_images:
        return card

    manifest, images, failed = await _images_payload(
        [{"kind": "product_photo", "url": u} for u in card.get("images", [])],
        limit=_clamp(image_limit, 1, 12),
        max_side=800,
    )
    card["image_manifest"] = manifest
    if failed:
        card["images_failed"] = failed
    return [card, *images] if images else card


# ── reviews ──────────────────────────────────────────────────────────────────


def _matches(review: dict[str, Any], *, min_rating: int, max_rating: int, media: str) -> bool:
    rating = review.get("rating")
    if isinstance(rating, (int, float)):
        if min_rating and rating < min_rating:
            return False
        if max_rating and rating > max_rating:
            return False
    elif min_rating or max_rating:
        return False
    if media == "photo" and not review.get("photos"):
        return False
    if media == "video" and not review.get("videos"):
        return False
    if media == "any" and not (review.get("photos") or review.get("videos")):
        return False
    return True


@server.tool()
async def ozon_reviews(
    url: str = "",
    product_id: str = "",
    page: int = 1,
    limit: int = 20,
    sort: str = "published_at_desc",
    min_rating: int = 0,
    max_rating: int = 0,
    media: str = "",
    max_pages: int = 4,
) -> dict[str, Any]:
    """Fetch buyer reviews for one product, with rating and media filters.

    Every review comes back with its text, pros (`pros`) and cons (`cons`), the
    1-5 rating, date, author, useful-vote count and the URLs of its photos and
    videos. It does NOT download the photos — feed those URLs (or the review id)
    to ozon_review_media to actually see them.

    `pros`/`cons` are empty on most modern reviews: Ozon's current review form is
    one free-text field and only older reviews have the split. `replies_count`
    says how many replies a review got, but Ozon loads their text separately, so
    `seller_answers` is usually empty even when the count is not — open the
    review `url` to read them. Seller responses to *questions* are complete:
    use ozon_questions for those.

    url / product_id: the product, same values ozon_product takes.
    page: 1-based, 30 reviews per Ozon page. limit: how many to return.
    sort: 'published_at_desc' (newest, default), 'score_desc' (best first),
      'score_asc' (worst first).
    min_rating / max_rating: keep only reviews in that star range (1-5).
    media: '' (all), 'photo' (only reviews with photos), 'video', 'any'.

    Ozon has no server-side media or star filter, so filtering scans up to
    max_pages upstream pages and reports how many it read in `pages_scanned` —
    raise max_pages if a rare filter comes back short."""
    page = _clamp(page, 1, 500)
    limit = _clamp(limit, 1, 100)
    max_rating = _clamp(max_rating, 0, 5)
    min_rating = _clamp(min_rating, 0, 5)
    media = (media or "").strip().lower()
    if media not in {"", "photo", "video", "any"}:
        raise ValueError("media must be one of: '', 'photo', 'video', 'any'")
    filtering = bool(media or min_rating or max_rating)
    max_pages = _clamp(max_pages, 1, 20) if filtering else 1

    try:
        product_url = client.normalize_product_url(url=url, product_id=product_id)
        collected: list[dict[str, Any]] = []
        summary: dict[str, Any] = {}
        scanned = 0
        for offset in range(max_pages):
            data = await SESSION.page_json(
                _product_path(product_url, "reviews/", page=page + offset, sort=sort),
                referer=product_url,
            )
            block = parse.parse_reviews(data)
            summary = summary or block
            scanned += 1
            batch = block.get("reviews") or []
            if not batch:
                break
            collected += [
                review
                for review in batch
                if _matches(review, min_rating=min_rating, max_rating=max_rating, media=media)
            ]
            if len(collected) >= limit:
                break
    except (OzonChallenge, OzonBlocked) as exc:
        return _expired(exc)

    reviews = collected[:limit]
    return {
        "ok": bool(reviews),
        "url": product_url,
        "page": page,
        "next_page": page + scanned,
        "pages_scanned": scanned,
        "total_reviews_on_product": summary.get("total", 0),
        "product_rating": summary.get("product_score", ""),
        "rating_breakdown": summary.get("rating_breakdown", {}),
        "filters_applied": {
            "min_rating": min_rating or None,
            "max_rating": max_rating or None,
            "media": media or None,
            "sort": sort,
        },
        "total_returned": len(reviews),
        "reviews": reviews,
        "available_sortings": summary.get("sortings", []),
        "next_step": "To look at review photos, call ozon_review_media with this url (and review_id for one review).",
    }


@server.tool()
async def ozon_review_media(
    url: str = "",
    product_id: str = "",
    review_id: str = "",
    image_urls: list[str] | None = None,
    limit: int = 6,
    page: int = 1,
    max_side: int = 800,
    max_pages: int = 3,
) -> Any:
    """Download review photos and return them as actual viewable images.

    Use this when the photos themselves matter — judging real colour, size, build
    quality or whether the item matches its listing. ozon_reviews only returns
    URLs; this fetches them, downscales to max_side px on the long edge, and
    attaches them as image content.

    review_id: photos from that one review only (the `id` from ozon_reviews).
    image_urls: fetch exactly these URLs and skip the review lookup entirely.
    limit: how many images (default 6, hard cap 12) — every image costs context,
      so ask for the fewest that answer the question.

    Each returned image is described in image_manifest in the same order:
    which review it came from, its author, rating and a text snippet."""
    limit = _clamp(limit, 1, 12)
    max_side = _clamp(max_side, 200, 1600)
    candidates: list[dict[str, Any]] = [
        {"kind": "explicit_url", "url": str(item)} for item in (image_urls or []) if str(item).strip()
    ]
    product_url = ""
    if not candidates:
        try:
            product_url = client.normalize_product_url(url=url, product_id=product_id)
            for offset in range(_clamp(max_pages, 1, 10)):
                data = await SESSION.page_json(
                    _product_path(product_url, "reviews/", page=page + offset),
                    referer=product_url,
                )
                reviews = parse.parse_reviews(data).get("reviews") or []
                if not reviews:
                    break
                for review in reviews:
                    if review_id and review.get("id") != review_id:
                        continue
                    for photo in review.get("photos") or []:
                        candidates.append(
                            {
                                "kind": "review_photo",
                                "review_id": review.get("id", ""),
                                "author": review.get("author", ""),
                                "rating": review.get("rating"),
                                "date": review.get("date", ""),
                                "text": (review.get("text") or "")[:160],
                                "url": photo,
                            }
                        )
                if len(candidates) >= limit or review_id and candidates:
                    break
        except (OzonChallenge, OzonBlocked) as exc:
            return _expired(exc)

    if not candidates:
        return {
            "ok": False,
            "error": "no_photos_found",
            "message": "No review photos matched. Try another page, drop review_id, or pass image_urls.",
        }
    manifest, images, failed = await _images_payload(candidates, limit=limit, max_side=max_side)
    payload = {
        "ok": bool(images),
        "url": product_url,
        "review_id": review_id,
        "total_candidates": len(candidates),
        "total_images_returned": len(images),
        "image_manifest": manifest,
        "images_failed": failed,
        "note": "Attached images follow image_manifest order.",
    }
    return [payload, *images] if images else payload


@server.tool()
async def ozon_questions(
    url: str = "",
    product_id: str = "",
    page: int = 1,
    limit: int = 20,
    sort: str = "usefulness_desc",
) -> dict[str, Any]:
    """Fetch the buyer questions asked about a product, with the answers to them.

    Answers usually come from the seller (the author name is the shop's), but
    other buyers answer too — each answer carries its author, date, text and
    useful-vote count, so the seller's word is distinguishable from a stranger's.

    url / product_id: the product, same values ozon_product takes.
    sort: 'usefulness_desc' (default), 'created_at_desc' (newest first),
      'has_answers_desc' (answered questions first)."""
    page = _clamp(page, 1, 200)
    limit = _clamp(limit, 1, 100)
    try:
        product_url = client.normalize_product_url(url=url, product_id=product_id)
        data = await SESSION.page_json(
            _product_path(product_url, "questions/", page=page, qsort=sort),
            referer=product_url,
        )
    except (OzonChallenge, OzonBlocked) as exc:
        return _expired(exc)
    block = parse.parse_questions(data)
    questions = block.get("questions", [])[:limit]
    return {
        "ok": bool(questions),
        "url": product_url,
        "page": page,
        "next_page": page + 1,
        "total_questions": block.get("total", 0),
        "total_returned": len(questions),
        "questions": questions,
        "available_sortings": block.get("sortings", []),
    }


# ── account (read-only) ──────────────────────────────────────────────────────


@server.tool()
async def ozon_orders(status: str = "active", page: int = 1) -> dict[str, Any]:
    """List the user's own Ozon orders with their current status. Read-only.

    status: 'active' (in progress, default) or 'archive' (completed and
    cancelled). Each order returns its number, status text ('Получен 15 января',
    'В пути'), delivery method, item thumbnails with prices, and a link to the
    order page on ozon.ru.

    This tool cannot place, change or cancel anything — ordering stays with the
    user on ozon.ru."""
    tab = "archive" if status.strip().lower().startswith(("arch", "заверш", "выпол")) else "active"
    try:
        data = await SESSION.page_json(
            _query("/my/orderlist", {"selectedTab": tab, "page": page if page > 1 else ""}),
            cache=False,
        )
    except (OzonChallenge, OzonBlocked) as exc:
        return _expired(exc)
    anonymous = _anonymous(data, "orders")
    if anonymous:
        return anonymous
    orders = parse.parse_orders(data)
    return {
        "ok": True,
        "tab": tab,
        "page": page,
        "total_returned": len(orders),
        "orders": orders,
        "empty_state": "" if orders else parse.parse_empty_state(data),
        "account": parse.parse_account(data).get("email", ""),
    }


@server.tool()
async def ozon_favorites(page: int = 1, limit: int = 36) -> dict[str, Any]:
    """List the products in the user's Ozon favourites. Read-only.

    Returns the same tile shape as ozon_search (title, Ozon Card price,
    old price, rating, url), so a favourite can be passed straight to
    ozon_product. Adding or removing favourites is not supported."""
    limit = _clamp(limit, 1, 200)
    try:
        data = await SESSION.page_json(
            _query("/my/favorites", {"page": page if page > 1 else ""}), cache=False
        )
    except (OzonChallenge, OzonBlocked) as exc:
        return _expired(exc)
    anonymous = _anonymous(data, "favourites")
    if anonymous:
        return anonymous
    tiles = parse.parse_tiles(data)[:limit]
    return {
        "ok": True,
        "page": page,
        "total_returned": len(tiles),
        "products": tiles,
        "empty_state": "" if tiles else parse.parse_empty_state(data),
    }


# ── cart ─────────────────────────────────────────────────────────────────────


def _cart_payload(data: dict[str, Any]) -> dict[str, Any]:
    sections = parse.parse_cart(data)
    items = [item for section in sections for item in section["items"]]
    return {
        "ok": True,
        "total_items": len(items),
        "sections": sections,
        "summary": parse.parse_cart_total(data) if items else {},
        "empty_state": "" if items else parse.parse_empty_state(data),
        "checkout_note": "Ordering is not possible from here — the user checks out on ozon.ru.",
    }


@server.tool()
async def ozon_cart() -> dict[str, Any]:
    """Show what is in the user's Ozon cart right now, with the totals. Read-only.

    Items come grouped in `sections` exactly as the page groups them, because the
    section title ('Доступны для заказа', 'Недоступны для заказа', …) is what says
    whether a line can actually be ordered. Each item carries its title, quantity,
    Ozon Card price (`price`), whether its checkbox is ticked (`selected` —
    unticked lines are left out of the total), its product url and image.

    `summary.total` is what the ticked items cost with an Ozon Card."""
    try:
        data = await SESSION.page_json("/cart", cache=False)
    except (OzonChallenge, OzonBlocked) as exc:
        return _expired(exc)
    return _anonymous(data, "the cart") or _cart_payload(data)


@server.tool()
async def ozon_cart_add(url: str = "", product_id: str = "", quantity: int = 1) -> dict[str, Any]:
    """Put a product into the user's real Ozon cart. This CHANGES their account.

    Ask the user before calling it. It does not buy anything — no order is placed
    and no money moves; checkout stays with the user on ozon.ru.

    url / product_id: the product, same values ozon_product takes. For a product
    with colour/size variants, pass the url or SKU of the exact variant from
    ozon_product's `variants` — the base product id adds whichever variant Ozon
    picks.

    quantity is the ABSOLUTE quantity of that item in the cart, not an increment:
    calling it twice with quantity=1 leaves one item, not two.

    Ozon answers 'success' even for a SKU it then refuses to keep, so the tool
    re-reads the cart afterwards and returns it. `added` is the line as it really
    ended up there — if it is null the product did not make it in (sold out,
    wrong SKU) and the response says so."""
    quantity = _clamp(quantity, 1, 2000)
    try:
        product_url = client.normalize_product_url(url=url, product_id=product_id)
        sku = client.product_id_from_url(product_url)
        if not sku:
            raise ValueError(f"No product id in {product_url} — pass a /product/<id>/ url.")
        await SESSION.action("addToCart", [{"id": int(sku), "quantity": quantity}], referer=product_url)
        data = await SESSION.page_json("/cart", cache=False)
    except (OzonChallenge, OzonBlocked) as exc:
        return _expired(exc)
    anonymous = _anonymous(data, "the cart")
    if anonymous:
        return anonymous
    cart = _cart_payload(data)
    added = next(
        (item for section in cart["sections"] for item in section["items"] if item["id"] == sku), None
    )
    return {
        **cart,
        "ok": bool(added),
        "requested": {"url": product_url, "id": sku, "quantity": quantity},
        "added": added,
        **(
            {}
            if added
            else {
                "error": "not_in_cart",
                "message": (
                    "Ozon accepted the request but the product is not in the cart — it is "
                    "usually out of stock or the SKU does not exist. Check ozon_product."
                ),
            }
        ),
    }


if __name__ == "__main__":
    os.chdir(PLUGIN_ROOT)
    server.run(transport="stdio")
