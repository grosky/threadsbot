"""Главный entry point. Поднимает диспетчер, регистрирует роутеры, запускает polling + aiohttp."""
import asyncio
import logging
import sys

from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode

from config import config
from database import cleanup_old_pending_posts, init_db
from handlers import setup_routers
from oauth_server import start_http_server

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)],
)
log = logging.getLogger(__name__)


async def _pending_posts_cleanup_loop() -> None:
    """Раз в час чистит pending_posts старше 24 часов."""
    while True:
        try:
            await asyncio.sleep(3600)
            deleted = await cleanup_old_pending_posts()
            if deleted:
                log.info("Cleanup: deleted %d expired pending_posts", deleted)
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("Cleanup loop error")


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

    # Фоновая чистка устаревших pending_posts
    cleanup_task = asyncio.create_task(_pending_posts_cleanup_loop())

    log.info("Запуск polling...")
    try:
        # Сбрасываем накопившиеся апдейты при рестарте
        await bot.delete_webhook(drop_pending_updates=True)
        await dp.start_polling(bot)
    finally:
        cleanup_task.cancel()
        if http_runner is not None:
            await http_runner.cleanup()
        await bot.session.close()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except (KeyboardInterrupt, SystemExit):
        log.info("Бот остановлен")
