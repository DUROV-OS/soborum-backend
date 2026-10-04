import hmac

from fastapi import Depends, FastAPI, Header, HTTPException, Request, status
from fastapi.responses import JSONResponse
from sqlalchemy.orm import Session

from app.common.module_access import Module
from app.conversations import service, telegram_channel
from app.conversations.schemas import (
    ConversationMessageOut,
    ConversationOut,
    ChannelStateOut,
    InviteCreate,
    MessageCreate,
)
from app.conversations.service import ChannelSendFailed, ChannelUnavailable, OwnerKind
from app.core.config import settings
from app.core.deps import require_edit, require_view
from app.db.session import get_db
from app.users.models import User

app = FastAPI(
    title="Soborbum — Переписка",
    description="Переписка с клиентами и партнёрами из их карточек через ботов "
    "MAX / Telegram / WhatsApp: ссылки-приглашения, единая лента, отправка.",
    version="0.1.0",
)

# Переписку ведут те же люди, что и клиентов/партнёров (0083): право — «Клиенты».
# Читать ленту могут все с просмотром — «доступна для чтения всем сотрудникам
# с правами доступа» (запрос заказчика).
require_conversations_view = require_view(Module.CLIENTS)
require_conversations_edit = require_edit(Module.CLIENTS)


@app.exception_handler(ChannelUnavailable)
def _channel_unavailable(_: Request, exc: ChannelUnavailable):
    # detail — строкой, чтобы общий разбор ошибок фронта показал текст как есть;
    # code/available — для кнопок переключения на другие каналы.
    return JSONResponse(
        status_code=status.HTTP_409_CONFLICT,
        content={
            "detail": exc.message,
            "code": "channel_unavailable",
            "available": [k.value for k in exc.available],
        },
    )


@app.exception_handler(ChannelSendFailed)
def _channel_send_failed(_: Request, exc: ChannelSendFailed):
    return JSONResponse(
        status_code=status.HTTP_502_BAD_GATEWAY,
        content={"detail": f"Сообщение не доставлено: {exc.message}", "code": "channel_send_failed"},
    )


@app.post("/telegram/webhook")
async def telegram_webhook(
    request: Request,
    x_telegram_bot_api_secret_token: str | None = Header(default=None),
    db: Session = Depends(get_db),
):
    """Приём событий Telegram-бота (0083-e). Без JWT — вместо него секрет,
    заданный при setWebhook (scripts/telegram_set_webhook.py); не задан или не
    совпал → 403 и ничего не пишется."""
    expected = settings.telegram_webhook_secret
    if not expected or not hmac.compare_digest(x_telegram_bot_api_secret_token or "", expected):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Неверный секрет webhook")
    body = await request.json()
    if isinstance(body, dict):
        telegram_channel.handle_updates(db, [body])
    return {"ok": True}


@app.get("/{owner}/{owner_id}", response_model=ConversationOut)
def get_conversation(
    owner: OwnerKind, owner_id: int, db: Session = Depends(get_db), _: User = Depends(require_conversations_view)
):
    obj = service.get_owner_or_404(db, owner, owner_id)
    return {"channels": service.channel_states(db, obj), "messages": service.feed(db, obj)}


@app.post("/{owner}/{owner_id}/invite", response_model=ChannelStateOut)
def create_invite(
    owner: OwnerKind,
    owner_id: int,
    payload: InviteCreate,
    db: Session = Depends(get_db),
    _: User = Depends(require_conversations_edit),
):
    """Ссылка на бота с меткой карточки; повторный вызов отдаёт ту же ссылку."""
    obj = service.get_owner_or_404(db, owner, owner_id)
    service.get_or_create_invite(db, obj, payload.channel)
    db.commit()
    return next(s for s in service.channel_states(db, obj) if s["channel"] == payload.channel)


@app.post("/{owner}/{owner_id}/messages", response_model=ConversationMessageOut, status_code=status.HTTP_201_CREATED)
def send_message(
    owner: OwnerKind,
    owner_id: int,
    payload: MessageCreate,
    db: Session = Depends(get_db),
    user: User = Depends(require_conversations_edit),
):
    obj = service.get_owner_or_404(db, owner, owner_id)
    message = service.send(db, obj, payload.channel, payload.text, user)
    db.commit()
    return next(m for m in service.feed(db, obj) if m["id"] == message.id)
