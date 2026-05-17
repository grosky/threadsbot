"""Подключение Threads-аккаунта (OAuth) + публикация постов."""
from __future__ import annotations

import html
import logging
from datetime import datetime

from aiogram import F, Router
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.types import (
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
)

from config import config
from database import (
    delete_threads_account,
    get_threads_account,
    is_subscription_active,
    mark_threads_post_sent,
)
from threads_api import (
    build_auth_url,
    debug_token,
    decrypt_token,
    publish_text_post,
)

router = Router()
log = logging.getLogger(__name__)

# In-memory cache: {user_id: {post_key: post_text}}
# Сохраняется только в RAM — после рестарта пропадает (юзер просто сгенерит заново).
# FSM не подходит т.к. state.clear() в конце генерации стирает.
_publishable_posts: dict[int, dict[str, str]] = {}


def remember_post(user_id: int, post_key: str, text: str) -> None:
    """Кладёт текст поста в кэш для последующей публикации."""
    if user_id not in _publishable_posts:
        _publishable_posts[user_id] = {}
    _publishable_posts[user_id][post_key] = text
    # Ограничиваем размер на юзера, чтобы не утечь по памяти
    if len(_publishable_posts[user_id]) > 50:
        oldest = next(iter(_publishable_posts[user_id]))
        _publishable_posts[user_id].pop(oldest)


def get_post(user_id: int, post_key: str) -> str | None:
    return _publishable_posts.get(user_id, {}).get(post_key)


def _expiry_status(token_expires_at: str) -> tuple[bool, int]:
    """Возвращает (валиден, дней до истечения)."""
    try:
        expires = datetime.fromisoformat(token_expires_at)
    except (ValueError, TypeError):
        return False, 0
    delta = expires - datetime.utcnow()
    return delta.total_seconds() > 0, max(0, delta.days)


@router.callback_query(F.data == "action:connect_threads")
async def show_connect_screen(callback: CallbackQuery) -> None:
    user_id = callback.from_user.id

    if not await is_subscription_active(user_id):
        await callback.answer()
        await callback.message.answer(
            "❌ Подписка неактивна. Активируй промокод через /start."
        )
        return

    if not config.threads_enabled:
        await callback.answer()
        await callback.message.answer(
            "⚠️ Подключение Threads пока не настроено на сервере. "
            "Скажи админу что нужно добавить META_APP_ID/SECRET в Railway."
        )
        return

    account = await get_threads_account(user_id)
    if account:
        valid, days_left = _expiry_status(account["token_expires_at"])
        username = account.get("threads_username") or "—"

        kb = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(
                text="🔄 Переподключить",
                callback_data="action:connect_threads_force",
            )],
            [InlineKeyboardButton(
                text="❌ Отключить Threads",
                callback_data="action:disconnect_threads",
            )],
        ])

        status = "✅ активен" if valid else "❌ истёк"
        await callback.answer()
        await callback.message.answer(
            f"🧵 <b>Threads подключён</b>\n\n"
            f"Аккаунт: <b>@{html.escape(username)}</b>\n"
            f"Токен: {status}, осталось <b>{days_left} дней</b>\n\n"
            "Под каждым сгенерированным постом доступна кнопка «📤 Опубликовать в Threads».",
            reply_markup=kb,
        )
        return

    # Не подключён — даём ссылку на OAuth + подробную инструкцию
    await callback.answer()
    auth_url = build_auth_url(user_id)
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🔗 Авторизоваться в Threads", url=auth_url)],
        [InlineKeyboardButton(
            text="❓ Что-то пошло не так",
            callback_data="action:connect_threads_help",
        )],
    ])
    await callback.message.answer(
        "🧵 <b>Подключение Threads — 3 шага</b>\n\n"
        "<b>1.</b> Жми кнопку ниже\n"
        "<b>2.</b> На странице Meta жми <b>«Allow»</b> (разрешить публиковать)\n"
        "<b>3.</b> Тебя перекинет на страницу «✅ Подключено» — закрывай и возвращайся в Telegram\n\n"
        "Бот сам пришлёт подтверждение что всё ок.\n\n"
        "⚠️ <b>Если Threads заблокирован у тебя в стране — включи VPN перед нажатием.</b>\n\n"
        "<i>Ссылка живёт 10 минут.</i>",
        reply_markup=kb,
    )


