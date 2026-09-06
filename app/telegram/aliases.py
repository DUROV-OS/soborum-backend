"""Статический словарь «алиас → id сотрудника Durov-OS».

Задел на будущее. В Telegram-чате пишут не только сотрудники, а динамическая
привязка аккаунтов (``telegram_account_links`` + диплинк-логин) только
раскатывается. Пока её нет для конкретного человека — этот словарь позволяет
заранее сопоставить его ник в Telegram (или произвольный алиас) с учётной
записью, чтобы ingest мог подписать сообщение реальным сотрудником.

Ключ — это либо ``@username`` без «@» в нижнем регистре, либо любой
короткий алиас, который проставлен в ``telegram_account_links.alias``.

Порядок разрешения — см. :func:`resolve_user_id`:

  1. Явная привязка аккаунта в БД (``telegram_account_links.user_id``).
  2. ``alias`` из строки привязки → этот словарь.
  3. ``@username`` из самого сообщения → этот словарь.

Правьте прямо здесь и коммитьте — это конфиг, а не данные.
"""

from __future__ import annotations

from sqlalchemy.orm import Session

# Заполняйте по мере онбординга сотрудников в Telegram-мост.
#   "ivan_petrov": 3,      # @ivan_petrov  → users.id = 3
#   "marina":      7,      # алиас "marina" → users.id = 7
ALIAS_TO_USER_ID: dict[str, int] = {}


def normalize_alias(value: str | None) -> str | None:
    """`@Ivan_Petrov` / `Ivan_Petrov ` → `ivan_petrov`."""
    if not value:
        return None
    cleaned = value.strip().lstrip("@").lower()
    return cleaned or None


def alias_user_id(alias: str | None) -> int | None:
    key = normalize_alias(alias)
    return ALIAS_TO_USER_ID.get(key) if key else None


def resolve_user_id(db: Session, tg_user_id: str | None, tg_username: str | None) -> int | None:
    """Best guess at which Durov-OS user this Telegram identity belongs to,
    or ``None`` if unknown. Never raises — a miss is normal."""
    from app.telegram.models import TelegramAccountLink

    link = None
    if tg_user_id:
        link = (
            db.query(TelegramAccountLink)
            .filter(TelegramAccountLink.tg_user_id == str(tg_user_id))
            .first()
        )
    if link is not None:
        if link.user_id is not None:
            return link.user_id
        by_alias = alias_user_id(link.alias)
        if by_alias is not None:
            return by_alias

    return alias_user_id(tg_username)
