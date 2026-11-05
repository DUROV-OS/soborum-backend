"""Local browser acceptance server. Synthetic, disposable DB; no company services."""
import os
import sys
from pathlib import Path
from tempfile import TemporaryDirectory

if os.environ.get("COSTING_BROWSER_TEST") != "1":
    raise RuntimeError("This isolated test server requires COSTING_BROWSER_TEST=1")
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
os.environ.update(APP_ENV="dev", DATABASE_URL="sqlite://", JWT_SECRET="costing-browser-isolated-test-only", ENABLE_DEMO_SEED="false")

from fastapi import Depends, FastAPI, Request
from sqlalchemy import create_engine
from sqlalchemy.orm import Session
from app.db import import_all_models  # noqa: F401
from app.db.base import Base
from app.db.session import get_db
from app.core.deps import get_current_user
from app.common.module_access import Module, AccessLevel
from app.costing.router import app as costing_app
from app.sales.router import app as sales_app
from app.moysklad.recipes import router as recipes_router
from app.users.models import User, UserRole, UserModuleAccess

temporary = TemporaryDirectory(prefix="durov-costing-browser-")
engine = create_engine("sqlite:///" + str(Path(temporary.name) / "isolated.db"), connect_args={"check_same_thread": False})
Base.metadata.create_all(engine)
with Session(engine) as db:
    db.add(User(id=1, email="owner@example.test", full_name="Тестовый руководитель", role=UserRole.ADMIN, hashed_password="not-a-login", is_active=True))
    worker = User(id=2, email="sales@example.test", full_name="Тестовый менеджер", role=UserRole.WORKER, job_role="manager", hashed_password="not-a-login", is_active=True)
    worker.module_access = [UserModuleAccess(module=Module.CLIENTS, level=AccessLevel.EDIT)]
    db.add(worker)
    db.commit()


def database():
    with Session(engine) as db:
        yield db


def user(request: Request, db: Session = Depends(get_db)):
    return db.get(User, 2 if request.headers.get("x-costing-test-role") == "sales" else 1)


app = FastAPI()
app.dependency_overrides[get_db] = database
app.dependency_overrides[get_current_user] = user
costing_app.dependency_overrides.update(app.dependency_overrides)
app.mount("/api/costing", costing_app)
sales_app.dependency_overrides.update(app.dependency_overrides)
app.mount('/api/sales', sales_app)
recipes_app = FastAPI()
recipes_app.dependency_overrides.update(app.dependency_overrides)
recipes_app.include_router(recipes_router)
app.mount('/api/moysklad', recipes_app)


@app.post("/api/costing-test/reset")
def reset():
    tables = [t for t in Base.metadata.sorted_tables if t.name not in ('users','user_module_access')]
    Base.metadata.drop_all(engine, tables=tables)
    Base.metadata.create_all(engine, tables=tables)
    return {"isolated_database_reset": True}


@app.post('/api/costing-test/native')
def seed_native():
    from app.accounting.models import Counterparty, Organization
    from app.accounting.seed import ensure_organizations_seed
    from app.crm.models import CRMEntity
    from app.moysklad.recipes import Recipe
    from app.warehouse.models import WarehouseMaterial, Warehouse, MaterialCategory
    with Session(engine) as db:
        ensure_organizations_seed(db)
        raw = WarehouseMaterial(title='Доска связанного заказа', code='LINK-RAW', warehouse=Warehouse.TECHNOLOGY,
            category=MaterialCategory.NONE, unit='м3', is_fractional=True, quantity_in_stock=100, purchase_price=50, threshold=0)
        product = WarehouseMaterial(title='Дом связанного заказа', code='LINK-HOUSE', warehouse=Warehouse.TECHNOLOGY,
            category=MaterialCategory.NONE, unit='шт', is_fractional=False, quantity_in_stock=0, purchase_price=0, threshold=0)
        buyer = Counterparty(name='Связанный покупатель', is_active=True)
        deal = CRMEntity(kind='deal', name='Связанная сделка', payload={})
        db.add_all([raw, product, buyer, deal]); db.flush()
        recipe = Recipe(title='Техкарта связанного дома', version=1, archived=False, content={'inputs':[
            {'material_id':raw.id,'title':raw.title,'unit':raw.unit,'quantity':'2'}], 'outputs':[
            {'material_id':product.id,'title':product.title,'unit':product.unit,'quantity':'1'}], 'stages':[],'warnings':[]})
        db.add(recipe); db.commit()
        return {'recipe_id':recipe.id,'deal_id':deal.id,'buyer_id':buyer.id,'organization_id':db.query(Organization).first().id}


@app.get("/api/auth/me")
def me(person=Depends(user)):
    return {"id": person.id, "email": person.email, "full_name": person.full_name, "role": person.role.value,
            "job_role": person.job_role, "is_active": True, "module_access": {m.value: person.access_level(m).value for m in Module}}


@app.get("/api/auth/users")
def users(person=Depends(user)):
    return []


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="127.0.0.1", port=8127, log_level="warning")
