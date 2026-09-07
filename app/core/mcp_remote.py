"""Headless OAuth and JSON-RPC against our remote MCP servers.

The servers advertise authorization_code only. Their /authorize redirects
immediately, so the grant is machine-to-machine in everything but spelling.
Tokens live in this process. Callers must only invoke read tools.
"""

from __future__ import annotations

import base64
import hashlib
import json
import logging
import secrets
import time
from dataclasses import dataclass
from urllib.parse import parse_qs, urlencode, urlparse

import httpx

log = logging.getLogger("app.core.mcp_remote")

CLAUDE_REDIRECT = "https://claude.ai/api/mcp/auth_callback"
PROTOCOL = "2025-03-26"


@dataclass(frozen=True)
class McpTarget:
    name: str
    url: str
    client_id: str
    client_secret: str
    scope: str = ""
    redirect_uri: str = CLAUDE_REDIRECT

    @property
    def configured(self) -> bool:
        return bool(self.url and self.client_id and self.client_secret)


class McpError(RuntimeError):
    pass


def parse_sse_json(body: str) -> dict:
    last = None
    for line in body.splitlines():
        if line.startswith("data:"):
            last = json.loads(line[5:].lstrip())
    if last is None:
        text = body.strip()
        if text.startswith("{"):
            parsed = json.loads(text)
            if isinstance(parsed, dict):
                return parsed
        raise McpError("MCP вернул не JSON")
    return last


def unwrap_tool_result(payload: dict):
    if payload.get("error"):
        raise McpError(str(payload["error"]))
    result = payload.get("result")
    if not isinstance(result, dict):
        return result
    if result.get("isError"):
        raise McpError(_text_from_content(result) or "MCP tool error")
    structured = result.get("structuredContent")
    if isinstance(structured, dict) and "result" in structured and set(structured) <= {"result"}:
        return structured["result"]
    if structured is not None:
        return structured
    text = _text_from_content(result)
    if not text:
        return result
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        return text


def _text_from_content(result: dict) -> str:
    chunks = []
    for block in result.get("content") or []:
        if isinstance(block, dict) and block.get("type") == "text":
            chunks.append(block.get("text") or "")
    return "\n".join(chunks).strip()


def as_rows(payload) -> list[dict]:
    if isinstance(payload, list):
        return [row for row in payload if isinstance(row, dict)]
    if isinstance(payload, dict):
        for key in ("result", "rows", "deals"):
            value = payload.get(key)
            if isinstance(value, list):
                return [row for row in value if isinstance(row, dict)]
    return []


