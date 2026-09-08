from fastapi import Depends, FastAPI, HTTPException, status
from sqlalchemy.orm import Session

from app.common.module_access import Module as AccessModule
from app.core.deps import require_module
from app.cycle.models import Cycle
from app.cycle.schemas import CycleOut
from app.db.session import get_db
from app.users.models import User

app = FastAPI(
    title="Soborbum — Цикл клиента",
    description="Сквозной, обобщающий взгляд на цикл: клиент + производство + монтаж + текущий статус.",
    version="0.4.0",
)

require_cycle = require_module(AccessModule.CYCLE)


@app.get("/", response_model=list[CycleOut])
def list_cycles(db: Session = Depends(get_db), _: User = Depends(require_cycle)):
    return db.query(Cycle).order_by(Cycle.id.desc()).all()


@app.get("/{cycle_id}", response_model=CycleOut)
def get_cycle(cycle_id: int, db: Session = Depends(get_db), _: User = Depends(require_cycle)):
    cycle = db.get(Cycle, cycle_id)
    if not cycle:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Цикл не найден")
    return cycle