@router.callback_query(F.data == "action:connect_threads_help")
async def connect_threads_help(callback: CallbackQuery) -> None:
    """Расширенный гайд — что делать если падает 'invite not accepted' и т.п."""
    await callback.answer()
    await callback.message.answer(
        "🛟 <b>Если выдаёт ошибку при авторизации</b>\n\n"
        "<b>«User has not accepted the invite»</b>\n"
        "Это значит твой Threads-аккаунт не принял приглашение тестировщика.\n\n"
        "Что делать:\n"
        "1. Открой Threads (с VPN)\n"
        "2. Внизу <b>Profile</b> → справа сверху <b>☰</b> → <b>Settings</b>\n"
        "3. <b>Account</b> → <b>Website permissions</b> (или <b>Apps</b>)\n"
        "4. Найди приглашение от <b>Threadsbot</b> → жми <b>Accept</b>\n"
        "5. Возвращайся в бот и пробуй авторизацию снова\n\n"
        "<b>«Session expired»</b>\n"
        "Ссылка устаревает за 10 минут. Запроси новую через /menu.\n\n"
        "<b>Страница Meta не открывается</b>\n"
        "Threads заблокирован в РФ — нужен VPN (страна = Бразилия / Турция / Латвия / любая не-РФ).\n\n"
        "<b>Что-то другое</b>\n"
        "Скрин ошибки автору → @grosky"
    )


@router.callback_query(F.data == "action:connect_threads_force")
async def force_reconnect(callback: CallbackQuery) -> None:
    """Кнопка «Переподключить» — выдаёт свежую OAuth-ссылку без удаления старой записи."""
    auth_url = build_auth_url(callback.from_user.id)
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🔗 Авторизоваться в Threads", url=auth_url)],
    ])
    await callback.answer()
    await callback.message.answer(
        "Жми кнопку — после новой авторизации старая запись перезапишется.",
        reply_markup=kb,
    )


@router.callback_query(F.data == "action:disconnect_threads")
async def disconnect(callback: CallbackQuery) -> None:
    await delete_threads_account(callback.from_user.id)
    await callback.answer("Отключено")
    await callback.message.answer(
        "🔌 Threads отключён. Данные удалены из бота.\n\n"
        "Если хочешь полностью отозвать доступ — также зайди в Threads → "
        "Settings → Account → Apps → Threadsbot → Remove."
    )


# ---------- ПУБЛИКАЦИЯ ----------

def publish_button(post_text_id: str) -> InlineKeyboardMarkup:
    """Кнопка «Опубликовать в Threads» — встраивается в результат генерации.

    post_text_id — ключ в FSM data, по которому достанем текст поста.
    """
    return InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(
            text="📤 Опубликовать в Threads",
            callback_data=f"publish:threads:{post_text_id}",
        ),
    ]])


