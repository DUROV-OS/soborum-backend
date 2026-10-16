"""Прогон и расписание автономного агента (0112).

Прогон: читает приоритеты и наблюдения → сканирует разделы по весу → сравнивает с
прошлым снимком → применяет плейбук → публикует → обновляет журнал, приоритеты, наблюдения.
Расписание — тот же daemon-поток, что у `start_shift_loop`, без нового планировщика.
"""

from __future__ import annotations

import logging
import threading
import time
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.agents.autonomy import workspace as ws
from app.agents.autonomy.guardrails import GuardrailViolation, resolve_level
from app.agents.autonomy.models import LEVEL_A, LEVEL_B, WATCH_OPEN, AgentAutoRun, AgentActionCounter
from app.agents.autonomy.playbook import DETECTORS, Finding
from app.agents.autonomy.publish import (
    find_open_proposal,
    object_type,
    publish_level_a,
    publish_level_b,
)
from app.core.config import settings
from app.dashboard.overview import METRICS
from app.dashboard.service import SECTION_BUILDERS, SECTION_LABELS, _snapshot_users
from app.db.session import SessionLocal
from app.notifications.models import Notification
from app.tasks.models import Task, TaskLinkType, TaskStageEvent, TaskStatus

log = logging.getLogger("app.agents.autonomy")

TZ = ZoneInfo("Europe/Moscow")
WORK_DAYS = {0, 1, 2, 3, 4, 5}  # Пн–Сб
WORK_FROM, WORK_TO = 8, 20
MIN_GAP = timedelta(minutes=90)
MAX_GAP = timedelta(hours=4)
POLL_SECONDS = 900
# Повторное напоминание по одному и тому же наблюдению — не чаще раза в сутки.
REMIND_EVERY = timedelta(hours=24)

_started = False

ATTENTION_METRICS: dict[str, list[tuple[str, str]]] = {}
for _section, _title, _metric, _attention in METRICS:
    if _attention:
        ATTENTION_METRICS.setdefault(_section, []).append((_metric, _title))


# ----------------------------------------------------------- когда запускать --

def should_run(db: Session, now: datetime | None = None) -> tuple[bool, str]:
    """Рабочее окно, не чаще MIN_GAP, не реже MAX_GAP; раньше срока — только при активности."""
    now = (now or ws.utcnow()).astimezone(TZ)
    if now.weekday() not in WORK_DAYS or not (WORK_FROM <= now.hour < WORK_TO):
        return False, "вне рабочего окна"
    last = db.scalars(select(AgentAutoRun).order_by(AgentAutoRun.started_at.desc()).limit(1)).first()
    if last is None:
        return True, "первый запуск"
    since = ws.aware(last.started_at)
    age = now - since.astimezone(TZ)
    if age >= MAX_GAP:
        return True, "плановый прогон"
    if age < MIN_GAP:
        return False, "слишком рано"
    activity = db.query(TaskStageEvent).filter(
        TaskStageEvent.created_at > since, TaskStageEvent.automatic.is_(False)
    ).count()
    if activity:
        return True, f"активность: переходов статусов задач {activity}"
    return False, "нет активности"


def next_run_hint(db: Session) -> str:
    ok, why = should_run(db)
    return f"Сейчас прогон выполнится ({why})" if ok else f"Сейчас не запускается: {why}"


# ------------------------------------------------------------------ реакции --

def sync_reactions(db: Session) -> list[str]:
    """Реакция человека обнуляет счётчик повторов сценария: прочитал уведомление агента
    или решил его карточку «на согласование» (карточка ушла из IN_REVIEW)."""
    reacted = []
    for counter in db.query(AgentActionCounter).filter(AgentActionCounter.repeats > 0).all():
        since = ws.aware(counter.last_reaction_at) or ws.aware(counter.last_action_at)
        read = db.query(Notification).filter(
            Notification.object_type == object_type(counter.scenario),
            Notification.read_at.isnot(None),
        )
        decided = db.query(TaskStageEvent).join(Task, Task.id == TaskStageEvent.task_id).filter(
            Task.link_type == TaskLinkType.AGENT_PROPOSAL,
            TaskStageEvent.from_status == TaskStatus.IN_REVIEW,
        )
        if since is not None:
            read = read.filter(Notification.read_at > since)
            decided = decided.filter(TaskStageEvent.created_at > since)
        decided_here = [e for e in decided if (e.task.link_meta or {}).get("scenario") == counter.scenario]
        if read.count() or decided_here:
            ws.register_reaction(db, counter.scenario)
            reacted.append(counter.scenario)
    return reacted


# -------------------------------------------------------------------- сканер --

def scan_signals(db: Session, section: str) -> tuple[dict, str | None]:
    """Числа раздела по тем же сборщикам, что у «Пульса». Ошибка раздела — не обвал прогона."""
    try:
        if section == "users":
            snapshot = _snapshot_users(db)
        else:
            entry = SECTION_BUILDERS.get(section)
            if entry is None:
                return {}, None
            snapshot = entry[1](db)
    except Exception as error:  # noqa: BLE001 — раздел не должен ронять весь прогон
        log.exception("Раздел %s не просканирован", section)
        db.rollback()
        return {}, f"{type(error).__name__}: {error}"
    return {metric: snapshot.get(metric, 0) for metric, _ in ATTENTION_METRICS.get(section, [])}, None


