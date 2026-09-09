import logging

from pydantic import model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

log = logging.getLogger("app.core.config")

_INSECURE_JWT_SECRETS = {"", "change-me-in-production", "changeme", "secret"}


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    # "prod" (default) hard-fails on insecure secrets; "dev" downgrades to a warning
    # so a local checkout still boots without a real .env.
    app_env: str = "prod"

    database_url: str = "postgresql+psycopg2://soborbum:soborbum@db:5432/soborbum"

    jwt_secret: str = ""
    jwt_algorithm: str = "HS256"
    jwt_expire_minutes: int = 60 * 12

    storage_dir: str = "/tmp/soborum-storage"

    admin_email: str = "admin@soborbum.local"
    # No default. Empty or a well-known value => bootstrap_admin refuses to create an admin.
    admin_password: str = ""
    admin_full_name: str = "Administrator"

    # Comma-separated list of origins the frontend is served from, e.g.
    # "http://localhost:5173,http://localhost:3000". "*" is rejected — set real origins.
    cors_allowed_origins: str = ""

    @property
    def cors_origins(self) -> list[str]:
        return [o.strip() for o in self.cors_allowed_origins.split(",") if o.strip()]

    @property
    def is_prod(self) -> bool:
        return self.app_env.strip().lower() not in {"dev", "development", "local", "test"}

    @model_validator(mode="after")
    def _reject_insecure_defaults(self) -> "Settings":
        problems: list[str] = []
        secret = self.jwt_secret.strip()
        if secret in _INSECURE_JWT_SECRETS:
            problems.append("JWT_SECRET не задан или равен небезопасному значению по умолчанию")
        elif len(secret) < 16:
            problems.append("JWT_SECRET слишком короткий (нужно ≥ 16 символов)")
        if self.cors_allowed_origins.strip() == "*":
            problems.append("CORS_ALLOWED_ORIGINS='*' запрещён — укажите явные origin фронтенда")
        if not problems:
            return self
        message = "Небезопасная конфигурация: " + "; ".join(problems)
        if self.is_prod:
            raise RuntimeError(
                message + ". Задайте значения в .env или выставьте APP_ENV=dev для локальной разработки."
            )
        log.warning("%s (APP_ENV=%s — продолжаю)", message, self.app_env)
        return self

    # Local checkout of DUROV-OS/vault_backups. Empty = pack without notes.
    vault_root: str = ""

    # Company shift ticks itself. Human does not press «start».
    agent_shift_autorun: bool = True
    agent_shift_interval_seconds: int = 3600

    # --- AI assistant (app/ai) ---
    anthropic_api_key: str = ""
    anthropic_base_url: str = ""
    ai_model: str = "claude-sonnet-5"

    # Reasoning depth / overall token spend per assistant turn: low | medium |
    # high | xhigh | max. "medium" is the main latency lever - it noticeably
    # shortens the model's thinking on routine work questions without hurting
    # answer quality. Raise to "high" if answers get shallow.
    ai_effort: str = "medium"

    # Anthropic-hosted web tools for the assistant. web_search finds pages,
    # web_fetch opens a URL already in the conversation. Both run on Anthropic's
    # side (no egress from us) and are billed per use. Disable to remove them.
    # Kept low (2) on purpose: every web_fetch round is 10-30s of wall clock and
    # was the biggest driver of turns long enough for the proxy to drop.
    web_tools_enabled: bool = True
    web_search_max_uses: int = 2
    web_fetch_max_uses: int = 2

    # Read-only freshness for the shift: МойСклад customer orders only (bookkeeping).
    moysklad_token: str = ""

    # Remote MCP connector to the knowledge base. The provider advertises
    # only the authorization_code grant (no client_credentials), but issues
    # the code without a consent screen, so app/ai/mcp_auth.py runs that
    # grant headlessly and keeps the token fresh with no human involved.
    # mcp_redirect_uri is never fetched in that flow, but is still sent and
    # validated, so it must match a redirect URI registered for this OAuth
    # client with the provider.
    mcp_server_url: str = ""
    mcp_oauth_client_id: str = ""
    mcp_oauth_client_secret: str = ""
    mcp_redirect_uri: str = "https://claude.ai/api/mcp/auth_callback"

    # Same OAuth shape as the knowledge-base MCP. Shift calls read tools only.
    moysklad_mcp_url: str = ""
    moysklad_mcp_client_id: str = ""
    moysklad_mcp_client_secret: str = ""
    moysklad_mcp_scope: str = "moysklad offline_access"
    dashboard_mcp_url: str = ""
    dashboard_mcp_client_id: str = ""
    dashboard_mcp_client_secret: str = ""
    dashboard_mcp_scope: str = "dashboard"

    @property
    def mcp_configured(self) -> bool:
        return bool(self.mcp_server_url and self.mcp_oauth_client_id and self.mcp_oauth_client_secret)

    @property
    def moysklad_mcp_configured(self) -> bool:
        return bool(self.moysklad_mcp_url and self.moysklad_mcp_client_id and self.moysklad_mcp_client_secret)

    # --- Мессенджер MAX (app/max) ---
    # Токен веб-сессии MAX: JSON.parse(localStorage.__oneme_auth).token на
    # web.max.ru. Без него раздел /api/max отдаёт 503.
    max_token: str = ""


settings = Settings()
