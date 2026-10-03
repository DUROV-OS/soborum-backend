"""REST-клиент официального Bot API мессенджера MAX (dev.max.ru, 0082).

Обычный HTTPS с токеном бота в заголовке ``Authorization``. Бот видит только диалоги, где
человек сам написал ему, и группы, куда его добавили (читать — если он там
администратор). Метода списка чатов у Bot API нет (``GET /chats`` → 404), поэтому
чаты и сообщения копятся в БД из событий — см. app/max/ingest.py.
"""

from __future__ import annotations

from typing import Any

import httpx

from app.core.config import settings

TIMEOUT = 30


class BotNotConfigured(RuntimeError):
    """Не задан MAX_BOT_TOKEN."""


class BotApiError(RuntimeError):
    """MAX отклонил запрос или не ответил. ``code`` — ключ ошибки MAX
    (``chat.denied``, ``attachment.not.ready``, ...), если он пришёл."""

    def __init__(self, status: int | None, code: str | None, message: str):
        super().__init__(message)
        self.status = status
        self.code = code
        self.message = message


def _headers() -> dict[str, str]:
    if not settings.max_bot_token:
        raise BotNotConfigured("MAX-бот не настроен: не задан MAX_BOT_TOKEN")
    return {"Authorization": settings.max_bot_token}


def _request(method: str, path: str, *, params: dict | None = None, json: Any = None,
             timeout: float = TIMEOUT) -> dict:
    url = settings.max_bot_api_url.rstrip("/") + path
    params = {k: v for k, v in (params or {}).items() if v is not None}
    try:
        resp = httpx.request(method, url, headers=_headers(), params=params, json=json, timeout=timeout)
    except httpx.HTTPError as exc:
        raise BotApiError(None, None, f"MAX не ответил: {exc}") from exc
    if resp.status_code >= 400:
        code, message = None, resp.text[:500]
        try:
            body = resp.json()
            code, message = body.get("code"), body.get("message") or message
        except ValueError:
            pass
        raise BotApiError(resp.status_code, code, f"MAX отклонил запрос ({resp.status_code}): {message}")
    try:
        return resp.json()
    except ValueError:
        return {}


# -- бот и подписки ----------------------------------------------------------

def get_me() -> dict:
    return _request("GET", "/me")


def subscribe(url: str, secret: str, update_types: list[str] | None = None) -> dict:
    body: dict[str, Any] = {"url": url, "secret": secret}
    if update_types:
        body["update_types"] = update_types
    return _request("POST", "/subscriptions", json=body)


def get_updates(marker: int | None = None, timeout: int = 30, limit: int = 100) -> dict:
    """Long polling. ``{updates: [...], marker}``; следующий запрос — с этим
    marker, иначе MAX повторит те же события."""
    return _request(
        "GET", "/updates",
        params={"marker": marker, "timeout": timeout, "limit": limit},
        timeout=timeout + 15,
    )


def get_chat(chat_id: int) -> dict:
    """``GET /chats/{id}`` — один чат (название группы, тип)."""
    return _request("GET", f"/chats/{chat_id}")


# -- сообщения ---------------------------------------------------------------

def get_messages(chat_id: int, count: int = 50) -> list[dict]:
    """Последние сообщения чата (бот должен быть в нём администратором)."""
    return _request("GET", "/messages", params={"chat_id": chat_id, "count": count}).get("messages") or []


def get_message(mid: str) -> dict | None:
    msgs = _request("GET", "/messages", params={"message_ids": mid}).get("messages") or []
    return msgs[0] if msgs else None


def send_message(chat_id: int, text: str | None, attachments: list[dict] | None = None,
                 notify: bool = True) -> dict:
    """``POST /messages?chat_id=`` → отправленный Message."""
    body: dict[str, Any] = {"text": text or None, "notify": notify}
    if attachments:
        body["attachments"] = attachments
    return _request("POST", "/messages", params={"chat_id": chat_id}, json=body).get("message") or {}


# -- вложения ----------------------------------------------------------------

def get_upload_url(kind: str) -> dict:
    """``POST /uploads?type=image|video|audio|file`` → ``{url, token?}``."""
    return _request("POST", "/uploads", params={"type": kind})


def upload(url: str, data: bytes, filename: str, content_type: str) -> dict:
    """Заливает байты на одноразовый URL multipart-полем ``data``. Для
    ``file`` в ответе ``{fileId, token}`` — token идёт в attachments. Токен
    бота сюда не отправляем: URL уже одноразовый и подписан MAX."""
    try:
        resp = httpx.post(
            url,
            files={"data": (filename, data, content_type or "application/octet-stream")},
            timeout=120,
        )
    except httpx.HTTPError as exc:
        raise BotApiError(None, None, f"MAX не принял файл: {exc}") from exc
    if resp.status_code >= 400:
        raise BotApiError(resp.status_code, None, f"MAX не принял файл ({resp.status_code}): {resp.text[:300]}")
    try:
        return resp.json()
    except ValueError:
        return {}


def get_video(token: str) -> dict:
    """``GET /videos/{token}`` → ``{urls: {mp4_720: ..., hls: ...}, thumbnail, duration}``."""
    return _request("GET", f"/videos/{token}")
