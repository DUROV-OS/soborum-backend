"""The Claude tool-use loop: sends a chat's history to the model, executes
(or gates behind approval) whatever tools it calls, and keeps going until
the model produces a final text answer or the turn pauses on PendingAction.
"""

import json
import logging
from collections.abc import Iterator
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
from app.ai.models import Chat, ChatDomain, ChatMode, Message, PendingAction, PendingActionStatus
from app.ai.prompts import SYSTEM_PROMPTS
from app.ai.tools import DOMAIN_TOOLS, TOOLS
from app.common.files import FileAsset
from app.core.config import settings
from app.db.session import SessionLocal
from app.users.models import User, UserRole

MAX_ITERATIONS = 5
MCP_BETA = "mcp-client-2025-04-04"
logger = logging.getLogger(__name__)

# Remote calls run on the provider side before our gateway can inspect them.
# Every mode therefore gets only this explicit read allowlist.
MCP_READ_ONLY_TOOLS = ["read_index", "list_notes", "search_notes", "read_note", "get_unread_files"]

# Anthropic-hosted web tools. The _20260209 variants (dynamic filtering) need
# no beta header and run on Sonnet 5. They execute on Anthropic's side, so
# there is no handler and no gateway hop - results come back inline as
# web_search_tool_result / web_fetch_tool_result blocks.
def _server_tools() -> list[dict]:
    if not settings.web_tools_enabled:
        return []
    return [
        {"type": "web_search_20260209", "name": "web_search", "max_uses": settings.web_search_max_uses},
        {"type": "web_fetch_20260209", "name": "web_fetch", "max_uses": settings.web_fetch_max_uses},
    ]


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
    from app.core.llm import anthropic_client

    # The KZ egress to api.anthropic.com blips; the SDK backs off exponentially
    # between retries, so a couple extra attempts keep a transient
    # APIConnectionError from surfacing as "Марина временно недоступна".
    return anthropic_client(timeout=60.0, max_retries=4)


def _system_for(chat: Chat, db: Session | None = None) -> str:
    text = SYSTEM_PROMPTS[chat.domain]
    if chat.domain != ChatDomain.GENERAL:
        return text
    try:
        from app.agents.connectors import live_briefing_text

        extra = live_briefing_text(db)
        if extra:
            text += "\n\n" + extra
    except Exception as error:
        logger.warning("живой срез для консультации: %s", error)
        text += (
            "\n\nЖивой срез базы DurovOS не собрался. "
            "Не утверждай числа по клиентам, складу, производству и задачам — данных нет."
        )
    return text


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


def _build_request(db: Session, system: str, messages: list[dict], tools: list[dict], user: User):
    """Shared request shape for the blocking and the streaming call. Returns
    (kwargs, mcp_servers) where mcp_servers is None unless the caller is an
    admin and the knowledge-base connector is configured."""
    kwargs: dict = {
        "model": settings.ai_model,
        # Generous on purpose: a single turn can involve reading several full
        # knowledge-base documents inline (the MCP connector embeds their
        # content as mcp_tool_result blocks in this same response) before the
        # model writes a generated document as a tool call argument - 2048
        # was getting exhausted mid-tool-call, silently truncating it.
        "max_tokens": 8192,
        # The main latency lever - see settings.ai_effort.
        "output_config": {"effort": settings.ai_effort},
        "system": system,
        "messages": messages,
    }
    # Anthropic-hosted web tools are available to every assistant user; they run
    # provider-side and never touch our data, so no role gate or approval hop.
    tools = list(tools) + _server_tools()
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
            "tool_configuration": {"allowed_tools": MCP_READ_ONLY_TOOLS},
        }
        return kwargs, [mcp_server]
    return kwargs, None


def _call_claude(db: Session, system: str, messages: list[dict], tools: list[dict], mode: ChatMode, user: User):
    client = _get_client()
    kwargs, mcp_servers = _build_request(db, system, messages, tools, user)
    if mcp_servers is not None:
        return client.beta.messages.create(betas=[MCP_BETA], mcp_servers=mcp_servers, **kwargs)
    return client.messages.create(**kwargs)


