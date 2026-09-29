"""Сверка ID задач: число «Открытых задач» на Пульсе против карточек на доске
(задача 0084-h). Только чтение — ничего не пишет в базу.

Для одного пользователя печатает:
  * ID из выборки Пульса до 0084-h (все задачи компании со статусом != done,
    без учёта прав) и после (`open_tasks_query` в области доски пользователя);
  * ID, которые отдаёт `GET /api/tasks` с `scope=mine` и `scope=all`
    (вызывается сам обработчик роутера, а не копия его фильтров);
  * разницу между ними и причину по каждому ID.

Персональных данных не выводит: только ID задач, статус, link_type, раздел
и роль пользователя в задаче — без названий, описаний, имён и почт.

Запуск (из папки backend, на копии данных):
    .venv/bin/python scripts/diagnose_task_counts.py --user-id 3
или внутри контейнера бэкенда:
    docker compose exec backend python scripts/diagnose_task_counts.py --user-id 3
"""

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.common.module_access import Module  # noqa: E402
from app.db import import_all_models  # noqa: E402,F401  (регистрирует все модели для relationship)
from app.db.session import SessionLocal  # noqa: E402
from app.tasks import service as task_service  # noqa: E402
from app.tasks.models import Task, TaskStatus  # noqa: E402
from app.tasks.router import list_tasks  # noqa: E402
from app.tasks.schemas import TaskScope  # noqa: E402
from app.users.models import User  # noqa: E402


def _board_ids(db, user: User, scope: TaskScope) -> list[int]:
    # Все аргументы явно: вне FastAPI значения по умолчанию `Query(...)` не
    # подставляются.
    tasks = list_tasks(
        db=db, current=user, scope=scope, assignee_id=None, reviewer_id=None,
        block_id=None, link_type=None, task_status=None, overdue=None,
    )
    return [t.id for t in tasks]


def _role(task: Task, user: User) -> str:
    roles = []
    if user in task.assignees:
        roles.append("исполнитель")
    if user in task.reviewers:
        roles.append("проверяющий")
    return ",".join(roles) or "-"


def _reason_not_on_mine(task: Task, user: User) -> str:
    """Почему задача Пульса не попала в «Мои задачи» (см. is_mine_or_claimable)."""
    if task.status == TaskStatus.DONE:
        return "done (на Пульсе не считается)"
    section = task_service.task_section(task)
    if task.assignees:
        return "не его задача (есть другие исполнители, он не исполнитель/проверяющий)"
    if task.status != TaskStatus.READY:
        return f"без исполнителя, но статус {task.status.value} — свободной не считается"
    if section is not None and not user.has_access(section):
        return f"свободная, но нет прав раздела {section.value}"
    return "? (не объяснено правилами — проверить вручную)"


def _describe(task: Task, user: User) -> str:
    section = task_service.task_section(task)
    return (
        f"#{task.id} status={task.status.value} link_type={task.link_type.value} "
        f"section={section.value if section else '-'} "
        f"assignees={len(task.assignees)} reviewers={len(task.reviewers)} role={_role(task, user)}"
    )


def _ids_line(title: str, ids: list[int]) -> list[str]:
    return [f"{title}: {len(ids)}", "  " + (", ".join(str(i) for i in sorted(ids)) or "—")]


def diagnose(db, user: User) -> list[str]:
    """Строки отчёта сверки для одного пользователя (печатает `main`)."""
    out: list[str] = []
    has_all = user.has_access(Module.TASKS_ALL)
    out.append(f"user_id={user.id} role={user.role.value} tasks_all={'да' if has_all else 'нет'}")
    out.append("")

    pulse = {t.id: t for t in db.query(Task).filter(Task.status != TaskStatus.DONE).all()}
    mine = _board_ids(db, user, TaskScope.MINE)
    mine_open = [i for i in mine if db.get(Task, i).status != TaskStatus.DONE]

    out += _ids_line("Пульс до 0084-h (статус != done, вся компания)", list(pulse))
    scope = task_service.default_board_scope(user)
    pulse_now = [t.id for t in task_service.open_tasks_query(db, user, scope)]
    out += _ids_line(f"Пульс сейчас (open_tasks_query, scope={scope.value})", pulse_now)
    out += _ids_line("Доска scope=mine (все колонки, включая done)", mine)
    out += _ids_line("Доска scope=mine без колонки done", mine_open)
    all_ids: list[int] | None = None
    if has_all:
        all_ids = _board_ids(db, user, TaskScope.ALL)
        all_open = [i for i in all_ids if db.get(Task, i).status != TaskStatus.DONE]
        out += _ids_line("Доска scope=all (все колонки, включая done)", all_ids)
        out += _ids_line("Доска scope=all без колонки done", all_open)
    else:
        out.append("Доска scope=all: недоступна (нет tasks_all)")
    out.append("")

    out.append("На Пульсе до 0084-h, но нет на доске scope=mine:")
    mine_set = set(mine)
    missing = [i for i in sorted(pulse) if i not in mine_set]
    out += [f"  {_describe(pulse[i], user)} — {_reason_not_on_mine(pulse[i], user)}" for i in missing] or ["  —"]

    out.append("На доске scope=mine, но не на Пульсе до 0084-h:")
    extra = sorted(i for i in mine if i not in pulse)
    out += [
        f"  {_describe(db.get(Task, i), user)} — done: колонка «done» есть на доске, Пульс её не считает"
        for i in extra
    ] or ["  —"]

    if all_ids is not None:
        out.append("Разница Пульс до 0084-h ↔ доска scope=all:")
        all_set = set(all_ids)
        diff = [i for i in sorted(pulse) if i not in all_set] + sorted(i for i in all_set if i not in pulse)
        for task_id in diff:
            task = db.get(Task, task_id)
            if task_id in pulse:
                side = "только на Пульсе"
            elif task.status == TaskStatus.DONE:
                side = "только на доске: done"
            else:
                side = "только на доске"
            out.append(f"  {_describe(task, user)} — {side}")
        if not diff:
            out.append("  — (серверные наборы совпадают; расхождение могло дать только "
                       "клиентские фильтры вкладки «Все»: раздел, сотрудник, текст, срок)")
    return out


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--user-id", type=int, required=True, help="ID пользователя, от имени которого сверяем доску")
    args = parser.parse_args()

    db = SessionLocal()
    try:
        user = db.get(User, args.user_id)
        if user is None:
            print(f"Пользователь {args.user_id} не найден", file=sys.stderr)
            return 1
        print("\n".join(diagnose(db, user)))
        return 0
    finally:
        db.rollback()
        db.close()


if __name__ == "__main__":
    raise SystemExit(main())
