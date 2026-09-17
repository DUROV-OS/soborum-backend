import enum


class Module(str, enum.Enum):
    """The sections of the system, used for per-worker access grants
    (app/users) and for tagging auto-created cross-section tasks (app/tasks)."""

    CLIENTS = "clients"
    PRODUCTION = "production"
    INSTALLATION = "installation"
    CYCLE = "cycle"
    WAREHOUSE = "warehouse"
    MARKETING = "marketing"
    HOUSE_MODELS = "house_models"
    TASKS = "tasks"
    AI = "ai"
    BOARD = "board"
    ACCOUNTING = "accounting"
    # Псевдо-раздел: не открывает своё API-приложение, только флаг доступа к
    # под-вкладке «Все задачи» в разделе tasks (см. задачу 0021).
    TASKS_ALL = "tasks_all"


class AccessLevel(str, enum.Enum):
    """Уровень доступа к разделу на гранте `UserModuleAccess` (задача 0052).

    Порядок объявления значим — сравнение (`>=` и т.д.) используется в
    `core/deps.require_view/require_edit/require_full`, чтобы проверить,
    достаточно ли выставленного уровня для операции."""

    NONE = "none"
    VIEW = "view"
    EDIT = "edit"
    FULL = "full"

    @property
    def _rank(self) -> int:
        return _ACCESS_LEVEL_RANK[self]

    def __lt__(self, other: "AccessLevel") -> bool:
        return self._rank < other._rank

    def __le__(self, other: "AccessLevel") -> bool:
        return self._rank <= other._rank

    def __gt__(self, other: "AccessLevel") -> bool:
        return self._rank > other._rank

    def __ge__(self, other: "AccessLevel") -> bool:
        return self._rank >= other._rank


_ACCESS_LEVEL_RANK = {
    AccessLevel.NONE: 0,
    AccessLevel.VIEW: 1,
    AccessLevel.EDIT: 2,
    AccessLevel.FULL: 3,
}
