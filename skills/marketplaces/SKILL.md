---
name: marketplaces
description: Use the local Marketplaces MCP tools to search Wildberries/Ozon, fetch product-card details and photos, and retrieve reviews. Trigger when the user asks to find products, inspect a WB/Ozon card, see item photos, continue marketplace pages, or read reviews.
---

# Marketplaces

Use the `marketplaces` MCP server when a task needs marketplace data from
Wildberries or Ozon.

Available tool intent:

- `marketplace_search`: paginated search. Call again with `next_page` to keep browsing.
- `marketplace_product`: product-card details, characteristics, variants, and photos.
- `marketplace_reviews`: WB reviews by `imt_id`/`nm_id` or Ozon reviews by product URL.
- `marketplace_refresh_cookies`: explicitly refresh anti-bot cookies through browser challenge.
- `marketplace_status`: local plugin/dependency/cookie status.

Prefer passing the exact `source` (`wb`, `ozon`, or `all`) when the user names a
marketplace. For WB reviews, pass `imtId` from search results when available.
For Ozon reviews, pass the product URL returned by search.
