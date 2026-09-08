"""OAuth2 (authorization_code + PKCE) against the knowledge-base MCP server.

Claude's MCP connector (see engine._call_claude) just wants a bearer token in
`authorization_token` - it doesn't do the OAuth exchange itself, so we obtain
and renew that token here.

The provider's .well-known/oauth-authorization-server lists only the
`authorization_code` and `refresh_token` grants, no `client_credentials`. But
its authorization endpoint issues the code straight away for our pre-shared
client - a 302 to the redirect URI, with no login or consent screen in
between - so the grant is machine-to-machine in everything but spelling, and
_grant_headless() completes it in-process without a browser.

Hence two ways in, both ending in _store_tokens():

  * _grant_headless() - the normal one, used automatically by
    get_access_token() whenever there is no usable token.
  * build_authorize_url() + exchange_code_for_tokens() - an admin opening
    GET /api/ai/mcp/authorize in a browser and being redirected to
    GET /callback. Kept for providers that do show a consent screen, and as
    a manual escape hatch; _grant_headless() says so explicitly if the
    authorization endpoint ever stops redirecting.

Tokens issued by the MCP server live only in that process's memory, so its
every restart invalidates whatever we stored. get_access_token() therefore
treats a failed refresh as normal and re-runs the headless grant.
"""

import base64
import hashlib
import secrets
from datetime import datetime, timedelta, timezone
from urllib.parse import parse_qs, urlencode, urlparse

import httpx
from fastapi import HTTPException, status
from sqlalchemy.orm import Session

from app.ai.models import McpCredential
from app.core.config import settings

# state -> code_verifier, for the short window between /authorize and
# /callback in the browser flow. The headless grant keeps its verifier on
# the stack instead - it never leaves the function.
_pending_pkce: dict[str, str] = {}

_cached_metadata: dict | None = None


def _aware(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value


def _require_configured() -> None:
    if not settings.mcp_configured:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="MCP не настроен: заполните MCP_SERVER_URL, MCP_OAUTH_CLIENT_ID, MCP_OAUTH_CLIENT_SECRET",
        )


def _discover_metadata() -> dict:
    global _cached_metadata
    if _cached_metadata:
        return _cached_metadata

    parsed = urlparse(settings.mcp_server_url)
    origin = f"{parsed.scheme}://{parsed.netloc}"

    candidates = []
    if parsed.path and parsed.path != "/":
        candidates.append(f"{origin}/.well-known/oauth-authorization-server{parsed.path}")
    candidates.append(f"{origin}/.well-known/oauth-authorization-server")

    for url in candidates:
        try:
            response = httpx.get(url, timeout=10.0)
            if response.status_code == 200:
                _cached_metadata = response.json()
                return _cached_metadata
        except httpx.HTTPError:
            continue

    # No discovery document - assume the bare server URL doubles as both endpoints.
    _cached_metadata = {
        "authorization_endpoint": settings.mcp_server_url,
        "token_endpoint": settings.mcp_server_url,
    }
    return _cached_metadata


def _generate_pkce_pair() -> tuple[str, str]:
    verifier = base64.urlsafe_b64encode(secrets.token_bytes(32)).rstrip(b"=").decode()
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
    return verifier, challenge


def _authorize_url(challenge: str, state: str) -> str:
    params = {
        "response_type": "code",
        "client_id": settings.mcp_oauth_client_id,
        "redirect_uri": settings.mcp_redirect_uri,
        "state": state,
        "code_challenge": challenge,
        "code_challenge_method": "S256",
        "resource": settings.mcp_server_url,
    }
    return f"{_discover_metadata()['authorization_endpoint']}?{urlencode(params)}"


def _request_tokens(grant: dict) -> dict:
    """POST the token endpoint and return its payload. `grant` carries
    grant_type and whatever that grant needs; the client credentials are
    added here."""
    try:
        response = httpx.post(
            _discover_metadata()["token_endpoint"],
            data={
                **grant,
                "client_id": settings.mcp_oauth_client_id,
                "client_secret": settings.mcp_oauth_client_secret,
                "resource": settings.mcp_server_url,
            },
            timeout=10.0,
        )
        response.raise_for_status()
        return response.json()
    except (httpx.HTTPError, ValueError) as e:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=f"MCP-сервер не выдал токен по гранту {grant['grant_type']}: {e}",
        ) from e


