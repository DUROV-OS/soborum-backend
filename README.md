# Soborbum — бэкенд

FastAPI-бэкенд для управления производством модульных домов: полный цикл клиента
(клиенты → производство → монтаж), склад, маркетинг и сквозные задачи сотрудников.

## Запуск

Скопировать `.env.example` в `.env`, заполнить уникальные `JWT_SECRET`, `POSTGRES_PASSWORD`, `DATABASE_URL` и пароль первого администратора `ADMIN_PASSWORD`. Пример больше не содержит работающих стандартных секретов. Для production указать `ENVIRONMENT=production` и точные `CORS_ALLOWED_ORIGINS`.

```bash
docker compose up --build
```

Compose публикует API на `http://localhost:8005`. Контейнер применяет миграции Alembic и создаёт администратора только при отсутствии администратора в БД. `ADMIN_PASSWORD` не меняет пароль существующего пользователя. После установки этого пакета требуется новый вход: старые токены отзываются.

«Сегодня» и рабочие разделы работают без AI-ключа. Для диалога Марины нужен `ANTHROPIC_API_KEY` и доступная провайдеру модель из `AI_MODEL`. Общий MCP в пилотной политике доступен только администратору и только для чтения.

## Проверки и передача

```bash
python -m venv .venv
.venv/bin/python -m pip install -r requirements-dev.txt
.venv/bin/python -m pytest -q
```

Тесты используют отдельную SQLite в памяти и синтетические данные. Секрет для тестов задаётся только в тестовой среде. Полные изменения, влияние на существующий деплой, ограничения и следующая приёмка: [пакет исправлений 05.09.2026](docs/ASTRA_REVIEW_2026-09-05.md).

## Стейдж-окружение

Отдельное окружение для проверки веток перед прод-мержем: `https://stage.soborum.durov.house`.
Деплой — пуш в ветку `staging` (workflow `.github/workflows/deploy-staging.yml`),
не задевает прод (`push:main`, `deploy.yml`) ни при успехе, ни при падении —
это отдельный job на отдельном триггере. Ручной передеплой без нового коммита —
`workflow_dispatch` на `deploy-staging.yml` в Actions.

Стейдж живёт на **том же VPS**, что и прод, но изолированно:

| | Прод | Стейдж |
| --- | --- | --- |
| Ветка деплоя | `main` | `staging` |
| Директория на сервере | `/srv/soborbum-backend` | `/srv/soborbum-backend-staging` |
| Порт бэка (127.0.0.1) | 8005 | 8006 |
| compose-проект/сеть | `soborbum-backend_*` | `soborbum-backend-staging_*` (отдельная БД, недостижима с прод-стороны) |
| GitHub Secrets | `SSH_HOST`, `SSH_USER`, `SSH_PRIVATE_KEY`, `JWT_SECRET`, `ADMIN_*`, `CORS_ALLOWED_ORIGINS`, `APP_ENV` | те же имена с префиксом `STAGING_` |
| Внешние интеграции (`ANTHROPIC_API_KEY`, `MOYSKLAD_MCP_*`, `MAX_TOKEN` и т.п.) | свои секреты | **переиспользует прод-секреты** — осознанный риск, см. `backlog/.../0069-staging-environment.md` |

Первичная настройка на сервере (руками, один раз — агент не имеет SSH-доступа):

```bash
git clone <репозиторий> /srv/soborbum-backend-staging
cd /srv/soborbum-backend-staging
git checkout staging   # ветка должна существовать в origin
cp .env.example .env
# в .env выставить: BACKEND_PORT=8006, POSTGRES_PASSWORD=<другой, не прод>,
# ADMIN_PASSWORD=<свой>, JWT_SECRET=<свой>, APP_ENV=staging,
# CORS_ALLOWED_ORIGINS=https://stage.soborum.durov.house
docker compose up -d --build
```

Плюс nginx-vhost на сервере (не в этом репозитории), например:

```nginx
server {
    server_name stage.soborum.durov.house;
    location /api/ { proxy_pass http://127.0.0.1:8006; }
    location /      { root /var/www/soborbum-frontend-staging/dist; try_files $uri /index.html; }
}
```

GitHub Secrets `STAGING_*` — добавить в оба репозитория (`soborum-backend`,
`soborbum-frontend`) через `gh secret set` или настройки репозитория; значения
(SSH-ключ деплоя, пароли) агент не генерирует и не имеет к ним доступа.

Логи стейдж-деплоя — вкладка Actions → workflow «Deploy staging». Логи
контейнера на сервере — `docker compose -p soborbum-backend-staging logs -f backend`
(или из `/srv/soborbum-backend-staging`: `docker compose logs -f backend`).

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
| Мессенджер MAX | http://localhost:8005/api/max/docs |

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
```
