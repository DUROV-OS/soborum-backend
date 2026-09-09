"""Read-only DurovOS-database snapshot for the agent shift and Marina consult.

Agents read the company's own database — clients, cycle, production, warehouse,
tasks, marketing — through :mod:`app.dashboard.service` snapshots plus a couple
of finance/legal/engineer aggregates built here. No CRM, no МойСклад, no external
sync. Everything here only reads; a missing row is an honest "нет данных", never
a guess.
"""

from __future__ import annotations

import logging
import time

from sqlalchemy.orm import Session

from app.agents.ids import AgentId
from app.agents.types import ContextHit
from app.clients.models import Client, ClientStage
from app.dashboard import service as dashboard
from app.db.session import SessionLocal
from app.production.models import ProductionModule

log = logging.getLogger("app.agents.connectors")

_CACHE_TTL = 60.0
_cache: dict[str, tuple[float, object]] = {}

# Which snapshot sections each role reads from the DurovOS database.
_ROLE_SECTIONS: dict[AgentId, tuple[str, ...]] = {
    AgentId.COORDINATOR: ("cycle", "clients", "tasks"),
    AgentId.SALES: ("clients",),
    AgentId.MARKETER: ("marketing",),
    AgentId.PRODUCTION: ("production", "tasks"),
    AgentId.WAREHOUSE: ("warehouse",),
    AgentId.FINANCE: ("finance", "clients"),
    AgentId.LAWYER: ("legal",),
    AgentId.ENGINEER: ("engineer", "production"),
}


def clear_cache() -> None:
    _cache.clear()


# ------------------------------------------------------------- snapshot --

def _build_snapshot(db: Session) -> dict:
    snap: dict[str, dict] = {}
    for key, (_module, builder) in dashboard.SECTION_BUILDERS.items():
        try:
            snap[key] = builder(db)
        except Exception as error:  # pragma: no cover - defensive
            log.warning("снимок раздела %s не собрался: %s", key, error)
    for key, builder in (
        ("finance", _finance_facts),
        ("legal", _legal_facts),
        ("engineer", _engineer_facts),
    ):
        try:
            snap[key] = builder(db)
        except Exception as error:  # pragma: no cover - defensive
            log.warning("снимок раздела %s не собрался: %s", key, error)
    return snap


def _snapshot(db: Session | None = None) -> dict:
    if db is not None:
        return _build_snapshot(db)
    cached = _cached("snapshot")
    if isinstance(cached, dict):
        return cached
    session = SessionLocal()
    try:
        snap = _build_snapshot(session)
    except Exception as error:  # pragma: no cover - defensive
        log.warning("снимок базы DurovOS не собрался: %s", error)
        snap = {}
    finally:
        session.close()
    _store("snapshot", snap)
    return snap


def _finance_facts(db: Session) -> dict:
    rows = db.query(Client).all()
    priced = [c for c in rows if c.final_price or c.estimated_price]
    pipeline = sum(float(c.final_price or c.estimated_price or 0) for c in rows)
    discounts = [
        (float(c.estimated_price) - float(c.final_price)) / float(c.estimated_price) * 100
        for c in rows
        if c.estimated_price and c.final_price and float(c.final_price) < float(c.estimated_price)
    ]
    unpaid_pipeline = sum(
        float(c.final_price or c.estimated_price or 0)
        for c in rows
        if c.stage == ClientStage.PAYMENT and c.is_paid is not True
    )
    return {
        "clients_with_price": len(priced),
        "pipeline_value": pipeline,
        "unpaid_pipeline": unpaid_pipeline,
        "discounts_over_5pct": sum(1 for pct in discounts if pct > 5),
        "max_discount_pct": round(max(discounts), 1) if discounts else 0.0,
    }


