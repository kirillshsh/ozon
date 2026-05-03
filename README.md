# Marketplaces

Codex MCP plugin for Ozon and Wildberries.

It can:

- search products on WB and Ozon with pagination
- open product cards with details and product photos returned as MCP image outputs
- fetch reviews, including review photos as MCP image outputs when the marketplace exposes them
- refresh marketplace cookies through a controlled browser login flow

## Install

From a GitHub repo:

```bash
npx github:kirillshsh/marketplaces
```

From a local checkout:

```bash
npm run install:local
```

The installer copies the plugin into `~/plugins/marketplaces`, creates a private
Python venv, registers `marketplaces@kirill-local` in Codex, opens Ozon and WB in
a controlled browser session, saves cookies locally, then closes each login
window after that marketplace is logged in. It works on macOS, Windows, and
Linux when Node.js, Python 3.10+, and a Chrome-compatible browser are available.

Use `--skip-login` when you only want to register the plugin:

```bash
npx github:kirillshsh/marketplaces -- --skip-login
```

## Browser Login

The login helper prefers Chrome-compatible browsers in this order:

1. `MARKETPLACES_BROWSER` environment variable
2. Google Chrome
3. Chromium
4. Brave
5. Microsoft Edge

It stores cookies only on the local machine under the plugin `data/` directory.
Cookie files are ignored by git and npm packaging.

If a persistent browser profile cannot be attached, the installer retries with a
temporary browser profile and still saves marketplace cookies into the plugin
data directory.
