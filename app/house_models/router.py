from fastapi import Depends, FastAPI, HTTPException
from sqlalchemy.orm import Session

from app.common.module_access import Module as AccessModule
from app.core.deps import require_view
from app.db.session import get_db
from app.house_models import service as house_models_service
from app.house_models.schemas import HouseModelCatalogOut, HouseModelDetailOut
from app.users.models import User

app = FastAPI(
    title="Soborbum — Типовые проекты домов",
    description=(
        "Read-only витрина каталожных моделей (Барн/Флэт) и индивидуальных "
        "проектов из базы знаний Durov.House. Никаких CRUD-операций."
    ),
    version="0.1.0",
)

require_house_models_view = require_view(AccessModule.HOUSE_MODELS)


@app.get("/catalog", response_model=HouseModelCatalogOut)
def catalog(db: Session = Depends(get_db), _: User = Depends(require_house_models_view)):
    return house_models_service.get_catalog(db)


@app.get("/catalog/{key}", response_model=HouseModelDetailOut)
def catalog_detail(key: str, db: Session = Depends(get_db), _: User = Depends(require_house_models_view)):
    card = house_models_service.get_by_key(db, key)
    if card is None:
        raise HTTPException(status_code=404, detail="Проект не найден")
    return HouseModelDetailOut.model_validate(card)
