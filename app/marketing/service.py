from datetime import date, datetime, timezone

from fastapi import HTTPException, status
from sqlalchemy.orm import Session

from app.common.files import FileAsset
from app.common.module_access import Module as AccessModule
from app.marketing.models import CONTENT_STAGE_ORDER, ContentItem, ContentStage, PostLink
from app.notifications.models import NotificationKind
from app.notifications.service import notify
from app.marketing.schemas import (
    ContentAnalysisUpdate,
    ContentFinalUpdate,
    ContentItemCreate,
    ContentItemUpdate,
    ContentRawUpdate,
    PostLinkIn,
)
from app.tasks import service as task_service
from app.tasks import sync as task_sync
from app.tasks.models import TaskLinkType
from app.users import service as user_service
from app.users.models import User


def _next_stage(stage: ContentStage) -> ContentStage | None:
    idx = CONTENT_STAGE_ORDER.index(stage)
    if idx + 1 < len(CONTENT_STAGE_ORDER):
        return CONTENT_STAGE_ORDER[idx + 1]
    return None


def _create_transition_task(db: Session, content: ContentItem) -> None:
    if _next_stage(content.stage) is None:
        return
    assignees = user_service.users_with_access(db, AccessModule.MARKETING)
    task_service.create_link_task(
        db,
        title=f"Материал «{content.title}»: перевести со стадии «{content.stage.value}» на следующую",
        link_type=TaskLinkType.CONTENT_STAGE,
        link_id=content.id,
        assignees=assignees,
        link_meta={"stage": content.stage.value},
    )


def _resolve_users(db: Session, ids: list[int]) -> list[User]:
    if not ids:
        return []
    users = db.query(User).filter(User.id.in_(ids)).all()
    if len(users) != len(set(ids)):
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Один или несколько пользователей не найдены")
    return users


def get_content_or_404(db: Session, content_id: int) -> ContentItem:
    content = db.get(ContentItem, content_id)
    if not content:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Единица контента не найдена")
    return content


def create_content(db: Session, payload: ContentItemCreate) -> ContentItem:
    content = ContentItem(
        title=payload.title,
        description=payload.description,
        planned_release_date=payload.planned_release_date,
        platforms=payload.platforms,
    )
    content.assignees = _resolve_users(db, payload.assignee_ids)
    db.add(content)
    db.flush()
    _create_transition_task(db, content)
    return content


def update_basic(db: Session, content: ContentItem, payload: ContentItemUpdate) -> ContentItem:
    data = payload.model_dump(exclude_unset=True, exclude={"assignee_ids"})
    for field, value in data.items():
        setattr(content, field, value)
    if payload.assignee_ids is not None:
        content.assignees = _resolve_users(db, payload.assignee_ids)
    db.flush()
    return content


def update_raw(db: Session, content: ContentItem, payload: ContentRawUpdate) -> ContentItem:
    if content.raw_locked_at is not None:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Сырой материал уже зафиксирован")
    content.raw_texts = payload.raw_texts
    if payload.raw_file_ids:
        content.raw_files = db.query(FileAsset).filter(FileAsset.id.in_(payload.raw_file_ids)).all()
    db.flush()
    return content


def update_final(db: Session, content: ContentItem, payload: ContentFinalUpdate) -> ContentItem:
    content.final_texts = payload.final_texts
    if payload.final_file_ids:
        content.final_files = db.query(FileAsset).filter(FileAsset.id.in_(payload.final_file_ids)).all()
    db.flush()
    return content


def set_post_links(db: Session, content: ContentItem, links: list[PostLinkIn]) -> ContentItem:
    db.query(PostLink).filter(PostLink.content_id == content.id).delete()
    for link in links:
        db.add(PostLink(content_id=content.id, platform=link.platform, url=link.url))
    db.flush()
    return content


def update_analysis(db: Session, content: ContentItem, payload: ContentAnalysisUpdate) -> ContentItem:
    content.analysis_notes = payload.analysis_notes
    content.analysis_reach = payload.analysis_reach
    db.flush()
    return content


def get_calendar(db: Session, date_from: date | None, date_to: date | None) -> list[ContentItem]:
    query = db.query(ContentItem).filter(ContentItem.planned_release_date.isnot(None))
    if date_from is not None:
        query = query.filter(ContentItem.planned_release_date >= date_from)
    if date_to is not None:
        query = query.filter(ContentItem.planned_release_date <= date_to)
    return query.order_by(ContentItem.planned_release_date).all()


# Только для текста уведомления (0080-c) — своего словаря подписей у стадий
# контента в этом разделе нет.
_CONTENT_STAGE_LABELS: dict[ContentStage, str] = {
    ContentStage.GATHERING: "сбор материала",
    ContentStage.EDITING: "монтаж",
    ContentStage.RELEASE: "публикация",
    ContentStage.ANALYSIS: "анализ",
}


def _notify_content_stage_change(db: Session, content: ContentItem) -> None:
    """Смена стадии поста/контента (0080-c) — уведомление всем `assignees`
    материала (M2M, `content_assignees`) — в отличие от задач с одним
    `responsible_id`, у материала несколько исполнителей сразу."""
    if not content.assignees:
        return
    label = _CONTENT_STAGE_LABELS.get(content.stage, content.stage.value)
    for assignee in content.assignees:
        notify(
            db,
            assignee.id,
            kind=NotificationKind.CONTENT_UPDATE,
            module=AccessModule.MARKETING,
            title=f"Материал «{content.title}» перешёл на стадию «{label}»",
            object_type="content_item",
            object_id=content.id,
        )


def transition_stage(db: Session, content: ContentItem) -> ContentItem:
    next_stage = _next_stage(content.stage)
    if next_stage is None:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Материал уже на последней стадии")

    if content.stage == ContentStage.GATHERING:
        if not content.raw_texts and not content.raw_files:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Не загружен сырой материал")
        content.raw_locked_at = datetime.now(timezone.utc)

    elif content.stage == ContentStage.EDITING:
        if not content.final_texts and not content.final_files:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Не загружен готовый материал")

    elif content.stage == ContentStage.RELEASE:
        existing_platforms = {pl.platform for pl in content.post_links}
        missing = set(content.platforms) - existing_platforms
        if missing:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"Не указаны ссылки для платформ: {', '.join(missing)}",
            )

    content.stage = next_stage
    db.flush()
    _notify_content_stage_change(db, content)

    task_service.close_open_link_task(db, TaskLinkType.CONTENT_STAGE, content.id)
    _create_transition_task(db, content)

    return content


@task_sync.register(TaskLinkType.CONTENT_STAGE)
def _on_content_task_closed(db: Session, task) -> None:
    content = db.get(ContentItem, task.link_id)
    if content is not None:
        transition_stage(db, content)
