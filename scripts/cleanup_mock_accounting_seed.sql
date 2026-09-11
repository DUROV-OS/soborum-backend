-- Разовая очистка мок-проводок «Бухгалтерии», созданных ensure_accounting_seed
-- (0011-j) до фикса is_prod-гварда (fix/dashboard/real-signals-not-mocked, PR #33).
-- Идентифицирует ровно 12 строк из app/accounting/seed.py::_SPECS по точному
-- совпадению (subkind, amount, payment_purpose) — эти суммы+формулировки не
-- совпадают с реальными данными.
--
-- ВАЖНО: postgres-enum money_subkind хранит значения ЗАГЛАВНЫМИ ИМЕНАМИ
-- Python-enum (SALE_INCOME), а не строчными .value ("sale_income") — так
-- сделала миграция a1f4c93e27b0 (sa.Enum без values_callable). Отсюда и
-- "invalid input value for enum money_subkind" при первой попытке.
--
-- Использование:
--   1. Сначала прогнать ШАГ 1 (SELECT) и глазами свериться, что вышло ровно
--      12 строк и все они выглядят как мок ("аванс по договору поставки дома" и т.п.).
--   2. Только после этого — ШАГ 2 (DELETE), в той же сессии psql.
--
-- Пример подключения на проде:
--   ssh durov@89.207.254.32
--   cd /srv/soborbum-backend
--   docker compose exec db psql -U soborbum -d soborbum -f scripts/cleanup_mock_accounting_seed.sql
-- (или скопировать блоки ниже и выполнить вручную по одному через -c "...")

-- ШАГ 1: посмотреть, что будет удалено (ничего не меняет)
SELECT id, subkind, status, amount, payment_purpose, created_at
FROM money_movements
WHERE (subkind, amount, payment_purpose) IN (
    ('SALE_INCOME', 1850000, 'аванс по договору поставки дома'),
    ('SALE_INCOME', 1200000, 'второй платёж по договору'),
    ('SALE_INCOME', 640000, 'остаток после получения дома'),
    ('SALARY_PAYOUT', 420000, 'зарплата за август'),
    ('SALARY_PAYOUT', 445000, 'зарплата за сентябрь'),
    ('SALARY_PAYOUT', 60000, 'премия по итогам монтажа'),
    ('SUPPLY_PAYMENT', 512400, 'оплата поставки бруса и доски'),
    ('SUPPLY_PAYMENT', 190000, 'предоплата за метизы и утеплитель'),
    ('TAX', 274000, 'НДС за 2 квартал'),
    ('RENT', 130000, 'аренда цеха за сентябрь'),
    ('OTHER_INCOME', 45000, 'возврат от поставщика за брак'),
    ('OTHER_EXPENSE', 27000, 'оплата вывоза мусора')
)
ORDER BY id;

-- ШАГ 2: удалить (раскомментировать и выполнить только после проверки ШАГА 1)
-- DELETE FROM money_movements
-- WHERE (subkind, amount, payment_purpose) IN (
--     ('SALE_INCOME', 1850000, 'аванс по договору поставки дома'),
--     ('SALE_INCOME', 1200000, 'второй платёж по договору'),
--     ('SALE_INCOME', 640000, 'остаток после получения дома'),
--     ('SALARY_PAYOUT', 420000, 'зарплата за август'),
--     ('SALARY_PAYOUT', 445000, 'зарплата за сентябрь'),
--     ('SALARY_PAYOUT', 60000, 'премия по итогам монтажа'),
--     ('SUPPLY_PAYMENT', 512400, 'оплата поставки бруса и доски'),
--     ('SUPPLY_PAYMENT', 190000, 'предоплата за метизы и утеплитель'),
--     ('TAX', 274000, 'НДС за 2 квартал'),
--     ('RENT', 130000, 'аренда цеха за сентябрь'),
--     ('OTHER_INCOME', 45000, 'возврат от поставщика за брак'),
--     ('OTHER_EXPENSE', 27000, 'оплата вывоза мусора')
-- );