def _legal_facts(db: Session) -> dict:
    rows = db.query(Client).all()
    return {
        "approval_without_contract": sum(
            1 for c in rows if c.stage == ClientStage.APPROVAL and not c.contract_file_id
        ),
        "past_approval_without_locked_docs": sum(
            1
            for c in rows
            if c.stage in (ClientStage.PAYMENT, ClientStage.POSTPAYMENT)
            and c.documents_locked_at is None
        ),
    }


def _engineer_facts(db: Session) -> dict:
    mods = db.query(ProductionModule).all()
    return {
        "modules_total": len(mods),
        "modules_without_description": sum(1 for m in mods if not (m.description or "").strip()),
    }


# ------------------------------------------------------------- hits --

def live_hits(query: str, agents: list[AgentId], db: Session | None = None) -> list[ContextHit]:
    snap = _snapshot(db)
    if not snap:
        return []
    hits: list[ContextHit] = []
    seen: set[str] = set()
    for agent_id in agents:
        for section in _ROLE_SECTIONS.get(agent_id, ()):
            if section in seen or section not in snap:
                continue
            seen.add(section)
            hit = _section_hit(section, snap[section])
            if hit is not None:
                hits.append(hit)
    return hits


def _section_hit(section: str, data: dict) -> ContextHit | None:
    render = _SECTION_RENDER.get(section)
    if render is None:
        return None
    title, line = render(data)
    return ContextHit(
        source="db",
        title=title,
        excerpt=line + f" Снимок базы {_now()}.",
        kind="record",
        path=f"durovos/{section}",
    )


def _clients_line(d: dict) -> tuple[str, str]:
    s = d.get("stage_counts", {})
    return (
        "База DurovOS · Клиенты",
        (
            f"Клиентов в базе {d.get('total_clients', 0)} "
            f"(лид {s.get('lead', 0)}, обсуждение {s.get('discussion', 0)}, "
            f"согласование {s.get('approval', 0)}, оплата {s.get('payment', 0)}, "
            f"постоплата {s.get('postpayment', 0)}). "
            f"Ждут подтверждения оплаты {d.get('awaiting_payment_confirmation', 0)}, "
            f"ждут остаток {d.get('awaiting_balance_payment', 0)}. "
            f"Лидов зависло дольше 14 дней {d.get('leads_stuck_over_14_days', 0)}."
        ),
    )


def _cycle_line(d: dict) -> tuple[str, str]:
    s = d.get("status_counts", {})
    return (
        "База DurovOS · Цикл клиента",
        (
            f"Циклов всего {d.get('total_cycles', 0)}: у клиента {s.get('client', 0)}, "
            f"в производстве {s.get('production', 0)}, на монтаже {s.get('installation', 0)}, "
            f"завершено {s.get('completed', 0)}."
        ),
    )


def _tasks_line(d: dict) -> tuple[str, str]:
    s = d.get("status_counts", {})
    return (
        "База DurovOS · Задачи",
        (
            f"Открытых задач {d.get('open_tasks', 0)}, просрочено {d.get('overdue_tasks', 0)}, "
            f"на сегодня {d.get('due_today', 0)}. В работе {s.get('in_progress', 0)}, "
            f"на проверке {s.get('in_review', 0)}, не готово {s.get('not_ready', 0)}."
        ),
    )


def _production_line(d: dict) -> tuple[str, str]:
    return (
        "База DurovOS · Производство",
        (
            f"Проектов {d.get('total_productions', 0)}, модулей {d.get('total_modules', 0)}. "
            f"Модулей с нехваткой материала {d.get('modules_with_material_shortfall', 0)}, "
            f"заявок на материалы в ожидании {d.get('pending_material_requests', 0)}."
        ),
    )


def _warehouse_line(d: dict) -> tuple[str, str]:
    top = d.get("top_shortage_materials", []) or []
    tail = ""
    if top:
        names = ", ".join(str(m.get("title") or "?") for m in top[:3])
        tail = f" Сильнее всего просели: {names}."
    return (
        "База DurovOS · Склад",
        (
            f"Позиций на складе {d.get('total_materials', 0)}, ниже порога "
            f"{d.get('materials_needing_supply', 0)}. Поставок за 7 дней "
            f"{d.get('supplies_recorded_last_7_days', 0)}.{tail}"
        ),
    )


