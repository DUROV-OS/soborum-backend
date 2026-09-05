from sqlalchemy.orm import Session

from app.common.module_access import Module
from app.core.config import settings
from app.core.security import hash_password
from app.users.models import User, UserModuleAccess, UserRole


def bootstrap_admin(db: Session) -> None:
    has_admin = db.query(User).filter(User.role == UserRole.ADMIN).first()
    if has_admin:
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
    module_access: list[Module],
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


def set_module_access(db: Session, user: User, module_access: list[Module]) -> None:
    db.query(UserModuleAccess).filter(UserModuleAccess.user_id == user.id).delete()
    for module in set(module_access):
        db.add(UserModuleAccess(user_id=user.id, module=module))
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
