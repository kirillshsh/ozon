# wb_bp.py — Wildberries: nodriver challenge + httpx
# pip install nodriver httpx
import asyncio
import gzip
import json
import re
import time
import httpx
import nodriver as uc

# ─────────────────────────────────────────────────────────────────────────────
# Конфигурация
# ─────────────────────────────────────────────────────────────────────────────
TARGET_URL = "https://www.wildberries.ru/"
COOKIES_FILE = "wb_cookies.json"
CHALLENGE_TIMEOUT = 30
HEADLESS = False

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/131.0.0.0 Safari/537.36"
)

RELEVANT_COOKIE_NAMES = {
    "__wba_s",
    "__wbl",
    "___wbu",
    "x-supplier-id-external",
    "wbx-validation-def-quest__val",
    "__store",
    "__wba_uid",
    "wb_geo",
}


# ─────────────────────────────────────────────────────────────────────────────
# Утилиты
# ─────────────────────────────────────────────────────────────────────────────


def is_challenge_page(html: str) -> bool:
    markers = (
        "challenge_solver",
        "antibot",
        "find-frontend-settings",
        "Доступ ограничен",
        "wbaas/challenges",
    )
    return any(m in html for m in markers)


def load_cookies(path: str = COOKIES_FILE) -> list[dict] | None:
    try:
        with open(path, encoding="utf-8") as f:
            cookies = json.load(f)
        print(f"[wb] Cookies загружены из {path} ({len(cookies)} шт.)")
        return cookies
    except FileNotFoundError:
        print(f"[wb] Файл {path} не найден")
        return None


def save_cookies(cookies: list[dict], path: str = COOKIES_FILE):
    with open(path, "w", encoding="utf-8") as f:
        json.dump(cookies, f, ensure_ascii=False, indent=2)
    print(f"[wb] Cookies сохранены в {path} ({len(cookies)} шт.)")


def cookies_to_dict(cookies: list[dict]) -> dict[str, str]:
    return {c["name"]: c["value"] for c in cookies}


def response_json(resp) -> dict:
    try:
        return resp.json()
    except UnicodeDecodeError:
        content = resp.content
        if content[:2] == b"\x1f\x8b":
            content = gzip.decompress(content)
        return json.loads(content.decode("utf-8"))


# ─────────────────────────────────────────────────────────────────────────────
# Шаг 1: Challenge через nodriver
# ─────────────────────────────────────────────────────────────────────────────


async def solve_challenge_browser(url: str = TARGET_URL) -> list[dict] | None:
    print(f"\n{'=' * 60}")
    print("[wb-browser] Запускаем Chrome...")
    print(f"{'=' * 60}")

    browser = await uc.start(
        headless=HEADLESS,
        browser_args=["--disable-dev-shm-usage", "--lang=ru-RU"],
    )

    tab = await browser.get(url)
    print(f"[wb-browser] Переходим на {url}")

    start = time.time()
    solved = False

    while time.time() - start < CHALLENGE_TIMEOUT:
        await tab.sleep(1)
        try:
            title = await tab.evaluate("document.title")
        except Exception:
            title = ""
        try:
            html_snippet = await tab.evaluate(
                "document.documentElement.innerHTML.substring(0, 3000)"
            )
        except Exception:
            html_snippet = ""

        elapsed = time.time() - start

        if title and "Wildberries" in title and not is_challenge_page(html_snippet):
            print(f"[wb-browser] Challenge пройден за {elapsed:.1f}s")
            print(f"[wb-browser] Title: {title}")
            solved = True
            break

        if is_challenge_page(html_snippet):
            print(f"[wb-browser] Challenge в процессе... ({elapsed:.1f}s)")

    if not solved:
        try:
            title = await tab.evaluate("document.title")
            print(f"[wb-browser] Таймаут. Title: {title}")
        except Exception:
            print("[wb-browser] Таймаут.")

    all_cookies = await browser.cookies.get_all()
    print(f"[wb-browser] Получено cookies: {len(all_cookies)}")

    cookies_list = []
    for c in all_cookies:
        cookie = {
            "name": c.name,
            "value": c.value,
            "domain": c.domain,
            "path": c.path,
        }
        if c.expires and c.expires > 0:
            cookie["expires"] = c.expires
        if c.secure:
            cookie["secure"] = True
        if c.http_only:
            cookie["httpOnly"] = True
        if c.same_site:
            ss = (
                str(c.same_site.value)
                if hasattr(c.same_site, "value")
                else str(c.same_site)
            )
            if ss and ss != "None":
                cookie["sameSite"] = ss
        cookies_list.append(cookie)

    relevant = [c for c in cookies_list if c["name"] in RELEVANT_COOKIE_NAMES]
    print(f"[wb-browser] Ключевые cookies: {[c['name'] for c in relevant]}")

    browser.stop()
    return cookies_list if cookies_list else None


