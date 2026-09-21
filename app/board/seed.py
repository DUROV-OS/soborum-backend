"""Generates the initial "Совет директоров" tree. Idempotent by design (skips
if a root node already exists), so it's safe to call unconditionally from
app.main's startup hook the same way app.users.service.bootstrap_admin is:
it actually does something exactly once, right after the first deploy, and
is a no-op on every restart after that. Nothing here is AI-authored - it's a
fixed starting point for the tree; the council and "актуализировать" take it
from here.
"""

from sqlalchemy.orm import Session

from app.board.models import BoardChangeSource, BoardChangeType, BoardNode, BoardNodeColor
from app.board.service import log_change
from app.users.models import User

ROOT_TITLE = "durov.house"
ROOT_DESCRIPTION = (
    "Компания строит и продаёт модульные дома, развивает собственные туристические базы и "
    "сопутствующие направления бизнеса. Здесь собраны стратегические направления развития и текущее "
    "положение дел по каждому из них."
)

# (title, description, color, [(sub_title, sub_description, sub_color), ...])
#
# Восстановленная по запросу Арсения "старая" структура (задача 0028): 6
# направлений, 27 поднаправлений. Она никогда не лежала в git - предыдущая
# версия жила только как данные в БД и была переписана поверх, когда сюда
# временно завезли структуру "4 направления / 12 поднаправлений". Содержание
# и цвета ниже восстановлены по описанию Арсения и базе знаний компании
# (раздел 02_Business), не выдуманы.
DIRECTIONS: list[tuple[str, str, BoardNodeColor, list[tuple[str, str, BoardNodeColor]]]] = [
    (
        "Маркетинг",
        "Сайт и SEO ещё не сделаны, часть запланированного контента пока не реализована.",
        BoardNodeColor.YELLOW,
        [
            (
                "контент о продукте",
                "Контент продаёт результат, а не характеристики: 3 флагманских продукта — дом для жизни "
                "60–70 м², семейный дом 96 м² (главный) и дом для аренды/глэмпинга.",
                BoardNodeColor.GREEN,
            ),
            (
                "контент о производстве",
                "Подневный план «Почему наши дома служат десятилетиями» показывает процесс и контроль на "
                "каждом этапе сборки, а не только готовые дома; 14-дневная серия в Instagram ещё не "
                "реализована буквально.",
                BoardNodeColor.YELLOW,
            ),
            (
                "поисковый трафик",
                "SEO отдельно не задействован, хотя дешёвые федеральные производители заходят именно через "
                "поисковый трафик и низкую цену — риск потери входящего спроса.",
                BoardNodeColor.RED,
            ),
            (
                "сайт",
                "Новый сайт Durov.House: путь клиента в стиле Apple (сначала продукт и философия, потом "
                "комплектация), квиз-конфигуратор и цена не ниже 64 000 ₽/м² как пол расчёта; стек и "
                "структура ещё не утверждены.",
                BoardNodeColor.YELLOW,
            ),
            (
                "соц. сети",
                "Instagram/Telegram/MAX компании: контент-план и мониторинг конкурентов; фактическое "
                "состояние аккаунта расходится по срокам с планом.",
                BoardNodeColor.YELLOW,
            ),
        ],
    ),
    (
        "Производство",
        "Изготовление модульных домов: ассортимент, поставщики, контроль качества и мощности цеха.",
        BoardNodeColor.GREEN,
        [
            (
                "ассортимент",
                "Серии Барн и Флэт, 14 моделей; ставка на 3 флагманских продукта вместо большого каталога.",
                BoardNodeColor.GREEN,
            ),
            (
                "поставщики",
                "Первое предложение поставщика не окончательное — несколько раундов переговоров это норма; "
                "хорошего поставщика не меняют, а улучшают условия, поиск новых — редкий и по гипотезе, "
                "мониторинг цен — постоянный.",
                BoardNodeColor.YELLOW,
            ),
            (
                "контроль качества",
                "Контроль на каждом этапе: входной контроль пиломатериала, геометрия, чек-листы с "
                "фотофиксацией и финальная приёмка из 50–100 пунктов.",
                BoardNodeColor.GREEN,
            ),
            (
                "оптимизация цеха",
                "Площадка в Новой Усмани (~800 м² чистой производственной зоны) — узкое место: факт цикла "
                "~42 дня против целевых 14; кондукторы и потоковая модель бригад должны сократить цикл.",
                BoardNodeColor.YELLOW,
            ),
            (
                "отказоустойчивость",
                "Реалистичный WIP — 4–5 домов одновременно, цикл держат 2 квалифицированных человека; "
                "расхождение заявленной мощности (2,5–4 дома/мес факт против 7+ домов/мес в презентации "
                "для Курской области) не устранено.",
                BoardNodeColor.RED,
            ),
        ],
    ),
    (
        "Персонал",
        "Незакрытая воронка продаж и критичный найм.",
        BoardNodeColor.RED,
        [
            (
                "система найма",
                "Продажи фактически ведёт один менеджер, совмещая все роли сразу (~94 сделки/мес, 0 "
                "закрытых на срезе 08.2026); нужна структура минимум из 3 человек (BDR, Sales Manager, "
                "Operations), срочный приоритет — найм Sales Manager.",
                BoardNodeColor.RED,
            ),
            (
                "должности и вакансии",
                "Карта из 8 ролей (продажник, СММ, проектировщик, бухгалтер, директор, начальник "
                "производства, подсобный работник ×2, сборщик высшей категории ×12) — состав требует "
                "подтверждения владельцем.",
                BoardNodeColor.YELLOW,
            ),
            (
                "система мотивации",
                "Оценка сотрудника по роли и фактам, а не по личности, с анализом первопричины перед "
                "оценкой; AI не снижает зарплату и не увольняет самостоятельно.",
                BoardNodeColor.GREEN,
            ),
            (
                "использование системы",
                "Подключены как источник данных amoCRM и МойСклад; не подключены телефония, "
                "соцсети/аналитика для СММ, CAD, банк-клиент, 1С/СБИС, мобильный задачник для линейного "
                "персонала и единый дашборд.",
                BoardNodeColor.YELLOW,
            ),
        ],
    ),
    (
        "Рынки",
        "Переход от частных заказов к серийным сегментам и приоритизация географии сбыта.",
        BoardNodeColor.YELLOW,
        [
            (
                "частники",
                "Массовый входящий спрос на дом для жизни 60–70 м² и семейный дом 96 м² — доказанный "
                "продукт (3 из 4 сделок в CRM на срезе).",
                BoardNodeColor.GREEN,
            ),
            (
                "турбазы",
                "Дом для аренды/глэмпинга стратегически важнее остальных продуктов: серийная загрузка, "
                "инвесторы и операторы, подтверждён крупными сделками (Мираторг, проект 12 домиков).",
                BoardNodeColor.GREEN,
            ),
            (
                "гостиничные сети",
                "Переход от разовых частных заказов к серийным клиентам — девелоперы, гостиничные сети, "
                "посёлки, инвесторы; формат Durov Village в московском направлении.",
                BoardNodeColor.YELLOW,
            ),
            (
                "рынки сбыта",
                "Москва — витрина бренда и источник дорогих клиентов (+15–25% к цене); выход в новые "
                "регионы РФ и за рубеж пока не приоритизирован.",
                BoardNodeColor.YELLOW,
            ),
        ],
    ),
    (
        "Экономика",
        "Часть методик расчёта себестоимости и издержек ещё не утверждена.",
        BoardNodeColor.YELLOW,
        [
            (
                "закупки",
                "Прибыль считается по группе: объём закупки — рычаг на собственную себестоимость (~310 тыс "
                "₽/мес экономии на 5 домах); переговоры в несколько раундов, календарь отношений с "
                "поставщиками.",
                BoardNodeColor.GREEN,
            ),
            (
                "зарплаты",
                "Труд бригады монтажа — 6250 ₽/м² (≈600 000 ₽ на дом); подсобные работники — по 80 000 ₽; "
                "новые роли продаж требуют бюджета найма.",
                BoardNodeColor.YELLOW,
            ),
            (
                "ценовая политика",
                "Правило пола: цена не ниже 64 000 ₽/м² площади застройки для комплектаций с отделкой; "
                "утверждённые цены (Стандарт 6,2 млн ₽, Премиум 7,1 млн ₽) прибыльны при любой методике "
                "расчёта себестоимости.",
                BoardNodeColor.GREEN,
            ),
            (
                "маржа",
                "4 версии себестоимости DH-96 не сведены в одну (3,26–4,74 млн ₽ в зависимости от "
                "методики); 3 источника цены расходятся (сайт/тариф/факт сделок) — не влияет на прайс, но "
                "мешает планированию и скидкам.",
                BoardNodeColor.RED,
            ),
            (
                "амортизация",
                "Методика учёта фиксированных расходов не утверждена (расхождение 2,16–5,0 млн ₽/мес, цена "
                "вопроса ~34 млн ₽/год).",
                BoardNodeColor.RED,
            ),
        ],
    ),
    (
        "Soborbum",
        "Рабочая гипотеза названия направления; по содержанию — «Durov OS как продукт».",
        BoardNodeColor.YELLOW,
        [
            (
                "окупаемость",
                "Монетизация — гипотезы, а не бизнес-план: базовая подписка по числу сотрудников/агентов, "
                "премиум за измеримый результат, маркетплейс агентов; процент от сэкономленного времени "
                "сознательно отклонён.",
                BoardNodeColor.YELLOW,
            ),
            (
                "использумаемость",
                "Принцип «сначала на себе, потом как продукт»: Durov.House — лаборатория; успех измеряется "
                "часами сокращённой рутины и предотвращёнными ошибками каждый день, а не обещаниями.",
                BoardNodeColor.GREEN,
            ),
            (
                "выгода",
                "Отличие от обычной автоматизации — полный проход, а не просто фиксация встречи: выяснить "
                "потребность, пересчитать комплектацию, проверить нормы, посчитать прибыль, подготовить "
                "договор, создать задачи, обновить прогноз.",
                BoardNodeColor.GREEN,
            ),
            (
                "перспективы развития",
                "5 эпох: Foundation → First Intelligence → Digital Factory → Product Intelligence "
                "(конфигуратор) → Autonomous Company → Durov Core Platform (ядро как отдельный продукт); "
                "переход по готовности, не по дате.",
                BoardNodeColor.YELLOW,
            ),
        ],
    ),
]