@router.callback_query(F.data.startswith("publish:threads:"))
async def publish_to_threads(callback: CallbackQuery, state: FSMContext) -> None:
    user_id = callback.from_user.id
    post_key = callback.data.split(":", 2)[2]

    # Достаём текст поста из in-memory кэша
    post_text = get_post(user_id, post_key)

    if not post_text:
        await callback.answer(
            "Текст поста потерян (бот перезапустили?). Сгенерируй заново.",
            show_alert=True,
        )
        return

    account = await get_threads_account(user_id)
    if not account:
        await callback.answer()
        await callback.message.answer(
            "Сначала подключи Threads через /menu → «🔗 Подключить Threads»."
        )
        return

    valid, _ = _expiry_status(account["token_expires_at"])
    if not valid:
        await callback.answer()
        await callback.message.answer(
            "🔑 Токен Threads истёк. Переподключись через /menu → «🔗 Подключить Threads»."
        )
        return

    await callback.answer("Публикую...")
    status_msg = await callback.message.answer(
        "📤 Публикую в Threads... (длинный пост разобью на тред)"
    )

    try:
        token = decrypt_token(account["access_token_encrypted"])
        result = await publish_text_post(
            threads_user_id=account["threads_user_id"],
            access_token=token,
            text=post_text,
        )
        await mark_threads_post_sent(user_id)
    except Exception as e:
        log.exception("Threads publish failed for user %s", user_id)
        await status_msg.edit_text(
            f"❌ Не удалось опубликовать.\n\n"
            f"<code>{html.escape(type(e).__name__)}: {html.escape(str(e))[:300]}</code>\n\n"
            "Если ошибка про токен — переподключи Threads через /menu."
        )
        return

    posts_count = result["posts_count"]
    permalink = result["permalink"]

    if posts_count == 1:
        suffix = "одним постом"
    else:
        suffix = f"тредом из <b>{posts_count}</b> постов"

    await status_msg.edit_text(
        f"✅ <b>Опубликовано в Threads</b> {suffix}\n\n"
        f"<a href='{html.escape(permalink)}'>Открыть в Threads ↗</a>"
    )


# ---------- /threads команда ----------

@router.message(Command("threads_debug"))
async def cmd_threads_debug(message: Message) -> None:
    """Debug: проверяет какие scopes реально в токене юзера.

    Помогает диагностировать «Application does not have permission» —
    видим сразу есть ли threads_content_publish в гранте.
    """
    user_id = message.from_user.id
    account = await get_threads_account(user_id)
    if not account:
        await message.answer("Threads не подключён — нечего дебажить.")
        return

    try:
        token = decrypt_token(account["access_token_encrypted"])
        info = await debug_token(token)
    except Exception as e:
        await message.answer(
            f"❌ debug_token failed:\n<code>{html.escape(type(e).__name__)}: "
            f"{html.escape(str(e))[:400]}</code>"
        )
        return

    me_status = info.get("me_status")
    me_body = info.get("me_body", "")
    create_status = info.get("create_status")
    create_body = info.get("create_body", "")
    fb_status = info.get("fb_debug_status")
    fb_body = info.get("fb_debug_body", "")

    text = (
        "🔍 <b>Token debug report</b>\n\n"
        f"<b>1. GET /me</b> → status <code>{me_status}</code>\n"
        f"<code>{html.escape(str(me_body))}</code>\n\n"
        f"<b>2. POST /threads (test container)</b> → status <code>{create_status}</code>\n"
        f"<code>{html.escape(str(create_body))}</code>\n\n"
        f"<b>3. FB debug_token</b> → status <code>{fb_status}</code>\n"
        f"<code>{html.escape(str(fb_body))}</code>"
    )
    # Telegram limit
    if len(text) > 4000:
        text = text[:4000] + "\n…(обрезано)"
    await message.answer(text)


@router.message(Command("threads"))
async def cmd_threads(message: Message) -> None:
    """Алиас для меню подключения."""
    # Эмулируем callback — просто вызовем экран
    user_id = message.from_user.id
    if not config.threads_enabled:
        await message.answer("⚠️ Подключение Threads не настроено.")
        return

    account = await get_threads_account(user_id)
    if account:
        valid, days_left = _expiry_status(account["token_expires_at"])
        username = account.get("threads_username") or "—"
        status = "✅ активен" if valid else "❌ истёк"
        await message.answer(
            f"🧵 Threads: <b>@{html.escape(username)}</b> — {status}, "
            f"<b>{days_left} дней</b>"
        )
    else:
        auth_url = build_auth_url(user_id)
        kb = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="🔗 Подключить Threads", url=auth_url)],
        ])
        await message.answer(
            "🧵 Threads не подключён. Жми кнопку для авторизации:",
            reply_markup=kb,
        )
