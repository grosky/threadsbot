"""Главное меню с шапкой статуса и группировкой по секциям."""
from __future__ import annotations

from datetime import datetime

from aiogram import F, Router
from aiogram.filters import Command
from aiogram.types import (
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
)

from config import DAILY_LIMIT, config
from database import (
    count_today_generations,
    get_streak,
    get_threads_account,
    get_user,
    get_user_achievements,
    is_subscription_active,
)

router = Router()


def main_menu_keyboard() -> InlineKeyboardMarkup:
    """Главное меню: 3 раздела."""
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="📝 Создание постов", callback_data="menu:create")],
            [InlineKeyboardButton(text="📊 Аналитика", callback_data="menu:analytics")],
            [InlineKeyboardButton(text="⚙️ Настройки", callback_data="menu:settings")],
        ]
    )


def create_menu_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="🎯 Сгенерить пост", callback_data="action:generate")],
            [InlineKeyboardButton(text="🎙 Голосовой сторителлинг", callback_data="action:storytelling")],
            [InlineKeyboardButton(text="✍️ Опубликовать свой пост", callback_data="action:custom_post")],
            [InlineKeyboardButton(text="⬅️ Назад", callback_data="menu:main")],
        ]
    )


def analytics_menu_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="📸 Анализ своего профиля", callback_data="action:analyze_profile")],
            [InlineKeyboardButton(text="🔍 Разбор чужой ленты", callback_data="action:feed_analysis")],
            [InlineKeyboardButton(text="⬅️ Назад", callback_data="menu:main")],
        ]
    )


def settings_menu_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="🔗 Подключение Threads", callback_data="action:connect_threads")],
            [InlineKeyboardButton(text="👤 Мой профиль", callback_data="action:profile")],
            [InlineKeyboardButton(text="💎 Подписка", callback_data="action:subscription")],
            [InlineKeyboardButton(text="🎁 Пригласить друга", callback_data="action:invite")],
            [InlineKeyboardButton(text="🏆 Ачивки", callback_data="action:achievements")],
            [InlineKeyboardButton(text="⬅️ Назад", callback_data="menu:main")],
        ]
    )


async def _build_status_header(user_id: int) -> str:
    """Шапка меню: статус подписки / Threads / лимит / стрик."""
    streak = await get_streak(user_id)

    user = await get_user(user_id)
    sub_active = await is_subscription_active(user_id)
    if sub_active and user and user.get("subscription_expires_at"):
        try:
            expires = datetime.fromisoformat(user["subscription_expires_at"])
            days = max(0, (expires - datetime.utcnow()).days)
            sub_line = f"💎 Подписка: <b>{days} дн.</b>"
        except (ValueError, TypeError):
            sub_line = "💎 Подписка: активна"
    else:
        sub_line = "💎 Подписка: ❌ неактивна"

    th_account = await get_threads_account(user_id)
    if th_account:
        username = th_account.get("threads_username") or "—"
        th_line = f"🔗 Threads: <b>@{username}</b>"
    else:
        th_line = "🔗 Threads: не подключён"

    used = await count_today_generations(user_id)
    if user_id == config.admin_telegram_id:
        limit_line = "🎯 Сегодня: <b>∞</b>"
    else:
        remaining = max(0, DAILY_LIMIT - used)
        limit_line = f"🎯 Сегодня: <b>{remaining}/{DAILY_LIMIT}</b>"

    lines = [sub_line, th_line, limit_line]
    if streak > 0:
        lines.append(f"🔥 Стрик: <b>{streak} дн.</b>")
    return "\n".join(lines)


async def show_main_menu(message: Message) -> None:
    header = await _build_status_header(message.from_user.id)
    await message.answer(
        header,
        reply_markup=main_menu_keyboard(),
    )


@router.message(Command("menu"))
async def cmd_menu(message: Message) -> None:
    await show_main_menu(message)


# ---------- Иерархическая навигация ----------

@router.callback_query(F.data == "menu:main")
async def go_main(callback: CallbackQuery) -> None:
    header = await _build_status_header(callback.from_user.id)
    await callback.answer()
    try:
        await callback.message.edit_text(header, reply_markup=main_menu_keyboard())
    except Exception:
        # На случай если сообщение не редактируется (например слишком старое)
        await callback.message.answer(header, reply_markup=main_menu_keyboard())


@router.callback_query(F.data == "menu:create")
async def go_create(callback: CallbackQuery) -> None:
    await callback.answer()
    text = (
        "📝 <b>Создание постов</b>\n\n"
        "🎯 <b>Сгенерить</b> — Gemini создаёт 3 варианта по выбранному формату\n"
        "🎙 <b>Голосом</b> — наговариваешь идею, бот собирает живой сторителлинг\n"
        "✍️ <b>Свой пост</b> — пишешь руками, бот публикует в Threads"
    )
    try:
        await callback.message.edit_text(text, reply_markup=create_menu_keyboard())
    except Exception:
        await callback.message.answer(text, reply_markup=create_menu_keyboard())


