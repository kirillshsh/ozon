# ozon_challenge_solver.py — nodriver + httpx
# pip install nodriver httpx
import asyncio
import json
import re
import time
import httpx
import nodriver as uc

# ─────────────────────────────────────────────────────────────────────────────
# Конфигурация
# ─────────────────────────────────────────────────────────────────────────────
TARGET_URL = "https://www.ozon.ru/"
COOKIES_FILE = "cookies.json"
CHALLENGE_TIMEOUT = 30  # секунды
HEADLESS = False  # True = без окна (может хуже проходить)

# UA синхронизирован между браузером и httpx
USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/131.0.0.0 Safari/537.36"
)

RELEVANT_COOKIE_NAMES = {
    "abt_att",
    "__Secure-access-token",
    "sess_id",
    "SSRT",
    "xcid",
    "__abt",
    "__Secure-ab-group",
}


# ─────────────────────────────────────────────────────────────────────────────
# Утилиты
# ─────────────────────────────────────────────────────────────────────────────


def is_challenge_page(html: str) -> bool:
    """Определяем challenge-страницу по характерным маркерам."""
    markers = ("challenge-data", "AntiBot Challenge", "runChallenge", "/abt/result")
    return any(m in html for m in markers)


def load_cookies(path: str = COOKIES_FILE) -> list[dict] | None:
    """Загрузить cookies из файла (формат list[dict])."""
    try:
        with open(path, encoding="utf-8") as f:
            cookies = json.load(f)
        print(f"[*] Cookies загружены из {path} ({len(cookies)} шт.)")
        return cookies
    except FileNotFoundError:
        print(f"[*] Файл {path} не найден, продолжаем без cookies")
        return None


def save_cookies(cookies: list[dict], path: str = COOKIES_FILE):
    """Сохранить cookies в файл."""
    with open(path, "w", encoding="utf-8") as f:
        json.dump(cookies, f, ensure_ascii=False, indent=2)
    print(f"[*] Cookies сохранены в {path} ({len(cookies)} шт.)")


def cookies_to_dict(cookies: list[dict]) -> dict[str, str]:
    """Конвертировать list[dict] cookies в простой dict для httpx."""
    # При дубликатах берём cookie с домена .ozon.ru (не .ozone.ru)
    result = {}
    for c in cookies:
        name = c["name"]
        domain = c.get("domain", "")
        # Если уже есть — перезаписываем только если домен .ozon.ru
        if name in result and ".ozone." in domain:
            continue
        result[name] = c["value"]
    return result


# ─────────────────────────────────────────────────────────────────────────────
# Шаг 1: Решить challenge через nodriver
# ─────────────────────────────────────────────────────────────────────────────


