"""Row locks for all writers of a production material's remaining norm."""
from fastapi import HTTPException
from sqlalchemy.orm import Session, joinedload
from app.production.models import BlockMaterial, ProductionBlock


def lock_block_material(db: Session, material_id: int) -> BlockMaterial:
    row = (db.query(BlockMaterial).filter(BlockMaterial.id == material_id)
           .populate_existing().with_for_update(of=BlockMaterial).one_or_none())
    if row is None:
        raise HTTPException(status_code=404, detail="Материал блока не найден")
    return row


def lock_house_materials(db: Session, production_id: int) -> list[BlockMaterial]:
    # Stable order prevents two issuances for the same house from deadlocking.
    rows = (db.query(BlockMaterial)
            .join(ProductionBlock, ProductionBlock.id == BlockMaterial.block_id)
            .filter(ProductionBlock.production_id == production_id)
            .options(joinedload(BlockMaterial.block), joinedload(BlockMaterial.warehouse_material))
            .order_by(BlockMaterial.id).populate_existing()
            .with_for_update(of=BlockMaterial).all())
    return sorted(rows, key=lambda row: (row.block.sequence, row.block.id, row.id))
