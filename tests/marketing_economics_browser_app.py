"""Disposable real-API fixture. Synthetic records only; no external services."""
import os
import sys
from pathlib import Path
from tempfile import TemporaryDirectory
if os.environ.get('COSTING_BROWSER_TEST') != '1':
    raise RuntimeError('Requires COSTING_BROWSER_TEST=1')
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
temporary = TemporaryDirectory(prefix='durov-marketing-ui-')
os.environ.update(APP_ENV='dev', DATABASE_URL='sqlite://', JWT_SECRET='synthetic-browser-only',
    ENABLE_DEMO_SEED='false', MAX_LIVE_UPDATES='false', STORAGE_DIR=temporary.name)
from datetime import datetime, timezone
from io import BytesIO
from uuid import uuid4
from fastapi import FastAPI, Depends
from pypdf import PdfWriter
from sqlalchemy import create_engine
from sqlalchemy.orm import Session
from app.db import import_all_models
from app.db.base import Base
from app.db.session import get_db
from app.core.deps import get_current_user
from app.users.models import User, UserRole
from app.common.module_access import Module
from app.common.files import FilePurpose, save_bytes_file
from app.marketing.router import app as marketing_app
from app.accounting.router import app as accounting_app
from app.clients.models import ClientLeadRequest
from app.clients.service import create_client
from app.clients.schemas import ClientCreate
from app.crm.models import CRMEntity
from app.costing.models import CostingEstimate
from app.costing.governance_models import CostingApprovalPolicy
from app.accounting.seed import ensure_organizations_seed
from app.accounting.models import Organization
from app.accounting.actual_models import ActualCostDocument
from app.accounting.actual_schemas import CostInput, CostDecision
from app.accounting import actual

engine = create_engine('sqlite:///'+str(Path(temporary.name)/'synthetic.db'), connect_args={'check_same_thread': False})
Base.metadata.create_all(engine)
with Session(engine) as db:
    ensure_organizations_seed(db)
    owner = User(id=1, email='owner@example.test', full_name='Тестовый руководитель', role=UserRole.ADMIN, hashed_password='not-a-login', is_active=True)
    db.add(owner); db.flush()
    db.add(CostingApprovalPolicy(id=1, accounting_user_id=1, director_user_id=1))
    client = create_client(db, ClientCreate(full_name='Тестовый клиент', phone='89000000000', email='test@example.test')); db.flush()
    receipt = ClientLeadRequest(client_id=client.id, source='Сайт', utm={'utm_source': 'yandex', 'utm_campaign': 'test-campaign'},
        tracking={'yclid': '123456789'}, created_at=datetime(2026, 11, 1, tzinfo=timezone.utc))
    db.add(receipt); db.flush()
    deal = CRMEntity(kind='deal', name='Тестовая сделка', payload={'client_id': client.id, 'client_request_id': receipt.id})
    db.add(deal); db.flush()
    project = CostingEstimate(title='Дом с подтверждённым расходом рекламы', client_id=client.id, crm_deal_id=deal.id, actor_id=1)
    db.add(project); db.flush()
    stream = BytesIO(); writer = PdfWriter(); writer.add_blank_page(width=100, height=100); writer.write(stream)
    asset = save_bytes_file(db, 'Рекламный акт.pdf', 'application/pdf', stream.getvalue(), FilePurpose.COSTING_EVIDENCE, owner)
    db.add(ActualCostDocument(file_id=asset.id, actor_id=1)); db.flush()
    body = CostInput.model_validate({'request_id': str(uuid4()), 'expected_version': 0, 'kind': 'document',
        'organization_id': db.query(Organization).first().id, 'issuer': 'Тестовый рекламный сервис', 'number': 'Р-1',
        'document_date': '2026-11-01', 'line_ref': 'реклама', 'incurred_on': '2026-11-01',
        'evidence': {'file_id': asset.id, 'locator': 'Страница 1, реклама дома'}, 'gross_amount': '12200', 'excluded_tax': '2200',
        'tax_basis': 'Налог исключён по строке акта', 'allocations': [{'category': 'advertising', 'project_id': project.id,
            'month': None, 'amount': '10000', 'locator': 'Строка 1, принадлежность дому'}], 'reason': 'Синтетический расход для проверки интерфейса'})
    version = actual.save(db, body, owner)
    actual.decide(db, version.record_id, CostDecision.model_validate({'request_id': str(uuid4()), 'version': 1,
        'source_hash': actual.canonical(version.content), 'action': 'recognize', 'confirmed': True, 'reason': 'Сверено в изолированном тесте'}), owner)
    db.add(CostingEstimate(title='Дом без связи с рекламой', actor_id=1))
    db.commit()

def database():
    with Session(engine) as db: yield db
def user(db=Depends(get_db)): return db.get(User, 1)
app = FastAPI()
app.dependency_overrides[get_db] = database
app.dependency_overrides[get_current_user] = user
for sub in (marketing_app, accounting_app): sub.dependency_overrides.update(app.dependency_overrides)
app.mount('/api/marketing', marketing_app)
app.mount('/api/accounting', accounting_app)
@app.get('/api/auth/me')
def me(): return {'id': 1, 'email': 'owner@example.test', 'full_name': 'Тестовый руководитель', 'role': 'admin', 'job_role': None, 'is_active': True, 'module_access': {m.value: 'full' for m in Module}}
@app.get('/api/auth/users')
def users(): return [me()]
@app.get('/api/notifications/')
@app.get('/api/notifications')
def notifications(): return []
@app.get('/api/notifications/unread-count')
def unread(): return {'count': 0}
if __name__ == '__main__':
    import uvicorn
    uvicorn.run(app, host='0.0.0.0', port=8133)