def _stream_claude(db: Session, system: str, messages: list[dict], tools: list[dict], mode: ChatMode, user: User):
    """Streaming twin of _call_claude. Returns the SDK stream context manager;
    the caller consumes events and then reads stream.get_final_message()."""
    client = _get_client()
    kwargs, mcp_servers = _build_request(db, system, messages, tools, user)
    if mcp_servers is not None:
        return client.beta.messages.stream(betas=[MCP_BETA], mcp_servers=mcp_servers, **kwargs)
    return client.messages.stream(**kwargs)


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


_MAX_TOKENS_NOTE = (
    "Ответ обрезан из-за лимита длины (возможно, посреди вызова инструмента) - "
    "попробуй сформулировать запрос уже или разбить его на части."
)
_STEP_LIMIT_NOTE = "Достигнут лимит шагов обработки запроса. Попробуй сформулировать вопрос иначе."


def _persist_assistant(db: Session, chat: Chat, content_blocks: list[dict]) -> Message:
    message = Message(chat_id=chat.id, role="assistant", content=content_blocks)
    db.add(message)
    db.commit()
    db.refresh(message)
    return message


def _max_tokens_reply(content_blocks: list[dict]) -> str:
    text = "".join(b.get("text", "") for b in content_blocks if b.get("type") == "text")
    return (text + "\n\n" + _MAX_TOKENS_NOTE) if text else _MAX_TOKENS_NOTE


def _run_tool_round(
    db: Session, chat: Chat, user: User, assistant_message: Message, content_blocks: list[dict]
) -> tuple[str, list[PendingAction]]:
    """Execute the read-only tool calls from one assistant turn and queue the
    mutating ones for approval. Returns ("pending", [actions]) if approval is
    needed, else ("continue", []) after a tool_result message is appended."""
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
        return "pending", pending

    tool_result_content = [_to_tool_result(tid, res) for tid, res in resolutions.items()]
    db.add(Message(chat_id=chat.id, role="user", content=tool_result_content))
    db.commit()
    db.refresh(chat)
    return "continue", []


def _advance(db: Session, chat: Chat, user: User) -> TurnResult:
    # Build system once: the DurovOS-database briefing must not re-fetch on every tool round.
    system = _system_for(chat, db)
    for _ in range(MAX_ITERATIONS):
        history = _build_history(db, chat)
        tools = _available_tools(chat, user)
        try:
            response = _call_claude(db, system, history, tools, chat.mode, user)
        except anthropic.APIError:
            logger.warning("AI provider unavailable for chat %s", chat.id)
            raise HTTPException(503, "Марина временно недоступна. Попробуйте позже.") from None

        content_blocks = [block.model_dump(mode="json") for block in response.content]
        assistant_message = _persist_assistant(db, chat, content_blocks)

        if response.stop_reason == "max_tokens":
            reply = _max_tokens_reply(content_blocks)
            # An unfinished tool_use cannot be replayed without a matching tool_result.
            # Preserve the user-visible partial answer, never the unexecuted tool block.
            assistant_message.content = [{"type": "text", "text": reply}]
            db.commit()
            return TurnResult(status="completed", reply=reply)

        if response.stop_reason == "pause_turn":
            # A provider-side tool (web_search / web_fetch) is mid-run. The
            # assistant turn is already persisted with its partial content;
            # resend the history unchanged so Anthropic resumes it.
            continue

        if response.stop_reason != "tool_use":
            text = "".join(b.get("text", "") for b in content_blocks if b.get("type") == "text")
            return TurnResult(status="completed", reply=text)

        outcome, pending = _run_tool_round(db, chat, user, assistant_message, content_blocks)
        if outcome == "pending":
            return TurnResult(status="pending_approval", pending_actions=pending)

    return TurnResult(status="completed", reply=_STEP_LIMIT_NOTE)


# --- Streaming variant -------------------------------------------------------
# Same loop as _advance, but yields SSE-ready event dicts as tokens arrive so
# the browser renders the answer progressively and a slow turn never sits past
# the reverse proxy's read timeout with no bytes on the wire.

_TOOL_LABELS = {"web_search": "Ищу в интернете…", "web_fetch": "Открываю страницу…"}


def _tool_label(block) -> str:
    name = getattr(block, "name", "") or ""
    if name in _TOOL_LABELS:
        return _TOOL_LABELS[name]
    if name.startswith(("knowledge-base", "knowledge_base")):
        return "Смотрю базу знаний…"
    return f"Выполняю: {name}" if name else "Работаю…"