def _populate_directions(db: Session, root: BoardNode) -> None:
    """Creates the level-1/level-2 subtree from `DIRECTIONS` under `root`.
    Assumes `root` has no children yet (ensure_seed only calls this for a
    freshly-created root; restore_previous_structure deletes the old
    children first)."""
    for i, (title, description, color, subdirections) in enumerate(DIRECTIONS):
        direction = BoardNode(
            parent_id=root.id, level=1, sort_order=i, title=title, description=description, color=color,
        )
        db.add(direction)
        db.flush()

        for j, (sub_title, sub_description, sub_color) in enumerate(subdirections):
            db.add(
                BoardNode(
                    parent_id=direction.id, level=2, sort_order=j,
                    title=sub_title, description=sub_description, color=sub_color,
                )
            )
    db.flush()


def ensure_seed(db: Session) -> BoardNode | None:
    """Creates the initial tree if the board is empty. Returns the root node
    if it just created one, None if the board already had a root (i.e. this
    already ran before, or the tree was populated some other way)."""
    if db.query(BoardNode).filter(BoardNode.parent_id.is_(None)).first() is not None:
        return None

    root = BoardNode(parent_id=None, level=0, sort_order=0, title=ROOT_TITLE, description=ROOT_DESCRIPTION, color=BoardNodeColor.GREEN)
    db.add(root)
    db.flush()

    _populate_directions(db, root)

    db.commit()
    db.refresh(root)
    return root


