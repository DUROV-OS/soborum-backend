# Soborbum — бэкенд

FastAPI-бэкенд для управления производством модульных домов: полный цикл клиента
(клиенты → производство → монтаж), склад, маркетинг и сквозные задачи сотрудников.

## Запуск

Скопировать `.env.example` в `.env`, заполнить уникальные `JWT_SECRET`, `POSTGRES_PASSWORD`, `DATABASE_URL` и пароль первого администратора `ADMIN_PASSWORD`. Пример больше не содержит работающих стандартных секретов. Для production указать `ENVIRONMENT=production` и точные `CORS_ALLOWED_ORIGINS`.

```bash
docker compose up --build
```

Compose публикует API на `http://localhost:8005`. Контейнер применяет миграции Alembic и создаёт администратора только при отсутствии администратора в БД. `ADMIN_PASSWORD` не меняет пароль существующего пользователя. После установки этого пакета требуется новый вход: старые токены отзываются.

«Сегодня» и рабочие разделы работают без AI-ключа. Для диалога Марины нужен `ANTHROPIC_API_KEY`. Общий MCP в пилотной политике доступен только администратору и только для чтения.

### Модели под задачи

Модель и «усилие» (`effort`) задаются не одним `AI_MODEL`, а по профилям — см. [app/ai/model_profiles.py](app/ai/model_profiles.py):

| Профиль | Модель | effort | Задача |
| --- | --- | --- | --- |
| `chat` | `claude-sonnet-5` | — | чат Марины |
| `quick` | `claude-haiku-4-5` | — (без extended thinking) | блоки-резюме, аналитика разделов, выбор актуальных задач |
| `board_lead` | `claude-opus-5` | medium | ведущий совета директоров: синтез, правки дерева, «актуализация» |
| `board_agent` | `claude-sonnet-5` | — | 7 ролей-агентов совета, исследовательская справка |
| `daily_job` | `claude-sonnet-5` | low | ежедневные фоновые задачи (`python -m app.jobs.daily`) |
| `weekly_job` | `claude-sonnet-5` | medium | еженедельные фоновые задачи (`python -m app.jobs.weekly`) |

`AI_MODEL` остаётся глобальным рубильником: если он отличается от `claude-sonnet-5`, он переопределяет модель во всех профилях (усилие берётся из профиля). Точечно: `AI_MODELS=chat=claude-opus-5,daily_job=:medium` (элемент — `<profile>=<model|пусто>[:<effort>]`, `effort ∈ low|medium|high|xhigh|max`).

## Проверки и передача

```bash
python -m venv .venv
.venv/bin/python -m pip install -r requirements-dev.txt
.venv/bin/python -m pytest -q
```

Тесты используют отдельную SQLite в памяти и синтетические данные. Секрет для тестов задаётся только в тестовой среде. Полные изменения, влияние на существующий деплой, ограничения и следующая приёмка: [пакет исправлений 05.09.2026](docs/ASTRA_REVIEW_2026-09-05.md).

## Разделы API

Каждый раздел — отдельное FastAPI-приложение со своей Swagger-документацией:

| Раздел | Swagger UI |
| --- | --- |
| Аутентификация и пользователи | http://localhost:8005/api/auth/docs |
| Клиенты | http://localhost:8005/api/clients/docs |
| Производство | http://localhost:8005/api/production/docs |
| Монтаж | http://localhost:8005/api/installation/docs |
| Цикл клиента | http://localhost:8005/api/cycles/docs |
| Склад | http://localhost:8005/api/warehouse/docs |
| Маркетинг | http://localhost:8005/api/marketing/docs |
| Задачи | http://localhost:8005/api/tasks/docs |
| ИИ-ассистент | http://localhost:8005/api/ai/docs |
| Совет директоров | http://localhost:8005/api/board/docs |
| Telegram-мост | http://localhost:8005/api/telegram/docs |

Авторизация — JWT: `POST /api/auth/login` (форма `username`/`password`), затем
`Authorize` в любом Swagger UI с полученным токеном (действует на все разделы,
т.к. проверяется общим образом через `/api/auth/login`).

