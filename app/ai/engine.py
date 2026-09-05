"""The Claude tool-use loop: sends a chat's history to the model, executes
(or gates behind approval) whatever tools it calls, and keeps going until
the model produces a final text answer or the turn pauses on PendingAction.
"""

import json
import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone

import anthropic
from fastapi import HTTPException, status
from pydantic import ValidationError
from sqlalchemy import update
from sqlalchemy.orm import Session

from app.ai import attachments as ai_attachments
from app.ai import mcp_auth
from app.ai.guardian import authorize_tool, decision_record
from app.ai.models import Chat, ChatMode, Message, PendingAction, PendingActionStatus
from app.ai.prompts import SYSTEM_PROMPTS
from app.ai.tools import DOMAIN_TOOLS, TOOLS
from app.common.files import FileAsset
from app.core.config import settings
from app.users.models import User, UserRole

MAX_ITERATIONS = 8
MCP_BETA = "mcp-client-2025-04-04"
logger = logging.getLogger(__name__)

# Remote calls run on the provider side before our gateway can inspect them.
# Every mode therefore gets only this explicit read allowlist.
MCP_READ_ONLY_TOOLS = ["read_index", "list_notes", "search_notes", "read_note", "get_unread_files"]


@dataclass
class TurnResult:
    status: str  # "completed" | "pending_approval"
    reply: str | None = None
    pending_actions: list[PendingAction] = field(default_factory=list)


def _get_client() -> anthropic.Anthropic:
    if not settings.anthropic_api_key:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Марина пока не подключена. Обратитесь к администратору.",
        )
    return anthropic.Anthropic(api_key=settings.anthropic_api_key, timeout=60.0, max_retries=1)


def _available_tools(chat: Chat, user: User) -> list[dict]:
    tools = []
    for name in DOMAIN_TOOLS[chat.domain]:
        tool_def = TOOLS[name]
        if not user.has_access(tool_def.required_module):
            continue
        if chat.mode == ChatMode.NO_ACTIONS and not tool_def.read_only:
            continue
        tools.append(tool_def.schema)
    return tools


def _resolve_content(db: Session, content: list) -> list:
    resolved = []
    for block in content:
        if block.get("type") == "file_ref":
            asset = db.get(FileAsset, block["file_id"])
            if asset is None:
                resolved.append({"type": "text", "text": f"[Файл «{block.get('filename')}» больше недоступен]"})
            else:
                resolved.append(ai_attachments.build_content_block(asset))
        else:
            resolved.append(block)
    return resolved


def _build_history(db: Session, chat: Chat) -> list[dict]:
    return [{"role": m.role, "content": _resolve_content(db, m.content)} for m in chat.messages]


def _call_claude(db: Session, system: str, messages: list[dict], tools: list[dict], mode: ChatMode, user: User):
    client = _get_client()
    kwargs = {
        "model": settings.ai_model,
        # Generous on purpose: a single turn can involve reading several full
        # knowledge-base documents inline (the MCP connector embeds their
        # content as mcp_tool_result blocks in this same response) before the
        # model writes a generated document as a tool call argument - 2048
        # was getting exhausted mid-tool-call, silently truncating it.
        "max_tokens": 8192,
        "system": system,
        "messages": messages,
    }
    if tools:
        kwargs["tools"] = tools

    # The shared connector has no employee/document ACL. Restrict it to administrators
    # until per-user knowledge access is implemented, and never allow remote writes.
    if settings.mcp_configured and user.role == UserRole.ADMIN:
        mcp_server: dict = {
            "type": "url",
            "url": settings.mcp_server_url,
            "name": "knowledge-base",
            "authorization_token": mcp_auth.get_access_token(db),
        }
        mcp_server["tool_configuration"] = {"allowed_tools": MCP_READ_ONLY_TOOLS}
        return client.beta.messages.create(
            betas=[MCP_BETA],
            mcp_servers=[mcp_server],
            **kwargs,
        )
    return client.messages.create(**kwargs)


