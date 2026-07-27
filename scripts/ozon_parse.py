#!/usr/bin/env python3
"""Turn Ozon composer-api widgetStates into plain dicts.

Ozon renames and reshapes widgets without notice, so every parser here is
deliberately forgiving: it looks widgets up by name *prefix*, treats every field
as optional, and returns whatever it managed to read instead of raising. A
caller that gets an empty dict back should say so rather than pretend.
"""
from __future__ import annotations

import contextlib
import json
import re
from datetime import datetime, timezone
from typing import Any

from ozon_client import abs_url, product_id_from_url

# ── generic widget access ────────────────────────────────────────────────────


def widgets(data: dict[str, Any], prefix: str) -> list[dict[str, Any]]:
    """All widget states whose name starts with `prefix` (e.g. 'webPrice')."""
    out = []
    for key, raw in (data.get("widgetStates") or {}).items():
        if not key.startswith(prefix + "-") or not isinstance(raw, str):
            continue
        with contextlib.suppress(Exception):
            parsed = json.loads(raw)
            if isinstance(parsed, dict):
                out.append(parsed)
    return out


def widget(data: dict[str, Any], prefix: str) -> dict[str, Any]:
    found = widgets(data, prefix)
    return found[0] if found else {}


def text_of(value: Any) -> str:
    """Flatten Ozon's many text shapes (textRs / textAtom / {text}) to a string."""
    if isinstance(value, str):
        return value.strip()
    if isinstance(value, dict):
        for key in ("text", "content", "title", "name"):
            inner = value.get(key)
            if isinstance(inner, str) and inner.strip():
                return inner.strip()
        for key in ("textRs", "titleRs", "descriptionRs", "textAtom", "textDS"):
            if key in value:
                return text_of(value[key])
        return ""
    if isinstance(value, list):
        parts = []
        for item in value:
            if isinstance(item, dict) and item.get("type") == "newLine":
                parts.append("\n")
                continue
            piece = text_of(item)
            if piece:
                parts.append(piece)
        return re.sub(r" *\n *", "\n", " ".join(parts)).strip()
    return ""


def _money(text: str) -> int | None:
    digits = re.sub(r"[^\d]", "", text or "")
    return int(digits) if digits else None


def _ts(value: Any) -> str:
    """Unix seconds -> ISO date; passes through anything already human-readable."""
    if isinstance(value, (int, float)) and value > 0:
        return datetime.fromtimestamp(value, tz=timezone.utc).date().isoformat()
    return str(value or "")


# ── search / category tiles ──────────────────────────────────────────────────

TILE_PREFIXES = ("tileGrid", "searchResultsV2", "skuGrid", "skuShelfGoods")


def parse_tiles(data: dict[str, Any]) -> list[dict[str, Any]]:
    """Products from any grid widget on a search, category or favourites page."""
    products: list[dict[str, Any]] = []
    for key, raw in (data.get("widgetStates") or {}).items():
        if not key.startswith(TILE_PREFIXES) or not isinstance(raw, str):
            continue
        with contextlib.suppress(Exception):
            for item in json.loads(raw).get("items") or []:
                tile = _parse_tile(item)
                if tile:
                    products.append(tile)
    seen: set[str] = set()
    unique = []
    for product in products:
        key = product.get("id") or product.get("url")
        if key and key not in seen:
            seen.add(key)
            unique.append(product)
    return unique


