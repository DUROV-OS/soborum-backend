from fastapi import HTTPException, status
from sqlalchemy.orm import Session

from app.ai.models import AgentActivity, Chat, ChatDomain, ChatMode, PendingAction
from app.users.models import User


def get_or_create_chat(db: Session, owner: User, domain: ChatDomain, chat_id: int | None, mode: ChatMode) -> Chat:
    if chat_id is not None:
        chat = db.get(Chat, chat_id)
        if not chat or chat.owner_id != owner.id:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Чат не найден")
        if chat.domain != domain:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Этот чат относится к другому разделу")
        return chat

    chat = Chat(owner_id=owner.id, domain=domain,
                mode=ChatMode.REQUIRE_APPROVAL if mode == ChatMode.AUTO_APPROVE else mode)
    db.add(chat)
    db.commit()
    db.refresh(chat)
    return chat


def get_own_chat_or_404(db: Session, owner: User, chat_id: int) -> Chat:
    chat = db.get(Chat, chat_id)
    if not chat or chat.owner_id != owner.id:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Чат не найден")
    return chat


def list_own_chats(db: Session, owner: User, domain: ChatDomain | None = None) -> list[Chat]:
    query = db.query(Chat).filter(Chat.owner_id == owner.id)
    if domain is not None:
        query = query.filter(Chat.domain == domain)
    return query.order_by(Chat.id.desc()).all()


def update_mode(db: Session, chat: Chat, mode: ChatMode) -> Chat:
    chat.mode = ChatMode.REQUIRE_APPROVAL if mode == ChatMode.AUTO_APPROVE else mode
    db.commit()
    db.refresh(chat)
    return chat


def update_title(db: Session, chat: Chat, title: str | None) -> Chat:
    chat.title = title.strip() or None if title is not None else None
    db.commit()
    db.refresh(chat)
    return chat


def delete_chat(db: Session, chat: Chat) -> None:
    if db.query(PendingAction).filter(PendingAction.chat_id == chat.id).first():
        raise HTTPException(409, "Чат содержит историю решений. Его удаление отключено для сохранения этой истории.")
    db.delete(chat)
    db.commit()


def wipe_chat(db: Session, chat: Chat) -> None:
    """Drop a consult thread so it cannot later land in the knowledge base."""
    db.query(PendingAction).filter(PendingAction.chat_id == chat.id).delete(synchronize_session=False)
    db.delete(chat)
    db.commit()


def user_texts(chat: Chat) -> list[str]:
    texts: list[str] = []
    for message in chat.messages:
        if message.role != "user":
            continue
        for block in message.content or []:
            if not isinstance(block, dict) or block.get("type") == "tool_result":
                continue
            text = block.get("text")
            if isinstance(text, str) and text.strip():
                texts.append(text.strip())
    return texts


def get_own_pending_action_or_404(db: Session, owner: User, pending_action_id: int) -> PendingAction:
    pa = db.get(PendingAction, pending_action_id)
    if not pa or pa.chat.owner_id != owner.id:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Действие не найдено")
    return pa


def list_own_pending_actions(db: Session, owner: User, chat_id: int | None = None) -> list[PendingAction]:
    query = db.query(PendingAction).join(Chat, Chat.id == PendingAction.chat_id).filter(Chat.owner_id == owner.id)
    if chat_id is not None:
        query = query.filter(PendingAction.chat_id == chat_id)
    return query.order_by(PendingAction.id.desc()).all()


def list_agent_activity(db: Session, limit: int = 30) -> list[AgentActivity]:
    """Общий (не по владельцу) лог «Действия агента» — витрина, не привязана
    к конкретному чату. См. app/ai/demo_seed.py, задача 0033."""
    return db.query(AgentActivity).order_by(AgentActivity.id.desc()).limit(limit).all()
