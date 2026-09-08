from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    database_url: str = "postgresql+psycopg2://soborbum:soborbum@db:5432/soborbum"

    jwt_secret: str = "change-me-in-production"
    jwt_algorithm: str = "HS256"
    jwt_expire_minutes: int = 60 * 12

    storage_dir: str = "/tmp/soborum-storage"

    admin_email: str = "admin@soborbum.local"
    admin_password: str = "admin123"
    admin_full_name: str = "Administrator"

    # Comma-separated list of origins the frontend is served from, e.g.
    # "http://localhost:5173,http://localhost:3000". "*" allows any origin.
    cors_allowed_origins: str = "*"

    @property
    def cors_origins(self) -> list[str]:
        return [o.strip() for o in self.cors_allowed_origins.split(",") if o.strip()]

    # Local checkout of DUROV-OS/vault_backups. Empty = pack without notes.
    vault_root: str = ""

    # Company shift ticks itself. Human does not press «start».
    agent_shift_autorun: bool = True
    agent_shift_interval_seconds: int = 3600

    # --- AI assistant (app/ai) ---
    anthropic_api_key: str = ""
    anthropic_base_url: str = ""
    ai_model: str = "claude-sonnet-5"

    # Read-only freshness. Never write deals or stock from the shift.
    amocrm_subdomain: str = ""
    amocrm_base_domain: str = "amocrm.ru"
    amocrm_long_lived_token: str = ""
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
    amocrm_mcp_url: str = ""
    amocrm_mcp_client_id: str = ""
    amocrm_mcp_client_secret: str = ""
    amocrm_mcp_scope: str = "amocrm offline_access"
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
    def amocrm_mcp_configured(self) -> bool:
        return bool(self.amocrm_mcp_url and self.amocrm_mcp_client_id and self.amocrm_mcp_client_secret)

    @property
    def moysklad_mcp_configured(self) -> bool:
        return bool(self.moysklad_mcp_url and self.moysklad_mcp_client_id and self.moysklad_mcp_client_secret)

    # --- Мессенджер MAX (app/max) ---
    # Токен веб-сессии MAX: JSON.parse(localStorage.__oneme_auth).token на
    # web.max.ru. Без него раздел /api/max отдаёт 503.
    max_token: str = ""


settings = Settings()