class RemoteMcp:
    def __init__(self, target: McpTarget):
        self.target = target
        self._token = ""
        self._token_exp = 0.0
        self._refresh = ""
        self._session_id = ""
        self._ready = False
        self._meta: dict | None = None

    def call_tool(self, name: str, arguments: dict | None = None, timeout: float = 30.0):
        payload = self._rpc(
            {
                "jsonrpc": "2.0",
                "id": 3,
                "method": "tools/call",
                "params": {"name": name, "arguments": arguments or {}},
            },
            timeout=timeout,
        )
        return unwrap_tool_result(payload)

    def _rpc(self, body: dict, timeout: float, retry: bool = True) -> dict:
        self._ensure_ready()
        response = self._post(body, timeout)
        if response.status_code in {401, 403} and retry:
            self._forget_auth()
            self._ensure_ready()
            response = self._post(body, timeout)
        elif response.status_code in {400, 404} and self._session_id and retry:
            self._ready = False
            self._session_id = ""
            self._ensure_ready()
            response = self._post(body, timeout)
        if response.status_code >= 400:
            raise McpError(f"{self.target.name} HTTP {response.status_code}: {response.text[:200]}")
        self._remember_session(response)
        return parse_sse_json(response.text)

    def _ensure_ready(self) -> None:
        if self._ready:
            return
        response = self._post(
            {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "initialize",
                "params": {
                    "protocolVersion": PROTOCOL,
                    "capabilities": {},
                    "clientInfo": {"name": "soborbum-shift", "version": "1"},
                },
            },
            timeout=20.0,
        )
        if response.status_code >= 400:
            raise McpError(f"{self.target.name} initialize HTTP {response.status_code}: {response.text[:200]}")
        self._remember_session(response)
        parse_sse_json(response.text)
        self._post({"jsonrpc": "2.0", "method": "notifications/initialized"}, timeout=12.0)
        self._ready = True

    def _post(self, body: dict, timeout: float) -> httpx.Response:
        headers = {
            "Authorization": f"Bearer {self._access_token()}",
            "Accept": "application/json, text/event-stream",
            "Content-Type": "application/json",
            "MCP-Protocol-Version": PROTOCOL,
        }
        if self._session_id:
            headers["Mcp-Session-Id"] = self._session_id
        with httpx.Client(timeout=timeout) as client:
            return client.post(self.target.url, headers=headers, json=body)

    def _remember_session(self, response: httpx.Response) -> None:
        sid = response.headers.get("mcp-session-id")
        if sid:
            self._session_id = sid

    def _access_token(self) -> str:
        if self._token and time.monotonic() < self._token_exp - 30:
            return self._token
        if self._refresh:
            try:
                payload = self._request_tokens(
                    {"grant_type": "refresh_token", "refresh_token": self._refresh}
                )
            except McpError:
                payload = self._grant_headless()
        else:
            payload = self._grant_headless()
        self._token = str(payload["access_token"])
        self._token_exp = time.monotonic() + float(payload.get("expires_in") or 3600)
        if payload.get("refresh_token"):
            self._refresh = str(payload["refresh_token"])
        return self._token

    def _forget_auth(self) -> None:
        self._token = ""
        self._token_exp = 0.0
        self._refresh = ""
        self._ready = False
        self._session_id = ""

    def _grant_headless(self) -> dict:
        meta = self._discover()
        verifier, challenge = _pkce()
        state = secrets.token_urlsafe(24)
        params = {
            "response_type": "code",
            "client_id": self.target.client_id,
            "redirect_uri": self.target.redirect_uri,
            "state": state,
            "code_challenge": challenge,
            "code_challenge_method": "S256",
            "resource": self.target.url,
        }
        if self.target.scope:
            params["scope"] = self.target.scope
        try:
            response = httpx.get(f"{meta['authorization_endpoint']}?{urlencode(params)}", timeout=12.0)
        except httpx.HTTPError as error:
            raise McpError(f"{self.target.name} authorize недоступен: {error}") from error
        location = response.headers.get("location")
        if response.status_code not in {301, 302, 303, 307, 308} or not location:
            raise McpError(
                f"{self.target.name} authorize {response.status_code}, нужен редирект с кодом"
            )
        query = parse_qs(urlparse(location).query)
        code = (query.get("code") or [None])[0]
        if not code:
            raise McpError(f"{self.target.name} не выдал код авторизации")
        if (query.get("state") or [None])[0] != state:
            raise McpError(f"{self.target.name} вернул чужой state")
        return self._request_tokens(
            {
                "grant_type": "authorization_code",
                "code": code,
                "redirect_uri": self.target.redirect_uri,
                "code_verifier": verifier,
            }
        )

    def _request_tokens(self, grant: dict) -> dict:
        meta = self._discover()
        try:
            response = httpx.post(
                meta["token_endpoint"],
                data={
                    **grant,
                    "client_id": self.target.client_id,
                    "client_secret": self.target.client_secret,
                    "resource": self.target.url,
                },
                timeout=12.0,
            )
            response.raise_for_status()
            return response.json()
        except (httpx.HTTPError, ValueError) as error:
            raise McpError(f"{self.target.name} token: {error}") from error

    def _discover(self) -> dict:
        if self._meta:
            return self._meta
        parsed = urlparse(self.target.url)
        origin = f"{parsed.scheme}://{parsed.netloc}"
        candidates = [f"{origin}/.well-known/oauth-authorization-server"]
        if parsed.path and parsed.path != "/":
            candidates.insert(0, f"{origin}/.well-known/oauth-authorization-server{parsed.path}")
        for url in candidates:
            try:
                response = httpx.get(url, timeout=10.0)
                if response.status_code == 200 and "authorization_endpoint" in response.json():
                    self._meta = response.json()
                    return self._meta
            except (httpx.HTTPError, ValueError):
                continue
        raise McpError(f"{self.target.name}: нет oauth-authorization-server")


def _pkce() -> tuple[str, str]:
    verifier = base64.urlsafe_b64encode(secrets.token_bytes(32)).rstrip(b"=").decode()
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
    return verifier, challenge


_clients: dict[str, RemoteMcp] = {}


def client_for(target: McpTarget) -> RemoteMcp:
    current = _clients.get(target.name)
    if current is not None and current.target == target:
        return current
    remote = RemoteMcp(target)
    _clients[target.name] = remote
    return remote


def clear_clients() -> None:
    _clients.clear()
