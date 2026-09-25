"""Разовая подписка MAX-бота на webhook (0082).

Запуск после деплоя, из backend/ с заданными MAX_BOT_TOKEN и MAX_WEBHOOK_SECRET:

    python -m scripts.max_bot_subscribe https://<прод-домен>/api/max/webhook

URL — только https на 443 с доверенным сертификатом (требование MAX). Пока
подписка активна, long polling (MAX_BOT_POLLING) событий не получает.
"""

import sys

from app.core.config import settings
from app.max import bot_api

UPDATE_TYPES = [
    "message_created", "message_edited", "message_removed",
    "bot_started", "bot_stopped", "bot_added", "bot_removed",
    "dialog_removed", "chat_title_changed",
]


def main() -> None:
    if len(sys.argv) != 2 or not sys.argv[1].startswith("https://"):
        sys.exit("usage: python -m scripts.max_bot_subscribe https://<домен>/api/max/webhook")
    if not settings.max_webhook_secret:
        sys.exit("MAX_WEBHOOK_SECRET не задан")
    print(bot_api.subscribe(sys.argv[1], settings.max_webhook_secret, UPDATE_TYPES))


if __name__ == "__main__":
    main()