async def solve_challenge_browser(url: str = TARGET_URL) -> list[dict] | None:
    """
    Открываем Chrome через nodriver, ждём решения challenge,
    возвращаем cookies.
    """
    print(f"\n{'=' * 60}")
    print("[browser] Запускаем Chrome через nodriver...")
    print(f"{'=' * 60}")

    browser = await uc.start(
        headless=HEADLESS,
        browser_args=[
            "--disable-dev-shm-usage",
            "--lang=ru-RU",
        ],
    )

    tab = await browser.get(url)
    print(f"[browser] Переходим на {url}")

    # Ждём пока challenge решится (title изменится или пройдёт редирект)
    start = time.time()
    solved = False

    while time.time() - start < CHALLENGE_TIMEOUT:
        await tab.sleep(1)

        # Проверяем title
        try:
            title = await tab.evaluate("document.title")
        except Exception:
            title = ""

        # Проверяем HTML на наличие challenge-маркеров
        try:
            html_snippet = await tab.evaluate(
                "document.documentElement.innerHTML.substring(0, 2000)"
            )
        except Exception:
            html_snippet = ""

        current_url = tab.target.url or ""
        elapsed = time.time() - start

        # Успех: title не содержит "challenge"/"ограничен" И нет маркеров challenge
        if title and "Antibot" not in title and "ограничен" not in title.lower():
            if not is_challenge_page(html_snippet):
                print(f"[browser] Challenge пройден за {elapsed:.1f}s")
                print(f"[browser] Title: {title}")
                print(f"[browser] URL: {current_url}")
                solved = True
                break

        # Проверяем статус run-status
        try:
            status = await tab.evaluate(
                "document.getElementById('run-status')?.textContent || ''"
            )
            if status:
                print(f"[browser] run-status: {status} ({elapsed:.1f}s)")
        except Exception:
            pass

    if not solved:
        # Последняя попытка — может title уже ок, но мы не заметили
        try:
            title = await tab.evaluate("document.title")
            current_url = tab.target.url or ""
            print(f"[browser] Таймаут. Title: {title}, URL: {current_url}")
        except Exception:
            print("[browser] Таймаут. Не удалось получить состояние страницы.")

    # Забираем cookies в любом случае
    all_cookies = await browser.cookies.get_all()
    print(f"[browser] Получено cookies: {len(all_cookies)}")

    # Конвертируем в сериализуемый формат
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
    print(f"[browser] Ключевые cookies: {[c['name'] for c in relevant]}")

    # Скриншот для отладки
    try:
        await tab.save_screenshot("result.png")
        print("[browser] Скриншот: result.png")
    except Exception:
        pass

    browser.stop()

    if not cookies_list:
        print("[browser] Cookies пусты, challenge скорее всего не пройден")
        return None

    return cookies_list


# ─────────────────────────────────────────────────────────────────────────────
# Шаг 2: Проверка / работа через httpx
# ─────────────────────────────────────────────────────────────────────────────


async def httpx_check(cookies: dict[str, str], url: str = TARGET_URL) -> bool:
    """
    Проверяем доступность с имеющимися cookies через curl_cffi.
    Возвращает True если страница доступна без challenge.
    """
    resp = await httpx_get(url, cookies)
    is_blocked = is_challenge_page(resp.text)
    title_match = None
    m = re.search(r"<title>(.*?)</title>", resp.text, re.IGNORECASE)
    if m:
        title_match = m.group(1)

    print(
        f"[check] {resp.status_code} | challenge: {is_blocked} | title: {title_match}"
    )
    return not is_blocked and resp.status_code == 200


class _CurlResponse:
    """Обёртка curl_cffi response чтобы выглядел как httpx.Response."""

    def __init__(self, r):
        self.status_code = r.status_code
        self.text = r.text
        self.content = r.content
        self.headers = r.headers
        self._r = r

    def json(self):
        import json

        return json.loads(self.text)


async def httpx_get(
    url: str,
    cookies: dict[str, str],
    referer: str = "",
) -> "_CurlResponse":
    """GET-запрос через curl_cffi с TLS fingerprint Chrome."""
    from curl_cffi.requests import AsyncSession

    headers = {
        "User-Agent": USER_AGENT,
        "Accept": (
            "text/html,application/xhtml+xml,application/xml;"
            "q=0.9,image/avif,image/webp,image/apng,*/*;"
            "q=0.8,application/signed-exchange;v=b3;q=0.7"
        ),
        "Accept-Language": "ru-RU,ru;q=0.9,en-US;q=0.8,en;q=0.7",
        "Accept-Encoding": "gzip, deflate, br",
        "Sec-Ch-Ua": '"Not_A Brand";v="8", "Chromium";v="131", "Google Chrome";v="131"',
        "Sec-Ch-Ua-Mobile": "?0",
        "Sec-Ch-Ua-Platform": '"Windows"',
        "Sec-Fetch-Dest": "document",
        "Sec-Fetch-Mode": "navigate",
        "Sec-Fetch-Site": "same-origin" if referer else "none",
        "Sec-Fetch-User": "?1",
        "Upgrade-Insecure-Requests": "1",
    }
    if referer:
        headers["Referer"] = referer

    async with AsyncSession(impersonate="chrome131") as s:
        resp = await s.get(
            url,
            headers=headers,
            cookies=cookies,
            allow_redirects=True,
            timeout=30,
        )
        return _CurlResponse(resp)


