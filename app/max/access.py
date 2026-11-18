from fastapi import Depends, HTTPException
from app.core.deps import get_current_user
from app.users.models import User


def require_global_write(user: User = Depends(get_current_user)):
    if user.job_role == 'manager':
        raise HTTPException(403, 'Менеджер может писать в MAX только из привязанной карточки клиента или заявки')
    return user