def _parse_tile(item: dict[str, Any]) -> dict[str, Any] | None:
    if not isinstance(item, dict) or "mainState" not in item:
        return None
    blocks = [*(item.get("mainState") or []), *(item.get("state") or [])]
    tile: dict[str, Any] = {
        "id": str(item.get("sku") or item.get("id") or ""),
        "title": "",
        "brand": "",
        "price": "",
        "price_value": None,
        "discount": "",
        "rating": "",
        "reviews": "",
        "badges": [],
    }
    for block in blocks:
        if not isinstance(block, dict):
            continue
        kind = block.get("type")
        if kind == "priceV2":
            # Only the price Ozon headlines (with an Ozon Card). The crossed-out and
            # no-card prices are deliberately dropped: nobody shops by them and two
            # extra numbers per tile only invite quoting the wrong one.
            price = block.get("priceV2") or {}
            for entry in price.get("price") or []:
                if entry.get("textStyle") == "PRICE":
                    tile["price"] = entry.get("text", "")
                    tile["price_value"] = _money(tile["price"])
            tile["discount"] = price.get("discount", "")
        elif kind in {"textDS", "textAtom"} and not tile["title"]:
            body = block.get(kind) or {}
            auto = (body.get("testInfo") or {}).get("automatizationId")
            if block.get("id") == "name" or auto == "tile-name":
                tile["title"] = text_of(body)
        elif kind in {"labelListV2", "labelList"}:
            body = block.get(kind) or {}
            auto = (body.get("testInfo") or {}).get("automatizationId")
            texts = [
                text_of(entry.get("text") or entry)
                for entry in body.get("items") or []
                if entry.get("type") == "text" or isinstance(entry.get("title"), str)
            ]
            texts = [text for text in texts if text]
            if auto == "tile-list-rating":
                for text in texts:
                    if re.fullmatch(r"\d[.,]\d", text):
                        tile["rating"] = text
                    elif "отзыв" in text:
                        tile["reviews"] = re.sub(r"[^\d]", "", text)
            elif auto == "tile-list-labels" and texts:
                tile["brand"] = tile["brand"] or texts[0]
                tile["badges"] += texts[1:]
            elif texts:
                tile["badges"] += texts

    link = (item.get("action") or {}).get("link") or item.get("link") or ""
    tile["url"] = abs_url(str(link).split("?")[0])
    if not tile["id"]:
        tile["id"] = product_id_from_url(tile["url"])
    images = []
    for entry in (item.get("tileImage") or {}).get("items") or []:
        if isinstance(entry, dict) and entry.get("type") == "image":
            url = (entry.get("image") or {}).get("link")
            if url:
                images.append(abs_url(url))
    tile["image"] = images[0] if images else ""
    tile["images"] = images
    button = (
        ((item.get("multiButton") or {}).get("ozonButton") or {}).get("addToCart") or {}
    ).get("actionButton") or {}
    tile["delivery"] = text_of(button.get("title"))
    tile["badges"] = [badge for badge in dict.fromkeys(tile["badges"]) if badge]
    return tile if (tile["title"] or tile["id"]) else None


def parse_paginator(data: dict[str, Any]) -> dict[str, str]:
    for prefix in ("infiniteVirtualPaginator", "paginator"):
        for state in widgets(data, prefix):
            if state.get("nextPage") or state.get("prevPage"):
                return {
                    "next": str(state.get("nextPage") or ""),
                    "prev": str(state.get("prevPage") or ""),
                }
    return {"next": "", "prev": ""}


def parse_filters(data: dict[str, Any]) -> list[dict[str, Any]]:
    """Filter keys the current page supports, for use as `filters` arguments."""
    out: list[dict[str, Any]] = []

    def walk(node: Any) -> None:
        if isinstance(node, dict):
            kind, key = node.get("type"), node.get("key")
            if isinstance(kind, str) and isinstance(key, str) and kind.endswith("Filter"):
                body = node.get(kind) or {}
                title = text_of((body.get("rangeFilter") or {}).get("title")) or text_of(
                    body.get("title")
                )
                entry: dict[str, Any] = {"key": key, "type": kind, "title": title}
                span = body.get("rangeFilter") or {}
                if span.get("minValue") is not None:
                    entry["min"] = span.get("minValue")
                    entry["max"] = span.get("maxValue")
                values = [
                    item.get("key")
                    for section in (body.get("checkboxesFilter") or body).get("sections") or []
                    for item in section.get("items") or []
                    if item.get("key")
                ]
                if values:
                    entry["values"] = values[:25]
                out.append(entry)
            for value in node.values():
                walk(value)
        elif isinstance(node, list):
            for value in node:
                walk(value)

    walk(widget(data, "filtersDesktop") or widget(data, "searchResultsFilters"))
    return out


