"""Тонкая синхронная обёртка над Telegram Bot API поверх httpx.

Намеренно без зависимости от python-telegram-bot: нужны всего несколько
методов, а httpx уже есть в проекте (app/ai/mcp_auth.py). Все методы
возвращают поле ``result`` из ответа Bot API и бросают
:class:`TelegramApiError` на ``ok: false`` или сетевой сбой.
"""

from __future__ import annotations

import httpx

from app.core.config import settings

API_ROOT = "https://api.telegram.org"


class TelegramApiError(RuntimeError):
    pass


class TelegramClient:
    def __init__(self, token: str | None = None, *, timeout: float = 65.0):
        self._token = token or settings.telegram_bot_token
        if not self._token:
            raise TelegramApiError("TELEGRAM_BOT_TOKEN не задан")
        self._http = httpx.Client(base_url=f"{API_ROOT}/bot{self._token}", timeout=timeout)

    def __enter__(self) -> "TelegramClient":
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    def close(self) -> None:
        self._http.close()

    def _call(self, method: str, **params):
        try:
            resp = self._http.post(f"/{method}", json={k: v for k, v in params.items() if v is not None})
            resp.raise_for_status()
            body = resp.json()
        except (httpx.HTTPError, ValueError) as e:
            raise TelegramApiError(f"{method}: {e}") from e
        if not body.get("ok"):
            raise TelegramApiError(f"{method}: {body.get('description', 'неизвестная ошибка')}")
        return body.get("result")

    # -- методы, которые реально используются --------------------------------

    def get_me(self) -> dict:
        return self._call("getMe")

    def get_updates(self, offset: int | None = None, timeout: int = 50, allowed_updates: list[str] | None = None):
        # long-poll: держим соединение до `timeout` секунд, httpx-таймаут выше.
        return self._call(
            "getUpdates",
            offset=offset,
            timeout=timeout,
            allowed_updates=allowed_updates or ["message"],
        )

    def send_message(self, chat_id: str | int, text: str, *, parse_mode: str | None = None) -> dict:
        return self._call("sendMessage", chat_id=chat_id, text=text, parse_mode=parse_mode)

    def get_file(self, file_id: str) -> dict:
        return self._call("getFile", file_id=file_id)

    def download_file(self, file_path: str) -> bytes:
        try:
            resp = httpx.get(f"{API_ROOT}/file/bot{self._token}/{file_path}", timeout=60.0)
            resp.raise_for_status()
            return resp.content
        except httpx.HTTPError as e:
            raise TelegramApiError(f"download {file_path}: {e}") from e
