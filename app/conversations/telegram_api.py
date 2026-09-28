"""Тонкий клиент Telegram Bot API (core.telegram.org/bots/api) для переписки
из карточек (0083-e). Токен — в пути запроса, поэтому URL с токеном нигде не
логируется: в тексте ошибок только метод и описание от Telegram.
"""

from __future__ import annotations

from typing import Any

import httpx

from app.core.config import settings

API = "https://api.telegram.org"
TIMEOUT = 30


class TelegramNotConfigured(RuntimeError):
    """Не задан TELEGRAM_BOT_TOKEN."""


class TelegramApiError(RuntimeError):
    def __init__(self, status: int | None, message: str):
        super().__init__(message)
        self.status = status
        self.message = message


def _call(method: str, payload: dict[str, Any] | None = None, timeout: float = TIMEOUT) -> Any:
    if not settings.telegram_bot_token:
        raise TelegramNotConfigured("Telegram-бот не настроен: не задан TELEGRAM_BOT_TOKEN")
    url = f"{API}/bot{settings.telegram_bot_token}/{method}"
    try:
        resp = httpx.post(url, json=payload or {}, timeout=timeout)
    except httpx.HTTPError as exc:
        # str(exc) у httpx может содержать URL с токеном — не выводим его.
        raise TelegramApiError(None, f"Telegram не ответил ({type(exc).__name__})") from exc
    try:
        body = resp.json()
    except ValueError:
        raise TelegramApiError(resp.status_code, f"Telegram ответил {resp.status_code} без JSON")
    if not body.get("ok"):
        raise TelegramApiError(
            body.get("error_code") or resp.status_code,
            f"Telegram отклонил {method}: {body.get('description') or resp.status_code}",
        )
    return body.get("result")


def get_me() -> dict:
    return _call("getMe")


def send_message(chat_id: int | str, text: str) -> dict:
    return _call("sendMessage", {"chat_id": chat_id, "text": text})


def get_updates(offset: int | None = None, timeout: int = 30) -> list[dict]:
    payload: dict[str, Any] = {"timeout": timeout, "allowed_updates": ["message", "my_chat_member"]}
    if offset is not None:
        payload["offset"] = offset
    return _call("getUpdates", payload, timeout=timeout + 15) or []


def set_webhook(url: str, secret: str) -> bool:
    return _call(
        "setWebhook",
        {"url": url, "secret_token": secret, "allowed_updates": ["message", "my_chat_member"]},
    )
