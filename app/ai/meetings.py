"""«Совещание» sessions: lifecycle + recorded audio.

First slice of feature 0004 (task 0004-a). No transcript, no AI here — the
browser captures audio with MediaRecorder and uploads the finished blob; this
module just owns the session row and links the FileAsset. Transcript lines
(0004-b) and AI notes (0004-c) attach to Meeting later.
"""

from datetime import datetime, timezone

from fastapi import HTTPException, UploadFile, status
from sqlalchemy.orm import Session

from app.ai.models import Meeting, MeetingStatus, MeetingTranscriptLine
from app.common.files import FileAsset, FilePurpose, save_upload_file
from app.users.models import User

_AUDIO_PREFIXES = ("audio/",)


def create_meeting(db: Session, owner: User, title: str | None) -> Meeting:
    meeting = Meeting(owner_id=owner.id, title=(title or None), status=MeetingStatus.RECORDING)
    db.add(meeting)
    db.commit()
    db.refresh(meeting)
    return meeting


def get_own_meeting_or_404(db: Session, owner: User, meeting_id: int) -> Meeting:
    meeting = db.get(Meeting, meeting_id)
    if not meeting or meeting.owner_id != owner.id:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Совещание не найдено")
    return meeting


def attach_audio(db: Session, meeting: Meeting, upload_file: UploadFile, user: User) -> Meeting:
    content_type = (upload_file.content_type or "").lower()
    if not content_type.startswith(_AUDIO_PREFIXES):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Ожидается аудиофайл записи совещания",
        )
    asset = save_upload_file(db, upload_file, FilePurpose.MEETING_AUDIO, user)
    meeting.audio_file_id = asset.id
    db.commit()
    db.refresh(meeting)
    return meeting


def finish_meeting(db: Session, meeting: Meeting) -> Meeting:
    if meeting.status == MeetingStatus.FINISHED:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Совещание уже завершено")
    meeting.status = MeetingStatus.FINISHED
    meeting.finished_at = datetime.now(timezone.utc)
    db.commit()
    db.refresh(meeting)
    return meeting


def list_own_meetings(db: Session, owner: User) -> list[Meeting]:
    return (
        db.query(Meeting)
        .filter(Meeting.owner_id == owner.id)
        .order_by(Meeting.started_at.desc(), Meeting.id.desc())
        .all()
    )


def audio_asset(db: Session, meeting: Meeting) -> FileAsset | None:
    if meeting.audio_file_id is None:
        return None
    return db.get(FileAsset, meeting.audio_file_id)


# --- Транскрипт (0004-b) -----------------------------------------------------

_SPEAKER_MAX = 32


def append_transcript_lines(
    db: Session, meeting: Meeting, lines: list[tuple[str, str, int]]
) -> list[MeetingTranscriptLine]:
    """lines — список (speaker, text, at_ms). Дописывать можно только пока
    совещание идёт: у завершённого транскрипт заморожен."""
    if meeting.status == MeetingStatus.FINISHED:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Совещание завершено — транскрипт больше не принимается",
        )
    created: list[MeetingTranscriptLine] = []
    for speaker, text, at_ms in lines:
        clean_text = (text or "").strip()
        if not clean_text:
            continue
        row = MeetingTranscriptLine(
            meeting_id=meeting.id,
            speaker=(speaker or "Спикер 1").strip()[:_SPEAKER_MAX] or "Спикер 1",
            text=clean_text,
            at_ms=max(0, int(at_ms or 0)),
        )
        db.add(row)
        created.append(row)
    db.commit()
    for row in created:
        db.refresh(row)
    return created


def set_line_speaker(
    db: Session, meeting: Meeting, line_id: int, speaker: str
) -> MeetingTranscriptLine:
    line = db.get(MeetingTranscriptLine, line_id)
    if line is None or line.meeting_id != meeting.id:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Реплика не найдена")
    line.speaker = (speaker or "").strip()[:_SPEAKER_MAX] or line.speaker
    db.commit()
    db.refresh(line)
    return line


def transcript_lines(db: Session, meeting: Meeting) -> list[MeetingTranscriptLine]:
    return (
        db.query(MeetingTranscriptLine)
        .filter(MeetingTranscriptLine.meeting_id == meeting.id)
        .order_by(MeetingTranscriptLine.at_ms, MeetingTranscriptLine.id)
        .all()
    )


def duration_sec(meeting: Meeting) -> int | None:
    if meeting.finished_at is None:
        return None
    started, finished = meeting.started_at, meeting.finished_at
    if started.tzinfo is None:
        started = started.replace(tzinfo=timezone.utc)
    if finished.tzinfo is None:
        finished = finished.replace(tzinfo=timezone.utc)
    return max(0, int((finished - started).total_seconds()))