def run_once(db: Session, trigger: str = "schedule") -> AgentAutoRun:
    run = AgentAutoRun(started_at=ws.utcnow(), trigger=trigger, sections=[], deviations=[], actions=[], signals={})
    db.add(run)
    db.flush()
    previous = db.scalars(
        select(AgentAutoRun).where(AgentAutoRun.id != run.id, AgentAutoRun.finished_at.isnot(None))
        .order_by(AgentAutoRun.started_at.desc()).limit(1)
    ).first()
    prev_signals = (previous.signals if previous else None) or {}
    reacted = sync_reactions(db)
    order = ws.ordered_sections(db)

    deviations: list[dict] = []
    actions: list[dict] = []
    signals: dict[str, dict] = {}
    seen_keys: set[str] = set()
    scanned_scenarios: set[str] = set()
    with_signal: set[str] = set()
    errors: list[str] = []

    for section in order:
        values, error = scan_signals(db, section)
        run.sections.append(section)
        signals[section] = values
        if error:
            errors.append(f"{section}: {error}")
            deviations.append({"section": section, "scenario": "scan_error", "key": f"scan_error:{section}",
                               "text": f"Раздел не просканирован: {error}"})
            continue
        for metric, title in ATTENTION_METRICS.get(section, []):
            value = values.get(metric, 0) or 0
            if value:
                with_signal.add(section)
                before = (prev_signals.get(section) or {}).get(metric)
                change = "" if before is None else (" (было %s)" % before if before != value else " (без изменений)")
                deviations.append({"section": section, "scenario": "metric", "key": f"metric:{section}:{metric}",
                                   "text": f"{title}: {value}{change}"})
        for detector in DETECTORS.get(section, []):
            scanned_scenarios.add(detector.__name__)
            try:
                findings = detector(db)
            except Exception as error:  # noqa: BLE001
                log.exception("Детектор %s упал", detector.__name__)
                db.rollback()
                errors.append(f"{detector.__name__}: {error}")
                continue
            for finding in findings:
                with_signal.add(section)
                seen_keys.add(finding.key)
                deviations.append({"section": section, "scenario": finding.scenario, "key": finding.key,
                                   "text": finding.text})
                action = handle_finding(db, finding)
                if action:
                    actions.append(action)

    closed = ws.resolve_cleared(db, seen_keys, scanned_scenarios)
    ws.update_priorities(db, with_signal, order)

    run.deviations, run.actions, run.signals = deviations, actions, signals
    run.error = "; ".join(errors) or None
    run.summary = build_summary(order, deviations, actions, closed, reacted)
    run.finished_at = ws.utcnow()
    db.commit()
    return run


def handle_finding(db: Session, finding: Finding) -> dict | None:
    """Наблюдение + решение, нужно ли действовать сейчас. None — действовать не нужно."""
    watch, is_new = ws.touch_watch(
        db, key=finding.key, section=finding.section, scenario=finding.scenario,
        text=finding.text, escalate_in=finding.escalate_in,
    )
    now = ws.utcnow()
    escalate = False
    if not is_new:
        if ws.due_for_escalation(watch):
            escalate = True
        elif finding.level == LEVEL_A and watch.status == WATCH_OPEN and (
            watch.last_acted_at is None or now - ws.aware(watch.last_acted_at) >= REMIND_EVERY
        ):
            pass  # повторное напоминание — пойдёт через счётчик
        else:
            return None
    level, reason = resolve_level(db, finding)
    if escalate:
        level, reason = LEVEL_B, "не сдвинулось к сроку наблюдения — эскалация"
    entry = {"scenario": finding.scenario, "key": finding.key, "level": level, "text": finding.title}
    try:
        if level == LEVEL_A:
            entry.update(publish_level_a(db, finding))
            ws.register_action(db, finding.scenario)
        else:
            if find_open_proposal(db, finding.key) is None or escalate:
                entry.update(publish_level_b(db, finding, reason=reason))
            else:
                return None
            if escalate:
                ws.mark_escalated(watch)
    except GuardrailViolation as error:
        entry.update({"result": f"заблокировано ограничителем: {error}"})
        return entry
    watch.last_acted_at = now
    if reason:
        entry["reason"] = reason
    return entry


def build_summary(order: list[str], deviations: list[dict], actions: list[dict], closed: int, reacted: list[str]) -> str:
    a = sum(1 for x in actions if x["level"] == LEVEL_A)
    b = sum(1 for x in actions if x["level"] == LEVEL_B)
    findings = [d for d in deviations if d["scenario"] not in ("metric", "scan_error")]
    parts = [
        f"Просканировано разделов: {len(order)} ({', '.join(SECTION_LABELS.get(s, s) for s in order[:3])}… по приоритету).",
        f"Отклонений по плейбуку: {len(findings)}, сигналов в метриках: {sum(1 for d in deviations if d['scenario'] == 'metric')}.",
        f"Действий сам (А): {a}, на согласование (Б): {b}.",
    ]
    if closed:
        parts.append(f"Закрыто наблюдений (условие исчезло): {closed}.")
    if reacted:
        parts.append(f"Реакция человека, счётчик сброшен: {', '.join(reacted)}.")
    return " ".join(parts)


# ---------------------------------------------------------------- расписание --

def start_autonomy_loop() -> None:
    global _started
    if _started or not settings.agent_autonomy_autorun:
        return
    _started = True
    threading.Thread(target=_loop, name="agent-autonomy-loop", daemon=True).start()
    log.info("Автономный агент: опрос раз в %s с, прогон в рабочем окне %s–%s МСК", POLL_SECONDS, WORK_FROM, WORK_TO)


def _loop() -> None:
    time.sleep(10)
    while True:
        try:
            tick()
        except Exception:
            log.exception("Тик автономного агента не выполнен")
        time.sleep(POLL_SECONDS)


def tick() -> AgentAutoRun | None:
    db = SessionLocal()
    try:
        ok, why = should_run(db)
        if not ok:
            return None
        run = run_once(db, trigger="schedule")
        log.info("Автономный агент #%s: %s", run.id, run.summary)
        return run
    finally:
        db.close()
