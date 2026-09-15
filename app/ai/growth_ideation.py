"""Раздел «Марина» → «Развитие» (0036): реальная генерация предложений по
развитию бизнеса взамен демо-сида (0050-a). Строит срез по тем же данным, что
и «Пульс» (app.dashboard.service.build_snapshot), просит Claude предложить
идеи, приоритет которых — бизнес-инициативы, а не только технические/
процессные доработки, и заменяет открытые (status=open) предложения новым
набором. Уже подготовленные (status=task_created) предложения не трогает.
"""

import json
from datetime import datetime, timezone

import anthropic
from fastapi import HTTPException, status
from sqlalchemy.orm import Session

from app.ai.models import GrowthProposal, GrowthProposalStatus
from app.core.config import settings
from app.dashboard.service import build_snapshot
from app.users.models import User

SUBMIT_TOOL_NAME = "submit_growth_proposals"

REQUIRED_FIELDS = ("title", "problem", "checkable_result", "executor_and_estimate", "expected_effect")

SYSTEM_PROMPT = (
    "Ты — стратегический советник владельца компании «Soborbum», которая производит модульные дома. "
    "Тебе передан срез реальных агрегированных цифр по разделам предприятия, доступным текущему "
    "пользователю (клиенты, производство, монтаж, склад, маркетинг, задачи, финансы). Придумай 3 "
    "предложения по развитию бизнеса.\n\n"
    "Посмотри на срез через несколько линз, чтобы не зацикливаться на одном разделе или только на "
    "технических правках:\n"
    "- ТРИЗ: какое противоречие мешает бизнесу расти и какой ресурс, уже имеющийся в компании (люди, "
    "остатки на складе, отношения с клиентами или поставщиками), можно использовать, чтобы его снять, "
    "без затрат на новые ресурсы.\n"
    "- Матрица Ансоффа / Blue Ocean Strategy: не только «улучшить то, что уже есть» — рассмотри новый "
    "рынок сбыта, новый сегмент клиентов, новое позиционирование или коллаборацию с другим брендом.\n"
    "- SCAMPER: замени, объедини, адаптируй, измени объём или тематику, найди другое применение или "
    "убери то, что мешает — в каталоге типовых проектов, в контенте, в списке поставщиков.\n"
    "- Business Model Canvas: не концентрируйся на одном разделе — переберни каналы сбыта, "
    "партнёрства и источники дохода, а не только внутренний процесс.\n\n"
    "Правила:\n"
    "- Не менее 2 из 3 предложений должны быть бизнес-инициативами уровня «как заработать/вырасти» "
    "(сменить поставщика по позиции, дать точечную скидку конкретному клиенту, добавить в каталог "
    "новую конфигурацию типового проекта, перераспределить задачи между сотрудниками, изменить объём "
    "или тематику контента, выйти на новый рынок сбыта, коллаборация с другим брендом и т.п.), а не "
    "техническая или процессная доработка системы. Остальное предложение может быть техническим или "
    "процессным улучшением.\n"
    "- Каждое предложение опирается СТРОГО на реальные сущности и цифры из переданного среза — "
    "конкретного клиента, поставщика, сотрудника или показатель, который есть во входных данных. Не "
    "выдумывай клиентов, поставщиков, суммы или факты, которых там нет; если для идеи не хватает "
    "конкретной сущности из среза — сформулируй её более общо, но не придумывай деталь.\n"
    "- Будь инициативным и смелым: не ограничивайся самыми безопасными и очевидными идеями, предлагай "
    "ходы уровня решения владельца бизнеса.\n"
    "- title — короткое название предложения. problem — в чём проблема или возможность (2-3 "
    "предложения со ссылкой на конкретные цифры или сущности среза). checkable_result — как проверить, "
    "что предложение сработало (конкретный измеримый критерий). executor_and_estimate — кто исполнитель "
    "и ориентировочный срок или трудозатраты. expected_effect — ожидаемый эффект для бизнеса.\n"
    "- Все поля — на русском языке, без markdown-разметки.\n"
    "- Отвечай ТОЛЬКО вызовом инструмента submit_growth_proposals, без текста."
)

TOOL_SCHEMA = {
    "name": SUBMIT_TOOL_NAME,
    "description": "Отправить 3 предложения по развитию бизнеса.",
    "input_schema": {
        "type": "object",
        "properties": {
            "proposals": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "title": {"type": "string"},
                        "problem": {"type": "string"},
                        "checkable_result": {"type": "string"},
                        "executor_and_estimate": {"type": "string"},
                        "expected_effect": {"type": "string"},
                    },
                    "required": list(REQUIRED_FIELDS),
                },
            },
        },
        "required": ["proposals"],
    },
}


def _get_client() -> anthropic.Anthropic:
    if not settings.anthropic_api_key:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="ИИ не настроена: не задан ANTHROPIC_API_KEY (см. backend/.env)",
        )
    from app.core.llm import anthropic_client

    return anthropic_client()


def generate_growth_proposals(db: Session, user: User) -> list[GrowthProposal]:
    """Строит срез компании и просит Claude предложить 3 идеи развития.
    Не трогает БД — возвращает несохранённые объекты GrowthProposal, замену
    существующих строк делает вызывающий код одной транзакцией."""
    snapshot = build_snapshot(db, user)

    client = _get_client()
    response = client.messages.create(
        model=settings.ai_model,
        max_tokens=2048,
        system=SYSTEM_PROMPT,
        messages=[
            {
                "role": "user",
                "content": f"Срез компании на {datetime.now(timezone.utc).date().isoformat()}:\n\n"
                + json.dumps(snapshot, ensure_ascii=False, default=str),
            }
        ],
        tools=[TOOL_SCHEMA],
        tool_choice={"type": "tool", "name": SUBMIT_TOOL_NAME},
    )

    tool_use = next((block for block in response.content if block.type == "tool_use"), None)
    if tool_use is None:
        raise HTTPException(status_code=status.HTTP_502_BAD_GATEWAY, detail="ИИ не вернул предложения")

    proposals: list[GrowthProposal] = []
    for item in tool_use.input.get("proposals", []):
        if not all(str(item.get(field, "")).strip() for field in REQUIRED_FIELDS):
            raise HTTPException(status_code=status.HTTP_502_BAD_GATEWAY, detail="ИИ вернул неполное предложение")
        proposals.append(
            GrowthProposal(
                title=item["title"],
                problem=item["problem"],
                checkable_result=item["checkable_result"],
                executor_and_estimate=item["executor_and_estimate"],
                expected_effect=item["expected_effect"],
                status=GrowthProposalStatus.OPEN,
            )
        )

    if not proposals:
        raise HTTPException(status_code=status.HTTP_502_BAD_GATEWAY, detail="ИИ не вернул предложения")

    return proposals