# ─────────────────────────────────────────────────────────────────────────────
# Шаг 2: httpx запросы
# ─────────────────────────────────────────────────────────────────────────────


# ── Basket routing (загружается динамически из CDN API WB) ──
_BASKET_MAP: list[tuple[int, int, str]] = []  # [(vol_from, vol_to, host), ...]
_BASKET_LOADED = False


async def _load_basket_map():
    """Загрузить актуальную таблицу basket -> vol из CDN API WB."""
    global _BASKET_MAP, _BASKET_LOADED
    if _BASKET_LOADED:
        return

    url = f"https://cdn.wbbasket.ru/api/v3/upstreams?t={int(time.time() * 1000)}"
    try:
        async with httpx.AsyncClient(verify=False, timeout=10) as client:
            resp = await client.get(url)
            if resp.status_code == 200:
                data = resp.json()
                hosts = (
                    data.get("recommend", {})
                    .get("mediabasket_route_map", [{}])[0]
                    .get("hosts", [])
                )
                _BASKET_MAP.clear()
                for entry in hosts:
                    vf = entry.get("vol_range_from", 0)
                    vt = entry.get("vol_range_to", 0)
                    host = entry.get("host", "")
                    if host:
                        _BASKET_MAP.append((vf, vt, host))
                _BASKET_MAP.sort(key=lambda x: x[0])
                _BASKET_LOADED = True
                print(
                    f"[wb] Basket map загружена: {len(_BASKET_MAP)} ranges (vol 0-{_BASKET_MAP[-1][1] if _BASKET_MAP else '?'})"
                )
                return
    except Exception as e:
        print(f"[wb] Не удалось загрузить basket map: {e}")

    # Fallback: хардкод (может устареть)
    if not _BASKET_MAP:
        _BASKET_MAP.extend(
            [
                (0, 143, "basket-01.wbbasket.ru"),
                (144, 287, "basket-02.wbbasket.ru"),
                (288, 431, "basket-03.wbbasket.ru"),
                (432, 719, "basket-04.wbbasket.ru"),
                (720, 1007, "basket-05.wbbasket.ru"),
                (1008, 1061, "basket-06.wbbasket.ru"),
                (1062, 1115, "basket-07.wbbasket.ru"),
                (1116, 1169, "basket-08.wbbasket.ru"),
                (1170, 1313, "basket-09.wbbasket.ru"),
                (1314, 1601, "basket-10.wbbasket.ru"),
                (1602, 1655, "basket-11.wbbasket.ru"),
                (1656, 1919, "basket-12.wbbasket.ru"),
                (1920, 2045, "basket-13.wbbasket.ru"),
                (2046, 2189, "basket-14.wbbasket.ru"),
                (2190, 2405, "basket-15.wbbasket.ru"),
                (2406, 2621, "basket-16.wbbasket.ru"),
                (2622, 2837, "basket-17.wbbasket.ru"),
                (2838, 3053, "basket-18.wbbasket.ru"),
            ]
        )
        _BASKET_LOADED = True


def _wb_basket_host(vol: int) -> str:
    """Получить hostname basket по vol из загруженной карты."""
    for vf, vt, host in _BASKET_MAP:
        if vf <= vol <= vt:
            return host
    # vol за пределами карты — берём последний basket
    if _BASKET_MAP:
        return _BASKET_MAP[-1][2]
    return "basket-01.wbbasket.ru"


def _wb_img_url(pid, size: str = "c246x328") -> str:
    """URL картинки WB."""
    pid = int(pid)
    vol = pid // 100000
    part = pid // 1000
    host = _wb_basket_host(vol)
    url = f"https://{host}/vol{vol}/part{part}/{pid}/images/{size}/1.webp"
    return url


def _wb_base_url(pid) -> str:
    """Базовый URL для card.json и картинок."""
    pid = int(pid)
    vol = pid // 100000
    part = pid // 1000
    host = _wb_basket_host(vol)
    return f"https://{host}/vol{vol}/part{part}/{pid}"


