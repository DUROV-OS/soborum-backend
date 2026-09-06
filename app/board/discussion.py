"""Free-form discussion with the "Совет директоров": a plain chat thread,
optionally pinned to one node, that never edits the tree. For actually
changing a node the employee still goes through app.board.council (POST
/nodes/{id}/propose). Here the board only talks - it answers questions,
thinks options through, and, when asked (consult_council=True), polls the
same 7 council roles for their individual takes on the question.

Each assistant turn is a single Claude call with the knowledge-base MCP
connector and web_search attached server-side (same shape as
app.board.conductor / app.ai.engine) - no local tool-execution loop.
"""

import json
from datetime import datetime, timezone

import anthropic
from fastapi import HTTPException, status
from sqlalchemy.orm import Session

from app.ai import mcp_auth
from app.ai import model_profiles
from app.ai.model_profiles import ModelProfile
from app.board import conductor as board_conductor
from app.board import council as board_council
from app.board import prompts
from app.board import service as board_service
from app.board.models import BoardDiscussion, BoardDiscussionMessage, BoardNode
from app.core.config import settings
from app.users.models import User

# How many trailing messages of a thread to replay to the model. Threads are
# expected to stay short; this is a guard against a runaway-long one blowing
# the context window, not a routine truncation.
MAX_HISTORY_MESSAGES = 40


def _get_client() -> anthropic.Anthropic:
    if not settings.anthropic_api_key:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="ИИ не настроен: не задан ANTHROPIC_API_KEY (см. backend/.env)",
        )
    return anthropic.Anthropic(api_key=settings.anthropic_api_key)


# ------------------------------------------------------------------ context --

def _tree_overview(node: BoardNode) -> dict:
    return {
        "id": node.id,
        "title": node.title,
        "level": node.level,
        "color": node.color.value,
        "summary": node.summary or (node.description or "")[:600],
        "children": [_tree_overview(c) for c in node.children],
    }


def _discussion_context(db: Session, discussion: BoardDiscussion) -> dict:
    ctx: dict = {}
    if discussion.node is not None:
        ctx["node"] = board_service.node_context(discussion.node)
    else:
        ctx["scope"] = "Обсуждение не привязано к конкретной ноде — речь про компанию в целом."
    root = board_service.get_root(db)
    if root is not None:
        ctx["tree_overview"] = _tree_overview(root)
    return ctx


def _history_blocks(discussion: BoardDiscussion) -> list[dict]:
    msgs = list(discussion.messages)[-MAX_HISTORY_MESSAGES:]
    return [{"role": m.role, "content": m.content} for m in msgs]


def _history_note(discussion: BoardDiscussion) -> str | None:
    """Compact recap of the thread so far, for the per-role council prompts
    (which take a single string, not a message list)."""
    msgs = list(discussion.messages)[-8:]
    if len(msgs) <= 1:
        return None
    lines = []
    for m in msgs[:-1]:
        who = "Сотрудник" if m.role == "user" else "Совет"
        lines.append(f"{who}: {m.content.strip()[:600]}")
    return "Предыдущий ход обсуждения:\n" + "\n".join(lines)


# ------------------------------------------------------------------- answer --

def _call(db: Session, system: str, messages: list[dict]) -> str:
    client = _get_client()
    kwargs = {
        **model_profiles.profile_params(ModelProfile.BOARD_LEAD),
        "max_tokens": 4096,
        "system": system,
        "messages": messages,
        "tools": [board_conductor.WEB_SEARCH_TOOL],
    }
    if settings.mcp_configured:
        mcp_server = {
            "type": "url",
            "url": settings.mcp_server_url,
            "name": "knowledge-base",
            "authorization_token": mcp_auth.get_access_token(db),
            "tool_configuration": {"allowed_tools": board_conductor.MCP_READ_ONLY_TOOLS},
        }
        response = client.beta.messages.create(
            betas=[board_conductor.MCP_BETA], mcp_servers=[mcp_server], **kwargs
        )
    else:
        response = client.messages.create(**kwargs)

    text = "".join(
        getattr(b, "text", "") for b in response.content if getattr(b, "type", None) == "text"
    ).strip()
    if not text:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY, detail="Совет директоров не смог сформулировать ответ"
        )
    return text