## Структура проекта

Код организован по разделам (vertical slices), а не по техническому слою —
у каждого раздела один файл на слой (`models.py`, `schemas.py`, `service.py`,
`router.py`), и большая часть логики раздела лежит в одной папке:

```
app/
  core/      конфиг, JWT, зависимости доступа (общие для всех разделов)
  db/        подключение к БД
  common/    Module enum (доступ) и хранение файлов (используются всеми разделами)
  users/     аутентификация, пользователи, матрица доступа
  tasks/     общий движок задач + синхронизация со всеми разделами
  cycle/     сквозной агрегатор клиент+производство+монтаж
  clients/   клиенты (лид → постоплата)
  production/ модули дома, материалы, заявки на материалы
  installation/ монтаж (доставка → установка → проработка)
  warehouse/ склад, поставки, история движения материалов
  marketing/ календарь контента
  telegram/  мост с рабочим Telegram-чатом (архив сообщений, алиасы, диплинк-логин)
  jobs/      ежедневный оркестратор: сборщики → саммари базы знаний
```

## Telegram-мост и ежедневный прогон

`app/telegram` слушает рабочую группу Telegram и архивирует каждое сообщение
в `telegram_messages` по мере поступления — Bot API не отдаёт историю чата,
поэтому сообщения надо собирать сразу. Два способа приёма апдейтов:

* **Поллер** — отдельный процесс `python -m app.telegram.poller`
  (сервис `telegram-poller` в docker-compose). Long-poll `getUpdates`.
* **Вебхук** — `POST /api/telegram/webhook/<secret>` (задать
  `TELEGRAM_WEBHOOK_SECRET` и вызвать у Telegram `setWebhook`). Тогда
  отдельный процесс не нужен.

`app/jobs` — оркестраторы фоновых прогонов. Ежедневный (`app/jobs/daily.py`):
сначала все задачи-сборщики из реестра (`app/jobs/registry.py`; пока это
только выгрузка Telegram-чата в базу знаний), затем отдельный шаг — саммари
дня по регламенту (`app/jobs/kb_summary.py`). Еженедельный
(`app/jobs/weekly.py`) — симметричный каркас под задачи `@weekly_job(...)`
(пока пуст). Ежедневные задачи ходят к модели на профиле `daily_job`
(Sonnet, low effort), еженедельные — `weekly_job` (Sonnet, medium).

```bash
python -m app.jobs.daily                 # за вчера в KB_TIMEZONE
python -m app.jobs.daily --date 2026-09-05
python -m app.jobs.daily --only telegram_ingest --skip-summary
python -m app.jobs.weekly                 # еженедельный прогон
```

Пример cron на хосте:

```cron
15 3 * * *  docker compose -f /opt/durov-os/backend/docker-compose.yml exec -T backend python -m app.jobs.daily
30 4 * * 1  docker compose -f /opt/durov-os/backend/docker-compose.yml exec -T backend python -m app.jobs.weekly
```

Фоновые задачи пишут в общую базу знаний через тот же MCP-коннектор, что и
чат Марины, но, в отличие от чата, им разрешены инструменты записи
(`append_note`, `create_note`, …) — это доверенный код без человека в контуре.
Если `ANTHROPIC_API_KEY` или MCP не настроены, шаг помечается `skipped`, а не
падает.

**Словарь «алиас → сотрудник».** `app/telegram/aliases.py` — статическая
часть (правится и коммитится), плюс динамическая в таблице
`telegram_account_links` (заполняется диплинк-логином или через
`PUT /api/telegram/links`). Разрешение: явная привязка в БД → alias из
привязки → `@username` из сообщения.

**Вход в Durov-OS через Telegram (диплинк).** Кнопка в боковом меню зовёт
`POST /api/telegram/login/deep-link` и получает ссылку вида
`https://t.me/<bot>?start=login_<token>`. Пользователь открывает её, бот на
`/start login_<token>` привязывает Telegram-аккаунт к учётной записи.
Токен одноразовый, живёт 15 минут.
