from fastapi import Depends, FastAPI, HTTPException, UploadFile
from sqlalchemy.orm import Session

from app.common.files import FileAssetOut, FilePurpose, save_upload_file
from app.common.module_access import Module as AccessModule
from app.core.deps import require_admin, require_view
from app.db.session import get_db
from app.house_models import service as house_models_service
from app.house_models.schemas import (
    HouseModelCatalogOut,
    HouseModelDetailOut,
    HouseModelProductionOut,
    HouseModelTypicalDocumentsPatch,
)
from app.users.models import User

app = FastAPI(
    title="Soborbum — Типовые проекты домов",
    description=(
        "Read-only витрина каталожных моделей (Барн/Флэт) и индивидуальных "
        "проектов из базы знаний Durov.House. Единственное исключение (0073-b) — "
        "типовые АР/КР карточки, доступные для правки только администратору."
    ),
    version="0.2.0",
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


@app.get("/catalog/{key}/productions", response_model=list[HouseModelProductionOut])
def catalog_productions(
    key: str, db: Session = Depends(get_db), _: User = Depends(require_house_models_view)
):
    """Реальные дома этой модели в производстве (задача 0073-b) — переход к их
    уже существующим задачам идёт через обычный `/production/{id}`, здесь не
    заводится отдельный редактор задач. Как и на вкладке «Главная» одного
    производства (`app.production.home`), цена/контакты клиента не отдаются."""
    card = house_models_service.get_by_key(db, key)
    if card is None:
        raise HTTPException(status_code=404, detail="Проект не найден")
    return house_models_service.get_model_productions(db, key)


# --------------------------------------------- типовые АР/КР (0073-b) --
# Единственное исключение из read-only витрины: образец АР/КР для модели
# целиком (не клиентский). Загрузка файла и его привязка к карточке —
# два раздельных шага (как `POST /accounting/money-movement-documents` +
# `document_ids` при создании проводки), чтобы PATCH ниже оставался
# единственной точкой записи в саму HouseModelCard.


@app.post("/catalog/typical-ar-file", response_model=FileAssetOut, status_code=201)
def upload_typical_ar_file(
    file: UploadFile, db: Session = Depends(get_db), user: User = Depends(require_admin)
):
    asset = save_upload_file(db, file, FilePurpose.TYPICAL_ARCHITECTURAL_DECISIONS, user)
    db.commit()
    db.refresh(asset)
    return asset


@app.post("/catalog/typical-kr-file", response_model=FileAssetOut, status_code=201)
def upload_typical_kr_file(
    file: UploadFile, db: Session = Depends(get_db), user: User = Depends(require_admin)
):
    asset = save_upload_file(db, file, FilePurpose.TYPICAL_CONSTRUCTIVE_DECISIONS, user)
    db.commit()
    db.refresh(asset)
    return asset


@app.patch("/catalog/{key}/typical-documents", response_model=HouseModelDetailOut)
def update_typical_documents(
    key: str,
    payload: HouseModelTypicalDocumentsPatch,
    db: Session = Depends(get_db),
    _: User = Depends(require_admin),
):
    """Единственный write-путь на HouseModelCard — всё остальное на карточке
    по-прежнему пишется только импортом (см. докстринг модели)."""
    card = house_models_service.get_by_key(db, key)
    if card is None:
        raise HTTPException(status_code=404, detail="Проект не найден")
    card = house_models_service.update_typical_documents(
        db, card, payload.model_dump(exclude_unset=True)
    )
    db.commit()
    db.refresh(card)
    return HouseModelDetailOut.model_validate(card)