def _to_tool_result(tool_use_id: str, resolution: dict) -> dict:
    content = resolution.get("content")
    text = content if isinstance(content, str) else json.dumps(content, ensure_ascii=False, default=str)
    return {
        "type": "tool_result",
        "tool_use_id": tool_use_id,
        "content": text,
        "is_error": bool(resolution.get("is_error", False)),
    }


def _execute_tool(db: Session, name: str, tool_input: dict, user: User, chat: Chat,
                  *, approved: bool = False, commit: bool = True) -> dict:
    try:
        tool_def = authorize_tool(chat, user, name, tool_input, approved=approved)
        # Domain handlers flush, never commit. A savepoint keeps a failed operation
        # from discarding the approval decision in the outer transaction.
        with db.begin_nested():
            result = tool_def.handler(db, user, **tool_input)
        if commit:
            db.commit()
        return {"content": result, "is_error": False, "guardian": decision_record(user, approved=approved)}
    except HTTPException as e:
        if commit:
            db.rollback()
        return {"content": {"error": e.detail}, "is_error": True, "guardian": decision_record(user, approved=approved)}
    except (ValidationError, TypeError, ValueError):
        if commit:
            db.rollback()
        return {"content": {"error": "Некорректные аргументы действия"}, "is_error": True,
                "guardian": decision_record(user, approved=approved)}


def run_turn(db: Session, chat: Chat, user: User, user_text: str, file_ids: list[int] | None = None) -> TurnResult:
    if db.query(PendingAction).filter(PendingAction.chat_id == chat.id,
                                     PendingAction.status == PendingActionStatus.PENDING).first():
        raise HTTPException(409, "Сначала подтвердите или отклоните ожидающие действия")
    content = ai_attachments.resolve_file_ids(db, file_ids or [], user)
    if user_text:
        content.append({"type": "text", "text": user_text})
    if not content:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Нужно отправить текст или файл")

    db.add(Message(chat_id=chat.id, role="user", content=content))
    db.commit()
    db.refresh(chat)
    return _advance(db, chat, user)


def _advance(db: Session, chat: Chat, user: User) -> TurnResult:
    for _ in range(MAX_ITERATIONS):
        system = SYSTEM_PROMPTS[chat.domain]
        history = _build_history(db, chat)
        tools = _available_tools(chat, user)
        try:
            response = _call_claude(db, system, history, tools, chat.mode, user)
        except anthropic.APIError:
            logger.warning("AI provider unavailable for chat %s", chat.id)
            raise HTTPException(503, "Марина временно недоступна. Попробуйте позже.") from None

        content_blocks = [block.model_dump(mode="json") for block in response.content]
        assistant_message = Message(chat_id=chat.id, role="assistant", content=content_blocks)
        db.add(assistant_message)
        db.commit()
        db.refresh(assistant_message)

        if response.stop_reason == "max_tokens":
            text = "".join(b.get("text", "") for b in content_blocks if b.get("type") == "text")
            note = (
                "Ответ обрезан из-за лимита длины (возможно, посреди вызова инструмента) - "
                "попробуй сформулировать запрос уже или разбить его на части."
            )
            reply = (text + "\n\n" + note) if text else note
            # An unfinished tool_use cannot be replayed without a matching tool_result.
            # Preserve the user-visible partial answer, never the unexecuted tool block.
            assistant_message.content = [{"type": "text", "text": reply}]
            db.commit()
            return TurnResult(status="completed", reply=reply)

        if response.stop_reason != "tool_use":
            text = "".join(b.get("text", "") for b in content_blocks if b.get("type") == "text")
            return TurnResult(status="completed", reply=text)

        tool_use_blocks = [b for b in content_blocks if b.get("type") == "tool_use"]
        resolutions: dict[str, dict] = {}
        pending: list[PendingAction] = []

        for block in tool_use_blocks:
            tool_use_id, name, tool_input = block["id"], block["name"], block.get("input", {})
            try:
                tool_def = authorize_tool(chat, user, name, tool_input, proposal=True)
            except HTTPException as exc:
                resolutions[tool_use_id] = {"status": "blocked", "content": {"error": exc.detail},
                                            "is_error": True, "guardian": decision_record(user)}
                continue

            if not tool_def.read_only:
                pa = PendingAction(
                    chat_id=chat.id,
                    message_id=assistant_message.id,
                    tool_use_id=tool_use_id,
                    tool_name=name,
                    tool_input=tool_input,
                )
                db.add(pa)
                db.commit()
                db.refresh(pa)
                pending.append(pa)
                resolutions[tool_use_id] = {"status": "pending", "guardian": decision_record(user, approved=True)}
            else:
                outcome = _execute_tool(db, name, tool_input, user, chat)
                resolutions[tool_use_id] = {"status": "executed", **outcome}

        assistant_message.tool_resolutions = resolutions
        db.add(assistant_message)
        db.commit()

        if pending:
            return TurnResult(status="pending_approval", pending_actions=pending)

        tool_result_content = [_to_tool_result(tid, res) for tid, res in resolutions.items()]
        db.add(Message(chat_id=chat.id, role="user", content=tool_result_content))
        db.commit()
        db.refresh(chat)

    return TurnResult(
        status="completed",
        reply="Достигнут лимит шагов обработки запроса. Попробуй сформулировать вопрос иначе.",
    )


