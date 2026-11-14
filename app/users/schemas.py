from datetime import datetime
from typing import Literal
import re

from pydantic import BaseModel, ConfigDict, EmailStr, Field, field_validator

from app.common.module_access import AccessLevel, Module
from app.users.models import UserRole


class Token(BaseModel):
    access_token: str
    token_type: str = "bearer"


class UserTelephonyFields(BaseModel):
    """Employee contact settings; these fields do not verify an outgoing caller ID."""

    work_phone: str | None = Field(default=None, max_length=16)
    telephony_extension: str | None = Field(default=None, max_length=10)

    @field_validator("work_phone", mode="before")
    @classmethod
    def normalize_work_phone(cls, value):
        if value is None:
            return None
        if not isinstance(value, str) or len(value) > 64:
            raise ValueError("Укажите рабочий номер в международном формате +код страны и номер")
        value = value.strip()
        if not value:
            return None
        if not re.fullmatch(r"\+?[0-9 ()\.\-\u00a0]+", value):
            raise ValueError("Рабочий номер может содержать только цифры, начальный +, пробелы, скобки и дефисы")
        compact = re.sub(r"[ ()\.\-\u00a0]", "", value)
        if re.fullmatch(r"8[0-9]{10}", compact):
            compact = "+7" + compact[1:]
        elif re.fullmatch(r"7[0-9]{10}", compact):
            compact = "+" + compact
        if not re.fullmatch(r"\+[1-9][0-9]{6,14}", compact):
            raise ValueError("Укажите код страны с +; российский номер также можно ввести через 8")
        if compact.startswith("+7") and len(compact) != 12:
            raise ValueError("Российский номер должен содержать 11 цифр, включая код 7")
        return compact

    @field_validator("telephony_extension", mode="before")
    @classmethod
    def validate_telephony_extension(cls, value):
        if value is None:
            return None
        if not isinstance(value, str):
            raise ValueError("Внутренний номер АТС должен состоять из 1–10 цифр")
        value = value.strip()
        if not value:
            return None
        if not re.fullmatch(r"[0-9]{1,10}", value):
            raise ValueError("Внутренний номер АТС должен состоять из 1–10 цифр")
        return value


class UserCreate(UserTelephonyFields):
    email: EmailStr
    password: str
    full_name: str
    role: UserRole = UserRole.WORKER
    module_access: dict[Module, AccessLevel] = {}


class UserUpdate(UserTelephonyFields):
    full_name: str | None = None
    password: str | None = None
    is_active: bool | None = None
    role: UserRole | None = None
    job_role: Literal["director", "manager", "accountant", "storekeeper", "production_manager"] | None = None


class WorkspacePreferences(BaseModel):
    view: Literal["general", "director", "manager", "accountant", "storekeeper", "production_manager"]
    blocks: list[Literal["personal_tasks", "my_clients", "attention", "metrics", "aktualnoe", "marina", "warehouse_links", "production_links"]] = Field(max_length=8)
    sections: list[Literal["clients", "production", "installation", "cycle", "warehouse", "marketing", "tasks", "users", "accounting", "ai"]] = Field(max_length=10)


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
    job_role: Literal["director", "manager", "accountant", "storekeeper", "production_manager"] | None = None
    work_phone: str | None = None
    telephony_extension: str | None = None

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
            work_phone=user.work_phone,
            telephony_extension=user.telephony_extension,
        )