def restore_previous_structure(db: Session, actor: User | None = None) -> BoardNode:
    """One-off operation for an ALREADY-populated board (task 0028): unlike
    ensure_seed(), which no-ops once a root exists, this always replaces
    whatever level-1/level-2 tree is currently there - including any
    council/actualize/manual edits layered on top of it - with the current
    `DIRECTIONS` above. The root node itself (`durov.house`) is left alone;
    only its descendants are torn down and rebuilt.

    Deliberately does not try to preserve the old board_proposals/
    board_node_changes rows tied to the deleted nodes (they stay in the audit
    table, just orphaned - board_node_changes.node_id is intentionally not
    FK-constrained for this reason, see BoardNodeChange's docstring). Instead
    it records ONE new BoardNodeChange entry (source=manual) noting that the
    whole tree was replaced, so the fact of the reset itself is auditable
    even though the individual old nodes' history isn't carried forward.

    Safe to call more than once (e.g. re-run against the same DB): it will
    just replace the tree again and log another audit entry.
    """
    root = db.query(BoardNode).filter(BoardNode.parent_id.is_(None)).order_by(BoardNode.id).first()
    if root is None:
        root = BoardNode(
            parent_id=None, level=0, sort_order=0, title=ROOT_TITLE, description=ROOT_DESCRIPTION,
            color=BoardNodeColor.GREEN,
        )
        db.add(root)
        db.flush()

    old_direction_titles = [child.title for child in root.children]
    for child in list(root.children):
        db.delete(child)
    db.flush()

    _populate_directions(db, root)

    if old_direction_titles:
        note = (
            "Восстановление старой структуры по запросу Арсения: дерево (уровни 1-2) заменено целиком "
            "на 6 направлений/27 поднаправлений. Заменённая версия содержала направления: "
            + ", ".join(old_direction_titles) + "."
        )
    else:
        note = (
            "Восстановление старой структуры по запросу Арсения: дерево было пустым, создано 6 "
            "направлений/27 поднаправлений."
        )
    log_change(db, root, BoardChangeType.UPDATED, BoardChangeSource.MANUAL, None, actor, note=note)

    db.commit()
    db.refresh(root)
    return root
