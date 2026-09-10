import json

from fastapi import Depends, FastAPI, HTTPException, UploadFile, status
from fastapi.responses import FileResponse, Response, StreamingResponse
from sqlalchemy.orm import Session

from app.ai import analytics as ai_analytics
from app.ai import attachments as ai_attachments
from app.ai import engine
from app.ai import mcp_auth
from app.ai import meetings as ai_meetings
from app.ai import priorities as ai_priorities
from app.ai import service as ai_service
from app.ai import topic as ai_topic
from app.ai import tts as ai_tts
from app.ai.models import Chat, ChatDomain, ChatMode, McpCredential, Meeting, PendingAction, PendingActionStatus
from app.ai.tools import TOOLS
from app.ai.schemas import (
    AskRequest,
    AskResponse,
    ChatDetailOut,
    ChatModeUpdate,
    ChatOut,
    ChatTitleUpdate,
    ConsultAskResponse,
    MeetingCreate,
    MeetingDetailOut,
    MeetingOut,
    PendingActionOut,
    SectionAnalyticsOut,
    SpeakRequest,
    TaskPrioritiesOut,
    TranscriptAppendIn,
    TranscriptLineOut,
    TranscriptSpeakerUpdate,
)
from app.common.files import FileAssetOut
from app.common.module_access import Module
from app.core.config import settings
from app.core.deps import get_current_user, require_admin, require_module
from app.db.session import get_db
from app.users.models import User

app = FastAPI(
    title="Soborbum — ИИ",
    description="Ассистент на Claude поверх всех разделов: чаты по каждому блоку, общий чат "
    "и одобрение действий, которые ИИ предлагает выполнить.",
    version="0.3",
)

require_ai = require_module(Module.AI)


def require_ai_and(module: Module):
    def dependency(user: User = Depends(require_ai)) -> User:
        if not user.has_access(module):
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail=f"Нет доступа к разделу «{module.value}»",
            )
        return user

    return dependency


def _to_pending_out(pa: PendingAction) -> PendingActionOut:
    resolution = (pa.message.tool_resolutions or {}).get(pa.tool_use_id, {})
    execution_status = "unknown"
    if pa.status == PendingActionStatus.PENDING:
        execution_status = "pending"
    elif pa.status == PendingActionStatus.REJECTED:
        execution_status = "rejected"
    elif resolution.get("status") == "executed":
        execution_status = "failed" if resolution.get("is_error") else "succeeded"
    tool = TOOLS.get(pa.tool_name)
    return PendingActionOut.model_validate(pa).model_copy(update={
        "summary": tool.schema["description"] if tool else "Действие Марины",
        "execution_status": execution_status,
        "policy_version": resolution.get("guardian", {}).get("policy_version"),
    })


def _ask(db: Session, user: User, domain: ChatDomain, payload: AskRequest) -> AskResponse:
    if not settings.anthropic_api_key:
        raise HTTPException(503, "Марина пока не подключена. Обратитесь к администратору.")
    chat = ai_service.get_or_create_chat(db, user, domain, payload.chat_id, payload.mode)
    result = engine.run_turn(db, chat, user, payload.message, payload.file_ids)
    return AskResponse(
        chat_id=chat.id,
        status=result.status,
        reply=result.reply,
        pending_actions=[_to_pending_out(pa) for pa in result.pending_actions],
    )


