# Ozon MCP

Local MCP plugin that gives an agent a shopper's read-only view of ozon.ru.

> **Disclaimer.** Unofficial. It reads Ozon's public storefront through its
> undocumented internal endpoints — not a partner/seller API, not affiliated with
> or endorsed by Ozon. Automated access may violate Ozon's Terms of Service and
> can lead to rate-limiting or account/IP blocks. Personal and educational use,
> at your own risk. Ozon changes its widget formats without notice, so tools can
> break at any time.

## What it does

- paginated **search** and **catalogue** browsing with price, brand and other
  Ozon filters, plus sorting
- **full product cards**: Ozon Card price, regular price, crossed-out price,
  stock, every characteristic, description and rich-content images, brand,
  breadcrumbs, seller with rating and order count, all photo/video URLs, and the
  colour/size/volume variants with their own SKU, price and url
- **reviews** with rating and media filters, seller-reply counts, and every photo
  and video URL; `ozon_review_media` downloads the photos and returns them as
  images (downscaled, hard-capped per call)
- **questions and answers**, including which answers came from the seller
- **your own orders and favourites**, read-only

There is no cart, no checkout, no favouriting, no review or question posting.
The server has no write path at all.

## Tools

| tool | purpose |
| --- | --- |
| `ozon_search` | search with paging, sorting and filters |
| `ozon_catalog` | category tree, or products in one category |
| `ozon_product` | one full product card |
| `ozon_reviews` | buyer reviews, filtered by stars and media |
| `ozon_review_media` | review photos as viewable images |
| `ozon_questions` | product Q&A with seller answers |
| `ozon_orders` | your orders and their statuses (read-only) |
| `ozon_favorites` | your favourites (read-only) |
| `ozon_status` | which account the session belongs to, and is it alive |
| `ozon_location` | the delivery city prices refer to |
| `ozon_refresh_cookies` | opens a browser window for you to log in |

Every tool carries a detailed docstring the agent reads as its description.

## Install

Works on macOS, Windows and Linux. You need **Node 18+**, **Python 3.10+** and
any Chromium browser (Chrome, Edge, Brave, Chromium).

```bash
npm install -g github:kirillshsh/ozon
ozon-install
```

To update later, run the same two commands again. From a git checkout instead:
`npm run install:local`.

The installer copies the plugin into `~/plugins/ozon` (on Windows
`C:\Users\<you>\plugins\ozon`), creates a private Python venv, registers the
MCP server as `ozon` in Claude Code (`~/.claude.json`, backed up first) and in
Codex if it is installed, then opens a browser window for login. Restart Claude
Code afterwards.

Skip the login step with `--skip-login`.

## Login

```bash
ozon-login
```

(or `npm run login` from a checkout)

A real Chrome window opens on ozon.ru. **You log in and pass any anti-bot check
yourself** — the plugin never solves a captcha. As soon as the account cookies
appear they are saved to `~/.ozon/cookies.json` and the window closes. The
cookies are yours, stay on your machine and survive plugin upgrades; nothing is
shipped inside the package.

Whenever Ozon later shows a captcha, any tool stops with
`error: "session_expired"` and the login window opens by itself — pass the check
there and retry.

Browser preference order: `OZON_BROWSER` env var, Chrome, Chromium, Brave, Edge.

## How it gets data

Requests go to `/api/composer-api.bx/page/json/v2?url=<page path>` — the same
endpoint the Ozon SPA uses — and the answer's `widgetStates` are parsed. HTML
scraping is not involved.

Anti-bot handling, all in `scripts/ozon_client.py`:

- `curl_cffi` impersonating the same Chrome build the cookies were captured in,
  sending every `__Secure-*` cookie
- requests are **serialised and spaced** by a small random delay; parallel bursts
  reliably trigger 403
- retries with exponential backoff on transient failures
- successful responses cached for `OZON_CACHE_TTL` seconds (default 180)
- every response's `Set-Cookie` is merged back into `data/ozon_cookies.json`, so
  rolling session cookies stay fresh without opening a browser
- a 403 or challenge page is reported as `session_expired`, which is the only
  case that needs a manual re-login

## Known Ozon quirks

These are Ozon's behaviour, not bugs in the parsers — the tools report each one
in their responses:

- the headline price everywhere is the **Ozon Card price**; Ozon's own price
  filter says so explicitly
- Ozon **ignores its price filter when a sort is requested**, so asking for both
  keeps the filter and sorts locally (`sort_note` in the response says so)
