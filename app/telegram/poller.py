"""Long-poll воркер Telegram.

Отдельный долгоживущий процесс: тянет getUpdates и складывает сообщения
наблюдаемой группы в ``telegram_messages`` по мере поступления (Bot API не
умеет отдавать историю чата — единственный способ иметь «сутки сообщений»
к приходу ежедневной задачи это писать их сразу). Здесь же обрабатывается
``/start login_<token>`` в личке бота.

Запуск:

    python -m app.telegram.poller

Альтернатива без отдельного процесса — вебхук
``POST /api/telegram/webhook/{secret}`` (см. app/telegram/router.py),
он зовёт тот же :func:`app.telegram.service.process_update`.
"""

from __future__ import annotations

import logging
import signal
import time

from app.core.config import settings
from app.db.session import SessionLocal
from app.telegram import service as tg_service
from app.telegram.client import TelegramApiError, TelegramClient

logger = logging.getLogger("app.telegram.poller")

_stop = False


def _handle_signal(signum, _frame) -> None:
    global _stop
    _stop = True
    logger.info("Получен сигнал %s, останавливаюсь после текущего батча", signum)


def run() -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    if not settings.telegram_configured:
        logger.error("TELEGRAM_BOT_TOKEN / CHAT_ID не заданы — поллеру нечего делать")
        return 1

    signal.signal(signal.SIGINT, _handle_signal)
    signal.signal(signal.SIGTERM, _handle_signal)

    tg = TelegramClient()
    me = tg.get_me()
    logger.info("Поллер запущен как @%s, наблюдаю чат %s", me.get("username"), settings.telegram_chat_id)

    backoff = 1
    while not _stop:
        try:
            with SessionLocal() as db:
                offset = tg_service.get_offset(db)
            updates = tg.get_updates(offset=offset, timeout=50)
            backoff = 1
            if not updates:
                continue
            with SessionLocal() as db:
                for update in updates:
                    tg_service.process_update(db, update, tg)
            logger.info("Обработано апдейтов: %d", len(updates))
        except TelegramApiError as e:
            logger.warning("Ошибка Telegram API: %s; пауза %ds", e, backoff)
            time.sleep(backoff)
            backoff = min(backoff * 2, 60)
        except Exception:  # noqa: BLE001 — воркер не должен падать целиком
            logger.exception("Непредвиденная ошибка в цикле поллера; пауза %ds", backoff)
            time.sleep(backoff)
            backoff = min(backoff * 2, 60)

    tg.close()
    logger.info("Поллер остановлен")
    return 0


if __name__ == "__main__":
    raise SystemExit(run())