def parse_sortings(data: dict[str, Any]) -> list[dict[str, str]]:
    state = widget(data, "searchResultsSort")
    options = (state.get("sortButton") or {}).get("options") or []
    out = []
    for option in options:
        link = (option.get("action") or {}).get("link", "")
        match = re.search(r"sorting=([a-z_]+)", link)
        out.append(
            {
                "name": option.get("name", ""),
                "sorting": match.group(1) if match else "",
                "active": bool(option.get("isSelected")),
            }
        )
    return out


# ── product card ─────────────────────────────────────────────────────────────


def parse_product(main: dict[str, Any], extra: dict[str, Any] | None = None) -> dict[str, Any]:
    """Merge the two PDP layout pages into one card dict."""
    extra = extra or {}
    card: dict[str, Any] = {}

    heading = widget(main, "webProductHeading")
    card["title"] = text_of(heading.get("title"))

    price = widget(main, "webPrice")
    # One price only — the Ozon Card one Ozon headlines, falling back to the plain
    # price for the rare product that has no card price at all.
    card["price"] = price.get("cardPrice", "") or price.get("price", "")
    card["price_value"] = _money(card["price"])
    card["price_note"] = "price is the Ozon Card price — the one Ozon shows as the price."
    if "isAvailable" in price:
        card["in_stock"] = bool(price.get("isAvailable"))

    out_of_stock = widget(main, "webOutOfStock")
    if out_of_stock:
        card["in_stock"] = False
        card["title"] = card["title"] or text_of(out_of_stock.get("skuName"))

    sku = widget(main, "webDetailSKU")
    card["id"] = str(sku.get("copyText") or "")

    score = widget(main, "webReviewProductScore")
    card["rating"] = score.get("totalScore", "")
    card["reviews_count"] = score.get("reviewsCount", "")
    card["reviews_url"] = abs_url(str(score.get("url") or ""))
    stars = score.get("score")
    if isinstance(stars, list):
        card["rating_breakdown"] = {
            str(item.get("title", "")): item.get("value") for item in stars
        }

    questions = widget(main, "webQuestionCount")
    card["questions_count"] = re.sub(r"[^\d]", "", text_of(questions.get("text"))) or "0"

    gallery = widget(main, "webGallery")
    images = [abs_url(str(gallery.get("coverImage") or ""))]
    images += [abs_url(str(item.get("src") or "")) for item in gallery.get("images") or []]
    card["images"] = [url for url in dict.fromkeys(images) if url]
    card["videos"] = _gallery_videos(gallery)

    stock = widget(main, "bigPromoPDP")
    if stock:
        card["stock_left"] = f"{text_of(stock.get('stockNumber'))} {text_of(stock.get('stockText'))}".strip()

    chars = _parse_characteristics(extra) or _parse_short_characteristics(main)
    crumbs = [
        {"title": crumb.get("text", ""), "url": abs_url(crumb.get("link", ""))}
        for crumb in widget(main, "breadCrumbs").get("breadcrumbs") or []
    ]
    card["breadcrumbs"] = crumbs
    # The last crumb is the brand when its link nests under the category crumb.
    brand_crumb = (
        len(crumbs) >= 2 and crumbs[-1]["url"].startswith(crumbs[-2]["url"].rstrip("/") + "/")
    )
    card["brand"] = chars.get("Бренд") or (crumbs[-1]["title"] if brand_crumb else "")
    card["category"] = (crumbs[-2] if brand_crumb else crumbs[-1])["title"] if crumbs else ""
    card["category_url"] = (crumbs[-2] if brand_crumb else crumbs[-1])["url"] if crumbs else ""

    card["seller"] = _parse_seller(main)
    card["labels"] = [
        text_of((item.get("badge") or {}).get("text"))
        for item in widget(main, "webMarketingLabels").get("labels") or []
    ]
    card["labels"] = [label for label in card["labels"] if label]
    card["variants"] = _parse_aspects(widget(main, "webAspects"))

    card["characteristics"] = chars
    card["characteristics_count"] = len(chars)
    description, rich_images = _parse_description(extra)
    card["description"] = description
    card["description_images"] = rich_images
    if not description:
        short = _parse_short_characteristics(main)
        card["characteristics"] = card["characteristics"] or short
    return card


