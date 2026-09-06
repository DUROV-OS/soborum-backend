"""Ежедневный «проход бота» по Telegram-чату.

Раз в сутки: берём все сообщения группы за календарный день (в kb-таймзоне),
собираем их в один транскрипт, прикладываем скачанные вложения (PDF,
картинки) как контент-блоки и отдаём ИИ-агенту, который вносит каждую
запись в файл ``01_Inbox/Daily/<ГГГГ-ММ-ДД>.md`` базы знаний, в секцию
«🔗 Не разобрано». Разбор по темам и выводы — это уже следующий шаг
(app/jobs/kb_summary.py), здесь только «занести как есть».

Идемпотентность: разнесённые сообщения помечаются ``ingested_into_kb_at``,
повторный запуск за тот же день добавит только новые.
"""

from __future__ import annotations

import logging
from datetime import date

from sqlalchemy.orm import Session

from app.ai import attachments as ai_attachments
from app.common.files import FileAsset
from app.core.config import settings
from app.jobs.dates import as_kb_local, day_bounds_utc, iso
from app.jobs.kb_agent import KbAgentUnavailable, run_kb_agent
from app.jobs.registry import JobResult, daily_job
from app.telegram import service as tg_service
from app.telegram.aliases import resolve_user_id
from app.telegram.models import TelegramMessage
from app.users.models import User

logger = logging.getLogger(__name__)

MAX_ATTACHMENTS = 12  # держим один запрос к модели в разумных рамках

_SYSTEM = """Ты — секретарь базы знаний Durov OS. Тебе дают транскрипт рабочего
Telegram-чата за один день. Твоя единственная задача — аккуратно занести эти
сообщения в дневной файл базы знаний. НЕ анализируй, НЕ раскладывай по темам,
НЕ делай выводов — этим займётся отдельный шаг позже.

Файл: 01_Inbox/Daily/<ДАТА>.md (точная дата — в запросе).

Порядок:
1. read_note этого файла. Если его нет — create_note со следующим шаблоном:
---
type: daily
date: <ДАТА>
status: raw
source: telegram
---

# <ДАТА>

## 🔗 Не разобрано

## Люди
## Клиенты и сделки
## Производство
## Финансы
## Идеи
## Вопросы без ответа

2. В КОНЕЦ секции «## 🔗 Не разобрано» добавь по одной строке-пункту на каждое
   сообщение из транскрипта, в исходном порядке, форматом:
   - HH:MM <автор> — <текст сообщения дословно>
   Для вложений добавь в ту же строку короткую фактическую пометку в скобках:
   (вложение: <имя файла>, <1–2 фразы что на нём/в нём>). Не пересказывай
   вложение подробно, только суть.
3. Ничего не удаляй и не переписывай в файле. Только дописывай.
4. Если какие-то сообщения уже есть в секции (совпадает время и автор) —
   не дублируй их.
5. Не меняй frontmatter (в т.ч. status).

В ответе кратко напиши, сколько записей добавил и в какой файл."""


def _actor_label(db: Session, msg: TelegramMessage, cache: dict[str, str]) -> str:
    """Имя автора для записи: сотрудник Durov-OS, если Telegram-аккаунт
    сопоставлен, иначе — имя/ник из Telegram."""
    key = msg.tg_user_id or f"name:{msg.sender_name}"
    if key in cache:
        return cache[key]

    label = msg.sender_name or (f"@{msg.tg_username}" if msg.tg_username else "неизвестный")
    user_id = resolve_user_id(db, msg.tg_user_id, msg.tg_username)
    if user_id is not None:
        user = db.get(User, user_id)
        if user is not None:
            label = user.full_name
    cache[key] = label
    return label


def _transcript(db: Session, messages: list[TelegramMessage]) -> str:
    cache: dict[str, str] = {}
    lines: list[str] = []
    for m in messages:
        stamp = as_kb_local(m.sent_at).strftime("%H:%M")
        who = _actor_label(db, m, cache)
        body = (m.text or "").replace("\n", " ").strip()
        if m.kind == "photo":
            body = f"[фото] {body}".strip()
        elif m.kind == "document":
            fname = m.file_asset.filename if m.file_asset else "файл"
            body = f"[документ: {fname}] {body}".strip()
        elif m.kind == "other":
            body = f"[вложение без текста] {body}".strip()
        lines.append(f"{stamp} {who}: {body or '—'}")
    return "\n".join(lines)


def _attachment_blocks(db: Session, messages: list[TelegramMessage]) -> list[dict]:
    blocks: list[dict] = []
    for m in messages:
        if m.file_asset_id is None or len(blocks) >= MAX_ATTACHMENTS:
            continue
        asset = db.get(FileAsset, m.file_asset_id)
        if asset is None:
            continue
        stamp = as_kb_local(m.sent_at).strftime("%H:%M")
        blocks.append({"type": "text", "text": f"↓ вложение к сообщению {stamp}, файл «{asset.filename}»:"})
        blocks.append(ai_attachments.build_content_block(asset))
    return blocks


def ingest_day(db: Session, day: date) -> JobResult:
    result = JobResult(name="telegram_ingest", status="ok")
    if not settings.telegram_configured:
        return result.done("skipped", "TELEGRAM_BOT_TOKEN/CHAT_ID не заданы")

    since, until = day_bounds_utc(day)
    messages = tg_service.messages_in_window(
        db, since, until, chat_id=settings.telegram_chat_id, only_uningested=True
    )
    if not messages:
        return result.done("skipped", f"нет новых сообщений за {iso(day)}")

    user_content: list[dict] = [
        {
            "type": "text",
            "text": (
                f"Дата: {iso(day)}. Файл: {settings.kb_daily_dir}/{iso(day)}.md\n\n"
                f"Транскрипт Telegram-чата ({len(messages)} сообщений):\n\n" + _transcript(db, messages)
            ),
        }
    ]
    user_content += _attachment_blocks(db, messages)

    try:
        reply = run_kb_agent(
            db,
            system=_SYSTEM.replace("<ДАТА>", iso(day)),
            user_content=user_content,
            allow_write=True,
        )
    except KbAgentUnavailable as e:
        return result.done("skipped", str(e))

    tg_service.mark_ingested(db, messages)
    return result.done("ok", f"{len(messages)} сообщений → {settings.kb_daily_dir}/{iso(day)}.md. {reply[:400]}")


@daily_job("telegram_ingest")
def _job(db: Session, day: date) -> JobResult:
    return ingest_day(db, day)
