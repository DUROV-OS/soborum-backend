from fastapi import Depends, FastAPI, HTTPException, UploadFile, status
from sqlalchemy.orm import Session

from app.ai import analytics as ai_analytics
from app.ai import attachments as ai_attachments
from app.ai import engine
from app.ai import mcp_auth
from app.ai import priorities as ai_priorities
from app.ai import service as ai_service
from app.ai.models import ChatDomain, McpCredential, PendingAction, PendingActionStatus
from app.ai.tools import TOOLS
from app.ai.schemas import (
    AskRequest,
    AskResponse,
    ChatDetailOut,
    ChatModeUpdate,
    ChatOut,
    ChatTitleUpdate,
    PendingActionOut,
    SectionAnalyticsOut,
    TaskPrioritiesOut,
)
from app.common.files import FileAssetOut
from app.common.module_access import Module
from app.core.config import settings
from app.core.deps import require_admin, require_module
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