def _gallery_videos(gallery: dict[str, Any]) -> list[dict[str, str]]:
    videos: list[dict[str, str]] = []

    def walk(node: Any) -> None:
        if isinstance(node, dict):
            url = node.get("url") or node.get("link") or ""
            if isinstance(url, str) and re.search(r"\.(?:mp4|m3u8)(?:[?#]|$)", url, re.I):
                videos.append(
                    {"url": abs_url(url), "preview": abs_url(str(node.get("previewUrl") or ""))}
                )
            for value in node.values():
                walk(value)
        elif isinstance(node, list):
            for value in node:
                walk(value)

    walk(gallery)
    seen: set[str] = set()
    return [v for v in videos if not (v["url"] in seen or seen.add(v["url"]))]


def _parse_seller(main: dict[str, Any]) -> dict[str, Any]:
    sticky = widget(main, "webStickyProducts").get("seller") or {}
    seller: dict[str, Any] = {
        "name": sticky.get("name", ""),
        "url": abs_url(str(sticky.get("link") or "")),
        "logo": abs_url(str(sticky.get("logoImageUrl") or "")),
    }
    current = widget(main, "webCurrentSeller")
    if current:
        cell = current.get("sellerCell") or {}
        seller["name"] = seller["name"] or text_of((cell.get("centerBlock") or {}).get("title"))
        blob = json.dumps(current, ensure_ascii=False)
        rating = re.search(r'"text":\s*"(\d[.,]\d)"', blob)
        if rating:
            seller["rating"] = rating.group(1)
        orders = re.search(r'"text":\s*"([\d.,]+\s*[KКkМM]?)"[^}]*}[^{]*{[^}]*"text":\s*"Заказы"', blob)
        if not orders:
            orders = re.search(r'"text":\s*"([\d.,]+\s*[KКkМM])"', blob)
        if orders:
            seller["orders"] = orders.group(1).strip()
        seller_id = re.search(r'"sellerId":\s*"(\d+)"', blob)
        if seller_id:
            seller["id"] = seller_id.group(1)
    return {key: value for key, value in seller.items() if value}


def _parse_aspects(state: dict[str, Any]) -> list[dict[str, Any]]:
    out = []
    for aspect in state.get("aspects") or []:
        variants = []
        for variant in aspect.get("variants") or []:
            data = variant.get("data") or {}
            variants.append(
                {
                    "sku": str(variant.get("sku") or ""),
                    "value": text_of(data.get("textRs")) or data.get("searchableText", ""),
                    "price": data.get("price", ""),
                    "price_value": variant.get("price"),
                    "availability": variant.get("availability", ""),
                    "active": bool(variant.get("active")),
                    "url": abs_url(str(variant.get("link") or "").split("?")[0]),
                    "image": abs_url(str(data.get("coverImage") or "")),
                }
            )
        if variants:
            out.append(
                {
                    "name": aspect.get("aspectName", ""),
                    "key": aspect.get("aspectKey", ""),
                    "variants": variants,
                }
            )
    return out


def _parse_characteristics(page: dict[str, Any]) -> dict[str, str]:
    chars: dict[str, str] = {}
    for state in widgets(page, "webCharacteristics"):
        for group in state.get("characteristics") or []:
            for item in group.get("short") or []:
                name = str(item.get("name") or "").strip()
                values = [text_of(value) for value in item.get("values") or []]
                values = [value for value in values if value]
                if name and values:
                    chars[name] = ", ".join(values)
    return chars


def _parse_short_characteristics(page: dict[str, Any]) -> dict[str, str]:
    chars: dict[str, str] = {}
    for state in widgets(page, "webShortCharacteristics"):
        for item in state.get("characteristics") or []:
            name = text_of(item.get("title"))
            values = [text_of(value) for value in item.get("values") or []]
            values = [value for value in values if value]
            if name and values:
                chars[name] = ", ".join(values)
    return chars


