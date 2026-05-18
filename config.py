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
    # Meta / Threads API
    meta_app_id: str
    meta_app_secret: str
    meta_redirect_uri: str
    # Шифрование access-токенов в БД (Fernet, base64-encoded 32 байта)
    encryption_key: str
    # Порт для HTTP-сервера OAuth callback. Railway передаёт через PORT.
    port: int
    # Tribute (оплаты)
    tribute_api_key: str
    tribute_subscription_url: str

    @classmethod
    def from_env(cls) -> "Config":
        bot_token = os.getenv("BOT_TOKEN", "").strip()
        gemini_api_key = os.getenv("GEMINI_API_KEY", "").strip()
        admin_id_str = os.getenv("ADMIN_TELEGRAM_ID", "").strip()
        database_path = os.getenv("DATABASE_PATH", "bot.db").strip()
        meta_app_id = os.getenv("META_APP_ID", "").strip()
        meta_app_secret = os.getenv("META_APP_SECRET", "").strip()
        meta_redirect_uri = os.getenv("META_REDIRECT_URI", "").strip()
        encryption_key = os.getenv("ENCRYPTION_KEY", "").strip()
        port_str = os.getenv("PORT", "8080").strip()
        tribute_api_key = os.getenv("TRIBUTE_API_KEY", "").strip()
        tribute_subscription_url = os.getenv("TRIBUTE_SUBSCRIPTION_URL", "").strip()

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
            meta_app_id=meta_app_id,
            meta_app_secret=meta_app_secret,
            meta_redirect_uri=meta_redirect_uri,
            encryption_key=encryption_key,
            port=int(port_str),
            tribute_api_key=tribute_api_key,
            tribute_subscription_url=tribute_subscription_url,
        )

    @property
    def threads_enabled(self) -> bool:
        """Threads-фичи доступны только если все Meta-переменные заданы."""
        return bool(
            self.meta_app_id
            and self.meta_app_secret
            and self.meta_redirect_uri
            and self.encryption_key
        )

    @property
    def tribute_enabled(self) -> bool:
        """Tribute-оплаты доступны только если есть API key + ссылка на продукт."""
        return bool(self.tribute_api_key and self.tribute_subscription_url)


config = Config.from_env()

# Дневной лимит генераций на одного юзера
DAILY_LIMIT = 4
