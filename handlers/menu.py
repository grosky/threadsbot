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
    """Меню с группировкой: создание / аналитика / прочее.

    Ряды 2-кнопочные где возможно — компактнее на мобиле.
    """
    return InlineKeyboardMarkup(
        inline_keyboard=[
            # Создание
            [
                InlineKeyboardButton(text="🎯 Сгенерить", callback_data="action:generate"),
                InlineKeyboardButton(text="🎙 Голосом", callback_data="action:storytelling"),
            ],
            [InlineKeyboardButton(text="✍️ Свой пост", callback_data="action:custom_post")],
            # Аналитика
            [
                InlineKeyboardButton(text="📸 Профиль", callback_data="action:analyze_profile"),
                InlineKeyboardButton(text="🔍 Лента", callback_data="action:feed_analysis"),
            ],
            # Threads
            [InlineKeyboardButton(text="🔗 Подключение Threads", callback_data="action:connect_threads")],
            # Прочее
            [
                InlineKeyboardButton(text="🎁 Пригласить", callback_data="action:invite"),
                InlineKeyboardButton(text="🏆 Ачивки", callback_data="action:achievements"),
            ],
            [
                InlineKeyboardButton(text="👤 Профиль", callback_data="action:profile"),
                InlineKeyboardButton(text="💎 Подписка", callback_data="action:subscription"),
            ],
        ]
    )


async def _build_status_header(user_id: int) -> str:
    """Шапка меню: имя бренда + статус подписки/Threads/лимит/стрик."""
    # Стрик
    streak = await get_streak(user_id)
    streak_line = f"🔥 {streak} дн." if streak > 0 else ""

    # Подписка
    user = await get_user(user_id)
    sub_active = await is_subscription_active(user_id)
    if sub_active and user and user.get("subscription_expires_at"):
        try:
            expires = datetime.fromisoformat(user["subscription_expires_at"])
            days = max(0, (expires - datetime.utcnow()).days)
            sub_line = f"💎 Подписка: {days} дн."
        except (ValueError, TypeError):
            sub_line = "💎 Подписка: активна"
    else:
        sub_line = "💎 Подписка: ❌"

    # Threads
    th_account = await get_threads_account(user_id)
    if th_account:
        username = th_account.get("threads_username") or "—"
        th_line = f"🔗 Threads: @{username}"
    else:
        th_line = "🔗 Threads: не подключён"

    # Лимит генераций
    used = await count_today_generations(user_id)
    if user_id == config.admin_telegram_id:
        limit_line = "🎯 Сегодня: ∞ (admin)"
    else:
        remaining = max(0, DAILY_LIMIT - used)
        limit_line = f"🎯 Сегодня: {remaining}/{DAILY_LIMIT}"

    # Сборка
    header = "🧵 <b>Lazy Threads</b>"
    if streak_line:
        header += f" · {streak_line}"
    header += "\n────────────────────"

    lines = [header, sub_line, th_line, limit_line]
    return "\n".join(lines)


async def show_main_menu(message: Message) -> None:
    header = await _build_status_header(message.from_user.id)
    await message.answer(
        f"{header}\n\n<b>Что делаем?</b>",
        reply_markup=main_menu_keyboard(),
    )


@router.message(Command("menu"))
async def cmd_menu(message: Message) -> None:
    await show_main_menu(message)


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
