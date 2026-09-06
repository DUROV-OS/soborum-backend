"""FastAPI-приложение раздела Telegram, монтируется в /api/telegram.

Даёт фронту: инфо о боте, выпуск диплинка для входа через Telegram
(кнопка в боковом меню), статус привязки текущего пользователя, а
администратору — просмотр архива сообщений, ручной запуск ingest/прогона
и правку словаря «алиас → сотрудник». Плюс приёмник вебхука как
альтернатива долгоживущему поллеру.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone

from fastapi import Depends, FastAPI, HTTPException, Path, Query, Response, status
from sqlalchemy.orm import Session

from app.core.config import settings
from app.core.deps import get_current_user, require_admin
from app.db.session import get_db
from app.jobs.dates import day_bounds_utc, yesterday
from app.telegram import service as tg_service
from app.telegram.client import TelegramApiError
from app.telegram.ingest import ingest_day
from app.telegram.models import TelegramAccountLink, TelegramMessage
from app.telegram.schemas import (
    BotInfoOut,
    DeepLinkOut,
    JobRunOut,
    LinkUpsertIn,
    TelegramLinkOut,
    TelegramMessageOut,
)
from app.users.models import User

app = FastAPI(
    title="Soborbum — Telegram",
    description="Мост с рабочим Telegram-чатом: ежедневная выгрузка сообщений в базу знаний, "
    "словарь «алиас → сотрудник» и вход в Durov-OS через диплинк бота.",
    version="0.1",
)


# --------------------------------------------------------------- общее --

@app.get("/bot", response_model=BotInfoOut)
def bot_info(_: User = Depends(get_current_user)):
    username = tg_service.bot_username() if settings.telegram_bot_token else None
    return BotInfoOut(
        configured=settings.telegram_configured,
        username=username,
        watched_chat_id=settings.telegram_chat_id or None,
    )


# --------------------------------------------- диплинк-логин --

@app.post("/login/deep-link", response_model=DeepLinkOut)
def issue_deep_link(db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    if not settings.telegram_bot_token:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, "Telegram-бот не настроен")
    username = tg_service.bot_username()
    if not username:
        raise HTTPException(status.HTTP_502_BAD_GATEWAY, "Не удалось получить имя бота у Telegram")

    token = tg_service.create_login_token(db, user)
    url = tg_service.deep_link_url(token.token, username=username)
    return DeepLinkOut(url=url, token=token.token, expires_at=token.expires_at, bot_username=username)


@app.get("/me/link", response_model=TelegramLinkOut)
def my_link(db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    link = tg_service.get_link_for_user(db, user.id)
    if link is None:
        return TelegramLinkOut(linked=False)
    return TelegramLinkOut(
        linked=link.user_id is not None,
        tg_user_id=link.tg_user_id,
        tg_username=link.tg_username,
        alias=link.alias,
        user_id=link.user_id,
        linked_at=link.linked_at,
    )


@app.delete("/me/link", status_code=status.HTTP_204_NO_CONTENT)
def unlink_me(db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    tg_service.unlink_user(db, user.id)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


# --------------------------------------------- словарь алиасов (админ) --

@app.get("/links", response_model=list[TelegramLinkOut])
def list_links(db: Session = Depends(get_db), _: User = Depends(require_admin)):
    rows = db.query(TelegramAccountLink).order_by(TelegramAccountLink.id).all()
    return [
        TelegramLinkOut(
            linked=r.user_id is not None,
            tg_user_id=r.tg_user_id,
            tg_username=r.tg_username,
            alias=r.alias,
            user_id=r.user_id,
            linked_at=r.linked_at,
        )
        for r in rows
    ]


@app.put("/links", response_model=TelegramLinkOut)
def upsert_link(payload: LinkUpsertIn, db: Session = Depends(get_db), _: User = Depends(require_admin)):
    if payload.user_id is not None and db.get(User, payload.user_id) is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Пользователь не найден")
    link = tg_service.link_account(
        db,
        tg_user_id=payload.tg_user_id,
        user_id=payload.user_id,
        tg_username=payload.tg_username,
        alias=payload.alias,
    )
    db.commit()
    db.refresh(link)
    return TelegramLinkOut(
        linked=link.user_id is not None,
        tg_user_id=link.tg_user_id,
        tg_username=link.tg_username,
        alias=link.alias,
        user_id=link.user_id,
        linked_at=link.linked_at,
    )


# --------------------------------------------- архив и ручной запуск --

@app.get("/messages", response_model=list[TelegramMessageOut])
def list_messages(
    on: date | None = Query(None, description="календарный день (kb-таймзона); по умолчанию — сутки назад"),
    db: Session = Depends(get_db),
    _: User = Depends(require_admin),
):
    if on is not None:
        since, until = day_bounds_utc(on)
    else:
        until = datetime.now(timezone.utc)
        since = until - timedelta(days=1)
    return tg_service.messages_in_window(db, since, until, chat_id=settings.telegram_chat_id or None)


@app.post("/run-ingest", response_model=JobRunOut)
def run_ingest(
    on: date | None = Query(None, description="день для выгрузки; по умолчанию вчера"),
    db: Session = Depends(get_db),
    _: User = Depends(require_admin),
):
    result = ingest_day(db, on or yesterday())
    return JobRunOut(name=result.name, status=result.status, detail=result.detail)


@app.post("/run-daily", response_model=list[JobRunOut])
def run_daily_endpoint(
    on: date | None = Query(None, description="день прогона; по умолчанию вчера"),
    _: User = Depends(require_admin),
):
    # Импорт здесь: app.jobs.daily тянет за собой регистрацию задач и не
    # нужен, пока никто не дёрнул ручной прогон.
    from app.jobs.daily import run_daily

    results = run_daily(on or yesterday())
    return [JobRunOut(name=r.name, status=r.status, detail=r.detail) for r in results]


# --------------------------------------------------------- вебхук --

@app.post("/webhook/{secret}")
def telegram_webhook(
    update: dict,
    secret: str = Path(...),
    db: Session = Depends(get_db),
):
    if not settings.telegram_webhook_secret or secret != settings.telegram_webhook_secret:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Not found")
    try:
        with tg_service.TelegramClient() as tg:
            tg_service.process_update(db, update, tg)
    except TelegramApiError:
        # Нет токена/сети для скачивания вложений — апдейт всё равно сохраняем
        # текстом, без файла.
        tg_service.process_update(db, update, None)
    return {"ok": True}