# ─────────────────────────────────────────────────────────────────────────────
# Основной flow
# ─────────────────────────────────────────────────────────────────────────────


async def get_valid_cookies(force_browser: bool = False) -> dict[str, str]:
    """
    Возвращает валидные cookies для httpx.
    1. Пробуем загрузить из файла
    2. Проверяем через httpx
    3. Если не работают — решаем challenge через nodriver
    """
    if not force_browser:
        saved = load_cookies()
        if saved:
            cookie_dict = cookies_to_dict(saved)
            print("\n[*] Проверяем сохранённые cookies через httpx...")
            if await httpx_check(cookie_dict):
                print("[ok] Cookies валидны!")
                return cookie_dict
            print("[!] Cookies невалидны, нужен новый challenge")

    # Решаем challenge через браузер
    cookies_list = await solve_challenge_browser()
    if not cookies_list:
        raise RuntimeError("Не удалось получить cookies из браузера")

    # Сохраняем
    save_cookies(cookies_list)

    # Проверяем
    cookie_dict = cookies_to_dict(cookies_list)
    print("\n[*] Проверяем новые cookies через httpx...")
    ok = await httpx_check(cookie_dict)
    if ok:
        print("[ok] Challenge пройден, cookies работают через httpx!")
    else:
        print("[!] Cookies получены, но httpx всё ещё блокируется")
        print("    Возможные причины: TLS fingerprint, IP, недостаточные cookies")

    return cookie_dict


# ─────────────────────────────────────────────────────────────────────────────
# Шаг 3: Парсинг результатов поиска Ozon
# ─────────────────────────────────────────────────────────────────────────────


def parse_ozon_products(html: str) -> list[dict]:
    """
    Извлечь товары из HTML страницы поиска Ozon.
    Ozon встраивает JSON в data-state атрибуты виджетов.
    """
    products = []

    # Способ 1: data-state атрибуты виджетов (основной)
    # Ozon использует как двойные, так и одинарные кавычки
    state_blocks = re.findall(r"data-state='([^']+)'", html)
    state_blocks += re.findall(r'data-state="([^"]+)"', html)
    for block_escaped in state_blocks:
        try:
            block_str = (
                block_escaped.replace("&quot;", '"')
                .replace("&amp;", "&")
                .replace("&lt;", "<")
                .replace("&gt;", ">")
            )
            block = json.loads(block_str)
            _extract_products_from_state(block, products)
        except (json.JSONDecodeError, KeyError, TypeError):
            continue

    # Способ 2: JSON в script тегах
    if not products:
        for m in re.finditer(r"<script[^>]*>\s*(\{.+?\})\s*</script>", html, re.DOTALL):
            try:
                data = json.loads(m.group(1))
                _deep_find_products(data, products)
            except (json.JSONDecodeError, KeyError):
                continue

    # Способ 3: fallback по ссылкам /product/
    if not products:
        products = _parse_products_regex(html)

    # Дедупликация
    seen = set()
    unique = []
    for p in products:
        key = p.get("title", "") or p.get("url", "")
        if key and key not in seen:
            seen.add(key)
            unique.append(p)

    return unique


