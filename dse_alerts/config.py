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

    # AI digest (optional). AI_PROVIDERS is an ordered, comma-separated chain; the first
    # provider that is configured and answers wins. Names: groq, openrouter, anthropic,
    # openai_compatible (a generic endpoint, e.g. a local Ollama).
    ai_providers: str = "groq,openrouter,anthropic"
    ai_provider: str = ""  # legacy single-provider setting; still honoured when AI_PROVIDERS is blank
    anthropic_api_key: str = ""
    anthropic_model: str = "claude-fable-5-1"
    groq_api_key: str = ""
    groq_model: str = "llama-3.3-70b-versatile"
    groq_base_url: str = "https://api.groq.com/openai/v1"
    openrouter_api_key: str = ""
    openrouter_model: str = "deepseek/deepseek-chat-v3-0324:free"
    openrouter_base_url: str = "https://openrouter.ai/api/v1"
    openai_compat_base_url: str = "http://localhost:11434/v1"
    openai_compat_api_key: str = ""
    openai_compat_model: str = "llama3.1"

    # web app
    app_secret_key: str = "change-me"
    app_host: str = "127.0.0.1"
    app_port: int = 8000
    app_public_url: str = ""          # e.g. https://dse-alerts.fly.dev; used in emails and the hosting guide
    app_default_lang: str = "en"      # en | bn; the language before a visitor or user picks one
    # Demo mode for a public instance: a "Try the demo" button logs visitors into a shared,
    # read-only account seeded with `dse demo seed`. Registration stays open.
    app_demo: bool = False

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
    def base_url(self) -> str:
        return self.app_public_url.rstrip("/") or f"http://{self.app_host}:{self.app_port}"

    @property
    def email_enabled(self) -> bool:
        return bool(self.smtp_user and self.smtp_password and (self.alert_from or self.smtp_user))

    @property
    def telegram_enabled(self) -> bool:
        return bool(self.telegram_bot_token)

    def ai_chain(self) -> list[dict]:
        """Configured providers in order of preference: [{name, kind, base_url, api_key, model}]."""
        names = [n.strip().lower() for n in (self.ai_providers or "").split(",") if n.strip()]
        if self.ai_provider and self.ai_provider.strip().lower() not in names:
            names.append(self.ai_provider.strip().lower())   # legacy single setting still works
        out = []
        for n in names:
            if n == "anthropic" and self.anthropic_api_key:
                out.append({"name": "anthropic", "kind": "anthropic", "api_key": self.anthropic_api_key, "model": self.anthropic_model, "base_url": ""})
            elif n == "groq" and self.groq_api_key:
                out.append({"name": "groq", "kind": "openai_compatible", "api_key": self.groq_api_key, "model": self.groq_model, "base_url": self.groq_base_url})
            elif n == "openrouter" and self.openrouter_api_key:
                out.append({"name": "openrouter", "kind": "openai_compatible", "api_key": self.openrouter_api_key, "model": self.openrouter_model, "base_url": self.openrouter_base_url})
            elif n == "openai_compatible" and (self.openai_compat_api_key or "localhost" in self.openai_compat_base_url or "127.0.0.1" in self.openai_compat_base_url):
                out.append({"name": "openai_compatible", "kind": "openai_compatible", "api_key": self.openai_compat_api_key, "model": self.openai_compat_model, "base_url": self.openai_compat_base_url})
        return out

    @property
    def ai_enabled(self) -> bool:
        return bool(self.ai_chain())

    @property
    def ai_label(self) -> str:
        return " -> ".join(f"{p['name']}:{p['model']}" for p in self.ai_chain()) or "off"


def get_settings() -> Settings:
    s = Settings()
    s.smtp_password = s.smtp_password.replace(" ", "")   # Gmail shows app passwords in groups of four
    s.dse_data_dir.mkdir(parents=True, exist_ok=True)
    return s