def _wb_headers() -> dict:
    return {
        "User-Agent": USER_AGENT,
        "Accept": "*/*",
        "Accept-Language": "ru-RU,ru;q=0.9,en-US;q=0.8,en;q=0.7",
        "Accept-Encoding": "gzip, deflate",
        "Origin": "https://www.wildberries.ru",
        "Referer": "https://www.wildberries.ru/",
        "Sec-Ch-Ua": '"Not_A Brand";v="8", "Chromium";v="131", "Google Chrome";v="131"',
        "Sec-Ch-Ua-Mobile": "?0",
        "Sec-Ch-Ua-Platform": '"Windows"',
        "Sec-Fetch-Dest": "empty",
        "Sec-Fetch-Mode": "cors",
        "Sec-Fetch-Site": "cross-site",
    }


async def httpx_check(cookies: dict[str, str], url: str = TARGET_URL) -> bool:
    async with httpx.AsyncClient(
        verify=False,
        follow_redirects=True,
        timeout=30.0,
        http2=True,
    ) as client:
        resp = await client.get(
            url,
            headers={
                **_wb_headers(),
                "Sec-Fetch-Dest": "document",
                "Sec-Fetch-Mode": "navigate",
            },
            cookies=cookies,
        )
        blocked = is_challenge_page(resp.text)
        m = re.search(r"<title>(.*?)</title>", resp.text, re.IGNORECASE)
        title = m.group(1) if m else ""
        print(f"[wb-httpx] {resp.status_code} | challenge: {blocked} | title: {title}")
        return not blocked and resp.status_code == 200


async def wb_search_api(
    query: str,
    cookies: dict[str, str],
    page: int = 1,
) -> dict:
    """
    WB search через внутренний endpoint __internal/u-search (v18).
    Этот URL проксируется через wildberries.ru → нужны cookies от WB.
    """
    params = {
        "ab_testing": "false",
        "appType": "1",
        "autoselectFilters": "false",
        "curr": "rub",
        "dest": "-1255987",
        "hide_vflags": "4294967296",
        "inheritFilters": "false",
        "lang": "ru",
        "page": str(page),
        "query": query,
        "resultset": "catalog",
        "scale": "4",
        "sort": "popular",
        "spp": "30",
        "suppressSpellcheck": "false",
    }

    urls = [
        "https://catalog.wb.ru/exactmatch/ru/common/v18/search",
        "https://search.wb.ru/exactmatch/ru/common/v18/search",
        "https://www.wildberries.ru/__internal/u-search/exactmatch/ru/common/v18/search",
    ]

    headers = {
        "User-Agent": USER_AGENT,
        "Accept": "application/json",
        "Accept-Language": "ru-RU,ru;q=0.9,en-US;q=0.8,en;q=0.7",
        "Accept-Encoding": "gzip, deflate",
        "Referer": "https://www.wildberries.ru/",
        "Sec-Ch-Ua": '"Not_A Brand";v="8", "Chromium";v="131", "Google Chrome";v="131"',
        "Sec-Ch-Ua-Mobile": "?0",
        "Sec-Ch-Ua-Platform": '"Windows"',
        "Sec-Fetch-Dest": "empty",
        "Sec-Fetch-Mode": "cors",
        "Sec-Fetch-Site": "same-origin",
    }

    async with httpx.AsyncClient(
        verify=False,
        follow_redirects=True,
        timeout=20.0,
        http2=True,
    ) as client:
        for url in urls:
            try:
                if "search.wb.ru" in url:
                    from curl_cffi.requests import AsyncSession

                    async with AsyncSession() as curl:
                        resp = await curl.get(
                            url,
                            params=params,
                            headers={"Accept": "application/json"},
                            timeout=20,
                        )
                elif "catalog.wb.ru" in url:
                    from curl_cffi.requests import AsyncSession

                    async with AsyncSession() as curl:
                        resp = await curl.get(url, params=params, timeout=20)
                else:
                    resp = await client.get(
                        url,
                        params=params,
                        headers=headers,
                        cookies=cookies,
                    )
            except Exception as e:
                print(f"[wb-search] {url} error: {e}")
                continue

            print(f"[wb-search] GET {resp.url}")
            print(f"[wb-search] HTTP {resp.status_code} | size={len(resp.content)}")
            if resp.status_code != 200:
                print(f"[wb-search] Response: {resp.text[:300]}")
                continue

            try:
                data = resp.json()
            except Exception:
                print(f"[wb-search] Не JSON: {resp.text[:200]}")
                continue

            nested_data = data.get("data") if isinstance(data.get("data"), dict) else {}
            if data.get("products") or nested_data.get("products"):
                return data
            print(f"[wb-search] JSON без products: keys={list(data)[:10]}")
    return {}