def _extract_products_from_state(data: dict, out: list[dict]):
    """Извлечь товары из state-объекта виджета Ozon (формат 2025+)."""
    items = data.get("items") or data.get("products") or []
    if not items:
        return

    # Фильтруем: нужны items с mainState (товары), а не меню
    product_items = [it for it in items if isinstance(it, dict) and "mainState" in it]
    if not product_items:
        return

    for item in product_items:
        main_state = item.get("mainState", [])

        # --- Название: ищем textAtom с id="name" ---
        title = ""
        for block in main_state:
            if isinstance(block, dict):
                if block.get("id") == "name":
                    title = (block.get("textAtom") or {}).get("text", "")
                    break
                # textAtom без id но с длинным текстом
                if block.get("type") == "textAtom" and not title:
                    t = (block.get("textAtom") or {}).get("text", "")
                    if len(t) > 15 and "₽" not in t:
                        title = t

        # --- Цена: ищем priceV2 ---
        price = ""
        old_price = ""
        discount = ""
        for block in main_state:
            if isinstance(block, dict) and block.get("type") == "priceV2":
                pv2 = block.get("priceV2", {})
                price_list = pv2.get("price", [])
                for p in price_list:
                    if p.get("textStyle") == "PRICE":
                        price = p.get("text", "")
                    elif p.get("textStyle") == "ORIGINAL_PRICE":
                        old_price = p.get("text", "")
                discount = pv2.get("discount", "")
                break

        # --- Рейтинг / отзывы: labelList с tile-list-rating / tile-list-comments ---
        rating = ""
        reviews = ""
        for block in main_state:
            if isinstance(block, dict) and block.get("type") == "labelList":
                for label_item in (block.get("labelList") or {}).get("items", []):
                    auto_id = (label_item.get("testInfo") or {}).get(
                        "automatizationId", ""
                    )
                    if auto_id == "tile-list-rating":
                        rating = label_item.get("title", "").strip()
                    elif auto_id == "tile-list-comments":
                        raw = label_item.get("title", "")
                        reviews = re.sub(r"[^\d\s]", "", raw).strip()

        # --- ID ---
        product_id = item.get("id", "") or item.get("sku", "")

        # --- URL ---
        link = (item.get("action") or {}).get("link", "")
        if link and not link.startswith("http"):
            link = "https://www.ozon.ru" + link

        # --- Бренд ---
        brand = ""
        for block in main_state:
            if isinstance(block, dict) and block.get("type") == "labelList":
                auto_id = ((block.get("labelList") or {}).get("testInfo") or {}).get(
                    "automatizationId", ""
                )
                if auto_id == "tile-list-labels":
                    for li in (block.get("labelList") or {}).get("items", []):
                        b = li.get("title", "")
                        # Убираем HTML-теги (<b>Бренд</b>)
                        brand = re.sub(r"<[^>]+>", "", b).strip()
                        break

        # --- Картинка (первая из tileImage.items) ---
        image = ""
        tile_img = item.get("tileImage") or {}
        for img_item in tile_img.get("items", []):
            if isinstance(img_item, dict) and img_item.get("type") == "image":
                image = (img_item.get("image") or {}).get("link", "")
                if image:
                    break

        if title or product_id:
            out.append(
                {
                    "id": str(product_id),
                    "title": str(title).strip(),
                    "brand": brand,
                    "price": price,
                    "old_price": old_price,
                    "discount": discount,
                    "rating": rating,
                    "reviews": reviews,
                    "url": link,
                    "image": image,
                }
            )


def _deep_find_products(data, out: list[dict], depth: int = 0):
    """Рекурсивный поиск товаров в JSON."""
    if depth > 8:
        return
    if isinstance(data, dict):
        if (
            "items" in data
            and isinstance(data["items"], list)
            and len(data["items"]) > 2
        ):
            _extract_products_from_state(data, out)
        for v in data.values():
            _deep_find_products(v, out, depth + 1)
    elif isinstance(data, list):
        for item in data:
            _deep_find_products(item, out, depth + 1)


