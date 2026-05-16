"""HTTP-сервер на aiohttp для приёма OAuth callback от Meta.

Запускается параллельно с aiogram polling. Слушает 3 эндпоинта:
- /auth/threads/callback — основной OAuth callback
- /auth/threads/deauthorize — уведомление от Meta при отзыве доступа юзером
- /auth/threads/data-deletion — запрос на удаление данных юзера
+ healthcheck / для Railway
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta

from aiogram import Bot
from aiohttp import web

from config import config
from database import save_threads_account
from threads_api import (
    encrypt_token,
    exchange_code_for_token,
    exchange_for_long_lived,
    get_me,
    verify_state,
)

log = logging.getLogger(__name__)


def make_html_response(title: str, body: str, status: int = 200) -> web.Response:
    """Простая HTML-страница для пользователя после OAuth."""
    html = f"""<!DOCTYPE html>
<html lang="ru">
<head>
    <meta charset="UTF-8">
    <title>{title}</title>
    <style>
        body {{
            font-family: -apple-system, BlinkMacSystemFont, sans-serif;
            background: #0a0a0a; color: #f0f0f0;
            display: flex; align-items: center; justify-content: center;
            min-height: 100vh; margin: 0; padding: 1rem;
        }}
        .card {{
            background: #1a1a1a; padding: 2rem; border-radius: 12px;
            max-width: 480px; text-align: center;
            border: 1px solid #2a2a2a;
        }}
        h1 {{ margin-top: 0; }}
        p {{ color: #c0c0c0; line-height: 1.5; }}
        a {{ color: #4a9eff; }}
    </style>
</head>
<body>
    <div class="card">
        <h1>{title}</h1>
        <p>{body}</p>
    </div>
</body>
</html>"""
    return web.Response(text=html, content_type="text/html", status=status)


async def handle_threads_callback(request: web.Request) -> web.Response:
    """OAuth callback от Meta. Обменивает code на токен и сохраняет в БД."""
    bot: Bot = request.app["bot"]

    # Meta может прислать error если юзер отказался
    error = request.query.get("error")
    if error:
        log.info("OAuth declined: %s", request.query.get("error_description", error))
        return make_html_response(
            "❌ Доступ не получен",
            "Авторизация отменена. Вернись в бот и попробуй ещё раз если передумал.",
            status=400,
        )

    code = request.query.get("code")
    state = request.query.get("state", "")
    if not code:
        return make_html_response(
            "❌ Ошибка",
            "Meta не вернула код авторизации. Попробуй ещё раз через бот.",
            status=400,
        )

    # Проверяем state — CSRF и привязываем к user_id
    user_id = verify_state(state)
    if user_id is None:
        return make_html_response(
            "❌ Сессия истекла",
            "OAuth-ссылка устарела (живёт 10 минут). Запроси новую в боте.",
            status=400,
        )

    # Обмен code → short-lived token
    try:
        short_data = await exchange_code_for_token(code)
        short_token = short_data["access_token"]
    except Exception as e:
        log.exception("Code exchange failed for user %s", user_id)
        return make_html_response(
            "❌ Ошибка обмена токена",
            f"Meta вернула ошибку: <code>{type(e).__name__}: {str(e)[:200]}</code>",
            status=500,
        )

    # Short-lived → long-lived (60 дней)
    try:
        long_data = await exchange_for_long_lived(short_token)
        long_token = long_data["access_token"]
        expires_in = int(long_data.get("expires_in", 60 * 24 * 3600))
    except Exception as e:
        log.exception("Long-lived exchange failed for user %s", user_id)
        return make_html_response(
            "❌ Ошибка продления токена",
            f"<code>{type(e).__name__}: {str(e)[:200]}</code>",
            status=500,
        )

    # Узнаём кто этот юзер в Threads
    try:
        me = await get_me(long_token)
        threads_user_id = str(me["id"])
        threads_username = me.get("username")
    except Exception as e:
        log.exception("get_me failed for user %s", user_id)
        return make_html_response(
            "❌ Не удалось получить профиль",
            f"<code>{type(e).__name__}: {str(e)[:200]}</code>",
            status=500,
        )

    # Сохраняем зашифрованный токен в БД
    encrypted = encrypt_token(long_token)
    expires_at = datetime.utcnow() + timedelta(seconds=expires_in)
    await save_threads_account(
        user_id=user_id,
        threads_user_id=threads_user_id,
        threads_username=threads_username,
        access_token_encrypted=encrypted,
        token_expires_at=expires_at,
    )
    log.info(
        "Threads connected: user_id=%s threads_user=@%s expires=%s",
        user_id, threads_username, expires_at.isoformat(),
    )

    # Шлём подтверждение в Telegram
    try:
        await bot.send_message(
            user_id,
            f"✅ <b>Threads подключён!</b>\n\n"
            f"Аккаунт: <b>@{threads_username}</b>\n"
            f"Действует до: {expires_at.strftime('%d.%m.%Y')}\n\n"
            "Теперь под каждым сгенерированным постом будет кнопка "
            "«📤 Опубликовать в Threads».",
        )
    except Exception as e:
        log.warning("Failed to notify user %s in TG: %s", user_id, e)

    return make_html_response(
        "✅ Threads подключён",
        f"Аккаунт <b>@{threads_username}</b> привязан к боту. "
        "Можешь закрыть эту вкладку и вернуться в Telegram.",
    )


async def handle_deauthorize(request: web.Request) -> web.Response:
    """Meta вызывает когда юзер отозвал доступ через настройки Threads.

    Подписанный запрос: body = signed_request. Для простоты в Dev Mode
    просто логируем — данные удалит юзер через бот.
    """
    body = await request.text()
    log.info("Threads deauthorize received: %s", body[:200])
    return web.json_response({"status": "ok"})


async def handle_data_deletion(request: web.Request) -> web.Response:
    """Meta вызывает когда юзер запросил удаление своих данных.

    Должен вернуть JSON с URL подтверждения. Для простоты возвращаем заглушку.
    """
    body = await request.text()
    log.info("Threads data deletion received: %s", body[:200])
    return web.json_response({
        "url": "https://github.com/grosky/threadsbot",
        "confirmation_code": "manual_request",
    })


async def handle_health(_request: web.Request) -> web.Response:
    """Healthcheck для Railway."""
    return web.json_response({"status": "ok", "service": "threads-bot"})


def build_app(bot: Bot) -> web.Application:
    """Собирает aiohttp приложение."""
    app = web.Application()
    app["bot"] = bot
    app.router.add_get("/", handle_health)
    app.router.add_get("/auth/threads/callback", handle_threads_callback)
    app.router.add_post("/auth/threads/deauthorize", handle_deauthorize)
    app.router.add_post("/auth/threads/data-deletion", handle_data_deletion)
    return app


async def start_http_server(bot: Bot) -> web.AppRunner:
    """Запускает HTTP-сервер. Возвращает runner для graceful shutdown."""
    app = build_app(bot)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, host="0.0.0.0", port=config.port)
    await site.start()
    log.info("HTTP server started on port %d", config.port)
    return runner