def parse_wb_products(data: dict) -> list[dict]:
    """Парсим ответ WB search API (v18: products на верхнем уровне)."""
    products = []
    # v18: products на верхнем уровне; v7: data.products
    items = data.get("products") or data.get("data", {}).get("products", [])

    for item in items:
        pid = item.get("id", "")
        name = item.get("name", "")
        brand = item.get("brand", "")

        # Цена: WB отдаёт в копейках
        price_obj = item.get("sizes", [{}])[0].get("price", {})
        price_raw = price_obj.get("product") or price_obj.get("total", 0)
        price = f"{price_raw // 100} ₽" if price_raw else ""

        old_price_raw = price_obj.get("basic", 0)
        old_price = (
            f"{old_price_raw // 100} ₽"
            if old_price_raw and old_price_raw != price_raw
            else ""
        )

        # Скидка
        discount = ""
        if old_price_raw and price_raw and old_price_raw > price_raw:
            pct = round((1 - price_raw / old_price_raw) * 100)
            discount = f"-{pct}%"

        # Рейтинг
        rating = str(item.get("reviewRating", ""))
        feedbacks = str(item.get("feedbacks", ""))

        # imtId (root) — нужен для отзывов
        imt_id = str(item.get("root", ""))

        image = _wb_img_url(pid)

        url = f"https://www.wildberries.ru/catalog/{pid}/detail.aspx"

        products.append(
            {
                "id": str(pid),
                "imtId": imt_id,
                "title": name,
                "brand": brand,
                "price": price,
                "old_price": old_price,
                "discount": discount,
                "rating": rating,
                "reviews": feedbacks,
                "url": url,
                "image": image,
            }
        )

    return products


# ─────────────────────────────────────────────────────────────────────────────
# Шаг 3: Карточка товара
# ─────────────────────────────────────────────────────────────────────────────


async def wb_variant_info(product_id: str, cookies: dict[str, str] = None) -> dict:
    """Быстрая инфа о варианте: цена, картинки, цвет."""
    await _load_basket_map()
    pid = int(product_id)
    base = _wb_base_url(pid)
    result = {
        "id": str(pid),
        "price": "",
        "old_price": "",
        "discount": "",
        "color": "",
        "title": "",
        "in_stock": False,
        "images": [],
        "url": f"https://www.wildberries.ru/catalog/{pid}/detail.aspx",
    }

    _cookies = cookies or {}

    async with httpx.AsyncClient(
        verify=False, follow_redirects=True, timeout=10
    ) as client:
        # card.json — цвет, название
        try:
            r = await client.get(f"{base}/info/ru/card.json", headers=_wb_headers())
            if r.status_code == 200:
                card = response_json(r)
                result["color"] = card.get("nm_colors_names", "")
                result["title"] = card.get("imt_name", "")
        except Exception:
            pass

        # Цена через __internal/u-card v4
        try:
            price_url = "https://www.wildberries.ru/__internal/u-card/cards/v4/detail"
            price_params = {
                "appType": "1",
                "curr": "rub",
                "dest": "-1255987",
                "spp": "30",
                "hide_vflags": "4294967296",
                "ab_testing": "false",
                "lang": "ru",
                "nm": str(pid),
            }
            price_headers = {
                **_wb_headers(),
                "Accept": "*/*",
                "Sec-Fetch-Site": "same-origin",
                "x-requested-with": "XMLHttpRequest",
                "x-spa-version": "14.0.7",
            }
            r = await client.get(
                price_url, params=price_params, headers=price_headers, cookies=_cookies
            )
            print(f"[wb-variant] u-card v4: {r.status_code}")
            if r.status_code == 200:
                data = response_json(r)
                # v4 может иметь разную структуру
                prods = data.get("products") or data.get("data", {}).get("products", [])
                print(
                    f"[wb-variant] products: {len(prods)}, top keys: {list(data.keys())[:8]}"
                )
                if prods:
                    p = prods[0]
                    sizes = p.get("sizes", [])
                    print(
                        f"[wb-variant] sizes: {len(sizes)}, product keys: {list(p.keys())[:12]}"
                    )
                    if sizes:
                        print(
                            f"[wb-variant] sizes[0] keys: {list(sizes[0].keys())[:10]}"
                        )
                        price_obj = sizes[0].get("price", {})
                        print(f"[wb-variant] price_obj: {price_obj}")
                    else:
                        price_obj = {}
                    price_raw = price_obj.get("product") or price_obj.get("total", 0)
                    old_raw = price_obj.get("basic", 0)
                    if price_raw:
                        result["price"] = f"{price_raw // 100} ₽"
                    if old_raw and old_raw != price_raw:
                        result["old_price"] = f"{old_raw // 100} ₽"
                    if old_raw and price_raw and old_raw > price_raw:
                        result["discount"] = (
                            f"-{round((1 - price_raw / old_raw) * 100)}%"
                        )
                    result["rating"] = str(p.get("reviewRating", ""))
                    result["reviews"] = str(p.get("feedbacks", ""))
                    result["title"] = result["title"] or p.get("name", "")
                    # Наличие: stocks в sizes или totalQuantity
                    total_qty = p.get("totalQuantity", 0)
                    has_stocks = (
                        any(bool(s.get("stocks")) for s in sizes) if sizes else False
                    )
                    result["in_stock"] = bool(total_qty > 0 or has_stocks)
                    if not result["in_stock"]:
                        result["price"] = ""
                        result["old_price"] = ""
                        result["discount"] = ""
        except Exception as e:
            print(f"[wb-variant] price error: {e}")

        # Картинки
        for i in range(1, 11):
            img_url = f"{base}/images/big/{i}.webp"
            try:
                r = await client.head(img_url, headers=_wb_headers())
                if r.status_code == 200:
                    result["images"].append(img_url)
                else:
                    break
            except Exception:
                break

    return result