def _parse_description(page: dict[str, Any]) -> tuple[str, list[str]]:
    parts: list[str] = []
    images: list[str] = []
    for state in widgets(page, "webDescription"):
        for item in state.get("characteristics") or []:
            title, content = item.get("title"), item.get("content")
            if title and content:
                parts.append(f"{title}: {content}")
        rich = state.get("richAnnotationJson")
        if isinstance(rich, dict):
            _walk_rich(rich, parts, images)
        plain = state.get("richAnnotation")
        if isinstance(plain, str) and plain.strip():
            parts.append(re.sub(r"<[^>]+>", " ", plain).strip())
    text = "\n".join(dict.fromkeys(part for part in parts if part))
    return re.sub(r"[ \t]+", " ", text)[:30_000], list(dict.fromkeys(images))[:30]


def _walk_rich(node: Any, parts: list[str], images: list[str]) -> None:
    if isinstance(node, dict):
        if isinstance(node.get("src"), str):
            images.append(abs_url(node["src"]))
        for key in ("content", "text", "title"):
            value = node.get(key)
            if isinstance(value, str) and value.strip():
                parts.append(re.sub(r"<[^>]+>", " ", value).strip())
        for value in node.values():
            if not isinstance(value, str):
                _walk_rich(value, parts, images)
    elif isinstance(node, list):
        for value in node:
            _walk_rich(value, parts, images)


# ── size table ───────────────────────────────────────────────────────────────


def size_table_link(data: dict[str, Any]) -> str:
    """The 'Таблица размеров' modal path, if this product has sizes at all."""
    for aspect in widget(data, "webAspects").get("aspects") or []:
        link = str((aspect.get("additionalInfo") or {}).get("linkModal") or "")
        if link:
            return link
    return ""


def parse_size_table(data: dict[str, Any]) -> dict[str, Any]:
    """The size chart behind that modal, in whichever of its three shapes it comes.

    Ozon serves 'constructor' (its own builder), 'legacy' (a brand's own grid) and
    'gallery' (the seller just uploaded a picture of a table). Only the first two
    carry text; the third is reported as image URLs, because guessing numbers off
    a JPEG is exactly the mistake that gets clothes returned.
    """
    state = widget(data, "webSizeTable")
    if not state:
        return {}
    params = state.get("params") or {}
    table: dict[str, Any] = {
        "title": params.get("heading", ""),
        "category": params.get("instruction", ""),
        "tables": [],
        "notes": [],
        "images": [],
    }
    kind = state.get("type")
    if kind == "constructor":
        for block in (state.get("tableConstructorJson") or {}).get("content") or []:
            body = block.get("table") or {}
            rows = [_size_row(row) for row in body.get("body") or []]
            if rows:
                table["tables"].append({"title": body.get("title", ""), "rows": rows})
            # A disclaimer rides along under more than one widgetName, so go by the key.
            note = block.get("disclaimer") or {}
            text = " ".join(part for part in (note.get("title"), note.get("body")) if part)
            if text:
                table["notes"].append(text.strip())
    elif kind == "legacy":
        for body in state.get("tables") or []:
            keys = [str(field.get("key")) for field in body.get("fields") or []]
            rows = [[str(item.get(key, "")).strip() for key in keys] for item in body.get("items") or []]
            if rows:
                table["tables"].append({"title": body.get("caption", ""), "rows": rows})
    elif kind == "gallery":
        table["images"] = [
            abs_url(str(image.get("src") or ""))
            for image in (state.get("gallery") or {}).get("images") or []
        ]
        table["images"] = [url for url in table["images"] if url]
    return table if (table["tables"] or table["images"]) else {}


def _size_row(row: dict[str, Any]) -> list[str]:
    """One chart row, flattened. Ozon writes the label cell as [name, unit]."""
    cells = []
    for cell in row.get("data") or []:
        if isinstance(cell, list):
            cells.append(" ".join(str(part).strip() for part in cell if str(part).strip()))
        else:
            cells.append(str(cell).strip())
    return cells


# ── reviews ──────────────────────────────────────────────────────────────────