def _parse_products_regex(html: str) -> list[dict]:
    """Fallback: парсим ссылки /product/ из HTML."""
    products = []
    seen = set()

    for m in re.finditer(r'href="(/product/[^"]+)"[^>]*>([^<]{5,150})<', html):
        link, title = m.group(1), m.group(2).strip()
        url = "https://www.ozon.ru" + link.split("?")[0]
        if url in seen:
            continue
        seen.add(url)

        # Цена поблизости
        idx = m.start()
        nearby = html[idx : idx + 2000]
        pm = re.search(r"(\d[\d\s\u2009]*₽)", nearby)
        price = pm.group(1).strip() if pm else ""

        products.append(
            {
                "id": "",
                "title": title,
                "price": price,
                "rating": "",
                "reviews": "",
                "url": url,
            }
        )

    return products


async def search_ozon(query_url: str, cookies: dict[str, str]) -> list[dict]:
    """Загрузить страницу поиска Ozon через httpx и спарсить товары."""
    print(f"\n[search] GET {query_url[:100]}...")
    resp = await httpx_get(query_url, cookies)
    print(f"[search] Status: {resp.status_code} | Size: {len(resp.text)}")

    if is_challenge_page(resp.text):
        print("[search] Challenge! Cookies протухли.")
        return []

    m = re.search(r"<title>(.*?)</title>", resp.text, re.IGNORECASE)
    if m:
        print(f"[search] Title: {m.group(1)}")

    # Сохраняем HTML для отладки
    with open("search_result.html", "w", encoding="utf-8") as f:
        f.write(resp.text)
    print("[search] HTML сохранён в search_result.html")

    products = parse_ozon_products(resp.text)
    print(f"[search] Найдено товаров: {len(products)}")
    return products


def print_products(products: list[dict], limit: int = 20):
    """Вывод товаров в консоль."""
    print(f"\n{'=' * 70}")
    print(f" Результаты ({len(products)} товаров)")
    print(f"{'=' * 70}")
    for i, p in enumerate(products[:limit], 1):
        title = p["title"][:90]
        brand = f" [{p['brand']}]" if p.get("brand") else ""
        print(f"\n  {i}. {title}{brand}")
        parts = []
        if p.get("price"):
            parts.append(p["price"])
        if p.get("old_price"):
            parts.append(f"(было {p['old_price']})")
        if p.get("discount"):
            parts.append(p["discount"])
        if parts:
            print(f"     Цена: {' '.join(parts)}")
        if p.get("rating") or p.get("reviews"):
            print(
                f"     Рейтинг: {p.get('rating', '-')}  Отзывы: {p.get('reviews', '-')}"
            )
        if p.get("url"):
            print(f"     {p['url'][:100]}")
    if len(products) > limit:
        print(f"\n  ... и ещё {len(products) - limit}")
    print()


# ─────────────────────────────────────────────────────────────────────────────
# Шаг 4: Парсинг карточки товара (описание)
# ─────────────────────────────────────────────────────────────────────────────


def parse_product_description(html: str) -> dict:
    """
    Извлечь описание и характеристики со страницы товара Ozon.
    """
    result = {"description": "", "characteristics": {}, "images": []}

    # Собираем все data-state блоки (оба формата кавычек)
    state_blocks = re.findall(r"data-state='([^']+)'", html)
    state_blocks += re.findall(r'data-state="([^"]+)"', html)

    for block_escaped in state_blocks:
        block_str = (
            block_escaped.replace("&quot;", '"')
            .replace("&amp;", "&")
            .replace("&lt;", "<")
            .replace("&gt;", ">")
        )
        try:
            block = json.loads(block_str)
        except (json.JSONDecodeError, TypeError):
            continue
        _extract_description_from_state(block, result)

    # Также ищем в __NUXT__.state
    m = re.search(
        r"window\.__NUXT__\.state='(.*?)';window\.__NUXT__\.",
        html,
        re.DOTALL,
    )
    if m:
        try:
            state = json.loads(m.group(1).replace("\\'", "'").replace('\\\\"', '\\"'))
            _deep_find_description(state, result)
        except (json.JSONDecodeError, TypeError):
            pass

    return result


