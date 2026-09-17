import enum
from datetime import datetime

from sqlalchemy import Boolean, DateTime, Enum, ForeignKey, String, UniqueConstraint, func
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.common.module_access import AccessLevel, Module
from app.db.base import Base


class UserRole(str, enum.Enum):
    ADMIN = "admin"
    WORKER = "worker"


class User(Base):
    __tablename__ = "users"

    id: Mapped[int] = mapped_column(primary_key=True)
    email: Mapped[str] = mapped_column(String(255), unique=True, index=True, nullable=False)
    hashed_password: Mapped[str] = mapped_column(String(255), nullable=False)
    full_name: Mapped[str] = mapped_column(String(255), nullable=False)
    role: Mapped[UserRole] = mapped_column(Enum(UserRole, name="user_role"), nullable=False, default=UserRole.WORKER)
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    module_access: Mapped[list["UserModuleAccess"]] = relationship(
        back_populates="user", cascade="all, delete-orphan"
    )

    @property
    def accessible_modules(self) -> set[Module]:
        if self.role == UserRole.ADMIN:
            return set(Module)
        return {grant.module for grant in self.module_access}

    def has_access(self, module: Module) -> bool:
        if self.role == UserRole.ADMIN:
            return True
        return any(grant.module == module for grant in self.module_access)


class UserModuleAccess(Base):
    __tablename__ = "user_module_access"
    __table_args__ = (UniqueConstraint("user_id", "module", name="uq_user_module"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), nullable=False)
    module: Mapped[Module] = mapped_column(Enum(Module, name="module"), nullable=False)
    level: Mapped[AccessLevel] = mapped_column(Enum(AccessLevel, name="access_level"), nullable=False)

    user: Mapped["User"] = relationship(back_populates="module_access")