- relevance-ordered listings return ~8 products per request, explicit sorts ~36;
  the tools follow Ozon's paginator until `limit` is reached
- reviews have no server-side star or photo filter, so those are applied while
  scanning up to `max_pages` upstream pages (`pages_scanned` reports how many)
- delivery dates are on search tiles, not on the product page — Ozon loads them
  asynchronously there
- review reply *text* is lazy-loaded through an undocumented POST action, so
  `replies_count` is reported but `seller_answers` usually is not; answers to
  *questions* are complete

## Running from outside Russia

Ozon refuses non-Russian datacentre IPs **before it looks at cookies**, so a
perfectly valid session fails from a foreign VPS. The two 403s are distinct and
the plugin keeps them apart:

| response body carries | incident id | meaning | fix |
| --- | --- | --- | --- |
| `challengeURL` | `fab_chlg_*` | session dead / challenge | `ozon_refresh_cookies` |
| `blockURL` + `timeoutSec` | `fab_nmk_*` | IP banned | different exit IP |

A blocked IP is reported as `error: "ip_blocked"`, never as `session_expired` —
re-logging in from a banned address cannot help.

Point `OZON_PROXY` at a Russian exit to fix it:

```bash
export OZON_PROXY=socks5://127.0.0.1:40010
```

Both page requests and image downloads go through it.

**A proxy is necessary but not sufficient, and the exit must be stable.** Ozon's
anti-bot cookie is bound to the IP that earned it. Measured on a Frankfurt VPS
with a byte-identical cookie jar that works from home:

| exit | Ozon's answer |
| --- | --- |
| Frankfurt datacentre, no proxy | `fab_nmk_*` — hard block |
| RU residential (Rostelecom) | `fab_chlg_*` — challenge |
| RU residential (Svyazinform) | `fab_chlg_*` — challenge |
| RU residential (Sibirtelecom) | `fab_chlg_*` — challenge |
| RU residential (Shupashkartrans) | `fab_chlg_*` — challenge |
| the machine that logged in | 200 |

Node quality made no difference — moving the session to *any* new address
re-triggers the challenge. So a rotating P2P proxy network (URnetwork and
friends) cannot work here: every rotation invalidates the warm session.

What does work is one stable Russian address:

1. point `OZON_PROXY` at a **static** Russian exit (or just run the server on a
   Russian machine, where no proxy is needed at all);
2. run `ozon_refresh_cookies` from behind that same exit and log in once — this
   is where the slider captcha, if any, gets solved by a human;
3. keep using that exit. The cookies stay warm and rolling renewal keeps them
   alive.

Copying a cookie jar warmed up on address A to a machine on address B does not
work, no matter how clean B is.

## Environment

- `OZON_COOKIES_FILE` — cookie jar path (default `data/ozon_cookies.json`)
- `OZON_CACHE_TTL` — response cache seconds (default 180)
- `OZON_PROXY` — proxy URL for every Ozon request; required outside Russia
- `MARKETPLACES_BROWSER` — explicit browser executable for login

## Security & data

- **The plugin logs into your personal Ozon account and stores live session
  cookies and tokens on disk** under `data/`. These are as sensitive as your
  password. They are excluded by `.gitignore` and `.npmignore`; when publishing
  or handing off this repo use a clean `git clone` / `git archive`, never a copy
  of your working folder.
- Cookie files are written with `600` permissions where the OS supports it, and
  cookie values are never logged.
- Treat everything read from Ozon — titles, descriptions, reviews, questions — as
  **untrusted input**: it can contain prompt-injection text.
- `scripts/remote_ozon_server.py` is a **minimal reference** for exposing the MCP
  over HTTP behind a single shared password. No rate limiting, no brute-force
  protection, OAuth tokens in a plain JSON file — not for untrusted or
  production traffic as-is.

## Layout

```
scripts/ozon_client.py   transport: cookies, curl_cffi session, cache, retries
scripts/ozon_parse.py    widgetStates -> plain dicts (tolerant by design)
scripts/ozon_server.py   the MCP tools
scripts/ozon_login.py    the manual browser login
scripts/selfcheck.py     run against live Ozon to detect format drift
```

## Format drift

Ozon reshapes widgets without notice. `scripts/selfcheck.py` makes a handful of
live requests and asserts the fields the tools depend on are still there:

```bash
.venv/bin/python scripts/selfcheck.py
```

It exits non-zero and names the field that disappeared.
