"""Бизнес-логика уведомлений (0080-b): создание с проверкой мьюта, список,
отметка прочитанным, настройка мьютов.

Генерацию уведомлений под конкретные события (задачи просрочены, обновления
клиента и т.д.) добавляет 0080-c — здесь только точка входа `notify`, через
которую должен идти любой будущий генератор, чтобы мьют соблюдался везде
одинаково."""

from datetime import datetime, timezone

from sqlalchemy import func
from sqlalchemy.orm import Session

from app.common.module_access import Module
from app.notifications.models import Notification, NotificationKind, NotificationMute, PushSubscription
from app.notifications.push import PushSubscriptionGone, send_web_push


def is_muted(db: Session, user_id: int, module: Module, object_id: int | None) -> bool:
    """Замьючен ли повод у получателя: сначала раздел целиком
    (`object_id IS NULL` в записи мьюта), затем — если `object_id` передан —
    конкретный объект."""
    whole_module_muted = (
        db.query(NotificationMute)
        .filter(
            NotificationMute.user_id == user_id,
            NotificationMute.module == module,
            NotificationMute.object_id.is_(None),
        )
        .first()
        is not None
    )
    if whole_module_muted:
        return True
    if object_id is None:
        return False
    return (
        db.query(NotificationMute)
        .filter(
            NotificationMute.user_id == user_id,
            NotificationMute.module == module,
            NotificationMute.object_id == object_id,
        )
        .first()
        is not None
    )


def notify(
    db: Session,
    user_id: int,
    *,
    kind: NotificationKind,
    module: Module,
    title: str,
    body: str | None = None,
    object_type: str | None = None,
    object_id: int | None = None,
) -> Notification | None:
    """Создаёт уведомление получателю, если он не замьютил `module`/`object_id`.
    Возвращает `None` без создания записи, если замьючено — вызывающему это
    не ошибка, просто уведомление не появилось."""
    if is_muted(db, user_id, module, object_id):
        return None
    notification = Notification(
        user_id=user_id,
        kind=kind,
        title=title,
        body=body,
        object_type=object_type,
        object_id=object_id,
    )
    db.add(notification)
    db.commit()
    db.refresh(notification)
    _push_to_subscriptions(db, notification)
    return notification


def _push_to_subscriptions(db: Session, notification: Notification) -> None:
    """Рассылает push во все подписки получателя (0080-e). Невалидные
    (404/410 от push-сервиса) удаляются сразу — следующий `notify()` на
    них уже не наткнётся."""
    subscriptions = (
        db.query(PushSubscription).filter(PushSubscription.user_id == notification.user_id).all()
    )
    if not subscriptions:
        return
    payload = {"title": notification.title, "body": notification.body or ""}
    for subscription in subscriptions:
        subscription_info = {
            "endpoint": subscription.endpoint,
            "keys": {"p256dh": subscription.p256dh, "auth": subscription.auth},
        }
        try:
            send_web_push(subscription_info, payload)
        except PushSubscriptionGone:
            db.delete(subscription)
            db.commit()


def save_push_subscription(db: Session, user_id: int, endpoint: str, p256dh: str, auth: str) -> PushSubscription:
    """Сохраняет подписку или обновляет её ключи, если браузер уже был
    подписан с этим `endpoint` (ключ уникальности — `endpoint`, не
    `user_id`: endpoint один на связку браузер+origin, а не на пользователя,
    но в редком случае смены пользователя на том же браузере ключи могут
    обновиться — пересохраняем их на новую подписку)."""
    existing = db.query(PushSubscription).filter(PushSubscription.endpoint == endpoint).first()
    if existing is not None:
        existing.user_id = user_id
        existing.p256dh = p256dh
        existing.auth = auth
        db.commit()
        db.refresh(existing)
        return existing
    subscription = PushSubscription(user_id=user_id, endpoint=endpoint, p256dh=p256dh, auth=auth)
    db.add(subscription)
    db.commit()
    db.refresh(subscription)
    return subscription


def delete_push_subscription(db: Session, user_id: int, endpoint: str) -> None:
    """Отписка. Молча ничего не делает, если такой подписки у пользователя
    нет (повторный вызов, отписка на другом устройстве и т.п.)."""
    db.query(PushSubscription).filter(
        PushSubscription.user_id == user_id, PushSubscription.endpoint == endpoint
    ).delete()
    db.commit()


def list_notifications(
    db: Session, user_id: int, *, unread_only: bool = False, limit: int = 50, offset: int = 0
) -> list[Notification]:
    """Новые сверху, с пагинацией (`limit`/`offset`)."""
    query = db.query(Notification).filter(Notification.user_id == user_id)
    if unread_only:
        query = query.filter(Notification.read_at.is_(None))
    return query.order_by(Notification.id.desc()).offset(offset).limit(limit).all()


def unread_count(db: Session, user_id: int) -> int:
    return (
        db.query(func.count(Notification.id))
        .filter(Notification.user_id == user_id, Notification.read_at.is_(None))
        .scalar()
        or 0
    )


def mark_read(db: Session, notification: Notification) -> Notification:
    if notification.read_at is None:
        notification.read_at = datetime.now(timezone.utc)
        db.commit()
        db.refresh(notification)
    return notification


def mark_all_read(db: Session, user_id: int) -> int:
    """Возвращает число записей, которые были отмечены этим вызовом (уже
    прочитанные не трогает и не считает)."""
    now = datetime.now(timezone.utc)
    updated = (
        db.query(Notification)
        .filter(Notification.user_id == user_id, Notification.read_at.is_(None))
        .update({Notification.read_at: now}, synchronize_session=False)
    )
    db.commit()
    return updated


def list_mutes(db: Session, user_id: int) -> list[NotificationMute]:
    return db.query(NotificationMute).filter(NotificationMute.user_id == user_id).order_by(NotificationMute.id).all()


def set_mute(db: Session, user_id: int, module: Module, object_id: int | None, muted: bool) -> None:
    """Включает (`muted=True`) или снимает мьют раздела целиком
    (`object_id=None`) или конкретного объекта. Идемпотентно: повторное
    включение/снятие уже установленного состояния ничего не меняет."""
    existing = (
        db.query(NotificationMute)
        .filter(
            NotificationMute.user_id == user_id,
            NotificationMute.module == module,
            NotificationMute.object_id == object_id if object_id is not None else NotificationMute.object_id.is_(None),
        )
        .first()
    )
    if muted and existing is None:
        db.add(NotificationMute(user_id=user_id, module=module, object_id=object_id))
        db.commit()
    elif not muted and existing is not None:
        db.delete(existing)
        db.commit()
