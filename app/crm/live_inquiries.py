"""A change feed for website receipts already committed to the sales inbox."""

import asyncio
from hashlib import sha256

from fastapi import Request
from starlette.concurrency import run_in_threadpool

from app.clients.models import Client
from app.common.module_access import Module
from app.crm.models import CRMEntity
from app.db.session import SessionLocal
from app.notifications.service import list_mutes

_WAIT_SECONDS = 20
_RECHECK_SECONDS = 4
_RECENT_LIMIT = 25
_waiters: set[asyncio.Event] = set()


def signal_change() -> None:
    """Wake local waiters after commit. Other workers recheck the database."""
    for waiter in _waiters:
        waiter.set()


def _snapshot(user_id: int, can_edit: bool, cursor: str | None) -> dict | None:
    with SessionLocal() as db:
        # The feed carries only CRM record IDs and the alert decision. The
        # existing CRM API still governs access to the content of each record.
        rows = (
            db.query(CRMEntity.id, CRMEntity.archived,
                     CRMEntity.payload["is_spam"].as_boolean().label("is_spam"),
                     Client.id.label("client_id"), Client.manager_id)
            .join(Client, Client.id == CRMEntity.payload["client_id"].as_integer())
            .filter(CRMEntity.kind == "inquiry", CRMEntity.source_id.startswith("website/"))
            .order_by(CRMEntity.id.desc())
            .limit(_RECENT_LIMIT)
            .all()
        )
        # Comparing a window also detects transactions that commit out of ID
        # order. It avoids replaying the existing inbox on the initial read.
        next_cursor = sha256(",".join(str(row.id) for row in rows).encode()).hexdigest()[:24]
        if cursor == next_cursor:
            return None
        muted = {mute.object_id for mute in list_mutes(db, user_id) if mute.module == Module.CLIENTS}
        return {
            "cursor": next_cursor,
            "entries": [{
                "id": row.id,
                "notify": (
                    not row.archived and not row.is_spam
                    and (can_edit if row.manager_id is None else row.manager_id == user_id)
                    and None not in muted and row.client_id not in muted
                ),
            } for row in rows],
        }


async def wait_for_change(
    request: Request, user_id: int, can_edit: bool, cursor: str | None,
) -> dict | None:
    waiter = asyncio.Event()
    _waiters.add(waiter)
    clock = asyncio.get_running_loop()
    deadline = clock.time() + _WAIT_SECONDS
    try:
        while True:
            waiter.clear()
            snapshot = await run_in_threadpool(_snapshot, user_id, can_edit, cursor)
            remaining = deadline - clock.time()
            if snapshot is not None or remaining <= 0 or await request.is_disconnected():
                return snapshot
            try:
                await asyncio.wait_for(waiter.wait(), timeout=min(remaining, _RECHECK_SECONDS))
            except TimeoutError:
                pass
    finally:
        _waiters.discard(waiter)