async def wb_product_detail(product_id: str, cookies: dict[str, str]) -> dict:
    """Получить описание и характеристики товара через API WB."""
    await _load_basket_map()
    result = {"description": "", "characteristics": {}, "images": [], "colors": []}

    pid = int(product_id)
    base = _wb_base_url(pid)
    card_url = f"{base}/info/ru/card.json"
    print(f"[wb-detail] pid={pid} base={base}")
    print(f"[wb-detail] card_url={card_url}")

    async with httpx.AsyncClient(
        verify=False,
        follow_redirects=True,
        timeout=15.0,
        http2=True,
    ) as client:
        # detail.json — описание + характеристики
        try:
            resp = await client.get(
                card_url,
                headers=_wb_headers(),
                cookies=cookies,
            )
            print(
                f"[wb-detail] card.json: {resp.status_code} | size={len(resp.content)}"
            )
            if resp.status_code == 200:
                card = response_json(resp)
                result["description"] = card.get("description", "")
                # Характеристики
                for group in card.get("grouped_options", []):
                    group_name = group.get("group_name", "")
                    for opt in group.get("options", []):
                        key = opt.get("name", "")
                        val = opt.get("value", "")
                        if key and val:
                            result["characteristics"][key] = val
                # Если нет grouped_options, пробуем options
                if not result["characteristics"]:
                    for opt in card.get("options", []):
                        key = opt.get("name", "")
                        val = opt.get("value", "")
                        if key and val:
                            result["characteristics"][key] = val

                # Варианты через cardVariants API (цвет + память + наличие)
                imt_id = card.get("imt_id", "")
                current_nm = card.get("nm_id", pid)
                if imt_id:
                    try:
                        cv_url = "https://www.wildberries.ru/__internal/meta/meta/ru/common/v5/search/cardVariants"
                        cv_params = {
                            "nmid": str(current_nm),
                            "imtid": str(imt_id),
                            "dest": "-1255987",
                        }
                        cv_headers = {
                            **_wb_headers(),
                            "Accept": "*/*",
                            "Sec-Fetch-Site": "same-origin",
                            "x-requested-with": "XMLHttpRequest",
                            "x-spa-version": "14.0.7",
                        }
                        cv_resp = await client.get(
                            cv_url,
                            params=cv_params,
                            headers=cv_headers,
                            cookies=cookies,
                        )
                        if cv_resp.status_code == 200:
                            cv_data = response_json(cv_resp).get("data", {})
                            filters = cv_data.get("filters", [])
                            result["variants"] = []
                            for f in filters:
                                group = {"name": f.get("name", ""), "items": []}
                                for item in f.get("items", []):
                                    nm = item.get("nmID", 0)
                                    nm_base = _wb_base_url(nm) if nm else ""
                                    group["items"].append(
                                        {
                                            "id": str(nm),
                                            "label": item.get("name", ""),
                                            "in_stock": item.get("count", 0) > 0,
                                            "selected": bool(item.get("selected")),
                                            "thumb": f"{nm_base}/images/tm/1.webp"
                                            if nm
                                            else "",
                                            "url": f"https://www.wildberries.ru/catalog/{nm}/detail.aspx"
                                            if nm
                                            else "",
                                        }
                                    )
                                result["variants"].append(group)
                            print(
                                f"[wb-detail] variants: {len(filters)} filters, {sum(len(g['items']) for g in result['variants'])} items"
                            )
                    except Exception as e:
                        print(f"[wb-detail] cardVariants error: {e}")

                # Fallback: colors из card.json
                if not result.get("variants"):
                    color_ids = card.get("colors", [])
                    for cid in color_ids:
                        cid = int(cid)
                        c_base = _wb_base_url(cid)
                        result["colors"].append(
                            {
                                "id": str(cid),
                                "thumb": f"{c_base}/images/tm/1.webp",
                                "url": f"https://www.wildberries.ru/catalog/{cid}/detail.aspx",
                                "active": cid == current_nm,
                            }
                        )
        except Exception as e:
            print(f"[wb-detail] card.json error: {e}")

        # Картинки — пробуем до 10
        for i in range(1, 11):
            img_url = f"{base}/images/big/{i}.webp"
            try:
                r = await client.head(img_url, headers=_wb_headers(), cookies=cookies)
                if r.status_code == 200:
                    result["images"].append(img_url)
                else:
                    break
            except Exception:
                break

    return result