def parse_reviews(data: dict[str, Any]) -> dict[str, Any]:
    state = widget(data, "webListReviews")
    if not state:
        return {"reviews": [], "total": 0, "sortings": [], "paging": {}}
    reviews = [_parse_review(item) for item in state.get("reviews") or []]
    paging = state.get("paging") or {}
    stars = widget(data, "webReviewProductScore").get("score")
    breakdown = (
        {str(item.get("title", "")): item.get("value") for item in stars}
        if isinstance(stars, list)
        else {}
    )
    return {
        "rating_breakdown": breakdown,
        "reviews": [review for review in reviews if review],
        "total": paging.get("total", 0),
        "page": paging.get("page", 1),
        "per_page": paging.get("perPage", 30),
        "next_page_params": paging.get("nextButton", ""),
        "sortings": [
            {"name": item.get("name", ""), "value": item.get("value", ""), "active": bool(item.get("active"))}
            for item in state.get("sortings") or []
        ],
        "product_score": state.get("productScore", ""),
    }


def _parse_review(item: dict[str, Any]) -> dict[str, Any] | None:
    if not isinstance(item, dict):
        return None
    content = item.get("content") or {}
    photos = [abs_url(str(photo.get("url") or "")) for photo in content.get("photos") or []]
    videos = [
        {
            "url": abs_url(str(video.get("url") or "")),
            "preview": abs_url(str(video.get("previewUrl") or "")),
            "duration": video.get("duration", ""),
        }
        for video in content.get("videos") or []
    ]
    # `comments` is {"list": [...], "totalCount": n} on current Ozon, a bare list before.
    # ponytail: Ozon lazy-loads reply text through a POST action whose body shape it
    # does not publish, so we surface the count and let the caller open the review URL.
    raw_comments = item.get("comments") or []
    reply_count = 0
    if isinstance(raw_comments, dict):
        reply_count = int(raw_comments.get("totalCount") or 0)
        raw_comments = raw_comments.get("list") or []
    answers = []
    for comment in raw_comments:
        if not isinstance(comment, dict):
            continue
        author = comment.get("author") or {}
        answers.append(
            {
                "author": text_of(author.get("name") or author.get("fio") or author.get("firstName")),
                "text": text_of(comment.get("text") or comment.get("content")),
                "date": _ts(comment.get("publishedAt") or comment.get("createdAt")),
            }
        )
    author = item.get("author") or {}
    return {
        "id": item.get("uuid", ""),
        "author": author.get("firstName") or author.get("fio") or "",
        "rating": content.get("score"),
        "date": _ts(item.get("publishedAt") or item.get("createdAt")),
        "text": content.get("comment", ""),
        "pros": content.get("positive", ""),
        "cons": content.get("negative", ""),
        "photos": [url for url in photos if url],
        "videos": [video for video in videos if video["url"]],
        "useful": (item.get("usefulness") or {}).get("useful", 0),
        "purchased": bool(item.get("isItemPurchased")),
        "variant": text_of(item.get("badgeDelivery")),
        "seller_answers": answers,
        "replies_count": reply_count or len(answers),
        "url": (item.get("sharing") or {}).get("url", "").split("&utm")[0],
    }


# ── questions ────────────────────────────────────────────────────────────────


def parse_questions(data: dict[str, Any]) -> dict[str, Any]:
    state = widget(data, "webListQuestions")
    if not state:
        return {"questions": [], "total": 0}
    answers = state.get("answers") or {}
    links = state.get("questionAnswers") or {}
    questions = []
    for qid in state.get("questionsIds") or list((state.get("questions") or {})):
        raw = (state.get("questions") or {}).get(str(qid)) or {}
        questions.append(
            {
                "id": str(qid),
                "author": ((raw.get("author") or {}).get("name") or ""),
                "date": raw.get("createdAt", ""),
                "text": text_of(raw.get("content")),
                "url": raw.get("url", ""),
                "useful": _useful(raw),
                "answers": [
                    {
                        "author": ((answers.get(str(aid)) or {}).get("author") or {}).get("name", ""),
                        "date": (answers.get(str(aid)) or {}).get("createdAt", ""),
                        "text": text_of((answers.get(str(aid)) or {}).get("content")),
                        "useful": _useful(answers.get(str(aid)) or {}),
                    }
                    for aid in links.get(str(qid), [])
                ],
            }
        )
    paging = state.get("paging") or {}
    return {
        "questions": questions,
        "total": paging.get("total", len(questions)),
        "page": paging.get("page", 1),
        "per_page": paging.get("perPage", 10),
        "sortings": [
            {"name": item.get("name", ""), "value": item.get("value", "")}
            for item in state.get("sortings") or []
        ],
        "active_sort": state.get("activeSortValue", ""),
    }


