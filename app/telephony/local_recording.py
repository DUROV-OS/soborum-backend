"""Finalize gateway recordings locally; no audio is sent to third-party AI."""
from datetime import timedelta
import json
import os
from pathlib import Path
import subprocess
from app.core.config import settings
from app.telephony.models import BrowserPhoneCall, PhoneCall
from app.telephony.service import audio_path
from app.telephony import browser_phone


def convert(row):
    directory = Path(settings.storage_dir) / 'calls' / 'browser'
    inputs = sorted(directory.glob(row.id + '-*.mjr'))
    if not inputs:
        raise ValueError('Gateway recording missing')
    converted = []
    for source in inputs:
        # MJR v2 starts with an 8-byte marker, then a big-endian JSON header length.
        with source.open('rb') as stream:
            if stream.read(8) != b'MJR00002':
                raise ValueError('Unexpected recording header')
            size = int.from_bytes(stream.read(2), 'big')
            if not 0 < size < 8192:
                raise ValueError('Invalid recording header')
            header = json.loads(stream.read(size))
        extension = {'opus': '.opus', 'pcmu': '.wav', 'pcma': '.wav', 'g711': '.wav', 'g722': '.wav', 'l16': '.wav'}.get(header.get('c'))
        if not extension:
            raise ValueError('Unsupported recording codec')
        target = source.with_suffix(extension)
        subprocess.run(['janus-pp-rec', str(source), str(target)], check=True, capture_output=True, timeout=180)
        os.chmod(target, 0o600)
        converted.append(target)
    destination = audio_path(row.call_id)
    partial = destination.with_suffix('.partial.mp3')
    command = ['ffmpeg', '-nostdin', '-y', '-loglevel', 'error']
    for source in converted:
        command += ['-i', str(source)]
    if len(converted) > 1:
        command += ['-filter_complex', f'amix=inputs={len(converted)}:duration=longest:normalize=1']
    command += ['-ac', '1', '-ar', '16000', '-codec:a', 'libmp3lame', '-b:a', '64k', str(partial)]
    try:
        subprocess.run(command, check=True, capture_output=True, timeout=240)
        os.chmod(partial, 0o600)
        partial.replace(destination)
    finally:
        partial.unlink(missing_ok=True)


def finalize_one(db):
    # A gateway session times out after the backend stops polling. Recover its CRM
    # record too, including after a process restart, without redialling anything.
    stale = db.query(BrowserPhoneCall).filter(BrowserPhoneCall.state.notin_(browser_phone.TERMINAL),
            BrowserPhoneCall.updated_at < browser_phone.now() - timedelta(minutes=3)).first()
    if stale:
        browser_phone.finish(db, stale, failed=not stale.accepted_at)
    call = db.query(PhoneCall).filter_by(provider='browser_sip', processing='local_recording').first()
    if not call:
        return False
    row = db.query(BrowserPhoneCall).filter_by(call_id=call.id).one()
    try:
        convert(row)
        call.audio_ready, call.processing, call.error = True, 'queued', ''
    except Exception:
        call.processing, call.error = 'no_recording', 'Запись разговора не получена. Проверьте подключение серверной записи.'
    db.commit()
    return True
