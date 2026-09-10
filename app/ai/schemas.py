from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from app.ai.models import ChatDomain, ChatMode, MeetingStatus, PendingActionStatus
from app.tasks.schemas import TaskOut


class AskRequest(BaseModel):
    chat_id: int | None = None
    message: str = ""
    file_ids: list[int] = Field(default_factory=list)  # ids from POST /ai/files, must belong to the caller
    mode: ChatMode = ChatMode.REQUIRE_APPROVAL  # only used when chat_id is absent (new chat)


class ChatModeUpdate(BaseModel):
    mode: ChatMode


class ChatTitleUpdate(BaseModel):
    title: str | None = Field(default=None, max_length=255)


class ChatOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    domain: ChatDomain
    mode: ChatMode
    title: str | None
    created_at: datetime


class MessageOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    role: str
    content: list
    tool_resolutions: dict | None
    created_at: datetime


class ChatDetailOut(ChatOut):
    messages: list[MessageOut] = []


class PendingActionOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    chat_id: int
    message_id: int
    tool_name: str
    tool_input: dict
    status: PendingActionStatus
    decided_by_id: int | None
    decided_at: datetime | None
    created_at: datetime
    summary: str = "Действие Марины"
    execution_status: Literal["pending", "succeeded", "failed", "rejected", "unknown"] = "unknown"
    policy_version: str | None = None


class AskResponse(BaseModel):
    chat_id: int
    status: Literal["completed", "pending_approval"]
    reply: str | None = None
    pending_actions: list[PendingActionOut] = []


class ConsultAskResponse(AskResponse):
    topic_reset: bool = False


class SpeakRequest(BaseModel):
    text: str = Field(..., min_length=1, max_length=500)


SectionStatus = Literal["red", "yellow", "green"]


class SectionAnalyticsOut(BaseModel):
    section: str
    generated_at: datetime
    summary: str
    status: SectionStatus


class PriorityTaskOut(BaseModel):
    task: TaskOut
    reason: str


# --- Режим «Совещание» (0004) -------------------------------------------------


class MeetingCreate(BaseModel):
    title: str | None = Field(default=None, max_length=255)


class MeetingOut(BaseModel):
    id: int
    title: str | None
    status: MeetingStatus
    started_at: datetime
    finished_at: datetime | None
    duration_sec: int | None
    has_audio: bool


class TranscriptLineOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    speaker: str
    text: str
    at_ms: int


class TranscriptLineIn(BaseModel):
    speaker: str = Field(default="Спикер 1", max_length=32)
    text: str = Field(min_length=1)
    at_ms: int = Field(default=0, ge=0)


class TranscriptAppendIn(BaseModel):
    lines: list[TranscriptLineIn] = Field(default_factory=list)


class TranscriptSpeakerUpdate(BaseModel):
    speaker: str = Field(min_length=1, max_length=32)


class MeetingAskIn(BaseModel):
    question: str = Field(min_length=1, max_length=2000)


class MeetingAskOut(BaseModel):
    answer_markdown: str


class MeetingDetailOut(MeetingOut):
    audio_url: str | None = None
    transcript: list[TranscriptLineOut] = Field(default_factory=list)
    # Заметки Марины — 0004-c; здесь всегда null.
    notes: dict | None = None
    ai_enabled: bool = False


class TaskPrioritiesOut(BaseModel):
    generated_at: datetime
    priorities: list[PriorityTaskOut]