def _useful(node: dict[str, Any]) -> int:
    for action in (node.get("usefulness") or {}).get("actions") or []:
        if action.get("type") == "like":
            return int(action.get("val") or 0)
    return 0


# ── catalog / account ────────────────────────────────────────────────────────


def parse_catalog_menu(data: dict[str, Any]) -> list[dict[str, str]]:
    state = widget(data, "catalogMenu")
    return [
        {
            "id": str(item.get("id") or ""),
            "title": item.get("title", ""),
            "url": abs_url(item.get("url", "")),
        }
        for item in state.get("categories") or []
        if item.get("title")
    ]


def parse_subcategories(data: dict[str, Any]) -> list[dict[str, Any]]:
    """The category filter's tree: ancestors, the current category, its children."""
    out: list[dict[str, Any]] = []

    def walk(node: Any) -> None:
        if isinstance(node, dict):
            if node.get("type") == "categoryFilter":
                for item in (node.get("categoryFilter") or {}).get("categories") or []:
                    title = text_of(item.get("title"))
                    link = str(item.get("urlValue") or "").split("?")[0]
                    if title:
                        out.append(
                            {
                                "title": title,
                                "url": abs_url(link),
                                "level": item.get("level", 0),
                                "current": bool(item.get("isActive")),
                            }
                        )
            for value in node.values():
                walk(value)
        elif isinstance(node, list):
            for value in node:
                walk(value)

    walk(widget(data, "filtersDesktop"))
    seen: set[str] = set()
    return [c for c in out if c["title"] not in seen and not seen.add(c["title"])][:80]


def parse_account(data: dict[str, Any]) -> dict[str, Any]:
    user = (data.get("userInfo") or {}).get("user") or {}
    current = (data.get("location") or {}).get("current") or {}
    # isLoggedIn is only present on some pages; a userId/email is proof enough.
    return {
        "logged_in": bool(user.get("isLoggedIn") or user.get("userId") or user.get("email")),
        "user_id": user.get("userId", ""),
        "first_name": user.get("firstName", ""),
        "last_name": user.get("lastName", ""),
        "email": user.get("email", ""),
        "registered": str(user.get("registrationDate") or "")[:10],
        "city": current.get("city", ""),
        "address": _clean_address(str(current.get("fullName") or current.get("name") or "")),
    }


def _clean_address(text: str) -> str:
    """Ozon wraps addresses in {{{value|tracking-key}}}."""
    match = re.search(r"\{\{\{(.+?)(?:\|[^}]*)?\}\}\}", text)
    return (match.group(1) if match else text).strip()


def parse_orders(data: dict[str, Any]) -> list[dict[str, Any]]:
    orders = []
    for state in widgets(data, "orderList"):
        for entry in state.get("ordersV2") or state.get("orders") or []:
            left = entry.get("leftBlock") or {}
            link = ((entry.get("common") or {}).get("action") or {}).get("link", "")
            number = re.search(r"order=([\d-]+)", link)
            products = []
            for product in ((entry.get("rightBlock") or {}).get("products") or {}).get(
                "products"
            ) or []:
                price = (product.get("price") or {}).get("price") or []
                products.append(
                    {
                        "image": abs_url(
                            str(
                                (((product.get("image") or {}).get("productMedia") or {}).get("image") or {}).get("url")
                                or ""
                            )
                        ),
                        "price": price[0].get("text", "") if price else "",
                    }
                )
            orders.append(
                {
                    "number": number.group(1) if number else "",
                    "status": text_of((left.get("textIcon") or {}).get("text")),
                    "delivery": text_of(left.get("subtitle")),
                    "url": abs_url(link),
                    "items": products,
                    "items_count": len(products),
                }
            )
    return orders