def _marketing_line(d: dict) -> tuple[str, str]:
    s = d.get("stage_counts", {})
    return (
        "База DurovOS · Маркетинг",
        (
            f"Единиц контента {d.get('total_content_items', 0)} "
            f"(идея {s.get('idea', 0)}, сбор {s.get('gathering', 0)}, "
            f"редактура {s.get('editing', 0)}, выпуск {s.get('release', 0)}, "
            f"анализ {s.get('analysis', 0)}). Выпуск в ближайшие 7 дней "
            f"{d.get('release_due_next_7_days', 0)}, просрочен {d.get('release_overdue', 0)}."
        ),
    )


def _finance_line(d: dict) -> tuple[str, str]:
    return (
        "База DurovOS · Финансы",
        (
            f"Клиентов с ценой {d.get('clients_with_price', 0)}, портфель "
            f"{_money(d.get('pipeline_value', 0))}, не оплачено на стадии оплаты "
            f"{_money(d.get('unpaid_pipeline', 0))}. Скидок сверх 5% в базе "
            f"{d.get('discounts_over_5pct', 0)}, максимальная {d.get('max_discount_pct', 0)}%."
        ),
    )


def _legal_line(d: dict) -> tuple[str, str]:
    return (
        "База DurovOS · Право",
        (
            f"Клиентов на согласовании без договора в базе "
            f"{d.get('approval_without_contract', 0)}. Прошли согласование, но документы "
            f"не зафиксированы у {d.get('past_approval_without_locked_docs', 0)}."
        ),
    )


def _engineer_line(d: dict) -> tuple[str, str]:
    return (
        "База DurovOS · Инженерия",
        (
            f"Модулей в производстве {d.get('modules_total', 0)}, из них без описания "
            f"конструктива {d.get('modules_without_description', 0)}."
        ),
    )


_SECTION_RENDER = {
    "clients": _clients_line,
    "cycle": _cycle_line,
    "tasks": _tasks_line,
    "production": _production_line,
    "warehouse": _warehouse_line,
    "marketing": _marketing_line,
    "finance": _finance_line,
    "legal": _legal_line,
    "engineer": _engineer_line,
}


# ------------------------------------------------------------- stance --

def live_stance_for(agent_id: AgentId, db: Session | None = None) -> str | None:
    hits = live_hits("", [agent_id], db)
    lines = [hit.excerpt for hit in hits if hit.excerpt][:2]
    return " ".join(lines) or None


# ------------------------------------------------------------- charts --

def live_charts(db: Session | None = None, *, wait: bool = False) -> list[dict]:
    snap = _snapshot(db)
    if not snap:
        return []
    charts = [
        *_coordinator_charts(snap),
        *_finance_charts(snap),
        *_warehouse_charts(snap),
        *_production_charts(snap),
    ]
    return [chart for chart in charts if chart.get("bars")]


def _coordinator_charts(snap: dict) -> list[dict]:
    cycle = snap.get("cycle", {}).get("status_counts", {})
    tasks = snap.get("tasks", {})
    bars = [
        {"label": "В производстве", "value": float(cycle.get("production", 0))},
        {"label": "На монтаже", "value": float(cycle.get("installation", 0))},
        {"label": "Просроченные задачи", "value": float(tasks.get("overdue_tasks", 0))},
    ]
    bars = [bar for bar in bars if bar["value"] > 0]
    if not bars:
        return []
    return [
        _chart(
            "coordinator_pulse",
            "Где горит",
            "шт",
            bars,
            ["coordinator"],
            f"В производстве {cycle.get('production', 0)}, просрочено задач {tasks.get('overdue_tasks', 0)}.",
            "warning",
        )
    ]


