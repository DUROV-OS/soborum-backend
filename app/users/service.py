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


def apply_account_change(
    db: Session,
    actor: User,
    target: User,
    *,
    role: UserRole | None = None,
    is_active: bool | None = None,
) -> None:
    """Проверки вокруг роли и активности учётной записи (0074).

    Администратор управляет чужими ролями, но не своей, и не может оставить
    систему без единого активного администратора — иначе матрицу доступа и
    список админов станет некому открыть."""
    changes_role = role is not None and role != target.role
    disables = is_active is False and target.is_active

    if actor.id == target.id:
        if changes_role:
            raise ValueError("Нельзя изменить собственную роль")
        if disables:
            raise ValueError("Нельзя отключить собственную учётную запись")

    loses_admin = target.role == UserRole.ADMIN and (
        (changes_role and role != UserRole.ADMIN) or disables
    )
    if loses_admin and target.is_active and _active_admin_count(db) <= 1:
        raise ValueError("В системе должен остаться хотя бы один администратор")

    if role is not None:
        target.role = role
        if role == UserRole.ADMIN:
            # Админу гранты не нужны — `access_levels()` и так отдаёт FULL на
            # всё. Чистим их здесь, чтобы при снятии админки человек оказался
            # в матрице без доступа, а не со старыми правами (0074).
            db.query(UserModuleAccess).filter(UserModuleAccess.user_id == target.id).delete()
    if is_active is not None:
        target.is_active = is_active


def _active_admin_count(db: Session) -> int:
    return (
        db.query(User)
        .filter(User.role == UserRole.ADMIN, User.is_active.is_(True))
        .count()
    )
