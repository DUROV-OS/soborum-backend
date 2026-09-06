"""Шаг «саммари базы знаний за день».

Запускается оркестратором после всех сборщиков (Telegram-ingest и т.д.).
Отдаёт ИИ-агенту с доступом к базе знаний (чтение + запись) дневной файл
и системный промпт-регламент разбора.
"""

from __future__ import annotations

import logging
from datetime import date

from sqlalchemy.orm import Session

from app.core.config import settings
from app.jobs.dates import iso
from app.jobs.kb_agent import KbAgentUnavailable, run_kb_agent
from app.jobs.registry import JobResult

logger = logging.getLogger(__name__)

SUMMARY_SYSTEM_PROMPT = """Обработай дневной файл 01_Inbox/Daily/<ГГГГ-ММ-ДД>.md (вставь вчерашнюю дату).

1. Прочитай файл целиком.
2. Разнеси все записи из секции «🔗 Не разобрано» по тематическим секциям файла.
   Если запись реально ни в одну не ложится — оставь с пометкой почему.
3. По каждой записи определи жанр: факт / запись / гипотеза / вопрос без ответа.
   Для фактов — чем подтверждены; для непроверенного — пометь «требует проверки».
4. Прогонись по всему тексту 2–3 раза подряд и вычлени сказанное вскользь:
   названные цифры, отклонённые варианты и причина отказа, предпочтения Игоря,
   риски, вопросы без ответа. Пропущенное — допиши в нужную секцию.
5. В конец файла добавь блок «## Итоги дня» с тремя списками:
   - «В 02_Business на неделе» — что и в какой раздел переносится;
   - «Открытые вопросы владельцу» — проверив по базе, что они ещё не закрыты;
   - «Новые клиенты / сделки» — кандидаты в 03_Clients/.
6. Что требует немедленного действия или эскалации — вынеси отдельно и сразу.
   Если такого нет — напиши «срочного нет».
7. Поставь во frontmatter status: reviewed.
8. Ничего не удаляй из исходных записей — только структурируй и дополняй.
"""


def summarize_day(db: Session, day: date) -> JobResult:
    result = JobResult(name="kb_daily_summary", status="ok")

    path = f"{settings.kb_daily_dir}/{iso(day)}.md"
    user_content = (
        f"Вчерашняя дата: {iso(day)}. Файл для обработки: {path}.\n"
        f"Работай строго по регламенту из системного сообщения. Инструменты записи "
        f"базы знаний тебе доступны — вноси правки прямо в файл, не показывай их мне текстом.\n"
        f"В ответе дай короткий отчёт: что перенёс, что срочного нашёл, поставлен ли status: reviewed."
    )

    try:
        reply = run_kb_agent(
            db,
            system=SUMMARY_SYSTEM_PROMPT,
            user_content=user_content,
            allow_write=True,
        )
    except KbAgentUnavailable as e:
        return result.done("skipped", str(e))

    return result.done("ok", f"{path}: {reply[:600]}")