def _feedback_photo_url(key: str) -> str:
    """Фото отзыва: key вида '4/uuid' → feedback-04.wbbasket.ru/uuid/ms.webp."""
    if not key:
        return ""
    parts = key.split("/", 1)
    if len(parts) == 2:
        shard = int(parts[0])
        uuid = parts[1]
        host = f"feedback-{shard:02d}.wbbasket.ru"
        return f"https://{host}/{uuid}/ms.webp"
    return ""


async def wb_get_reviews(imt_id: str, nm_id: str = "") -> dict:
    """
    Получить отзывы WB через feedbacks2.wb.ru.
    nm_id — если указан, фильтрует отзывы по конкретному варианту.
    """
    url = f"https://feedbacks2.wb.ru/feedbacks/v2/{imt_id}"
    headers = {
        "User-Agent": USER_AGENT,
        "Accept": "*/*",
        "Accept-Language": "en-US,en;q=0.9",
        "Sec-Ch-Ua": '"Not_A Brand";v="8", "Chromium";v="131", "Google Chrome";v="131"',
        "Sec-Ch-Ua-Mobile": "?0",
        "Sec-Ch-Ua-Platform": '"Windows"',
        "Sec-Fetch-Dest": "empty",
        "Sec-Fetch-Mode": "cors",
        "Sec-Fetch-Site": "cross-site",
        "Referer": "https://www.wildberries.ru/",
    }

    print(f"[wb-reviews] GET {url} (nm_id filter: {nm_id or 'none'})")
    async with httpx.AsyncClient(
        verify=False, timeout=15, follow_redirects=True
    ) as client:
        resp = await client.get(url, headers=headers)
        print(f"[wb-reviews] {resp.status_code} | size={len(resp.content)}")

        if resp.status_code != 200:
            return {"reviews": [], "valuation": "", "feedbackCount": 0}

        data = response_json(resp)
        nm_filter = int(nm_id) if nm_id else 0

        # Парсим отзывы
        reviews = []
        for fb in data.get("feedbacks") or []:
            # Фильтр по nmId варианта
            if nm_filter and fb.get("nmId") != nm_filter:
                continue

            photos = []
            for p in fb.get("photos", []):
                photo_url = _feedback_photo_url(p.get("key", ""))
                if photo_url:
                    photos.append(photo_url)

            reviews.append(
                {
                    "name": (fb.get("wbUserDetails") or {}).get("name", ""),
                    "rating": fb.get("productValuation", 0),
                    "date": fb.get("createdDate", ""),
                    "text": fb.get("text", ""),
                    "pros": fb.get("pros", ""),
                    "cons": fb.get("cons", ""),
                    "color": fb.get("color", ""),
                    "photos": photos,
                    "votes": fb.get("votes", {}),
                }
            )

        # Дистрибуция для конкретного варианта
        distribution = data.get("valuationDistributionPercent", {})
        valuation = data.get("valuation", "")
        total_count = data.get("feedbackCount", 0)
        if nm_filter:
            # Ищем дистрибуцию для конкретного nm
            for nv in data.get("nmValuationDistribution") or []:
                if nv.get("nm") == nm_filter:
                    distribution = nv.get("valuationDistributionPercent", distribution)
                    break
            total_count = len(reviews)

        return {
            "valuation": valuation,
            "feedbackCount": total_count,
            "distribution": distribution,
            "reviews": reviews,
        }