def _sse(events) -> StreamingResponse:
    """Wrap an event-dict generator as an SSE stream. X-Accel-Buffering: no keeps
    nginx from buffering the whole response (which would defeat streaming)."""
    def encode():
        yield ": open\n\n"
        for event in events:
            yield f"event: {event['type']}\ndata: {json.dumps(event, ensure_ascii=False, default=str)}\n\n"
        yield "event: end\ndata: {}\n\n"

    return StreamingResponse(
        encode(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )


def _ask_stream(db: Session, user: User, domain: ChatDomain, payload: AskRequest) -> StreamingResponse:
    if not settings.anthropic_api_key:
        raise HTTPException(503, "Марина пока не подключена. Обратитесь к администратору.")
    chat = ai_service.get_or_create_chat(db, user, domain, payload.chat_id, payload.mode)
    # Preflight synchronously so a 400/409 is a real HTTP error, not a stream event.
    engine.prepare_stream_turn(db, chat, user, payload.message, payload.file_ids)
    chat_id, user_id = chat.id, user.id
    return _sse(engine.stream_turn(chat_id, user_id))


@app.post("/clients/ask", response_model=AskResponse)
def ask_clients(payload: AskRequest, db: Session = Depends(get_db), user: User = Depends(require_ai_and(Module.CLIENTS))):
    return _ask(db, user, ChatDomain.CLIENTS, payload)


@app.post("/production/ask", response_model=AskResponse)
def ask_production(payload: AskRequest, db: Session = Depends(get_db), user: User = Depends(require_ai_and(Module.PRODUCTION))):
    return _ask(db, user, ChatDomain.PRODUCTION, payload)


@app.post("/cycle/ask", response_model=AskResponse)
def ask_cycle(payload: AskRequest, db: Session = Depends(get_db), user: User = Depends(require_ai_and(Module.CYCLE))):
    return _ask(db, user, ChatDomain.CYCLE, payload)


@app.post("/warehouse/ask", response_model=AskResponse)
def ask_warehouse(payload: AskRequest, db: Session = Depends(get_db), user: User = Depends(require_ai_and(Module.WAREHOUSE))):
    return _ask(db, user, ChatDomain.WAREHOUSE, payload)


@app.post("/marketing/ask", response_model=AskResponse)
def ask_marketing(payload: AskRequest, db: Session = Depends(get_db), user: User = Depends(require_ai_and(Module.MARKETING))):
    return _ask(db, user, ChatDomain.MARKETING, payload)


@app.post("/tasks/ask", response_model=AskResponse)
def ask_tasks(payload: AskRequest, db: Session = Depends(get_db), user: User = Depends(require_ai_and(Module.TASKS))):
    return _ask(db, user, ChatDomain.TASKS, payload)


@app.post("/chat/ask", response_model=AskResponse)
def ask_general(payload: AskRequest, db: Session = Depends(get_db), user: User = Depends(require_ai)):
    return _ask(db, user, ChatDomain.GENERAL, payload)


# --- Streaming (SSE) twins: same turn, tokens delivered as they generate. -----
# The blocking /ask routes above stay for the pending-action resume path and as
# a client fallback.

@app.post("/clients/ask/stream")
def ask_clients_stream(payload: AskRequest, db: Session = Depends(get_db), user: User = Depends(require_ai_and(Module.CLIENTS))):
    return _ask_stream(db, user, ChatDomain.CLIENTS, payload)


@app.post("/production/ask/stream")
def ask_production_stream(payload: AskRequest, db: Session = Depends(get_db), user: User = Depends(require_ai_and(Module.PRODUCTION))):
    return _ask_stream(db, user, ChatDomain.PRODUCTION, payload)


@app.post("/cycle/ask/stream")
def ask_cycle_stream(payload: AskRequest, db: Session = Depends(get_db), user: User = Depends(require_ai_and(Module.CYCLE))):
    return _ask_stream(db, user, ChatDomain.CYCLE, payload)


@app.post("/warehouse/ask/stream")
def ask_warehouse_stream(payload: AskRequest, db: Session = Depends(get_db), user: User = Depends(require_ai_and(Module.WAREHOUSE))):
    return _ask_stream(db, user, ChatDomain.WAREHOUSE, payload)


@app.post("/marketing/ask/stream")
def ask_marketing_stream(payload: AskRequest, db: Session = Depends(get_db), user: User = Depends(require_ai_and(Module.MARKETING))):
    return _ask_stream(db, user, ChatDomain.MARKETING, payload)


@app.post("/tasks/ask/stream")
def ask_tasks_stream(payload: AskRequest, db: Session = Depends(get_db), user: User = Depends(require_ai_and(Module.TASKS))):
    return _ask_stream(db, user, ChatDomain.TASKS, payload)


@app.post("/chat/ask/stream")
def ask_general_stream(payload: AskRequest, db: Session = Depends(get_db), user: User = Depends(require_ai)):
    return _ask_stream(db, user, ChatDomain.GENERAL, payload)


@app.post("/consult/ask/stream")
def ask_consult_stream(payload: AskRequest, db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    if not settings.anthropic_api_key:
        raise HTTPException(503, "Марина пока не подключена. Обратитесь к администратору.")
    previous: list[str] = []
    chat = None
    if payload.chat_id is not None:
        found = db.get(Chat, payload.chat_id)
        if found is not None and found.owner_id == user.id:
            chat = found
            previous = ai_service.user_texts(chat)
    reset = ai_topic.topic_shifted(previous, payload.message)
    if reset and chat is not None:
        ai_service.wipe_chat(db, chat)
        chat = None
    if chat is None:
        chat = ai_service.get_or_create_chat(
            db, user, ChatDomain.GENERAL, None, payload.mode or ChatMode.REQUIRE_APPROVAL
        )
    engine.prepare_stream_turn(db, chat, user, payload.message, payload.file_ids)
    chat_id, user_id = chat.id, user.id

    def events():
        yield {"type": "topic_reset", "value": reset}
        yield from engine.stream_turn(chat_id, user_id)

    return _sse(events())


@app.post("/consult/ask", response_model=ConsultAskResponse)
def ask_consult(payload: AskRequest, db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    if not settings.anthropic_api_key:
        raise HTTPException(503, "Марина пока не подключена. Обратитесь к администратору.")
    previous: list[str] = []
    chat = None
    if payload.chat_id is not None:
        found = db.get(Chat, payload.chat_id)
        if found is not None and found.owner_id == user.id:
            chat = found
            previous = ai_service.user_texts(chat)
    reset = ai_topic.topic_shifted(previous, payload.message)
    if reset and chat is not None:
        ai_service.wipe_chat(db, chat)
        chat = None
    if chat is None:
        chat = ai_service.get_or_create_chat(
            db, user, ChatDomain.GENERAL, None, payload.mode or ChatMode.REQUIRE_APPROVAL
        )
    result = engine.run_turn(db, chat, user, payload.message, payload.file_ids)
    return ConsultAskResponse(
        chat_id=chat.id,
        status=result.status,
        reply=result.reply,
        pending_actions=[_to_pending_out(pa) for pa in result.pending_actions],
        topic_reset=reset,
    )


@app.delete("/consult", status_code=status.HTTP_204_NO_CONTENT)
def clear_consult(chat_id: int | None = None, db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    if chat_id is None:
        return
    chat = db.get(Chat, chat_id)
    if chat is not None and chat.owner_id == user.id:
        ai_service.wipe_chat(db, chat)


@app.post("/tts/speak")
async def speak_text(payload: SpeakRequest, user: User = Depends(get_current_user)):
    """Neural female Russian voice for consult résumé. Free Edge TTS, no paid key."""
    _ = user
    try:
        audio = await ai_tts.synthesize_mp3(payload.text)
    except ValueError as exc:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(exc)) from None
    except Exception as exc:
        raise HTTPException(
            status.HTTP_503_SERVICE_UNAVAILABLE,
            "Голос временно недоступен. Попробуйте ещё раз.",
        ) from exc
    return Response(content=audio, media_type="audio/mpeg")


@app.post("/consult/pending-actions/{pending_action_id}/approve", response_model=AskResponse)
def consult_approve(pending_action_id: int, db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    pa = ai_service.get_own_pending_action_or_404(db, user, pending_action_id)
    result = engine.resolve_pending_action(db, pa, approve=True, decided_by=user)
    return AskResponse(
        chat_id=pa.chat_id,
        status=result.status,
        reply=result.reply,
        pending_actions=[_to_pending_out(p) for p in result.pending_actions],
    )


@app.post("/consult/pending-actions/{pending_action_id}/reject", response_model=AskResponse)
def consult_reject(pending_action_id: int, db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    pa = ai_service.get_own_pending_action_or_404(db, user, pending_action_id)
    result = engine.resolve_pending_action(db, pa, approve=False, decided_by=user)
    return AskResponse(
        chat_id=pa.chat_id,
        status=result.status,
        reply=result.reply,
        pending_actions=[_to_pending_out(p) for p in result.pending_actions],
    )


@app.get("/clients/analytics", response_model=SectionAnalyticsOut)
def analytics_clients(reload: bool = False, db: Session = Depends(get_db), user: User = Depends(require_ai_and(Module.CLIENTS))):
    return ai_analytics.generate_section_analytics(db, user, "clients", force=reload)


@app.get("/production/analytics", response_model=SectionAnalyticsOut)
def analytics_production(reload: bool = False, db: Session = Depends(get_db), user: User = Depends(require_ai_and(Module.PRODUCTION))):
    return ai_analytics.generate_section_analytics(db, user, "production", force=reload)


@app.get("/installation/analytics", response_model=SectionAnalyticsOut)
def analytics_installation(reload: bool = False, db: Session = Depends(get_db), user: User = Depends(require_ai_and(Module.INSTALLATION))):
    return ai_analytics.generate_section_analytics(db, user, "installation", force=reload)


@app.get("/cycle/analytics", response_model=SectionAnalyticsOut)
def analytics_cycle(reload: bool = False, db: Session = Depends(get_db), user: User = Depends(require_ai_and(Module.CYCLE))):
    return ai_analytics.generate_section_analytics(db, user, "cycle", force=reload)


@app.get("/warehouse/analytics", response_model=SectionAnalyticsOut)
def analytics_warehouse(reload: bool = False, db: Session = Depends(get_db), user: User = Depends(require_ai_and(Module.WAREHOUSE))):
    return ai_analytics.generate_section_analytics(db, user, "warehouse", force=reload)


@app.get("/marketing/analytics", response_model=SectionAnalyticsOut)
def analytics_marketing(reload: bool = False, db: Session = Depends(get_db), user: User = Depends(require_ai_and(Module.MARKETING))):
    return ai_analytics.generate_section_analytics(db, user, "marketing", force=reload)


@app.get("/tasks/analytics", response_model=SectionAnalyticsOut)
def analytics_tasks(reload: bool = False, db: Session = Depends(get_db), user: User = Depends(require_ai_and(Module.TASKS))):
    return ai_analytics.generate_section_analytics(db, user, "tasks", force=reload)


@app.get("/tasks/priorities", response_model=TaskPrioritiesOut)
def task_priorities(reload: bool = False, db: Session = Depends(get_db), user: User = Depends(require_ai_and(Module.TASKS))):
    return ai_priorities.generate_task_priorities(db, user, force=reload)


@app.post("/files", response_model=FileAssetOut)
def upload_chat_file(file: UploadFile, db: Session = Depends(get_db), user: User = Depends(require_ai)):
    """Upload a file (image, PDF, or plain-text document) to attach to a chat
    message: pass the returned id in AskRequest.file_ids. Download stays on
    the shared GET /files/{file_id} route."""
    asset = ai_attachments.upload_attachment(db, file, user)
    db.commit()
    db.refresh(asset)
    return asset


@app.get("/chats", response_model=list[ChatOut])
def list_chats(domain: ChatDomain | None = None, db: Session = Depends(get_db), user: User = Depends(require_ai)):
    return ai_service.list_own_chats(db, user, domain)


@app.get("/chats/{chat_id}", response_model=ChatDetailOut)
def get_chat(chat_id: int, db: Session = Depends(get_db), user: User = Depends(require_ai)):
    return ai_service.get_own_chat_or_404(db, user, chat_id)


@app.patch("/chats/{chat_id}/mode", response_model=ChatOut)
def update_chat_mode(
    chat_id: int, payload: ChatModeUpdate, db: Session = Depends(get_db), user: User = Depends(require_ai)
):
    chat = ai_service.get_own_chat_or_404(db, user, chat_id)
    return ai_service.update_mode(db, chat, payload.mode)


@app.patch("/chats/{chat_id}/title", response_model=ChatOut)
def update_chat_title(
    chat_id: int, payload: ChatTitleUpdate, db: Session = Depends(get_db), user: User = Depends(require_ai)
):
    chat = ai_service.get_own_chat_or_404(db, user, chat_id)
    return ai_service.update_title(db, chat, payload.title)


@app.delete("/chats/{chat_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_chat(chat_id: int, db: Session = Depends(get_db), user: User = Depends(require_ai)):
    chat = ai_service.get_own_chat_or_404(db, user, chat_id)
    ai_service.delete_chat(db, chat)


@app.get("/pending-actions", response_model=list[PendingActionOut])
def list_pending_actions(
    chat_id: int | None = None, db: Session = Depends(get_db), user: User = Depends(require_ai)
):
    return [_to_pending_out(pa) for pa in ai_service.list_own_pending_actions(db, user, chat_id)]


@app.post("/pending-actions/{pending_action_id}/approve", response_model=AskResponse)
def approve_pending_action(pending_action_id: int, db: Session = Depends(get_db), user: User = Depends(require_ai)):
    pa = ai_service.get_own_pending_action_or_404(db, user, pending_action_id)
    result = engine.resolve_pending_action(db, pa, approve=True, decided_by=user)
    return AskResponse(
        chat_id=pa.chat_id,
        status=result.status,
        reply=result.reply,
        pending_actions=[_to_pending_out(p) for p in result.pending_actions],
    )


@app.post("/pending-actions/{pending_action_id}/reject", response_model=AskResponse)
def reject_pending_action(pending_action_id: int, db: Session = Depends(get_db), user: User = Depends(require_ai)):
    pa = ai_service.get_own_pending_action_or_404(db, user, pending_action_id)
    result = engine.resolve_pending_action(db, pa, approve=False, decided_by=user)
    return AskResponse(
        chat_id=pa.chat_id,
        status=result.status,
        reply=result.reply,
        pending_actions=[_to_pending_out(p) for p in result.pending_actions],
    )


#  --- Режим «Совещание» (0004-a): сессия + запись аудио, без ИИ ------------

def _meeting_out(m: Meeting) -> MeetingOut:
    return MeetingOut(
        id=m.id,
        title=m.title,
        status=m.status,
        started_at=m.started_at,
        finished_at=m.finished_at,
        duration_sec=ai_meetings.duration_sec(m),
        has_audio=m.audio_file_id is not None,
    )


@app.post("/meetings", response_model=MeetingOut, status_code=status.HTTP_201_CREATED)
def create_meeting(payload: MeetingCreate, db: Session = Depends(get_db), user: User = Depends(require_ai)):
    return _meeting_out(ai_meetings.create_meeting(db, user, payload.title))


@app.get("/meetings", response_model=list[MeetingOut])
def list_meetings(db: Session = Depends(get_db), user: User = Depends(require_ai)):
    return [_meeting_out(m) for m in ai_meetings.list_own_meetings(db, user)]


@app.get("/meetings/{meeting_id}", response_model=MeetingDetailOut)
def get_meeting(meeting_id: int, db: Session = Depends(get_db), user: User = Depends(require_ai)):
    meeting = ai_meetings.get_own_meeting_or_404(db, user, meeting_id)
    base = _meeting_out(meeting)
    return MeetingDetailOut(
        **base.model_dump(),
        audio_url=f"/api/ai/meetings/{meeting.id}/audio" if meeting.audio_file_id else None,
        transcript=[
            TranscriptLineOut.model_validate(line) for line in ai_meetings.transcript_lines(db, meeting)
        ],
        ai_enabled=bool(settings.anthropic_api_key),
    )


@app.post("/meetings/{meeting_id}/transcript", response_model=list[TranscriptLineOut])
def append_meeting_transcript(
    meeting_id: int,
    payload: TranscriptAppendIn,
    db: Session = Depends(get_db),
    user: User = Depends(require_ai),
):
    meeting = ai_meetings.get_own_meeting_or_404(db, user, meeting_id)
    created = ai_meetings.append_transcript_lines(
        db, meeting, [(line.speaker, line.text, line.at_ms) for line in payload.lines]
    )
    return [TranscriptLineOut.model_validate(line) for line in created]


@app.patch("/meetings/{meeting_id}/transcript/{line_id}", response_model=TranscriptLineOut)
def update_meeting_transcript_speaker(
    meeting_id: int,
    line_id: int,
    payload: TranscriptSpeakerUpdate,
    db: Session = Depends(get_db),
    user: User = Depends(require_ai),
):
    meeting = ai_meetings.get_own_meeting_or_404(db, user, meeting_id)
    return TranscriptLineOut.model_validate(
        ai_meetings.set_line_speaker(db, meeting, line_id, payload.speaker)
    )


@app.post("/meetings/{meeting_id}/audio", response_model=MeetingOut)
def upload_meeting_audio(
    meeting_id: int, file: UploadFile, db: Session = Depends(get_db), user: User = Depends(require_ai)
):
    meeting = ai_meetings.get_own_meeting_or_404(db, user, meeting_id)
    return _meeting_out(ai_meetings.attach_audio(db, meeting, file, user))


@app.post("/meetings/{meeting_id}/finish", response_model=MeetingOut)
def finish_meeting(meeting_id: int, db: Session = Depends(get_db), user: User = Depends(require_ai)):
    meeting = ai_meetings.get_own_meeting_or_404(db, user, meeting_id)
    return _meeting_out(ai_meetings.finish_meeting(db, meeting))


@app.get("/meetings/{meeting_id}/audio")
def download_meeting_audio(meeting_id: int, db: Session = Depends(get_db), user: User = Depends(require_ai)):
    meeting = ai_meetings.get_own_meeting_or_404(db, user, meeting_id)
    asset = ai_meetings.audio_asset(db, meeting)
    if asset is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Запись совещания не найдена")
    return FileResponse(asset.path_on_disk, media_type=asset.content_type, filename=asset.filename)


@app.get("/mcp/authorize")
def mcp_authorize(_: User = Depends(require_admin)):
    """Manual authorization: open the returned URL in a browser and approve
    access, which redirects to GET /callback (mounted on the root app, not
    here, since its path has to exactly match the redirect_uri registered
    with the provider).

    Not normally needed - mcp_auth.get_access_token() obtains and renews
    tokens on its own. Use this if the provider ever starts requiring a
    consent screen, which is the one case the automatic grant cannot
    handle."""
    return {"authorize_url": mcp_auth.build_authorize_url()}


@app.get("/mcp/status")
def mcp_status(db: Session = Depends(get_db), _: User = Depends(require_admin)):
    if not settings.mcp_configured:
        return {"configured": False, "authorized": False}
    credential = db.get(McpCredential, 1)
    if credential is None:
        return {"configured": True, "authorized": False}
    return {
        "configured": True,
        "authorized": True,
        "expires_at": credential.expires_at,
        "has_refresh_token": credential.refresh_token is not None,
    }
