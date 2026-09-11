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
    TASKS = "tasks"
    AI = "ai"
    BOARD = "board"
    ACCOUNTING = "accounting"
