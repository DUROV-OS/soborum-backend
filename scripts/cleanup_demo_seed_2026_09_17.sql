-- Разовая очистка данных, которые попали на реальный прод 2026-09-17: PR #63
-- (feat/production/home-demo-documents, задача 0065-d) добавил демо-сид
-- производства/документов клиента, и из-за того, что APP_ENV на реальном
-- сервере не распознавался как prod, демо-данные создались по-настоящему
-- при автодеплое. Код уже откачен и защищён (fix/core: демо-сиды теперь
-- требуют явный ENABLE_DEMO_SEED=1, см. коммит 82a0062) — этот скрипт
-- только чистит уже вставленные строки, откат кода их не убирает.
--
-- Идентифицирует ровно демо-клиентов app.clients.demo_seed._CLIENTS по их
-- фиктивным email на домене @durov.local (гарантированно не совпадают с
-- реальными данными) и всё, что от них зависит: заметки, цикл, производство
-- (модуль, задачи), а также реальные загруженные АР/КР-файлы, которые
-- app.clients.demo_seed._attach_demo_documents прикрепила к первому из них.
--
-- ВАЖНО: удаляет ТОЛЬКО производство/документы (то, что добавил PR #63
-- сегодня). Сами демо-клиенты (Никитин/Панова/Громов) — это отдельный,
-- более старый сидер (задача 0025), который мог существовать на проде и
-- раньше, независимо от сегодняшнего инцидента; секция их удаления вынесена
-- отдельно и закомментирована — решите отдельно, нужно ли её выполнять.
--
-- Использование:
--   1. ШАГ 1 (SELECT) — посмотреть, что реально найдено. Проверить глазами:
--      ровно эти 3 email на @durov.local, файлы — именно эти 2 документа.
--   2. ШАГ 2 (DELETE) — раскомментировать и выполнить в ТОЙ ЖЕ сессии psql.
--   3. ШАГ 3 — вывести path_on_disk удалённых файлов, чтобы вручную стереть
--      сами байты с диска контейнера (SQL их не трогает).
--   4. ШАГ 4 (опционально, закомментирован) — удалить сами демо-клиентов
--      целиком, если решите, что их тоже быть не должно.
--
-- Пример подключения на проде (файл виден только на хосте — контейнер db
-- не монтирует репозиторий, поэтому содержимое передаётся через stdin,
-- а не путём -f внутри контейнера):
--   ssh durov@89.207.254.32
--   cd /srv/soborbum-backend
--   docker compose exec -T db psql -U soborbum -d soborbum < scripts/cleanup_demo_seed_2026_09_17.sql

-- ШАГ 1: посмотреть, что будет удалено (ничего не меняет)
SELECT c.id AS client_id, c.full_name, c.email, c.cycle_id,
       p.id AS production_id, p.name AS production_name,
       m.id AS module_id, m.name AS module_name,
       t.id AS task_id, t.title AS task_title, t.deadline
FROM clients c
LEFT JOIN productions p ON p.cycle_id = c.cycle_id
LEFT JOIN modules m ON m.production_id = p.id
LEFT JOIN tasks t ON t.module_id = m.id
WHERE c.email IN ('demo.nikitin@durov.local', 'demo.panova@durov.local', 'demo.gromov@durov.local')
ORDER BY c.id;

SELECT id, filename, content_type, path_on_disk, purpose, created_at
FROM file_assets
WHERE filename IN ('АР_ИП Рура от 29.01.pdf', 'КР_1 блок6.pdf');

-- ШАГ 2: удалить производство/модули/задачи и отвязать документы
-- (раскомментировать и выполнить только после проверки ШАГА 1)

-- DELETE FROM tasks WHERE module_id IN (
--     SELECT m.id FROM modules m
--     JOIN productions p ON p.id = m.production_id
--     JOIN clients c ON c.cycle_id = p.cycle_id
--     WHERE c.email IN ('demo.nikitin@durov.local', 'demo.panova@durov.local', 'demo.gromov@durov.local')
-- );

-- DELETE FROM productions WHERE cycle_id IN (
--     SELECT cycle_id FROM clients
--     WHERE email IN ('demo.nikitin@durov.local', 'demo.panova@durov.local', 'demo.gromov@durov.local')
-- );

-- UPDATE clients SET ar_file_id = NULL, kr_file_id = NULL
-- WHERE email IN ('demo.nikitin@durov.local', 'demo.panova@durov.local', 'demo.gromov@durov.local');

-- DELETE FROM file_assets
-- WHERE filename IN ('АР_ИП Рура от 29.01.pdf', 'КР_1 блок6.pdf');

-- ШАГ 3: пути к файлам на диске контейнера backend — стереть их вручную
-- (SQL DELETE выше убирает только строку в БД, не сами байты):
--   docker compose exec backend rm -f <путь из path_on_disk>

-- ШАГ 4 (ОПЦИОНАЛЬНО — отдельное решение, не связано напрямую с сегодняшним
-- инцидентом, это старый сидер задачи 0025): удалить сами демо-клиентов
-- целиком (каскадно уберёт и их заметки — client_notes.client_id ON DELETE
-- CASCADE). Выполнять только после ШАГА 2 (иначе упадёт на FK productions).
-- Client ссылается НА cycle (не наоборот), поэтому сначала clients, потом
-- их теперь уже осиротевшие cycles.

-- DELETE FROM clients
-- WHERE email IN ('demo.nikitin@durov.local', 'demo.panova@durov.local', 'demo.gromov@durov.local');

-- DELETE FROM cycles
-- WHERE id NOT IN (SELECT cycle_id FROM clients)
--   AND id NOT IN (SELECT cycle_id FROM productions)
--   AND id NOT IN (SELECT cycle_id FROM installations);
