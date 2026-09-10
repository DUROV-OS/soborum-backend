"""Free neural TTS for Marina's spoken résumé (Microsoft Edge voices via edge-tts).

No paid API key. Female Russian neural voice: ru-RU-SvetlanaNeural.
"""

from __future__ import annotations

import io
import logging

log = logging.getLogger("app.ai.tts")

# Soft, warm heroine voice — free neural, much better than OS speechSynthesis.
VOICE = "ru-RU-SvetlanaNeural"
# Slightly slower and a touch brighter for a gentle, attractive delivery.
RATE = "-12%"
PITCH = "+8Hz"
MAX_CHARS = 420

# Выбираемые нейросетевые голоса Edge TTS (бесплатные, без ключа). Значение —
# кортеж (voice, rate, pitch); дефолт совпадает с настройками раздела «Агенты».
VOICES: dict[str, tuple[str, str, str]] = {
    "ru-RU-SvetlanaNeural": (VOICE, RATE, PITCH),
    "ru-RU-DmitryNeural": ("ru-RU-DmitryNeural", "-6%", "+0Hz"),
    "ru-RU-DariyaNeural": ("ru-RU-DariyaNeural", "-10%", "+4Hz"),
}


async def synthesize_mp3(text: str, voice: str | None = None) -> bytes:
    cleaned = " ".join((text or "").split()).strip()
    if not cleaned:
        raise ValueError("Пустой текст для озвучки")
    if len(cleaned) > MAX_CHARS:
        cleaned = cleaned[: MAX_CHARS - 1].rsplit(" ", 1)[0] + "…"

    try:
        import edge_tts
    except ImportError as exc:
        raise RuntimeError("edge-tts не установлен") from exc

    name, rate, pitch = VOICES.get(voice or "", VOICES[VOICE])
    communicate = edge_tts.Communicate(cleaned, name, rate=rate, pitch=pitch)
    buffer = io.BytesIO()
    async for chunk in communicate.stream():
        if chunk["type"] == "audio":
            buffer.write(chunk["data"])
    data = buffer.getvalue()
    if not data:
        raise RuntimeError("Пустой аудиоответ TTS")
    return data