def resolve_pending_action(db: Session, pending_action: PendingAction, approve: bool, decided_by: User) -> TurnResult:
    # Serialize sibling decisions so their JSON resolutions cannot overwrite one another.
    chat = (db.query(Chat).filter(Chat.id == pending_action.chat_id)
            .populate_existing().with_for_update().one())
    db.refresh(decided_by)
    db.expire(decided_by, ["module_access"])
    if chat.owner_id != decided_by.id or not decided_by.is_active:
        raise HTTPException(403, "Нет полномочий на это решение")
    if approve:
        authorize_tool(chat, decided_by, pending_action.tool_name, pending_action.tool_input, approved=True)
    changed = db.execute(update(PendingAction).where(
        PendingAction.id == pending_action.id, PendingAction.status == PendingActionStatus.PENDING,
    ).values(status=PendingActionStatus.APPROVED if approve else PendingActionStatus.REJECTED,
             decided_by_id=decided_by.id, decided_at=datetime.now(timezone.utc)),
             execution_options={"synchronize_session": False})
    if changed.rowcount != 1:
        db.rollback()
        raise HTTPException(409, "Это действие уже обработано")
    db.refresh(pending_action)

    if approve:
        outcome = _execute_tool(db, pending_action.tool_name, pending_action.tool_input, decided_by, chat,
                                approved=True, commit=False)
        resolution = {"status": "executed", **outcome}
    else:
        resolution = {"status": "executed", "content": {"error": "Действие отклонено сотрудником"}, "is_error": True}

    message = pending_action.message
    db.refresh(message)
    resolutions = dict(message.tool_resolutions or {})
    resolutions[pending_action.tool_use_id] = resolution
    message.tool_resolutions = resolutions
    db.add(message)
    db.flush()
    db.refresh(message)

    tool_use_ids = [b["id"] for b in message.content if b.get("type") == "tool_use"]
    still_pending = (
        db.query(PendingAction)
        .filter(PendingAction.message_id == message.id, PendingAction.status == PendingActionStatus.PENDING)
        .all()
    )
    if still_pending:
        db.commit()
        return TurnResult(status="pending_approval", pending_actions=still_pending)

    tool_result_content = [_to_tool_result(tid, resolutions[tid]) for tid in tool_use_ids]
    db.add(Message(chat_id=chat.id, role="user", content=tool_result_content))
    db.commit()
    db.refresh(chat)

    try:
        return _advance(db, chat, decided_by)
    except HTTPException as exc:
        if exc.status_code != 503:
            raise
        # The action transaction already committed. A provider outage must not turn
        # a completed action into a failed HTTP response inviting another attempt.
        return TurnResult(status="completed", reply="Решение сохранено. Продолжение ответа Марины временно недоступно.")
