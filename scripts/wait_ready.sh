#!/bin/sh
# Ждёт, пока свежевыкаченный backend ответит 200 на /ready (0084-a).
# Запускается деплоем на сервере из каталога compose-проекта сразу после
# `docker compose up -d --build`. Не дождались — печатает хвост логов backend
# и выходит с ошибкой, чтобы деплой стал красным.
# Использование: sh scripts/wait_ready.sh [секунд, по умолчанию 90]
set -u

timeout="${1:-90}"
deadline=$(( $(date +%s) + timeout ))
# Опрос изнутри контейнера: порт снаружи у прода и стейджа разный (BACKEND_PORT),
# а внутри всегда 8000. Пока контейнер перезапускается, exec просто падает.
probe="import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/ready', timeout=3)"

while [ "$(date +%s)" -lt "$deadline" ]; do
    if docker compose exec -T backend python -c "$probe" >/dev/null 2>&1; then
        echo "ready: ok"
        exit 0
    fi
    sleep 3
done

echo "ready: backend не ответил на /ready за ${timeout} с — последние 50 строк логов:" >&2
docker compose logs --no-color --tail=50 backend >&2
exit 1
