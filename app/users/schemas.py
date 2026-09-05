from datetime import datetime

from pydantic import BaseModel, ConfigDict, EmailStr

from app.common.module_access import Module
from app.users.models import UserRole


class Token(BaseModel):
    access_token: str
    token_type: str = "bearer"


class UserCreate(BaseModel):
    email: EmailStr
    password: str
    full_name: str
    role: UserRole = UserRole.WORKER
    module_access: list[Module] = []


class UserUpdate(BaseModel):
    full_name: str | None = None
    password: str | None = None
    is_active: bool | None = None
    role: UserRole | None = None


class UserAccessUpdate(BaseModel):
    module_access: list[Module]


class UserOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    email: str
    full_name: str
    role: UserRole
    is_active: bool
    created_at: datetime
    module_access: list[Module] = []

    @staticmethod
    def from_model(user) -> "UserOut":
        return UserOut(
            id=user.id,
            email=user.email,
            full_name=user.full_name,
            role=user.role,
            is_active=user.is_active,
            created_at=user.created_at,
            module_access=sorted(user.accessible_modules, key=lambda m: m.value),
        )