def _store_tokens(db: Session, payload: dict) -> McpCredential:
    expires_at = datetime.now(timezone.utc) + timedelta(seconds=float(payload.get("expires_in", 3600)))
    credential = db.get(McpCredential, 1)
    if credential is None:
        credential = McpCredential(id=1, access_token=payload["access_token"], expires_at=expires_at)
        db.add(credential)
    else:
        credential.access_token = payload["access_token"]
        credential.expires_at = expires_at
    # Some providers only send a refresh_token on the very first grant.
    if payload.get("refresh_token"):
        credential.refresh_token = payload["refresh_token"]
    db.commit()
    db.refresh(credential)
    return credential


def _grant_headless() -> dict:
    """Run authorization_code + PKCE end to end without a browser, and return
    the token payload. Only works because this provider's authorization
    endpoint redirects immediately - see the module docstring."""
    _require_configured()

    verifier, challenge = _generate_pkce_pair()
    state = secrets.token_urlsafe(24)
    try:
        # follow_redirects is off by default, and must stay off: the whole
        # point is to read the code out of the Location header ourselves
        # rather than let it be delivered to mcp_redirect_uri.
        response = httpx.get(_authorize_url(challenge, state), timeout=10.0)
    except httpx.HTTPError as e:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=f"MCP-сервер недоступен на этапе авторизации: {e}",
        ) from e

    location = response.headers.get("location")
    if response.status_code not in (301, 302, 303, 307, 308) or not location:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=f"MCP-сервер ответил {response.status_code} вместо редиректа с кодом авторизации. "
            "Похоже, он требует подтверждения в браузере - пройдите GET /api/ai/mcp/authorize.",
        )

    query = parse_qs(urlparse(location).query)
    code = query.get("code", [None])[0]
    if code is None:
        error = query.get("error_description", query.get("error", ["без объяснения"]))[0]
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=f"MCP-сервер отклонил запрос авторизации: {error}",
        )
    if query.get("state", [None])[0] != state:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail="MCP-сервер вернул чужой state - ответ не соответствует нашему запросу авторизации.",
        )

    return _request_tokens(
        {
            "grant_type": "authorization_code",
            "code": code,
            "redirect_uri": settings.mcp_redirect_uri,
            "code_verifier": verifier,
        }
    )


def build_authorize_url() -> str:
    """Browser flow, step 1: a URL for an admin to open. The verifier waits in
    _pending_pkce until exchange_code_for_tokens() is called from /callback."""
    _require_configured()

    verifier, challenge = _generate_pkce_pair()
    state = secrets.token_urlsafe(24)
    _pending_pkce[state] = verifier
    return _authorize_url(challenge, state)


def exchange_code_for_tokens(db: Session, code: str, state: str) -> McpCredential:
    """Browser flow, step 2: called from GET /callback."""
    verifier = _pending_pkce.pop(state, None)
    if verifier is None:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Неизвестный или устаревший state - начните авторизацию заново через /api/ai/mcp/authorize",
        )

    return _store_tokens(
        db,
        _request_tokens(
            {
                "grant_type": "authorization_code",
                "code": code,
                "redirect_uri": settings.mcp_redirect_uri,
                "code_verifier": verifier,
            }
        ),
    )


def get_access_token(db: Session) -> str:
    """A token that is valid right now, obtaining or renewing one as needed.
    Requires no human intervention as long as the headless grant works."""
    credential = db.get(McpCredential, 1)
    if credential is not None and _aware(credential.expires_at) > datetime.now(timezone.utc) + timedelta(seconds=30):
        return credential.access_token

    if credential is not None and credential.refresh_token:
        try:
            payload = _request_tokens(
                {"grant_type": "refresh_token", "refresh_token": credential.refresh_token}
            )
        except HTTPException:
            # Expected after the MCP server restarts: it forgets every token
            # it ever issued, ours included. A fresh grant is the recovery,
            # and it reports its own failure if the server is truly down.
            payload = _grant_headless()
    else:
        payload = _grant_headless()

    return _store_tokens(db, payload).access_token
