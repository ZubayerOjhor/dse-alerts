"""Runtime settings. Everything comes from environment variables or a local .env file;
nothing is read from anywhere else, and nothing is sent anywhere the user did not configure."""

from __future__ import annotations

from pathlib import Path

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    # storage
    dse_data_dir: Path = Field(default=Path("./data"))

    # email (optional)
    smtp_host: str = "smtp.gmail.com"
    smtp_port: int = 587
    smtp_user: str = ""
    smtp_password: str = ""
    alert_from: str = ""
    alert_to: str = ""

    # Telegram (optional)
    telegram_bot_token: str = ""
    telegram_chat_id: str = ""

    # AI digest (optional)
    ai_provider: str = ""  # "", "anthropic" or "openai_compatible"
    anthropic_api_key: str = ""
    anthropic_model: str = "claude-fable-5-1"
    openai_compat_base_url: str = "https://api.groq.com/openai/v1"
    openai_compat_api_key: str = ""
    openai_compat_model: str = "llama-3.3-70b-versatile"

    # web app
    app_secret_key: str = "change-me"
    app_host: str = "127.0.0.1"
    app_port: int = 8000

    @property
    def market_db(self) -> Path:
        return self.dse_data_dir / "market.duckdb"

    @property
    def app_db(self) -> Path:
        return self.dse_data_dir / "app.sqlite"

    @property
    def raw_dir(self) -> Path:
        return self.dse_data_dir / "raw"

    @property
    def outbox_dir(self) -> Path:
        return self.dse_data_dir / "outbox"

    @property
    def email_enabled(self) -> bool:
        return bool(self.smtp_user and self.smtp_password and (self.alert_from or self.smtp_user))

    @property
    def telegram_enabled(self) -> bool:
        return bool(self.telegram_bot_token)

    @property
    def ai_enabled(self) -> bool:
        if self.ai_provider == "anthropic":
            return bool(self.anthropic_api_key)
        if self.ai_provider == "openai_compatible":
            return bool(self.openai_compat_api_key or "localhost" in self.openai_compat_base_url)
        return False


def get_settings() -> Settings:
    s = Settings()
    s.smtp_password = s.smtp_password.replace(" ", "")   # Gmail shows app passwords in groups of four
    s.dse_data_dir.mkdir(parents=True, exist_ok=True)
    return s
