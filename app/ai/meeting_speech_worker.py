"""Speech-only queue consumer, independent of the web app's optional SDKs.

Uses SQLAlchemy Core, so the existing speech image need not import the entire
web application's ORM registry. The planner consumes the resulting durable text.
"""
from datetime import datetime, timedelta, timezone
import logging
import time
from uuid import uuid4
from sqlalchemy import MetaData, Table, and_, create_engine, insert, or_, select, update
from app.core.config import settings
from app.integrity.context import apply_actor

log = logging.getLogger(__name__)


class SpeechQueue:
    def __init__(self, engine):
        self.engine = engine
        metadata = MetaData()
        self.jobs = Table('ai_meeting_processing', metadata, autoload_with=engine, resolve_fks=False)
        self.meetings = Table('ai_meetings', metadata, autoload_with=engine, resolve_fks=False)
        self.assets = Table('file_assets', metadata, autoload_with=engine, resolve_fks=False)
        self.lines = Table('ai_meeting_transcript_lines', metadata, autoload_with=engine, resolve_fks=False)

    def actor(self, connection):
        apply_actor(connection, {'kind': 'meeting_speech_worker'})

    def claim(self):
        now = datetime.now(timezone.utc)
        c = self.jobs.c
        ready = c.data['transcript_ready'].as_boolean()
        eligible = or_(
            and_(c.status.in_(['queued', 'error']), c.attempts < 3, or_(c.lease_until.is_(None), c.lease_until < now)),
            and_(c.status == 'transcribing', or_(c.lease_until.is_(None), c.lease_until < now)))
        with self.engine.begin() as connection:
            self.actor(connection)
            row = connection.execute(select(self.jobs).where(eligible, or_(ready.is_(None), ready.is_(False)))
                .order_by(c.meeting_id).with_for_update(skip_locked=True)).mappings().first()
            if row is None: return None
            token = str(uuid4())
            connection.execute(update(self.jobs).where(c.meeting_id == row['meeting_id']).values(
                status='transcribing', attempts=row['attempts']+1, error='', lease_token=token,
                lease_until=now+timedelta(hours=4), version=row['version']+1, updated_at=now))
            return row['meeting_id'], token

    def current(self, connection, id, token):
        return connection.execute(select(self.jobs).where(self.jobs.c.meeting_id == id,
            self.jobs.c.lease_token == token).with_for_update()).mappings().first()

    def process(self, id, token, transcriber):
        try:
            with self.engine.begin() as connection:
                if self.current(connection, id, token) is None: return
                path = connection.execute(select(self.assets.c.path_on_disk).select_from(
                    self.meetings.join(self.assets, self.meetings.c.audio_file_id == self.assets.c.id))
                    .where(self.meetings.c.id == id)).scalar_one()
            segments = transcriber(path)
            if not segments: raise ValueError('Speech not recognized')
            with self.engine.begin() as connection:
                self.actor(connection)
                row = self.current(connection, id, token)
                if row is None: return
                ids = []
                for segment in segments:
                    value = segment['text'].strip()
                    if not value: continue
                    ids.append(connection.execute(insert(self.lines).values(meeting_id=id, speaker='Участник',
                        text=value, at_ms=max(0, int(segment['start']*1000)), is_assistant_query=False)
                        .returning(self.lines.c.id)).scalar_one())
                if not ids: raise ValueError('Speech not recognized')
                connection.execute(update(self.jobs).where(self.jobs.c.meeting_id == id).values(
                    data={**row['data'], 'transcript_ready': True, 'transcript_line_ids': ids,
                          'speech_model': settings.call_whisper_model},
                    status='analyzing', error='', lease_token=None, lease_until=None,
                    version=row['version']+1, updated_at=datetime.now(timezone.utc)))
        except Exception as error:
            with self.engine.begin() as connection:
                self.actor(connection)
                row = self.current(connection, id, token)
                if row is not None:
                    connection.execute(update(self.jobs).where(self.jobs.c.meeting_id == id).values(
                        status='error', lease_token=None, lease_until=datetime.now(timezone.utc)+timedelta(minutes=5),
                        error='Не удалось распознать речь. Аудио сохранено; проверьте запись и повторите обработку.',
                        version=row['version']+1, updated_at=datetime.now(timezone.utc)))
            log.warning('Meeting speech failed id=%s kind=%s', id, type(error).__name__)


def main():
    logging.basicConfig(level=logging.INFO)
    queue = SpeechQueue(create_engine(settings.database_url, pool_pre_ping=True))
    model = None
    def transcriber(path):
        nonlocal model
        if model is None:
            from faster_whisper import WhisperModel
            model = WhisperModel(settings.call_whisper_model, device='cpu', compute_type='int8', cpu_threads=1)
        # Same local model and recognition parameters used for company calls.
        segments, _ = model.transcribe(str(path), language='ru', beam_size=3, vad_filter=True,
                                        condition_on_previous_text=False)
        return [{'start': s.start, 'text': s.text.strip()} for s in segments if s.text.strip()]
    while True:
        job = queue.claim()
        if job: queue.process(*job, transcriber)
        else: time.sleep(10)


if __name__ == '__main__': main()
