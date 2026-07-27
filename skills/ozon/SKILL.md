---
name: ozon
description: Use the local Ozon MCP tools to search ozon.ru, open full product cards, read buyer reviews (with the actual review photos) and product Q&A, browse the catalogue, and check the user's own Ozon orders and favourites. Trigger whenever the user asks to find something on Ozon, compare Ozon products, check a price or its Ozon Card price, read what buyers say about an item, look at real photos from reviews, or see their Ozon orders.
---

# Ozon

Read-only access to ozon.ru through the logged-in browser session saved on this
machine. Nothing writes to the account: no cart, no orders, no favourites edits,
no posting reviews or questions.

## Tools

- `ozon_search` — paginated search. Returns tiles with url, price, old price,
  rating, review count and the delivery date. Filters: `sort`, `price_min`,
  `price_max`, `brand`, plus any key from the response's `available_filters`.
- `ozon_catalog` — the category tree (no arguments) or the products inside one
  category, with the same filters as search.
- `ozon_product` — one full card by url or product id: Ozon Card price, regular
  price, crossed-out price, stock, all characteristics, description, brand,
  seller with rating, every photo and video URL, and the colour/size variants.
- `ozon_reviews` — buyer reviews with `min_rating` / `max_rating` / `media`
  filters and sorting. Returns text, rating, date, author and photo URLs.
- `ozon_review_media` — downloads review photos and returns them as images.
- `ozon_questions` — buyer questions with the seller's answers.
- `ozon_orders`, `ozon_favorites` — the user's own data, read-only.
- `ozon_status` — which account the session belongs to and whether it is alive.
- `ozon_refresh_cookies` — opens a browser window for the user to log in.

## How to use them

Start with `ozon_search`, then pass a returned `url` straight to
`ozon_product` / `ozon_reviews` / `ozon_questions` — they all take the same url.

Prices: the headline price on Ozon is **the price with an Ozon Card**. That is
what search tiles show and what Ozon's own price filter matches. A product card
also returns `price` (regular) and `price_original` (crossed out) — quote the
right one for the question being asked.

Photos are never attached unless asked for. Use `ozon_review_media` when real
buyer photos matter (true colour, size, build quality, does it match the
listing), and `ozon_product(include_images=true)` for catalogue shots. Keep the
limit small — every image costs context.

When a tool returns `error: session_expired`, tell the user to run
`ozon_refresh_cookies` and that they will have to log in in the browser window
themselves — do not retry the failing tool first.

Do not use these tools to buy anything, change the cart, or post reviews or
questions; the server has no such capability by design.