def _extract_description_from_state(data: dict, result: dict):
    """Извлечь описание/характеристики из state-блока."""
    # --- Описание (webDescription / richAnnotation / description) ---
    desc = ""

    # richAnnotation формат: содержит массив content с текстовыми блоками
    if "content" in data and isinstance(data["content"], list):
        parts = []
        for block in data["content"]:
            if isinstance(block, dict):
                # type: "text", "htmlText", "paragraph" и т.д.
                text = (
                    block.get("text", "")
                    or block.get("content", "")
                    or block.get("htmlText", "")
                )
                if isinstance(text, str) and text.strip():
                    parts.append(re.sub(r"<[^>]+>", "", text).strip())
                # Вложенные children
                for child in block.get("children", []):
                    if isinstance(child, dict):
                        t = child.get("text", "") or child.get("value", "")
                        if t:
                            parts.append(re.sub(r"<[^>]+>", "", t).strip())
        if parts:
            desc = "\n".join(parts)

    # description как строка
    if not desc:
        for key in ("description", "webDescription", "shortDescription", "plainText"):
            val = data.get(key, "")
            if isinstance(val, str) and len(val) > 20:
                desc = re.sub(r"<[^>]+>", "", val).strip()
                break

    if desc and len(desc) > len(result["description"]):
        result["description"] = desc

    # --- Характеристики (Ozon 2025+) ---
    # Формат: characteristics: [{title: {textRs: [{content}]}, values: [{text}]}]
    chars = data.get("characteristics") or []
    if isinstance(chars, list):
        for item in chars:
            if not isinstance(item, dict):
                continue
            # Название из title.textRs[0].content
            title_obj = item.get("title") or {}
            text_rs = title_obj.get("textRs") or []
            char_name = ""
            for rs in text_rs:
                if isinstance(rs, dict) and rs.get("content"):
                    char_name = rs["content"]
                    break
            # Fallback: title как строка
            if not char_name:
                char_name = (
                    item.get("title", "") if isinstance(item.get("title"), str) else ""
                )
            # Значения из values[].text
            values = item.get("values") or []
            val_parts = []
            for v in values:
                if isinstance(v, dict) and v.get("text"):
                    val_parts.append(v["text"])
            char_value = ", ".join(val_parts)
            if char_name and char_value:
                result["characteristics"][char_name] = char_value

    # --- Картинки ---
    # coverImage + images (формат карточки товара)
    cover = data.get("coverImage")
    if isinstance(cover, dict):
        url = cover.get("src", "") or cover.get("link", "") or cover.get("url", "")
        if url and url not in result["images"]:
            result["images"].append(url)
    gallery = data.get("images") or data.get("gallery") or []
    if isinstance(gallery, list):
        for img in gallery:
            url = ""
            if isinstance(img, dict):
                url = img.get("src", "") or img.get("url", "") or img.get("link", "")
            elif isinstance(img, str):
                url = img
            if url and url not in result["images"]:
                result["images"].append(url)


def _deep_find_description(data, result: dict, depth: int = 0):
    """Рекурсивный поиск описания в JSON."""
    if depth > 6:
        return
    if isinstance(data, dict):
        _extract_description_from_state(data, result)
        # Ищем вложенные строки-JSON
        for v in data.values():
            if isinstance(v, str) and len(v) > 100 and "description" in v.lower():
                try:
                    nested = json.loads(v)
                    _extract_description_from_state(nested, result)
                except (json.JSONDecodeError, TypeError):
                    pass
            _deep_find_description(v, result, depth + 1)
    elif isinstance(data, list):
        for item in data[:30]:
            _deep_find_description(item, result, depth + 1)


