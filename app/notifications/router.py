"""API уведомлений (0080-b): лента для текущего пользователя и его настройки
мьюта. Своё, не чужое — как заявки «Пожелания» (`app/feedback/router.py`),
доступ — любому вошедшему (`get_current_user`), без привязки к `Module`:
уведомление принадлежит получателю независимо от того, есть ли у него грант
на раздел, о котором оно сообщает."""

from fastapi import Depends, FastAPI, HTTPException, Query, status
from sqlalchemy.orm import Session

from app.db.session import get_db
from app.core.deps import get_current_user
from app.notifications.models import Notification
from app.notifications.schemas import (
    NotificationMuteOut,
    NotificationMuteUpdate,
    NotificationOut,
    UnreadCountOut,
)
from app.notifications.service import (
    list_mutes,
    list_notifications,
    mark_all_read,
    mark_read,
    set_mute,
    unread_count,
)
from app.users.models import User

app = FastAPI(
    title="Soborbum — Уведомления",
    description="Лента уведомлений пользователя и настройки мьюта по разделам/объектам.",
    version="0.1.0",
)


@app.get("/", response_model=list[NotificationOut])
def list_my_notifications(
    unread_only: bool = Query(default=False),
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    return list_notifications(db, user.id, unread_only=unread_only, limit=limit, offset=offset)


@app.get("/unread-count", response_model=UnreadCountOut)
def get_unread_count(db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    return UnreadCountOut(unread_count=unread_count(db, user.id))


@app.post("/{notification_id}/read", response_model=NotificationOut)
def read_notification(
    notification_id: int, db: Session = Depends(get_db), user: User = Depends(get_current_user)
):
    notification = db.get(Notification, notification_id)
    if not notification or notification.user_id != user.id:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Уведомление не найдено")
    return mark_read(db, notification)


@app.post("/read-all", response_model=UnreadCountOut)
def read_all_notifications(db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    mark_all_read(db, user.id)
    return UnreadCountOut(unread_count=unread_count(db, user.id))


@app.get("/mutes", response_model=list[NotificationMuteOut])
def get_mutes(db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    return list_mutes(db, user.id)


@app.put("/mutes", response_model=list[NotificationMuteOut])
def put_mute(
    payload: NotificationMuteUpdate, db: Session = Depends(get_db), user: User = Depends(get_current_user)
):
    set_mute(db, user.id, payload.module, payload.object_id, payload.muted)
    return list_mutes(db, user.id)
