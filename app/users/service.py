import logging

from sqlalchemy.orm import Session

from app.common.module_access import AccessLevel, Module
from app.core.config import settings
from app.core.security import hash_password, verify_password
from app.users.models import User, UserModuleAccess, UserRole

log = logging.getLogger("app.users.service")

_INSECURE_ADMIN_PASSWORDS = {"", "admin123", "admin", "password"}

DEFAULT_RESET_PASSWORD = "password1234"


def bootstrap_admin(db: Session) -> None:
    has_admin = db.query(User).filter(User.role == UserRole.ADMIN).first()
    if has_admin:
        return
    if settings.admin_password.strip() in _INSECURE_ADMIN_PASSWORDS:
        message = (
            "ADMIN_PASSWORD не задан или небезопасен — администратор не создан. "
            "Задайте ADMIN_PASSWORD в .env и перезапустите."
        )
        if settings.is_prod:
            raise RuntimeError(message)
        log.warning("%s (APP_ENV=%s)", message, settings.app_env)
        return
    admin = User(
        email=settings.admin_email,
        hashed_password=hash_password(settings.admin_password),
        full_name=settings.admin_full_name,
        role=UserRole.ADMIN,
        is_active=True,
    )
    db.add(admin)
    db.commit()


def create_user(
    db: Session,
    email: str,
    password: str,
    full_name: str,
    role: UserRole,
    module_access: dict[Module, AccessLevel],
) -> User:
    user = User(
        email=email,
        hashed_password=hash_password(password),
        full_name=full_name,
        role=role,
        is_active=True,
    )
    db.add(user)
    db.flush()
    set_module_access(db, user, module_access)
    db.commit()
    db.refresh(user)
    return user


def reset_password_to_default(db: Session, user: User) -> None:
    user.hashed_password = hash_password(DEFAULT_RESET_PASSWORD)
    db.commit()


def change_password(db: Session, user: User, current_password: str, new_password: str) -> None:
    if not verify_password(current_password, user.hashed_password):
        raise ValueError("Текущий пароль указан неверно")
    user.hashed_password = hash_password(new_password)
    db.commit()


def set_module_access(db: Session, user: User, module_access: dict[Module, AccessLevel]) -> None:
    """Матрица доступа (`AccessMatrixPage.tsx`, 0052-c) — уровень на раздел
    из выпадающего списка. Строка гранта хранится только при уровне отличном
    от `NONE` (см. инвариант в `UserModuleAccess` / 0052-a)."""
    db.query(UserModuleAccess).filter(UserModuleAccess.user_id == user.id).delete()
    for module, level in module_access.items():
        if level == AccessLevel.NONE:
            continue
        db.add(UserModuleAccess(user_id=user.id, module=module, level=level))
    db.flush()


def users_with_access(db: Session, module: Module) -> list[User]:
    admins = db.query(User).filter(User.role == UserRole.ADMIN, User.is_active.is_(True)).all()
    workers = (
        db.query(User)
        .join(UserModuleAccess, UserModuleAccess.user_id == User.id)
        .filter(UserModuleAccess.module == module, User.is_active.is_(True))
        .all()
    )
    seen = {u.id: u for u in admins}
    for u in workers:
        seen[u.id] = u
    return list(seen.values())
