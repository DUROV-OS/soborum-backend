"""Разовый скрипт для задачи 0015: найти opcode запроса URL для загрузки
файла в MAX (`Settings.max_file_upload_opcode` — сейчас не задан, см. его
докстринг в app/core/config.py).

Песочница агента блокирует прямые сетевые запросы к MAX, поэтому опкод
не подтверждён кодом — запустить этот скрипт может тот, у кого есть живой
`MAX_TOKEN` и доступ в интернет без такого ограничения.

Запуск:
    cd backend && .venv/bin/python scripts/max_discover_upload_opcode.py

Что делает:
  1. Открывает сессию MAX (тот же MaxSession, что и весь app/max).
  2. Перебирает кандидатов opcode из CANDIDATES, для каждого шлёт
     {"count": 1} и печатает ответ. Рабочий даёт payload вида
     {"info": [{"fileId": <int>, "url": "https://..."}]} — это и есть
     нужный opcode.
  3. Ничего не загружает и не отправляет — только запрашивает URL
     (побочных эффектов в чатах нет).

Если ни один кандидат не подошёл — сверить с реальным перебором точнее:
открыть web.max.ru, DevTools → Network → WS, отправить файл в «Избранное»
и найти исходящий кадр с payload {"count": 1} прямо перед PUT/POST на
сторонний домен — opcode этого кадра и есть искомый.
"""

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.core.config import settings  # noqa: E402
from app.max.client import MaxSession  # noqa: E402

# Известные opcode из app/max/client.py: 6 HELLO, 19 AUTH, 49 история,
# 64 MSG_SEND, 83 GET_VIDEO_URL, 88 GET_ATTACH_URL — исключены из перебора.
CANDIDATES = [n for n in range(65, 100) if n not in (83, 88)]


def looks_like_upload_url_reply(payload: dict) -> bool:
    info = (payload or {}).get("info")
    if isinstance(info, list) and info and isinstance(info[0], dict):
        return "url" in info[0] and ("fileId" in info[0] or "videoId" in info[0])
    return "url" in (payload or {})  # PHOTO-вариант — просто {"url": ...}


def main() -> None:
    if not settings.max_token:
        print("MAX_TOKEN не задан — нечем авторизоваться.")
        return

    s = MaxSession(settings.max_token).open()
    try:
        for opcode in CANDIDATES:
            seq = s._send(opcode, {"count": 1})
            try:
                frame = s._wait(opcode, seq, tries=5)
            except TimeoutError:
                continue
            payload = frame.get("payload") or {}
            marker = "  <-- похоже на искомый!" if looks_like_upload_url_reply(payload) else ""
            print(f"opcode {opcode}: cmd={frame.get('cmd')} payload={json.dumps(payload)[:200]}{marker}")
    finally:
        s.close()


if __name__ == "__main__":
    main()
