"""Import every section's models module so SQLAlchemy's mapper registry and
Base.metadata know about all tables/relationships before create_all/Alembic
autogenerate run. Import this module (not its contents) for the side effect.
"""

from app.accounting import models as _accounting_models  # noqa: F401
from app.agents import models as _agents_models  # noqa: F401
from app.ai import models as _ai_models  # noqa: F401
from app.board import models as _board_models  # noqa: F401
from app.clients import models as _clients_models  # noqa: F401
from app.common import files as _files_models  # noqa: F401
from app.cycle import models as _cycle_models  # noqa: F401
from app.installation import models as _installation_models  # noqa: F401
from app.marketing import models as _marketing_models  # noqa: F401
from app.production import models as _production_models  # noqa: F401
from app.tasks import models as _tasks_models  # noqa: F401
from app.users import models as _users_models  # noqa: F401
from app.warehouse import models as _warehouse_models  # noqa: F401
