import enum
from datetime import datetime

from sqlalchemy import DateTime, Enum, ForeignKey, Integer, JSON, String, Text, func
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base


class ChatDomain(str, enum.Enum):
    CLIENTS = "clients"
    PRODUCTION = "production"
    CYCLE = "cycle"
    WAREHOUSE = "warehouse"
    MARKETING = "marketing"
    TASKS = "tasks"
    GENERAL = "general"


class ChatMode(str, enum.Enum):
    NO_ACTIONS = "no_actions"
    REQUIRE_APPROVAL = "require_approval"
    AUTO_APPROVE = "auto_approve"


class PendingActionStatus(str, enum.Enum):
    PENDING = "pending"
    APPROVED = "approved"
    REJECTED = "rejected"


class Chat(Base):
    __tablename__ = "ai_chats"

    id: Mapped[int] = mapped_column(primary_key=True)
    owner_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), nullable=False)
    domain: Mapped[ChatDomain] = mapped_column(Enum(ChatDomain, name="ai_chat_domain"), nullable=False)
    mode: Mapped[ChatMode] = mapped_column(
        Enum(ChatMode, name="ai_chat_mode"), nullable=False, default=ChatMode.REQUIRE_APPROVAL
    )
    title: Mapped[str | None] = mapped_column(String(255), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    owner: Mapped["User"] = relationship()  # noqa: F821
    messages: Mapped[list["Message"]] = relationship(
        back_populates="chat", cascade="all, delete-orphan", order_by="Message.id"
    )


class Message(Base):
    __tablename__ = "ai_messages"

    id: Mapped[int] = mapped_column(primary_key=True)
    chat_id: Mapped[int] = mapped_column(ForeignKey("ai_chats.id", ondelete="CASCADE"), nullable=False)
    role: Mapped[str] = mapped_column(String(16), nullable=False)  # "user" | "assistant" (Anthropic wire format)
    content: Mapped[list] = mapped_column(JSON, nullable=False)
    tool_resolutions: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    chat: Mapped["Chat"] = relationship(back_populates="messages")


class PendingAction(Base):
    __tablename__ = "ai_pending_actions"

    id: Mapped[int] = mapped_column(primary_key=True)
    chat_id: Mapped[int] = mapped_column(ForeignKey("ai_chats.id", ondelete="CASCADE"), nullable=False)
    message_id: Mapped[int] = mapped_column(ForeignKey("ai_messages.id", ondelete="CASCADE"), nullable=False)
    tool_use_id: Mapped[str] = mapped_column(String(128), nullable=False)
    tool_name: Mapped[str] = mapped_column(String(128), nullable=False)
    tool_input: Mapped[dict] = mapped_column(JSON, nullable=False)
    status: Mapped[PendingActionStatus] = mapped_column(
        Enum(PendingActionStatus, name="ai_pending_action_status"),
        nullable=False,
        default=PendingActionStatus.PENDING,
    )
    decided_by_id: Mapped[int | None] = mapped_column(ForeignKey("users.id"), nullable=True)
    decided_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    chat: Mapped["Chat"] = relationship()
    message: Mapped["Message"] = relationship()
    decided_by: Mapped["User"] = relationship()  # noqa: F821


class AiCacheEntry(Base):
    """Generic TTL cache for AI-generated answers that aren't a chat turn -
    section analytics (app/ai/analytics.py), task priorities
    (app/ai/priorities.py) and the "Сегодня" dashboard
    (app/dashboard/service.py) today. `key` identifies what was asked (e.g.
    "section_analytics:clients", "task_priorities:42", "today_dashboard:42");
    `generated_at` is checked against app.ai.cache.CACHE_TTL to decide
    whether to serve this row or call Claude again - see app/ai/cache.py for
    the read/write helpers every producer goes through instead of touching
    this table directly."""

    __tablename__ = "ai_cache_entries"

    key: Mapped[str] = mapped_column(String(128), primary_key=True)
    payload: Mapped[dict] = mapped_column(JSON, nullable=False)
    generated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class McpCredential(Base):
    """Singleton row (id=1) caching the OAuth tokens for the knowledge-base
    MCP server, refreshed and re-obtained by app/ai/mcp_auth.py as needed.
    Losing this row costs one extra round trip, nothing more. There is one
    shared connection to the knowledge base for the whole system, not one
    per employee."""

    __tablename__ = "ai_mcp_credentials"

    id: Mapped[int] = mapped_column(primary_key=True, default=1)
    access_token: Mapped[str] = mapped_column(String(4096), nullable=False)
    refresh_token: Mapped[str | None] = mapped_column(String(4096), nullable=True)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )


class MeetingStatus(str, enum.Enum):
    RECORDING = "recording"
    FINISHED = "finished"


class Meeting(Base):
    """A «Совещание» session: Marina listens in, the browser records audio and
    (from 0004-b) streams a live transcript. This first slice stores only the
    session lifecycle and the recorded audio blob; transcript lines and AI
    notes hang off it in later slices."""

    __tablename__ = "ai_meetings"

    id: Mapped[int] = mapped_column(primary_key=True)
    owner_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), nullable=False)
    title: Mapped[str | None] = mapped_column(String(255), nullable=True)
    status: Mapped[MeetingStatus] = mapped_column(
        Enum(MeetingStatus, name="ai_meeting_status"), nullable=False, default=MeetingStatus.RECORDING
    )
    audio_file_id: Mapped[int | None] = mapped_column(
        ForeignKey("file_assets.id", ondelete="SET NULL"), nullable=True
    )
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    owner: Mapped["User"] = relationship()  # noqa: F821
    audio_file: Mapped["FileAsset | None"] = relationship()  # noqa: F821
    transcript_lines: Mapped[list["MeetingTranscriptLine"]] = relationship(
        back_populates="meeting",
        cascade="all, delete-orphan",
        order_by="MeetingTranscriptLine.at_ms, MeetingTranscriptLine.id",
    )


class MeetingTranscriptLine(Base):
    """One finalized speech fragment of a meeting. The browser recognizes speech
    (Web Speech API) and posts finalized fragments in batches; `speaker` is a
    naive «Спикер N» label the client assigns by pause length and the user can
    correct. `at_ms` is the offset from Meeting.started_at."""

    __tablename__ = "ai_meeting_transcript_lines"

    id: Mapped[int] = mapped_column(primary_key=True)
    meeting_id: Mapped[int] = mapped_column(
        ForeignKey("ai_meetings.id", ondelete="CASCADE"), nullable=False, index=True
    )
    speaker: Mapped[str] = mapped_column(String(32), nullable=False)
    text: Mapped[str] = mapped_column(Text, nullable=False)
    at_ms: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    meeting: Mapped["Meeting"] = relationship(back_populates="transcript_lines")


class MeetingNotes(Base):
    """Marina's structured notes for one meeting (1:1). Recomputed from the
    full transcript — резюме / решения / задачи / открытые вопросы —
    incrementally as the transcript grows and once more on finish.
    `source_line_count` is how many transcript lines the current notes reflect."""

    __tablename__ = "ai_meeting_notes"

    meeting_id: Mapped[int] = mapped_column(
        ForeignKey("ai_meetings.id", ondelete="CASCADE"), primary_key=True
    )
    summary: Mapped[str] = mapped_column(Text, nullable=False, default="")
    decisions: Mapped[list] = mapped_column(JSON, nullable=False, default=list)
    tasks: Mapped[list] = mapped_column(JSON, nullable=False, default=list)
    questions: Mapped[list] = mapped_column(JSON, nullable=False, default=list)
    source_line_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )

    meeting: Mapped["Meeting"] = relationship()
