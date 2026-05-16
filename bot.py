"""Главный entry point. Поднимает диспетчер, регистрирует роутеры, запускает polling + aiohttp."""
import asyncio
import logging
import sys

from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode

from config import config
from database import init_db
from handlers import setup_routers
from oauth_server import start_http_server

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)],
)
log = logging.getLogger(__name__)


async def main() -> None:
    log.info("Инициализация БД...")
    await init_db()

    bot = Bot(
        token=config.bot_token,
        default=DefaultBotProperties(parse_mode=ParseMode.HTML),
    )
    dp = Dispatcher()
    setup_routers(dp)

    # HTTP-сервер для OAuth (Threads) — параллельно с polling
    http_runner = None
    if config.threads_enabled:
        log.info("Threads-фичи активны, запускаю HTTP-сервер на порту %d", config.port)
        http_runner = await start_http_server(bot)
    else:
        log.warning(
            "META_APP_ID/SECRET/REDIRECT_URI/ENCRYPTION_KEY не заданы — "
            "Threads-фичи отключены, HTTP-сервер не запускаю"
        )

    log.info("Запуск polling...")
    try:
        # Сбрасываем накопившиеся апдейты при рестарте
        await bot.delete_webhook(drop_pending_updates=True)
        await dp.start_polling(bot)
    finally:
        if http_runner is not None:
            await http_runner.cleanup()
        await bot.session.close()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except (KeyboardInterrupt, SystemExit):
        log.info("Бот остановлен")