def _finance_charts(snap: dict) -> list[dict]:
    fin = snap.get("finance", {})
    bars = [
        {"label": "Не оплачено на стадии оплаты", "value": float(fin.get("unpaid_pipeline", 0))},
    ]
    bars = [bar for bar in bars if bar["value"] > 0]
    if not bars:
        return []
    return [
        _chart(
            "finance_money",
            "Где висят деньги",
            "₽",
            bars,
            ["finance"],
            f"Не оплачено: {_money(fin.get('unpaid_pipeline', 0))}.",
            "timber",
        )
    ]


def _warehouse_charts(snap: dict) -> list[dict]:
    wh = snap.get("warehouse", {})
    bars = [{"label": "Позиций ниже порога", "value": float(wh.get("materials_needing_supply", 0))}]
    bars = [bar for bar in bars if bar["value"] > 0]
    if not bars:
        return []
    return [
        _chart(
            "warehouse_gap",
            "Чего не хватает",
            "шт",
            bars,
            ["warehouse"],
            f"Ниже порога {wh.get('materials_needing_supply', 0)} позиций.",
            "warning",
        )
    ]


def _production_charts(snap: dict) -> list[dict]:
    prod = snap.get("production", {})
    tasks = snap.get("tasks", {}).get("status_counts", {})
    bars = [
        {"label": "Модули с нехваткой", "value": float(prod.get("modules_with_material_shortfall", 0))},
        {"label": "Задачи в работе", "value": float(tasks.get("in_progress", 0))},
    ]
    bars = [bar for bar in bars if bar["value"] > 0]
    if not bars:
        return []
    return [
        _chart(
            "production_tasks",
            "Узкое место цеха",
            "шт",
            bars,
            ["production"],
            f"Модулей с нехваткой материала {prod.get('modules_with_material_shortfall', 0)}.",
            "brand",
        )
    ]


# ------------------------------------------------------------- briefing --

def live_briefing(db: Session | None = None) -> dict:
    return _snapshot(db)


def live_briefing_text(db: Session | None = None) -> str:
    snap = _snapshot(db)
    if not snap:
        return (
            "Живой срез базы DurovOS сейчас недоступен. "
            "Не утверждай числа по клиентам, складу, производству и задачам — данных нет."
        )
    lines = ["Живой срез базы DurovOS (собственная база системы, не CRM и не МойСклад):"]
    for section in ("clients", "cycle", "production", "warehouse", "tasks", "marketing", "finance"):
        data = snap.get(section)
        render = _SECTION_RENDER.get(section)
        if not data or render is None:
            continue
        _title, line = render(data)
        lines.append(f"- {line}")
    lines.append(
        "Пиши по этим числам. Чего нет в срезе — не выдумывай и не ссылайся на CRM или МойСклад."
    )
    return "\n".join(lines)


# ------------------------------------------------------------- helpers --

def _money(value: object) -> str:
    try:
        amount = float(value or 0)
    except (TypeError, ValueError):
        return "сумма не указана"
    return f"{amount:,.0f} ₽".replace(",", " ")


def _now() -> str:
    from datetime import datetime, timezone

    return datetime.now(timezone.utc).astimezone().strftime("%d.%m.%Y %H:%M")


def _cached(key: str):
    row = _cache.get(key)
    if row is None:
        return None
    when, value = row
    if time.monotonic() - when > _CACHE_TTL:
        return None
    return value


def _store(key: str, value: object) -> None:
    _cache[key] = (time.monotonic(), value)


def _chart(
    chart_id: str,
    title: str,
    unit: str,
    bars: list[dict],
    agents: list[str],
    lead: str,
    tone: str,
) -> dict:
    return {
        "id": chart_id,
        "title": title,
        "unit": unit,
        "bars": bars[:6],
        "agents": agents,
        "lead": lead,
        "tone": tone,
    }
