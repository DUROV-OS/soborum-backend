"""Разовая установка webhook Telegram-бота для переписки из карточек (0083-e).

Запуск после деплоя, из backend/ с заданными TELEGRAM_BOT_TOKEN и
TELEGRAM_WEBHOOK_SECRET:

    python -m scripts.telegram_set_webhook https://<домен>/api/conversations/telegram/webhook

Пока webhook выставлен, long polling (TELEGRAM_BOT_POLLING) событий не получает.
"""

import sys

from app.conversations import telegram_api
from app.core.config import settings


def main() -> None:
    if len(sys.argv) != 2 or not sys.argv[1].startswith("https://"):
        sys.exit("usage: python -m scripts.telegram_set_webhook https://<домен>/api/conversations/telegram/webhook")
    if not settings.telegram_webhook_secret:
        sys.exit("TELEGRAM_WEBHOOK_SECRET не задан")
    print(telegram_api.set_webhook(sys.argv[1], settings.telegram_webhook_secret))


if __name__ == "__main__":
    main()
