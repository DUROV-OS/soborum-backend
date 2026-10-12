"""Сервисный токен экспорта заявок (0109).

Не JWT пользователя: агент читает заявки из прода без пользовательской
сессии. Токен открывает только маршруты `/api/feedback/export/*`."""

import hmac

from fastapi import Header, HTTPException, status

from app.core.config import settings


def require_export_token(x_export_token: str | None = Header(default=None)) -> None:
    expected = settings.feedback_export_token.strip()
    if not expected:
        # Функция выключена: путь не должен выдавать, что он существует.
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Not Found")
    if not x_export_token or not hmac.compare_digest(x_export_token.encode(), expected.encode()):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Неверный токен экспорта")
