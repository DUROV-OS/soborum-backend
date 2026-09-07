"""«Актуализировать»: a single data-driven refresh pass over the whole board
tree, independent of the council flow - no proposal, no accept/reject, just
"does this node's description/status still match reality". Reuses the exact
same section snapshots the "Сегодня" dashboard is built from (see
app.dashboard.service.SECTION_BUILDERS) so there's no parallel data-gathering
logic and the numbers the model sees are the real, current ones. Content
only: this never creates/deletes nodes, unlike the council's structural
authority.
"""

import json
from datetime import datetime, timezone

import anthropic
from fastapi import HTTPException, status
from sqlalchemy.orm import Session

from app.board import prompts
from app.board import service as board_service
from app.board.models import BoardChangeSource, BoardChangeType, BoardNode, BoardNodeChange
from app.core.config import settings
from app.dashboard.service import SECTION_BUILDERS
from app.users.models import User


def _get_client() -> anthropic.Anthropic:
    if not settings.anthropic_api_key:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="ИИ не настроен: не задан ANTHROPIC_API_KEY (см. backend/.env)",
        )
    from app.core.llm import anthropic_client

    return anthropic_client()


def _serialize_tree(node: BoardNode) -> list[dict]:
    return [
        {"id": n.id, "parent_id": n.parent_id, "level": n.level, "title": n.title, "description": n.description, "color": n.color.value}
        for n in board_service.flatten(node)
    ]


def actualize(db: Session, root: BoardNode, actor: User) -> list[BoardNodeChange]:
    operational_data = {key: builder(db) for key, (_module, builder) in SECTION_BUILDERS.items()}
    payload = {
        "as_of": datetime.now(timezone.utc).date().isoformat(),
        "tree": _serialize_tree(root),
        "operational_data": operational_data,
    }

    client = _get_client()
    response = client.messages.create(
        model=settings.ai_model,
        max_tokens=4096,
        system=prompts.ACTUALIZE_SYSTEM_PROMPT,
        messages=[{"role": "user", "content": json.dumps(payload, ensure_ascii=False, default=str)}],
        tools=[prompts.actualize_tool_schema()],
        tool_choice={"type": "tool", "name": prompts.ACTUALIZE_TOOL_NAME},
    )
    tool_use = next((b for b in response.content if b.type == "tool_use" and b.name == prompts.ACTUALIZE_TOOL_NAME), None)
    if tool_use is None:
        raise HTTPException(status_code=status.HTTP_502_BAD_GATEWAY, detail="ИИ не вернул обновления")

    by_id = {n.id: n for n in board_service.flatten(root)}
    changes: list[BoardNodeChange] = []
    for update in tool_use.input.get("updates", []):
        node = by_id.get(update.get("node_id"))
        if node is None or not update.get("needs_change"):
            continue
        old_description, old_color = node.description, node.color.value
        new_description = update.get("new_description")
        if new_description:
            node.description = new_description
            node.summary = update.get("summary") or node.summary
        new_color = board_service.safe_color(update.get("new_color"))
        if new_color:
            node.color = new_color
        db.flush()
        changes.append(
            board_service.log_change(
                db, node, BoardChangeType.UPDATED, BoardChangeSource.ACTUALIZE, None, actor,
                old_description=old_description, new_description=node.description,
                old_color=old_color, new_color=node.color.value,
                note=update.get("change_summary") or "Актуализация по операционным данным",
            )
        )

    db.commit()
    return changes
