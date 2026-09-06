from pydantic import AliasChoices, Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    database_url: str = "postgresql+psycopg2://soborbum:soborbum@db:5432/soborbum"

    jwt_secret: str = "change-me-in-production"
    jwt_algorithm: str = "HS256"
    jwt_expire_minutes: int = 60 * 12

    storage_dir: str = "/app/storage"

    admin_email: str = "admin@soborbum.local"
    admin_password: str = "admin123"
    admin_full_name: str = "Administrator"

    # Comma-separated list of origins the frontend is served from, e.g.
    # "http://localhost:5173,http://localhost:3000". "*" allows any origin.
    cors_allowed_origins: str = "*"

    @property
    def cors_origins(self) -> list[str]:
        return [o.strip() for o in self.cors_allowed_origins.split(",") if o.strip()]

    # --- AI assistant (app/ai) ---
    anthropic_api_key: str = ""
    # У каждой задачи своя модель и усилие — см. app/ai/model_profiles.py.
    # ai_model остаётся глобальным рубильником: если он отличается от значения
    # по умолчанию ниже, он переопределяет модель во всех профилях (обратная
    # совместимость). Усилие (effort) при этом берётся из профиля.
    ai_model: str = "claude-sonnet-5"
    # Точечные переопределения профилей, через запятую. Элемент:
    #   <profile>=<model|пусто>[:<effort>]
    # Примеры:
    #   AI_MODELS=chat=claude-opus-5,quick=claude-haiku-4-5
    #   AI_MODELS=daily_job=:medium,board_lead=claude-opus-5:high
    # Пустая модель — оставить дефолтную профиля, поменяв только effort.
    # effort ∈ low|medium|high|xhigh|max. Профили: chat, quick, board_lead,
    # board_agent, daily_job, weekly_job.
    ai_models: str = ""

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
    mcp_redirect_uri: str = "http://127.0.0.1:8000/callback"

    @property
    def mcp_configured(self) -> bool:
        return bool(self.mcp_server_url and self.mcp_oauth_client_id and self.mcp_oauth_client_secret)

    # --- Telegram bot (app/telegram) ---
    # CHAT_ID is the historical env name (kept in .env); TELEGRAM_CHAT_ID is
    # accepted too. Stored as a string: Telegram group ids are large and
    # negative, and we only ever compare/format them, never do arithmetic.
    telegram_bot_token: str = ""
    telegram_chat_id: str = Field(
        default="", validation_alias=AliasChoices("CHAT_ID", "TELEGRAM_CHAT_ID")
    )
    # Shared secret for the POST /api/telegram/webhook/{secret} update sink,
    # the alternative to running app.telegram.poller as a long-lived process.
    telegram_webhook_secret: str = ""
    # Public origin of this backend, used to build the Telegram deep-link
    # login target shown in the side menu button.
    public_base_url: str = "http://localhost:8005"
    # Where the daily Telegram ingest and the day-summary step write in the
    # knowledge base, and the timezone whose calendar day "вчера" means.
    kb_daily_dir: str = "01_Inbox/Daily"
    kb_timezone: str = "Europe/Moscow"

    @property
    def telegram_configured(self) -> bool:
        return bool(self.telegram_bot_token and self.telegram_chat_id)


settings = Settings()
