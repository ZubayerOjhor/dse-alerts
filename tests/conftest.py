import os

# The developer's own .env (real API keys, SMTP login) must never leak into the tests:
# environment variables beat the .env file in pydantic-settings, so blank them here.
_SECRET_VARS = ("GROQ_API_KEY", "OPENROUTER_API_KEY", "ANTHROPIC_API_KEY", "OPENAI_COMPAT_API_KEY",
                "SMTP_USER", "SMTP_PASSWORD", "TELEGRAM_BOT_TOKEN", "TELEGRAM_CHAT_ID", "AI_PROVIDER")
for _v in _SECRET_VARS:
    os.environ[_v] = ""
