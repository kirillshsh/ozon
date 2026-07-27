#!/usr/bin/env python3
from __future__ import annotations

import contextlib
import hmac
import json
import os
import secrets
import time
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs
from urllib.parse import urlencode

import httpx
import uvicorn
from mcp.server.auth.provider import (
    AccessToken,
    AuthorizationCode,
    AuthorizationParams,
    OAuthAuthorizationServerProvider,
    ProviderTokenVerifier,
    RefreshToken,
)
from mcp.server.auth.settings import (
    AuthSettings,
    ClientRegistrationOptions,
    RevocationOptions,
)
from mcp.server.transport_security import TransportSecuritySettings
from mcp.shared.auth import OAuthClientInformationFull, OAuthToken
from starlette.requests import Request
from starlette.responses import HTMLResponse, JSONResponse, RedirectResponse, Response
from starlette.types import ASGIApp, Receive, Scope, Send
from starlette.routing import Route

from ozon_server import PLUGIN_ROOT, server


HOST = os.getenv("OZON_MCP_HOST", "0.0.0.0")
PORT = int(os.getenv("OZON_MCP_PORT", "9443"))
PUBLIC_BASE_URL = os.getenv(
    "OZON_MCP_PUBLIC_BASE_URL",
    f"http://127.0.0.1:{PORT}",
).rstrip("/")
LOGIN_PASSWORD = os.environ.get("OZON_MCP_PASSWORD", "")
if not LOGIN_PASSWORD:
    raise SystemExit(
        "OZON_MCP_PASSWORD is required to run the remote server. "
        "Set it to the password clients must present, e.g. "
        "OZON_MCP_PASSWORD=... python scripts/remote_ozon_server.py"
    )
SSL_CERTFILE = os.getenv("OZON_MCP_SSL_CERTFILE", "").strip()
SSL_KEYFILE = os.getenv("OZON_MCP_SSL_KEYFILE", "").strip()

SCOPES = ["ozon:read"]
STATE_FILE = PLUGIN_ROOT / "data" / "remote_oauth_state.json"
KNOWN_CIMD_CLIENT_ID_PREFIXES = (
    "https://chatgpt.com/",
    "https://chat.openai.com/",
    "https://claude.ai/",
)


def _now() -> int:
    return int(time.time())


def _dump_model(model: Any) -> dict[str, Any]:
    return model.model_dump(mode="json", exclude_none=True)


