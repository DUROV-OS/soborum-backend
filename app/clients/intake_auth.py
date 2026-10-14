"""Защита вебхука заявок с сайта (0111-a).

Не JWT сотрудника: форму отправляет сайт. Доступ — общий секрет
`LEAD_INTAKE_TOKEN`, лимит запросов на адрес. Лимит хранится в памяти процесса:
этого достаточно как защиты от спама, но между воркерами он не общий."""

import hmac
import threading
import time
from collections import defaultdict, deque

from fastapi import HTTPException, Request, status

from app.core.config import settings

_hits: dict[str, deque[float]] = defaultdict(deque)
_lock = threading.Lock()


def reset_rate_limit() -> None:
    with _lock:
        _hits.clear()


def client_address(request: Request) -> str:
    """Адрес отправителя. За прокси (Caddy) берём последнее значение
    X-Forwarded-For — его дописал сам прокси, а левые значения может подставить
    отправитель."""
    forwarded = request.headers.get("x-forwarded-for", "")
    if forwarded:
        return forwarded.split(",")[-1].strip()
    return request.client.host if request.client else "unknown"


def check_rate_limit(address: str) -> None:
    limit = settings.lead_intake_rate_limit
    window = settings.lead_intake_rate_window_seconds
    if limit <= 0:
        return
    now = time.monotonic()
    with _lock:
        hits = _hits[address]
        while hits and now - hits[0] > window:
            hits.popleft()
        if len(hits) >= limit:
            raise HTTPException(
                status_code=status.HTTP_429_TOO_MANY_REQUESTS,
                detail="Слишком много заявок, повторите позже",
                headers={"Retry-After": str(window)},
            )
        hits.append(now)


def check_token_configured() -> None:
    if not settings.lead_intake_token.strip():
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail="Приём заявок не настроен")


def check_token(provided: str | None) -> None:
    expected = settings.lead_intake_token.strip()
    if not provided or not hmac.compare_digest(provided.encode(), expected.encode()):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Неверный токен приёма заявок")
