---
name: ozon
description: Use the local Ozon MCP tools to search ozon.ru, open full product cards with size charts for clothing, read buyer reviews (with the actual review photos) and product Q&A, browse the catalogue, check the user's own Ozon orders and favourites, see what is in their cart and put a product into it. Trigger whenever the user asks to find something on Ozon, compare Ozon products, check a price or its Ozon Card price, read what buyers say about an item, look at real photos from reviews, see their Ozon orders, pick a size for clothing or shoes, or add something to their Ozon cart.
---

# Ozon

Access to ozon.ru through the logged-in browser session saved on this machine.
Everything is read-only except the cart and favourites add/remove tools;
ordering and posting reviews or questions are not possible.

## Tools

- `ozon_search` — paginated search. Returns tiles with url, price, rating,
  review count and the delivery date. Filters: `sort`, `price_min`,
  `price_max`, `brand`, plus any key from the response's `available_filters`.
- `ozon_catalog` — the category tree (no arguments) or the products inside one
  category, with the same filters as search.
- `ozon_product` — one full card by url or product id: price, stock, all
  characteristics, description, brand, seller with rating, every photo and
  video URL, the colour/size variants and, for clothing and shoes, the size
  chart (`size_table`).
- `ozon_reviews` — buyer reviews with `min_rating` / `max_rating` / `media`
  filters and sorting. Returns text, rating, date, author and photo URLs.
- `ozon_review_media` — downloads review photos and returns them as images.
- `ozon_questions` — buyer questions with the seller's answers.
- `ozon_orders`, `ozon_favorites`, `ozon_cart` — the user's own data, read-only.
- `ozon_cart_add` / `ozon_cart_remove` — put a product in the user's real cart
  or take it out.
- `ozon_favorites_add` / `ozon_favorites_remove` — add a product to the user's
  favourites or remove it. These four are the only tools that change anything.
- `ozon_status` — which account the session belongs to and whether it is alive.
- `ozon_refresh_cookies` — opens a browser window for the user to log in.

## How to use them

Start with `ozon_search`, then pass a returned `url` straight to
`ozon_product` / `ozon_reviews` / `ozon_questions` — they all take the same url.

Prices: every `price` these tools return is **the price with an Ozon Card** —
what Ozon headlines and what its own price filter matches. Crossed-out and
no-card prices are not reported, so there is only ever one number to quote.

Clothing and shoes: `variants` with `key: "size"` lists the sizes, and only a
variant with `availability: "inStock"` can be bought — check that before
recommending one, and pass that variant's own url or SKU to `ozon_cart_add`.
`size_table` maps the labels to body measurements in cm; its first row is the
size labels and the rest are measurements, with the seller's measuring
instructions in `notes`. Some sellers upload the chart as a picture instead —
then `size_table.images` holds the URLs and `tables` is empty, so read them
with `ozon_review_media(image_urls=[...])` rather than guessing the numbers.
Ask the user for their measurements or usual RU size before choosing.

Photos are never attached unless asked for. Use `ozon_review_media` when real
buyer photos matter (true colour, size, build quality, does it match the
listing), and `ozon_product(include_images=true)` for catalogue shots. Keep the
limit small — every image costs context.

When a tool returns `error: session_expired`, tell the user to run
`ozon_refresh_cookies` and that they will have to log in in the browser window
themselves — do not retry the failing tool first.

Ask the user before calling any cart or favourites add/remove tool — they
change the real account. `ozon_cart_add`'s
`quantity` is absolute, not an increment, and for a product with colour/size
variants pass the exact variant url or SKU from `ozon_product`'s `variants`.
It never buys anything: no order is placed and no money moves, and the server
cannot check out, order, or post reviews or questions at all.
