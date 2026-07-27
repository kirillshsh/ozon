#!/usr/bin/env python3
"""Live check that Ozon still returns the fields the tools depend on.

Ozon reshapes its widgets without notice, so this makes a handful of real
requests and asserts the shape rather than mocking it. Run it when a tool starts
returning empty fields:

    .venv/bin/python scripts/selfcheck.py

Exits non-zero and names the field that went missing.
"""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import ozon_client as client  # noqa: E402
import ozon_server as tools  # noqa: E402
from ozon_client import OzonBlocked, OzonChallenge  # noqa: E402

PROBE_QUERY = "беспроводные наушники"
failures: list[str] = []


def check(label: str, ok: bool, detail: str = "") -> None:
    print(f"  {'ok  ' if ok else 'FAIL'}  {label}{'  ' + detail if detail else ''}")
    if not ok:
        failures.append(label)


def check_cookie_merge() -> None:
    """The rotation path rewrites the user's credential file — exercise it offline."""
    import json
    import tempfile

    original = client.COOKIES_FILE
    with tempfile.TemporaryDirectory() as tmp:
        client.COOKIES_FILE = Path(tmp) / "cookies.json"
        try:
            client.save_cookie_list(
                [{"name": "__Secure-access-token", "value": "old", "domain": ".ozon.ru", "path": "/"}]
            )
            session = client.Session()
            session.jar()
            session._persist(
                {
                    "__Secure-access-token": "new",  # rotated -> must be written
                    "abt_data": "fresh",  # known session cookie -> added
                    "ozon_devtools": "on",  # junk -> must be dropped
                }
            )
            stored = {c["name"]: c["value"] for c in json.loads(client.COOKIES_FILE.read_text())}
            check("rotated token written back", stored.get("__Secure-access-token") == "new")
            check("new session cookie added", stored.get("abt_data") == "fresh")
            check("junk cookie ignored", "ozon_devtools" not in stored)
            check("no duplicate entries", len(json.loads(client.COOKIES_FILE.read_text())) == 2)
        finally:
            client.COOKIES_FILE = original


async def main() -> int:
    print("cookie rotation (offline)")
    check_cookie_merge()

    print("session")
    status = await tools.ozon_status(check_live=True)
    if status.get("error") == "ip_blocked":
        check("session alive", False, "ip_blocked")
        print(
            "\nOzon refuses this IP address, not this session. Re-logging in will not help.\n"
            f"Proxy currently configured: {status.get('proxy') or 'none'}\n"
            "Set OZON_PROXY to a Russian exit (socks5://host:port) and run again."
        )
        return 1
    check("session alive", bool(status.get("session_alive")), str(status.get("error", "")))
    check("account identified", bool(status.get("account", {}).get("logged_in")))
    if not status.get("session_alive"):
        print("\nSession is dead — run ozon_refresh_cookies first.")
        return 1

    print("search")
    found = await tools.ozon_search(query=PROBE_QUERY, limit=8)
    products = found.get("products") or []
    check("search returns products", bool(products), f"{len(products)} tiles")
    check("tile has url", bool(products and products[0].get("url")))
    check("tile has price", bool(products and products[0].get("price")))
    check("tile has rating", any(tile.get("rating") for tile in products))
    check("filters exposed", bool(found.get("available_filters")))
    check("sortings exposed", bool(found.get("available_sortings")))
    if not products:
        return 1
    url = products[0]["url"]

    print(f"product  {url}")
    card = await tools.ozon_product(url=url)
    check("title", bool(card.get("title")))
    check("ozon card price", bool(card.get("price")))
    check("characteristics", card.get("characteristics_count", 0) > 0, str(card.get("characteristics_count")))
    check("images", len(card.get("images") or []) > 0, str(len(card.get("images") or [])))
    check("seller", bool(card.get("seller", {}).get("name")))
    check("breadcrumbs", bool(card.get("breadcrumbs")))

    print("reviews")
    reviews = await tools.ozon_reviews(url=url, limit=5)
    items = reviews.get("reviews") or []
    check("reviews returned", bool(items), f"{len(items)} of {reviews.get('total_reviews_on_product')}")
    check("review has rating", any(item.get("rating") for item in items))
    check("review has date", any(item.get("date") for item in items))
    check("rating breakdown", bool(reviews.get("rating_breakdown")))

    print("questions")
    questions = await tools.ozon_questions(url=url, limit=5)
    check(
        "questions endpoint answers",
        isinstance(questions.get("questions"), list),
        f"{questions.get('total_questions')} on this product",
    )

    print("catalog")
    catalog = await tools.ozon_catalog()
    check("root categories", len(catalog.get("categories") or []) > 5)

    # Sizes decide whether clothing can be shopped for at all, and they live in two
    # places that break independently: the size aspect on the card and the chart
    # behind its own modal. Scan a few tiles — not every listing is sized.
    print("clothing sizes")
    clothes = await tools.ozon_search(query="футболка мужская", limit=6)
    sized = chart = None
    for tile in (clothes.get("products") or [])[:3]:
        card = await tools.ozon_product(url=tile["url"], include_description=False)
        aspects = {aspect.get("key"): aspect for aspect in card.get("variants") or []}
        sized = sized or aspects.get("size")
        chart = chart or card.get("size_table")
    check("size variants offered", bool(sized and sized.get("variants")))
    check("size variants say availability", bool(sized and sized["variants"][0].get("availability")))
    check(
        "size chart returned",
        bool(chart and (chart.get("tables") or chart.get("images"))),
        "as text" if (chart or {}).get("tables") else "as image only",
    )

    # Read-only: the cart is the user's own, so nothing is added here. With an empty
    # cart only the empty state can be asserted; the line shape is checked when there
    # is something to check.
    print("cart")
    cart = await tools.ozon_cart()
    items = [item for section in cart.get("sections") or [] for item in section["items"]]
    check("cart answers", cart.get("ok") is True, f"{len(items)} item(s)")
    if items:
        check("cart item has title", all(item.get("title") for item in items))
        check("cart item has quantity", all(item.get("quantity") for item in items))
        check("cart item has price", all(item.get("price") for item in items))
        check("cart total", bool(cart.get("summary", {}).get("total")))
    else:
        check("empty cart explained", bool(cart.get("empty_state")), cart.get("empty_state", ""))

    print()
    if failures:
        print(f"{len(failures)} check(s) failed: {', '.join(failures)}")
        return 1
    print("all checks passed")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(asyncio.run(main()))
    except OzonBlocked as exc:
        print(f"ip blocked: {exc}")
        raise SystemExit(1) from exc
    except OzonChallenge as exc:
        print(f"session expired: {exc}")
        raise SystemExit(1) from exc
