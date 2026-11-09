from uuid import UUID
from fastapi import APIRouter, Depends, HTTPException, Response
from pydantic import BaseModel, SecretStr

from app.core.deps import require_admin
from app.max import authentication, session_store
from app.max.client import session
from app.users.models import User

router = APIRouter(prefix='/connection', tags=['MAX connection'])


class Start(BaseModel):
    request_id: UUID


class Password(BaseModel):
    password: SecretStr


def no_cache(response: Response):
    response.headers['Cache-Control'] = 'no-store'
    response.headers['Pragma'] = 'no-cache'


@router.get('')
def status(response: Response, user: User = Depends(require_admin)):
    no_cache(response)
    return session_store.public_status()


@router.post('/check')
def check(response: Response, user: User = Depends(require_admin)):
    no_cache(response)
    try:
        with session():
            pass
    except HTTPException:
        pass
    return session_store.public_status()


@router.post('/qr')
def start(body: Start, response: Response, user: User = Depends(require_admin)):
    no_cache(response)
    return authentication.start(body.request_id, user.id)


@router.get('/qr/{attempt_id}')
def get(attempt_id: UUID, response: Response, user: User = Depends(require_admin)):
    no_cache(response)
    return authentication.get(str(attempt_id), user.id)


@router.post('/qr/{attempt_id}/password')
def password(attempt_id: UUID, body: Password, response: Response, user: User = Depends(require_admin)):
    no_cache(response)
    return authentication.password(str(attempt_id), user.id, body.password.get_secret_value())


@router.post('/qr/{attempt_id}/confirm')
def confirm(attempt_id: UUID, response: Response, user: User = Depends(require_admin)):
    no_cache(response)
    return authentication.confirm(str(attempt_id), user.id)


@router.delete('/qr/{attempt_id}')
def cancel(attempt_id: UUID, response: Response, user: User = Depends(require_admin)):
    no_cache(response)
    return authentication.cancel(str(attempt_id), user.id)
