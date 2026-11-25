import asyncio
from time import monotonic

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy.orm import Session

from app.clients import intake_auth
from app.clients.models import Client, ClientLeadRequest
from app.common.module_access import AccessLevel, Module
from app.core.config import settings
from app.core.security import create_access_token
from app.crm import live_inquiries
from app.db.session import get_db
from app.main import app
from app.notifications.service import set_mute

PATH = "/api/crm/inquiries/live"


@pytest.fixture
def live_api(db, monkeypatch):
    bind = db.get_bind()

    def session():
        return Session(bind)

    def get_test_db():
        with session() as instance:
            yield instance

    monkeypatch.setattr(live_inquiries, "SessionLocal", session)
    monkeypatch.setattr(settings, "lead_intake_token", "synthetic-inquiry-token")
    monkeypatch.setattr(settings, "lead_intake_rate_limit", 20)
    intake_auth.reset_rate_limit()
    mounted = [r.app for r in app.routes if hasattr(getattr(r, "app", None), "dependency_overrides")]
    for subapp in mounted:
        subapp.dependency_overrides[get_db] = get_test_db
    yield lambda: AsyncClient(transport=ASGITransport(app=app), base_url="http://testserver")
    for subapp in mounted:
        subapp.dependency_overrides.pop(get_db, None)
    intake_auth.reset_rate_limit()


def auth(user):
    return {"Authorization": "Bearer " + create_access_token(str(user.id))}


async def submit(http, tranid):
    response = await http.post(
        "/api/clients/intake",
        headers={"X-Intake-Token": "synthetic-inquiry-token"},
        json={"name": "Проверка на стенде", "phone": "+7 900 000-00-10", "tranid": tranid,
              "utm_source": "yandex", "utm_campaign": "707176283"},
    )
    assert response.status_code == 200
    return response.json()


def test_live_update_wakes_once_per_committed_receipt(live_api, make_user, db, monkeypatch):
    manager = make_user(Module.CLIENTS)
    manager.job_role = "manager"
    db.commit()
    reader = make_user(Module.CLIENTS, level=AccessLevel.VIEW)
    employee = make_user(Module.TASKS)
    manager_session, reader_session, employee_session = map(auth, (manager, reader, employee))

    async def verify():
        async with live_api() as http:
            assert (await http.get(PATH)).status_code == 401
            assert (await http.get(PATH, headers=employee_session)).status_code == 403
            baseline = (await http.get(PATH, headers=manager_session)).json()
            assert baseline["entries"] == []

            # The periodic recheck is slower than the assertion. The committed
            # webhook itself must wake the already waiting request.
            monkeypatch.setattr(live_inquiries, "_RECHECK_SECONDS", 10)
            request = asyncio.create_task(http.get(PATH, params={"cursor": baseline["cursor"]}, headers=manager_session))
            async with asyncio.timeout(3):
                while not live_inquiries._waiters:
                    await asyncio.sleep(0.01)
            started = monotonic()
            first = await submit(http, "new-receipt")
            incoming = (await asyncio.wait_for(request, timeout=3)).json()
            assert monotonic() - started < 3
            [entry] = incoming["entries"]
            assert entry["notify"] is True
            assert first["status"] == "created"

            for headers in (manager_session, reader_session):
                snapshot = (await http.get(PATH, headers=headers)).json()
                assert snapshot["cursor"] == incoming["cursor"]
                assert snapshot["entries"] == [{"id": entry["id"], "notify": headers == manager_session}]
            assert (await submit(http, "new-receipt"))["status"] == "duplicate"
            monkeypatch.setattr(live_inquiries, "_WAIT_SECONDS", 0)
            response = await http.get(PATH, params={"cursor": incoming["cursor"]}, headers=manager_session)
            assert response.status_code == 204
            assert response.headers["cache-control"] == "private, no-store"

            assert (await submit(http, "genuine-repeat"))["status"] == "duplicate"
            repeat = (await http.get(PATH, params={"cursor": incoming["cursor"]}, headers=manager_session)).json()
            assert len(repeat["entries"]) == 2
            assert repeat["entries"][0]["notify"] is True
            assert repeat["entries"][0]["id"] != entry["id"]
            assert db.query(ClientLeadRequest).count() == 2

    asyncio.run(verify())


def test_sound_follows_assignment_and_existing_mute(live_api, make_user, db):
    manager = make_user(Module.CLIENTS)
    other = make_user(Module.CLIENTS)
    manager_session, other_session = auth(manager), auth(other)

    async def verify():
        async with live_api() as http:
            receipt = await submit(http, "initial")
            client = db.get(Client, receipt["client_id"])
            client.manager_id = manager.id
            db.commit()

            await submit(http, "assigned")
            assert (await http.get(PATH, headers=manager_session)).json()["entries"][0]["notify"] is True
            assert (await http.get(PATH, headers=other_session)).json()["entries"][0]["notify"] is False
            set_mute(db, manager.id, Module.CLIENTS, client.id, muted=True)
            await submit(http, "muted")
            assert (await http.get(PATH, headers=manager_session)).json()["entries"][0]["notify"] is False

    asyncio.run(verify())