def _load_state() -> dict[str, Any]:
    try:
        data = json.loads(STATE_FILE.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {}
    return data if isinstance(data, dict) else {}


def _save_state(data: dict[str, Any]) -> None:
    STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    tmp = STATE_FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(STATE_FILE)


def _scopes(scopes: list[str] | None) -> list[str]:
    return [scope for scope in (scopes or SCOPES) if scope in SCOPES] or SCOPES


def _is_known_cimd_client(client_id: str) -> bool:
    return client_id.startswith(KNOWN_CIMD_CLIENT_ID_PREFIXES)


def _is_allowed_redirect_uri(uri: str) -> bool:
    return (
        uri.startswith("https://chatgpt.com/connector/oauth/")
        or uri == "https://claude.ai/api/mcp/auth_callback"
        or uri.startswith("http://localhost:")
        or uri.startswith("http://127.0.0.1:")
    )


async def _load_cimd_client(client_id: str) -> OAuthClientInformationFull | None:
    if not _is_known_cimd_client(client_id):
        return None
    try:
        async with httpx.AsyncClient(follow_redirects=True, timeout=10) as client:
            resp = await client.get(client_id, headers={"Accept": "application/json"})
        resp.raise_for_status()
        metadata = resp.json()
    except Exception:
        return None

    redirect_uris = metadata.get("redirect_uris") or []
    if not redirect_uris:
        return None
    if not all(_is_allowed_redirect_uri(str(uri)) for uri in redirect_uris):
        return None

    token_auth = metadata.get("token_endpoint_auth_method") or "none"
    if token_auth != "none":
        return None

    metadata["client_id"] = client_id
    metadata["token_endpoint_auth_method"] = "none"
    metadata.setdefault("grant_types", ["authorization_code", "refresh_token"])
    metadata.setdefault("response_types", ["code"])
    metadata.setdefault("scope", " ".join(SCOPES))
    return OAuthClientInformationFull.model_validate(metadata)


class FileOAuthProvider(
    OAuthAuthorizationServerProvider[AuthorizationCode, RefreshToken, AccessToken]
):
    def _state(self) -> dict[str, Any]:
        data = _load_state()
        data.setdefault("clients", {})
        data.setdefault("pending", {})
        data.setdefault("codes", {})
        data.setdefault("access_tokens", {})
        data.setdefault("refresh_tokens", {})
        return data

    async def get_client(self, client_id: str) -> OAuthClientInformationFull | None:
        data = self._state()
        client = data["clients"].get(client_id)
        if client:
            return OAuthClientInformationFull.model_validate(client)
        cimd_client = await _load_cimd_client(client_id)
        if not cimd_client:
            return None
        data["clients"][client_id] = _dump_model(cimd_client)
        _save_state(data)
        return cimd_client

    async def register_client(self, client_info: OAuthClientInformationFull) -> None:
        if not client_info.client_id:
            raise ValueError("client_id is required")
        data = self._state()
        data["clients"][client_info.client_id] = _dump_model(client_info)
        _save_state(data)

    async def authorize(
        self,
        client: OAuthClientInformationFull,
        params: AuthorizationParams,
    ) -> str:
        ticket = secrets.token_urlsafe(32)
        data = self._state()
        data["pending"][ticket] = {
            "client_id": client.client_id,
            "state": params.state,
            "scopes": _scopes(params.scopes),
            "code_challenge": params.code_challenge,
            "redirect_uri": str(params.redirect_uri),
            "redirect_uri_provided_explicitly": params.redirect_uri_provided_explicitly,
            "resource": params.resource,
            "expires_at": _now() + 600,
        }
        _save_state(data)
        return f"{PUBLIC_BASE_URL}/auth/login?{urlencode({'ticket': ticket})}"

    async def approve_ticket(self, ticket: str, password: str) -> str | None:
        if not hmac.compare_digest(password, LOGIN_PASSWORD):
            return None
        data = self._state()
        grant = data["pending"].pop(ticket, None)
        if not grant or int(grant.get("expires_at", 0)) < _now():
            _save_state(data)
            return None
        code = secrets.token_urlsafe(32)
        grant["code"] = code
        grant["expires_at"] = time.time() + 300
        data["codes"][code] = grant
        _save_state(data)
        query = {"code": code}
        if grant.get("state"):
            query["state"] = grant["state"]
        return f"{grant['redirect_uri']}?{urlencode(query)}"

    async def load_authorization_code(
        self,
        client: OAuthClientInformationFull,
        authorization_code: str,
    ) -> AuthorizationCode | None:
        data = self._state()
        raw = data["codes"].get(authorization_code)
        if not raw:
            return None
        return AuthorizationCode.model_validate(raw)

    async def exchange_authorization_code(
        self,
        client: OAuthClientInformationFull,
        authorization_code: AuthorizationCode,
    ) -> OAuthToken:
        data = self._state()
        data["codes"].pop(authorization_code.code, None)
        access_token = secrets.token_urlsafe(48)
        refresh_token = secrets.token_urlsafe(48)
        expires_in = 30 * 24 * 60 * 60
        data["access_tokens"][access_token] = {
            "token": access_token,
            "client_id": client.client_id,
            "scopes": authorization_code.scopes,
            "expires_at": _now() + expires_in,
            "resource": authorization_code.resource,
        }
        data["refresh_tokens"][refresh_token] = {
            "token": refresh_token,
            "client_id": client.client_id,
            "scopes": authorization_code.scopes,
            "expires_at": _now() + 180 * 24 * 60 * 60,
        }
        _save_state(data)
        return OAuthToken(
            access_token=access_token,
            expires_in=expires_in,
            scope=" ".join(authorization_code.scopes),
            refresh_token=refresh_token,
        )

    async def load_refresh_token(
        self,
        client: OAuthClientInformationFull,
        refresh_token: str,
    ) -> RefreshToken | None:
        data = self._state()
        raw = data["refresh_tokens"].get(refresh_token)
        if not raw:
            return None
        return RefreshToken.model_validate(raw)

    async def exchange_refresh_token(
        self,
        client: OAuthClientInformationFull,
        refresh_token: RefreshToken,
        scopes: list[str],
    ) -> OAuthToken:
        data = self._state()
        data["refresh_tokens"].pop(refresh_token.token, None)
        access_token = secrets.token_urlsafe(48)
        new_refresh_token = secrets.token_urlsafe(48)
        expires_in = 30 * 24 * 60 * 60
        granted_scopes = _scopes(scopes)
        data["access_tokens"][access_token] = {
            "token": access_token,
            "client_id": client.client_id,
            "scopes": granted_scopes,
            "expires_at": _now() + expires_in,
        }
        data["refresh_tokens"][new_refresh_token] = {
            "token": new_refresh_token,
            "client_id": client.client_id,
            "scopes": granted_scopes,
            "expires_at": _now() + 180 * 24 * 60 * 60,
        }
        _save_state(data)
        return OAuthToken(
            access_token=access_token,
            expires_in=expires_in,
            scope=" ".join(granted_scopes),
            refresh_token=new_refresh_token,
        )

    async def load_access_token(self, token: str) -> AccessToken | None:
        data = self._state()
        raw = data["access_tokens"].get(token)
        if not raw or int(raw.get("expires_at", 0)) < _now():
            return None
        return AccessToken.model_validate(raw)

    async def revoke_token(self, token: AccessToken | RefreshToken) -> None:
        data = self._state()
        data["access_tokens"].pop(token.token, None)
        data["refresh_tokens"].pop(token.token, None)
        _save_state(data)


provider = FileOAuthProvider()


def _login_page(ticket: str, error: str = "") -> str:
    error_html = (
        '<p style="color:#b00020;margin-top:0">Неверный пароль или сессия истекла.</p>'
        if error
        else ""
    )
    return f"""<!doctype html>
<html lang="ru">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Marketplace MCP Login</title>
  <style>
    body {{ font-family: system-ui, -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif; margin: 0; background: #f6f7f9; color: #16181d; }}
    main {{ max-width: 420px; margin: 12vh auto; background: #fff; border: 1px solid #d9dee7; border-radius: 8px; padding: 24px; }}
    h1 {{ font-size: 22px; margin: 0 0 8px; }}
    p {{ line-height: 1.45; color: #4a5260; }}
    label {{ display: block; font-weight: 650; margin: 18px 0 6px; }}
    input {{ box-sizing: border-box; width: 100%; font-size: 16px; padding: 10px 12px; border: 1px solid #aeb7c4; border-radius: 6px; }}
    button {{ margin-top: 16px; width: 100%; font-size: 16px; padding: 10px 12px; border: 0; border-radius: 6px; background: #1458d4; color: white; font-weight: 700; cursor: pointer; }}
  </style>
</head>
<body>
  <main>
    <h1>Marketplace MCP</h1>
    <p>Введите пароль, чтобы выдать ChatGPT доступ к инструментам Ozon.</p>
    {error_html}
    <form method="post" action="/auth/login">
      <input type="hidden" name="ticket" value="{ticket}">
      <label for="password">Пароль</label>
      <input id="password" name="password" type="password" autocomplete="current-password" autofocus>
      <button type="submit">Подключить</button>
    </form>
  </main>
</body>
</html>"""


async def login_get(request: Request) -> Response:
    ticket = request.query_params.get("ticket", "")
    return HTMLResponse(_login_page(ticket))


async def login_post(request: Request) -> Response:
    form = await request.form()
    ticket = str(form.get("ticket") or "")
    password = str(form.get("password") or "")
    redirect_url = await provider.approve_ticket(ticket, password)
    if not redirect_url:
        return HTMLResponse(_login_page(ticket, "invalid"), status_code=401)
    return RedirectResponse(redirect_url, status_code=302)


async def healthz(request: Request) -> Response:
    return JSONResponse(
        {
            "ok": True,
            "service": "ozon",
            "transport": "streamable-http",
            "mcp": f"{PUBLIC_BASE_URL}/mcp",
            "oauth": f"{PUBLIC_BASE_URL}/.well-known/oauth-authorization-server",
        }
    )


class PreflightMiddleware:
    def __init__(self, app: ASGIApp):
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        path = scope.get("path", "").rstrip("/")
        method = scope.get("method", "")
        if method in {"GET", "HEAD"} and path == "/.well-known/oauth-authorization-server":
            await _json_response(_authorization_server_metadata())(scope, receive, send)
            return
        if method in {"GET", "HEAD"} and path in {
            "/.well-known/oauth-protected-resource",
            "/.well-known/oauth-protected-resource/mcp",
        }:
            await _json_response(_protected_resource_metadata())(scope, receive, send)
            return
        if method == "POST" and path == "/revoke":
            await _revoke_response(scope, receive, send)
            return

        if method == "OPTIONS":
            headers = [
                (b"access-control-allow-origin", b"*"),
                (b"access-control-allow-methods", b"GET, POST, DELETE, OPTIONS"),
                (
                    b"access-control-allow-headers",
                    b"authorization, content-type, accept, mcp-protocol-version, mcp-session-id",
                ),
                (b"access-control-max-age", b"600"),
                (b"content-length", b"2"),
                (b"content-type", b"text/plain; charset=utf-8"),
            ]
            await send({"type": "http.response.start", "status": 200, "headers": headers})
            await send({"type": "http.response.body", "body": b"OK"})
            return
        await self.app(scope, receive, send)


def _json_response(payload: dict[str, Any]) -> JSONResponse:
    return JSONResponse(
        payload,
        headers={
            "Access-Control-Allow-Origin": "*",
            "Cache-Control": "public, max-age=3600",
        },
    )


def _authorization_server_metadata() -> dict[str, Any]:
    return {
        "issuer": PUBLIC_BASE_URL,
        "authorization_endpoint": f"{PUBLIC_BASE_URL}/authorize",
        "token_endpoint": f"{PUBLIC_BASE_URL}/token",
        "registration_endpoint": f"{PUBLIC_BASE_URL}/register",
        "client_id_metadata_document_supported": True,
        "scopes_supported": SCOPES,
        "response_types_supported": ["code"],
        "grant_types_supported": ["authorization_code", "refresh_token"],
        "token_endpoint_auth_methods_supported": [
            "none",
            "client_secret_post",
            "client_secret_basic",
        ],
        "revocation_endpoint": f"{PUBLIC_BASE_URL}/revoke",
        "revocation_endpoint_auth_methods_supported": [
            "none",
            "client_secret_post",
            "client_secret_basic",
        ],
        "code_challenge_methods_supported": ["S256"],
    }


def _protected_resource_metadata() -> dict[str, Any]:
    return {
        "resource": f"{PUBLIC_BASE_URL}/mcp",
        "authorization_servers": [PUBLIC_BASE_URL],
        "scopes_supported": SCOPES,
        "bearer_methods_supported": ["header"],
    }


async def _revoke_response(scope: Scope, receive: Receive, send: Send) -> None:
    body = b""
    while True:
        message = await receive()
        if message["type"] != "http.request":
            break
        body += message.get("body", b"")
        if not message.get("more_body"):
            break
    params = parse_qs(body.decode("utf-8", errors="replace"), keep_blank_values=True)
    token = (params.get("token") or [""])[0]
    if token:
        data = _load_state()
        data.setdefault("access_tokens", {}).pop(token, None)
        data.setdefault("refresh_tokens", {}).pop(token, None)
        _save_state(data)
    response = Response(status_code=200, headers={"Access-Control-Allow-Origin": "*"})
    await response(scope, receive, send)


def configure_server() -> None:
    public_host = PUBLIC_BASE_URL.removeprefix("https://").removeprefix("http://")
    server.settings.host = HOST
    server.settings.port = PORT
    server.settings.streamable_http_path = "/mcp"
    server.settings.stateless_http = True
    server.settings.transport_security = TransportSecuritySettings(
        allowed_hosts=[
            public_host,
            f"127.0.0.1:{PORT}",
            f"localhost:{PORT}",
        ],
        allowed_origins=[
            PUBLIC_BASE_URL,
            "https://chatgpt.com",
            "https://chat.openai.com",
            "https://claude.ai",
            "https://www.claude.ai",
            "https://claude.com",
            f"http://127.0.0.1:{PORT}",
            f"http://localhost:{PORT}",
        ],
    )
    server.settings.auth = AuthSettings(
        issuer_url=PUBLIC_BASE_URL,
        resource_server_url=f"{PUBLIC_BASE_URL}/mcp",
        required_scopes=SCOPES,
        client_registration_options=ClientRegistrationOptions(
            enabled=True,
            valid_scopes=SCOPES,
            default_scopes=SCOPES,
        ),
        revocation_options=RevocationOptions(enabled=True),
    )
    server._auth_server_provider = provider
    server._token_verifier = ProviderTokenVerifier(provider)
    server._session_manager = None
    server._custom_starlette_routes.extend(
        [
            Route("/", healthz, methods=["GET"]),
            Route("/healthz", healthz, methods=["GET"]),
            Route("/auth/login", login_get, methods=["GET"]),
            Route("/auth/login", login_post, methods=["POST"]),
        ]
    )


def main() -> None:
    os.chdir(PLUGIN_ROOT)
    configure_server()
    ssl_kwargs: dict[str, str] = {}
    if SSL_CERTFILE and SSL_KEYFILE:
        ssl_kwargs = {
            "ssl_certfile": SSL_CERTFILE,
            "ssl_keyfile": SSL_KEYFILE,
        }
    app = PreflightMiddleware(server.streamable_http_app())
    uvicorn.run(app, host=HOST, port=PORT, log_level="info", **ssl_kwargs)


if __name__ == "__main__":
    main()
