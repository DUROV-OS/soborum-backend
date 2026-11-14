from datetime import date, datetime
from sqlalchemy import Date, DateTime, ForeignKey, Integer, JSON, String, Text, UniqueConstraint, func
from sqlalchemy.orm import Mapped, mapped_column
from app.db.base import Base


class Project(Base):
    __tablename__ = 'collaboration_projects'
    __table_args__ = (UniqueConstraint('created_by_id', 'request_id'),)
    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(200))
    description: Mapped[str] = mapped_column(Text, default='')
    owner_id: Mapped[int] = mapped_column(ForeignKey('users.id'))
    created_by_id: Mapped[int] = mapped_column(ForeignKey('users.id'))
    client_id: Mapped[int | None] = mapped_column(ForeignKey('clients.id'))
    deal_id: Mapped[int | None] = mapped_column(ForeignKey('crm_entities.id'))
    production_id: Mapped[int | None] = mapped_column(ForeignKey('productions.id'))
    deadline: Mapped[date | None] = mapped_column(Date)
    status: Mapped[str] = mapped_column(String(20), default='active')
    version: Mapped[int] = mapped_column(Integer, default=1)
    request_id: Mapped[str] = mapped_column(String(36))
    request_hash: Mapped[str] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class Member(Base):
    __tablename__ = 'collaboration_members'
    project_id: Mapped[int] = mapped_column(ForeignKey('collaboration_projects.id'), primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey('users.id'), primary_key=True)
    last_read_id: Mapped[int] = mapped_column(Integer, default=0)


class ProjectMessage(Base):
    __tablename__ = 'collaboration_messages'
    __table_args__ = (UniqueConstraint('project_id', 'author_id', 'request_id'),)
    id: Mapped[int] = mapped_column(primary_key=True)
    project_id: Mapped[int] = mapped_column(ForeignKey('collaboration_projects.id'), index=True)
    author_id: Mapped[int] = mapped_column(ForeignKey('users.id'))
    body: Mapped[str] = mapped_column(Text)
    reply_to_id: Mapped[int | None] = mapped_column(ForeignKey('collaboration_messages.id'))
    file_ids: Mapped[list] = mapped_column(JSON, default=list)
    mention_ids: Mapped[list] = mapped_column(JSON, default=list)
    request_id: Mapped[str] = mapped_column(String(36))
    request_hash: Mapped[str] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class ProjectFile(Base):
    __tablename__ = 'collaboration_files'
    __table_args__ = (UniqueConstraint('project_id', 'uploaded_by_id', 'request_id'),)
    id: Mapped[int] = mapped_column(primary_key=True)
    project_id: Mapped[int] = mapped_column(ForeignKey('collaboration_projects.id'), index=True)
    uploaded_by_id: Mapped[int] = mapped_column(ForeignKey('users.id'))
    filename: Mapped[str] = mapped_column(String(255))
    path: Mapped[str] = mapped_column(String(600))
    size: Mapped[int] = mapped_column(Integer)
    sha256: Mapped[str] = mapped_column(String(64))
    request_id: Mapped[str] = mapped_column(String(36))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class ProjectTask(Base):
    __tablename__ = 'collaboration_tasks'
    __table_args__ = (UniqueConstraint('project_id', 'created_by_id', 'request_id'), UniqueConstraint('project_id', 'task_id'))
    id: Mapped[int] = mapped_column(primary_key=True)
    project_id: Mapped[int] = mapped_column(ForeignKey('collaboration_projects.id'), index=True)
    task_id: Mapped[int] = mapped_column(ForeignKey('tasks.id'))
    source_message_id: Mapped[int | None] = mapped_column(ForeignKey('collaboration_messages.id'))
    created_by_id: Mapped[int] = mapped_column(ForeignKey('users.id'))
    request_id: Mapped[str] = mapped_column(String(36))
    request_hash: Mapped[str] = mapped_column(String(64))


class ProjectEvent(Base):
    __tablename__ = 'collaboration_events'
    id: Mapped[int] = mapped_column(primary_key=True)
    project_id: Mapped[int] = mapped_column(ForeignKey('collaboration_projects.id'), index=True)
    actor_id: Mapped[int] = mapped_column(ForeignKey('users.id'))
    kind: Mapped[str] = mapped_column(String(40))
    details: Mapped[dict] = mapped_column(JSON)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
