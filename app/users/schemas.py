from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, EmailStr, Field

from app.common.module_access import AccessLevel, Module
from app.users.models import UserRole


class Token(BaseModel):
    access_token: str
    token_type: str = "bearer"


class UserCreate(BaseModel):
    email: EmailStr
    password: str
    full_name: str
    role: UserRole = UserRole.WORKER
    module_access: dict[Module, AccessLevel] = {}


class UserUpdate(BaseModel):
    full_name: str | None = None
    password: str | None = None
    is_active: bool | None = None
    role: UserRole | None = None
    job_role: Literal["director", "manager", "accountant"] | None = None


class WorkspacePreferences(BaseModel):
    view: Literal["general", "director", "manager", "accountant"]
    blocks: list[Literal["personal_tasks", "my_clients", "attention", "metrics", "aktualnoe", "marina"]] = Field(max_length=6)
    sections: list[Literal["clients", "production", "installation", "cycle", "warehouse", "marketing", "tasks", "users", "accounting"]] = Field(max_length=9)


class UserAccessUpdate(BaseModel):
    module_access: dict[Module, AccessLevel]


class PasswordChange(BaseModel):
    current_password: str
    new_password: str = Field(min_length=6)


class UserOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    email: str
    full_name: str
    role: UserRole
    is_active: bool
    created_at: datetime
    module_access: dict[Module, AccessLevel] = {}
    job_role: Literal["director", "manager", "accountant"] | None = None

    @staticmethod
    def from_model(user) -> "UserOut":
        return UserOut(
            id=user.id,
            email=user.email,
            full_name=user.full_name,
            role=user.role,
            is_active=user.is_active,
            created_at=user.created_at,
            module_access=user.access_levels(),
            job_role=user.job_role,
        )