def _pending_payload(pa: PendingAction) -> dict:
    """PendingActionOut-shaped dict for the SSE `pending_approval` event - the
    router's _to_pending_out lives in the request layer, and this generator
    runs on its own session after the request has returned."""
    tool = TOOLS.get(pa.tool_name)
    return {
        "id": pa.id,
        "chat_id": pa.chat_id,
        "message_id": pa.message_id,
        "tool_name": pa.tool_name,
        "tool_input": pa.tool_input,
        "status": pa.status.value if hasattr(pa.status, "value") else pa.status,
        "decided_by_id": pa.decided_by_id,
        "decided_at": pa.decided_at.isoformat() if pa.decided_at else None,
        "created_at": pa.created_at.isoformat() if pa.created_at else None,
        "summary": tool.schema["description"] if tool else "Действие Марины",
        "execution_status": "pending",
        "policy_version": None,
    }


def prepare_stream_turn(
    db: Session, chat: Chat, user: User, user_text: str, file_ids: list[int] | None = None
) -> None:
    """Preflight + persist the user message, synchronously, so a 400/409 is a
    real HTTP error rather than a mid-stream event. Mirrors run_turn's head."""
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


def stream_turn(chat_id: int, user_id: int) -> Iterator[dict]:
    """Drive the tool-use loop on a dedicated session (the request session is
    gone by the time this generator is iterated by StreamingResponse) and yield
    event dicts: block_start / text / block_end / status / pending_approval /
    error / done."""
    db = SessionLocal()
    try:
        chat = db.get(Chat, chat_id)
        user = db.get(User, user_id)
        if chat is None or user is None:
            yield {"type": "error", "detail": "Чат недоступен."}
            return
        yield from _advance_stream(db, chat, user)
    except anthropic.APIError:
        logger.warning("AI provider unavailable mid-stream for chat %s", chat_id)
        yield {"type": "error", "detail": "Марина временно недоступна. Попробуйте позже."}
    except Exception:
        logger.exception("streaming turn failed for chat %s", chat_id)
        yield {"type": "error", "detail": "Не удалось получить ответ Марины."}
    finally:
        db.close()


def _advance_stream(db: Session, chat: Chat, user: User) -> Iterator[dict]:
    system = _system_for(chat, db)
    for _ in range(MAX_ITERATIONS):
        history = _build_history(db, chat)
        tools = _available_tools(chat, user)

        text_block_open = False
        with _stream_claude(db, system, history, tools, chat.mode, user) as stream:
            for event in stream:
                etype = getattr(event, "type", None)
                if etype == "content_block_start":
                    block_type = getattr(event.content_block, "type", None)
                    if block_type == "text":
                        text_block_open = True
                        yield {"type": "block_start"}
                    elif block_type in ("tool_use", "server_tool_use"):
                        yield {"type": "status", "text": _tool_label(event.content_block)}
                elif etype == "content_block_delta":
                    if getattr(event.delta, "type", None) == "text_delta":
                        yield {"type": "text", "text": event.delta.text}
                elif etype == "content_block_stop" and text_block_open:
                    text_block_open = False
                    yield {"type": "block_end"}
            final = stream.get_final_message()

        content_blocks = [block.model_dump(mode="json") for block in final.content]
        assistant_message = _persist_assistant(db, chat, content_blocks)

        if final.stop_reason == "max_tokens":
            reply = _max_tokens_reply(content_blocks)
            assistant_message.content = [{"type": "text", "text": reply}]
            db.commit()
            yield {"type": "block_start"}
            yield {"type": "text", "text": reply}
            yield {"type": "block_end"}
            yield {"type": "done", "chat_id": chat.id}
            return

        if final.stop_reason == "pause_turn":
            yield {"type": "status", "text": "Продолжаю…"}
            continue

        if final.stop_reason != "tool_use":
            yield {"type": "done", "chat_id": chat.id}
            return

        outcome, pending = _run_tool_round(db, chat, user, assistant_message, content_blocks)
        if outcome == "pending":
            yield {
                "type": "pending_approval",
                "chat_id": chat.id,
                "pending_actions": [_pending_payload(pa) for pa in pending],
            }
            return
        yield {"type": "status", "text": "Продолжаю…"}

    yield {"type": "done", "chat_id": chat.id, "note": _STEP_LIMIT_NOTE}


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