# ── cart ─────────────────────────────────────────────────────────────────────

# Ozon prices a cart line twice: with an Ozon Card and without it. Only the card
# price is reported; the no-card one is the fallback for a line that lacks it.
CART_PRICE_STYLES = ("CARD_PRICE", "SECOND_LVL")


def parse_cart(data: dict[str, Any]) -> list[dict[str, Any]]:
    """Cart lines, grouped the way the page groups them.

    Ozon renders one cartSplit widget per section ('Доступны для заказа',
    'Недоступны для заказа', …), so the section title is the only thing saying an
    item cannot actually be ordered — keep it rather than flattening it away.
    """
    sections = []
    for state in widgets(data, "cartSplit"):
        items = [_parse_cart_item(entry) for entry in state.get("cartItems") or []]
        items = [item for item in items if item]
        if items:
            sections.append(
                {"title": text_of((state.get("header") or {}).get("title")), "items": items}
            )
    return sections


def _parse_cart_item(entry: dict[str, Any]) -> dict[str, Any] | None:
    if not isinstance(entry, dict):
        return None
    product = entry.get("product") or {}
    quantity = ((entry.get("controls") or {}).get("quantity")) or {}
    prices: dict[str, str] = {}
    item: dict[str, Any] = {
        "id": str(product.get("id") or ""),
        "title": "",
        "price": "",
        "quantity": quantity.get("current"),
        "max_quantity": quantity.get("maximum"),
        "selected": bool((entry.get("checkbox") or {}).get("isChecked")),
        "badges": [],
        "url": abs_url(
            str(((product.get("common") or {}).get("action") or {}).get("link") or "").split("?")[0]
        ),
        "image": abs_url(str(((product.get("image") or {}).get("image") or {}).get("url") or "")),
    }
    for block in product.get("titleColumn") or []:
        kind = block.get("type")
        if kind == "text" and not item["title"]:
            item["title"] = text_of(block.get("text"))
        elif kind == "badges":
            item["badges"] += [
                text_of(badge) for badge in (block.get("badges") or {}).get("elements") or []
            ]
    for block in product.get("priceColumn") or []:
        for element in (block.get("prices") or {}).get("elements") or []:
            style = (element.get("priceStyle") or {}).get("styleType")
            text = next(
                (
                    entry.get("text", "")
                    for entry in element.get("price") or []
                    if entry.get("textStyle") == "PRICE"
                ),
                "",
            )
            if style in CART_PRICE_STYLES and text:
                prices.setdefault(style, text)
    item["price"] = prices.get("CARD_PRICE") or prices.get("SECOND_LVL") or ""
    item["price_value"] = _money(item["price"])
    item["badges"] = [badge for badge in dict.fromkeys(item["badges"]) if badge]
    return item if item["id"] else None


def parse_cart_total(data: dict[str, Any]) -> dict[str, Any]:
    """The cart footer: what it costs to buy what is ticked.

    Only the Ozon Card total is reported. Ozon's footer also carries the
    pre-discount subtotal and a no-card total, and three totals in one answer is
    exactly how the wrong one ends up quoted.
    """
    summary = widget(data, "total").get("summary") or {}
    # Ozon colours some totals with an inline <span>.
    total = re.sub(r"<[^>]+>", "", text_of((summary.get("footer") or {}).get("price"))).strip()
    return {
        "info": text_of((summary.get("header") or {}).get("info")),
        "total": total,
        "total_value": _money(total),
        "note": "total is the Ozon Card total for the ticked items.",
    }


def parse_empty_state(data: dict[str, Any]) -> str:
    for prefix in ("statusWidget", "emptyState", "emptyCart"):
        state = widget(data, prefix)
        title = text_of(state.get("titleAtom") or state.get("title"))
        if title:
            return title
    return ""