# ─────────────────────────────────────────────────────────────────────────────
# Основной flow
# ─────────────────────────────────────────────────────────────────────────────


async def get_valid_cookies(force_browser: bool = False) -> dict[str, str]:
    if not force_browser:
        saved = load_cookies()
        if saved:
            cookie_dict = cookies_to_dict(saved)
            print("[wb] Проверяем cookies...")
            if await httpx_check(cookie_dict):
                print("[wb] Cookies валидны!")
                return cookie_dict
            print("[wb] Cookies невалидны")

    cookies_list = await solve_challenge_browser()
    if not cookies_list:
        raise RuntimeError("Не удалось получить WB cookies")

    save_cookies(cookies_list)
    cookie_dict = cookies_to_dict(cookies_list)

    print("[wb] Проверяем новые cookies...")
    ok = await httpx_check(cookie_dict)
    if ok:
        print("[wb] Challenge пройден!")
    else:
        print("[wb] Cookies получены, но httpx блокируется")

    return cookie_dict


async def search_wb(query: str, cookies: dict[str, str], page: int = 1) -> list[dict]:
    # Загружаем basket map при первом вызове
    await _load_basket_map()

    print(f"[wb-search] query='{query}' page={page}")
    data = await wb_search_api(query, cookies, page)
    products = parse_wb_products(data)
    print(f"[wb-search] Найдено: {len(products)} товаров")
    return products


async def fetch_wb_product_details(product: dict, cookies: dict[str, str]) -> dict:
    pid = product.get("id", "")
    if not pid:
        return product
    print(f"  [wb-product] {product['title'][:50]}...", end=" ", flush=True)
    details = await wb_product_detail(pid, cookies)
    product["description"] = details["description"]
    product["characteristics"] = details["characteristics"]
    if details["images"]:
        product["images"] = details["images"]
    print(
        f"ok (desc={len(details['description'])}, chars={len(details['characteristics'])}, imgs={len(details['images'])})"
    )
    return product


# ─────────────────────────────────────────────────────────────────────────────
# CLI
# ─────────────────────────────────────────────────────────────────────────────


async def main():
    cookies = await get_valid_cookies()

    products = await search_wb("конфеты шоколадные", cookies)

    if products:
        for i, p in enumerate(products[:10], 1):
            print(f"\n  {i}. {p['title'][:70]} [{p['brand']}]")
            old = f"(было {p['old_price']})" if p["old_price"] else ""
            print(f"     {p['price']} {old} {p['discount']}")
            print(f"     {p['rating']} / {p['reviews']} отзывов")

        # Описание первого товара
        print("\n[wb] Загружаем описание первого товара...")
        await fetch_wb_product_details(products[0], cookies)
        p = products[0]
        if p.get("description"):
            print(f"\n  Описание: {p['description'][:300]}...")
        if p.get("characteristics"):
            print(f"  Характеристики ({len(p['characteristics'])}):")
            for k, v in list(p["characteristics"].items())[:5]:
                print(f"    {k}: {v}")

        with open("wb_products.json", "w", encoding="utf-8") as f:
            json.dump(products, f, ensure_ascii=False, indent=2)
        print("\n[wb] Сохранено в wb_products.json")
    else:
        print("[wb] Товары не найдены")


if __name__ == "__main__":
    uc.loop().run_until_complete(main())
