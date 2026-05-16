"""Конфигурация бота. Загружает переменные окружения через python-dotenv."""
import os
from dataclasses import dataclass

from dotenv import load_dotenv

load_dotenv()


@dataclass
class Config:
    bot_token: str
    gemini_api_key: str
    admin_telegram_id: int
    database_path: str

    @classmethod
    def from_env(cls) -> "Config":
        bot_token = os.getenv("BOT_TOKEN", "").strip()
        gemini_api_key = os.getenv("GEMINI_API_KEY", "").strip()
        admin_id_str = os.getenv("ADMIN_TELEGRAM_ID", "").strip()
        database_path = os.getenv("DATABASE_PATH", "bot.db").strip()

        missing = []
        if not bot_token:
            missing.append("BOT_TOKEN")
        if not gemini_api_key:
            missing.append("GEMINI_API_KEY")
        if not admin_id_str:
            missing.append("ADMIN_TELEGRAM_ID")
        if missing:
            raise RuntimeError(f"Не заданы переменные окружения: {', '.join(missing)}")

        return cls(
            bot_token=bot_token,
            gemini_api_key=gemini_api_key,
            admin_telegram_id=int(admin_id_str),
            database_path=database_path,
        )


config = Config.from_env()

# Дневной лимит генераций на одного юзера
DAILY_LIMIT = 4
