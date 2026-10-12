"""Низкоуровневая отправка одного web-push сообщения (0080-e). Обёртка над
`pywebpush`, чтобы `service.py` не знал деталей библиотеки и тестам было
что мокать одной точкой, не трогая реальную сеть."""

import json
import logging

from pywebpush import WebPushException, webpush

from app.core.config import settings

log = logging.getLogger("app.notifications.push")


class PushSubscriptionGone(Exception):
    """Подписка больше не действительна (push-сервис ответил 404/410) —
    вызывающий должен удалить её из БД."""


def send_web_push(subscription_info: dict, payload: dict) -> None:
    """Отправляет один push. Поднимает `PushSubscriptionGone`, если подписка
    просрочена/отозвана (404/410). Любую другую ошибку логирует и глушит —
    сбой push-сервиса (сеть, 5xx и т.д.) не должен мешать созданию
    уведомления в приложении. Ничего не делает, если VAPID-ключи не заданы
    (`settings.push_configured`), например в тестовом/локальном окружении
    без настроенного push."""
    if not settings.push_configured:
        return
    try:
        webpush(
            subscription_info=subscription_info,
            data=json.dumps(payload),
            vapid_private_key=settings.vapid_private_key,
            vapid_claims={"sub": settings.vapid_subject},
        )
    except WebPushException as exc:
        status_code = exc.response.status_code if exc.response is not None else None
        if status_code in (404, 410):
            raise PushSubscriptionGone from exc
        log.warning("web push failed (status=%s): %s", status_code, exc)
