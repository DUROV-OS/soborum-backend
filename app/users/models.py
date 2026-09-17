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

    def access_level(self, module: Module) -> AccessLevel:
        if self.role == UserRole.ADMIN:
            return AccessLevel.FULL
        for grant in self.module_access:
            if grant.module == module:
                return grant.level
        return AccessLevel.NONE

    def has_access(self, module: Module) -> bool:
        return self.access_level(module) != AccessLevel.NONE

    def access_levels(self) -> dict[Module, AccessLevel]:
        """Уровень по каждому разделу `Module` (матрица доступа, 0052-c) —
        `FULL` на всё для `ADMIN`, иначе уровень гранта или `NONE`."""
        if self.role == UserRole.ADMIN:
            return {module: AccessLevel.FULL for module in Module}
        granted = {grant.module: grant.level for grant in self.module_access}
        return {module: granted.get(module, AccessLevel.NONE) for module in Module}


class UserModuleAccess(Base):
    __tablename__ = "user_module_access"
    __table_args__ = (UniqueConstraint("user_id", "module", name="uq_user_module"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), nullable=False)
    module: Mapped[Module] = mapped_column(Enum(Module, name="module"), nullable=False)
    level: Mapped[AccessLevel] = mapped_column(Enum(AccessLevel, name="access_level"), nullable=False)

    user: Mapped["User"] = relationship(back_populates="module_access")