def _answer(db: Session, discussion: BoardDiscussion, opinions: list[dict], research_brief: str | None) -> str:
    system = prompts.DISCUSSION_SYSTEM_PROMPT
    system += "\n\nСПРАВОЧНЫЙ КОНТЕКСТ:\n" + json.dumps(_discussion_context(db, discussion), ensure_ascii=False, default=str)
    if research_brief:
        system += "\n\nСВОДКА АГЕНТА-ДИРИЖЁРА (база знаний + интернет) по последнему вопросу:\n" + research_brief
    if opinions:
        system += "\n\nМНЕНИЯ ЧЛЕНОВ СОВЕТА по последнему вопросу сотрудника:\n" + json.dumps(
            opinions, ensure_ascii=False, default=str
        )
    return _call(db, system, _history_blocks(discussion))


# -------------------------------------------------------------------- CRUD --

def get_discussion_or_404(db: Session, discussion_id: int) -> BoardDiscussion:
    discussion = db.get(BoardDiscussion, discussion_id)
    if discussion is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Обсуждение не найдено")
    return discussion


def list_discussions(db: Session, node_id: int | None, owner: User | None) -> list[BoardDiscussion]:
    query = db.query(BoardDiscussion)
    if node_id is not None:
        query = query.filter(BoardDiscussion.node_id == node_id)
    if owner is not None:
        query = query.filter(BoardDiscussion.created_by_id == owner.id)
    return query.order_by(BoardDiscussion.updated_at.desc(), BoardDiscussion.id.desc()).all()


def create_discussion(db: Session, node: BoardNode | None, user: User, title: str) -> BoardDiscussion:
    discussion = BoardDiscussion(
        node_id=node.id if node is not None else None,
        created_by_id=user.id,
        title=title.strip()[:255],
    )
    db.add(discussion)
    db.commit()
    db.refresh(discussion)
    return discussion


def rename_discussion(db: Session, discussion: BoardDiscussion, title: str) -> BoardDiscussion:
    discussion.title = title.strip()[:255]
    db.add(discussion)
    db.commit()
    db.refresh(discussion)
    return discussion


def delete_discussion(db: Session, discussion: BoardDiscussion) -> None:
    db.delete(discussion)
    db.commit()


def post_message(
    db: Session, discussion: BoardDiscussion, user: User, text: str, consult_council: bool
) -> BoardDiscussionMessage:
    text = (text or "").strip()
    if not text:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Пустое сообщение")

    db.add(
        BoardDiscussionMessage(discussion_id=discussion.id, role="user", author_id=user.id, content=text)
    )
    discussion.updated_at = datetime.now(timezone.utc)
    db.add(discussion)
    db.commit()
    db.refresh(discussion)

    opinions: list[dict] = []
    research_brief: str | None = None
    if consult_council:
        if discussion.node is None:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Спросить мнение совета можно только в обсуждении, привязанном к ноде",
            )
        client = _get_client()
        node_ctx = board_service.node_context(discussion.node)
        context = board_conductor.gather_context(client, db, node_ctx, text)
        research_brief = context.get("research_brief")
        opinions = board_council.gather_role_opinions(
            db, client, node_ctx, text, _history_note(discussion), context
        )

    reply = _answer(db, discussion, opinions, research_brief)

    message = BoardDiscussionMessage(
        discussion_id=discussion.id,
        role="assistant",
        content=reply,
        council=opinions or None,
        research_brief=research_brief,
    )
    db.add(message)
    discussion.updated_at = datetime.now(timezone.utc)
    db.add(discussion)
    db.commit()
    db.refresh(message)
    return message