async def fetch_product_details(
    product: dict,
    cookies: dict[str, str],
    save_html: bool = False,
) -> dict:
    """
    Загрузить страницу товара и извлечь описание + характеристики.
    """
    url = product.get("url", "")
    if not url:
        return product

    print(f"  [product] {product['title'][:50]}...", end=" ", flush=True)

    resp = await httpx_get(url, cookies)

    if is_challenge_page(resp.text):
        print("BLOCKED")
        return product

    if save_html:
        pid = product.get("id", "unknown")
        with open(f"product_{pid}.html", "w", encoding="utf-8") as f:
            f.write(resp.text)

    details = parse_product_description(resp.text)
    product["description"] = details["description"]
    product["characteristics"] = details["characteristics"]
    if details["images"]:
        product["images"] = details["images"]

    desc_len = len(details["description"])
    chars_count = len(details["characteristics"])
    print(f"ok (desc={desc_len}, chars={chars_count})")

    return product


async def enrich_products(
    products: list[dict],
    cookies: dict[str, str],
    limit: int = 5,
    delay: float = 1.0,
    save_first_html: bool = True,
) -> list[dict]:
    """
    Обогатить товары описаниями с карточек.
    limit — сколько товаров обработать (чтобы не перегружать).
    """
    print(f"\n[enrich] Загружаем описания для {min(limit, len(products))} товаров...")

    for i, product in enumerate(products[:limit]):
        save_html = save_first_html and i == 0
        await fetch_product_details(product, cookies, save_html=save_html)
        if i < limit - 1:
            await asyncio.sleep(delay)

    return products


async def main():
    cookies = await get_valid_cookies()

    # Поиск «конфеты шоколадные»
    search_url = (
        "https://www.ozon.ru/category/konfety-30695/"
        "?category_was_predicted=true"
        "&deny_category_prediction=true"
        "&from_global=true"
        "&text=%D0%BA%D0%BE%D0%BD%D1%84%D0%B5%D1%82%D1%8B"
        "+%D1%88%D0%BE%D0%BA%D0%BE%D0%BB%D0%B0%D0%B4%D0%BD%D1%8B%D0%B5"
    )

    products = await search_ozon(search_url, cookies)

    # Если httpx не отдал товары — пробуем через браузер
    if not products:
        print("\n[!] httpx не вернул товары, пробуем через браузер...")
        browser = await uc.start(headless=HEADLESS)
        tab = await browser.get(search_url)
        await tab.sleep(5)
        html = await tab.evaluate("document.documentElement.outerHTML")
        with open("search_result_browser.html", "w", encoding="utf-8") as f:
            f.write(str(html))
        print("[browser] HTML сохранён в search_result_browser.html")
        products = parse_ozon_products(str(html))
        print(f"[browser] Найдено товаров: {len(products)}")
        browser.stop()

    if products:
        print_products(products)

        # Обогащаем описаниями (первые 5 товаров, первый HTML сохраняется)
        await enrich_products(products, cookies, limit=5, save_first_html=True)

        # Выводим описания
        print(f"\n{'=' * 70}")
        print(" Описания товаров")
        print(f"{'=' * 70}")
        for i, p in enumerate(products[:5], 1):
            print(f"\n  {i}. {p['title'][:80]}")
            if p.get("description"):
                # Первые 300 символов описания
                desc = p["description"][:300]
                print(
                    f"     Описание: {desc}{'...' if len(p['description']) > 300 else ''}"
                )
            else:
                print("     Описание: --")
            if p.get("characteristics"):
                print(f"     Характеристики ({len(p['characteristics'])} шт.):")
                for k, v in list(p["characteristics"].items())[:5]:
                    print(f"       {k}: {v}")
        print()

        with open("products.json", "w", encoding="utf-8") as f:
            json.dump(products, f, ensure_ascii=False, indent=2)
        print("[*] Результаты сохранены в products.json")
    else:
        print("[!] Товары не найдены. Проверь search_result.html")


if __name__ == "__main__":
    uc.loop().run_until_complete(main())