@router.callback_query(F.data == "menu:analytics")
async def go_analytics(callback: CallbackQuery) -> None:
    await callback.answer()
    text = (
        "📊 <b>Аналитика</b>\n\n"
        "📸 <b>Анализ профиля</b> — скрин шапки Threads → разбор упаковки + правки\n"
        "🔍 <b>Разбор ленты</b> — кидаешь 3-10 чужих постов → находит паттерны и адаптирует под тебя"
    )
    try:
        await callback.message.edit_text(text, reply_markup=analytics_menu_keyboard())
    except Exception:
        await callback.message.answer(text, reply_markup=analytics_menu_keyboard())


@router.callback_query(F.data == "menu:settings")
async def go_settings(callback: CallbackQuery) -> None:
    await callback.answer()
    text = (
        "⚙️ <b>Настройки</b>\n\n"
        "🔗 <b>Threads</b> — подключение / отключение аккаунта для публикаций\n"
        "👤 <b>Профиль</b> — твои ниша, ЦА, продукт\n"
        "💎 <b>Подписка</b> — срок действия\n"
        "🎁 <b>Пригласить</b> — твоя ref-ссылка и статистика\n"
        "🏆 <b>Ачивки</b> — открытые и закрытые"
    )
    try:
        await callback.message.edit_text(text, reply_markup=settings_menu_keyboard())
    except Exception:
        await callback.message.answer(text, reply_markup=settings_menu_keyboard())


@router.callback_query(F.data == "action:profile")
async def show_profile(callback: CallbackQuery) -> None:
    user = await get_user(callback.from_user.id)
    if not user:
        await callback.answer("Профиль не найден", show_alert=True)
        return

    text = (
        "👤 <b>Твой профиль</b>\n\n"
        f"<b>Ниша:</b> {user.get('niche') or '—'}\n"
        f"<b>ЦА:</b> {user.get('audience') or '—'}\n"
        f"<b>Что продаёт:</b> {user.get('product') or '—'}\n"
        f"<b>Ссылка:</b> {user.get('product_link') or '—'}\n"
        f"<b>Tone:</b> {user.get('tone') or '—'}\n"
        f"<b>Личные факты:</b> {user.get('facts') or '—'}\n"
        f"<b>Боли ЦА:</b> {user.get('pains') or '—'}\n"
        f"<b>Social proof:</b> {user.get('social_proof') or '—'}\n\n"
        "<i>Чтобы изменить профиль — пройди /start заново.</i>"
    )
    await callback.answer()
    await callback.message.answer(text)


@router.callback_query(F.data == "action:subscription")
async def show_subscription(callback: CallbackQuery) -> None:
    user = await get_user(callback.from_user.id)
    if not user:
        await callback.answer("Профиль не найден", show_alert=True)
        return

    is_active = await is_subscription_active(callback.from_user.id)

    if is_active and user.get("subscription_expires_at"):
        try:
            expires = datetime.fromisoformat(user["subscription_expires_at"])
            days_left = max(0, (expires - datetime.utcnow()).days)
            text = (
                f"💎 <b>Подписка активна</b>\n\n"
                f"Действует до: <b>{expires.strftime('%d.%m.%Y')}</b>\n"
                f"Осталось дней: <b>{days_left}</b>"
            )
        except (ValueError, TypeError):
            text = "💎 Подписка активна (не удалось распарсить дату)."
    else:
        text = (
            "❌ <b>Подписка неактивна</b>\n\n"
            "Активируй промокод через /start или напиши автору для продления."
        )

    await callback.answer()
    await callback.message.answer(text)


@router.callback_query(F.data == "action:achievements")
async def show_achievements(callback: CallbackQuery) -> None:
    """Экран ачивок: разблокированные + закрытые."""
    from achievements import ACHIEVEMENTS

    unlocked = set(await get_user_achievements(callback.from_user.id))

    lines = ["🏆 <b>Ачивки</b>", ""]

    if unlocked:
        lines.append("<b>Открыто:</b>")
        for a in ACHIEVEMENTS:
            if a.code in unlocked:
                lines.append(f"{a.emoji} <b>{a.name}</b> — <i>{a.description}</i>")
        lines.append("")

    locked = [a for a in ACHIEVEMENTS if a.code not in unlocked]
    if locked:
        lines.append("<b>Закрыто:</b>")
        for a in locked:
            lines.append(f"🔒 <b>{a.name}</b> — <i>{a.description}</i>")

    if not unlocked:
        lines.append("\n<i>Пока пусто. Начни с «🎯 Сгенерить» — первая ачивка откроется автоматически.</i>")

    await callback.answer()
    await callback.message.answer("\n".join(lines))
