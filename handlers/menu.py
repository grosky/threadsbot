"""Главное меню, просмотр профиля, статус подписки."""
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
    get_user,
    is_subscription_active,
)

router = Router()


def main_menu_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="🎯 Сгенерить пост", callback_data="action:generate")],
            [InlineKeyboardButton(text="🎙 Голосовой сторителлинг", callback_data="action:storytelling")],
            [InlineKeyboardButton(text="✍️ Опубликовать свой пост", callback_data="action:custom_post")],
            [InlineKeyboardButton(text="🔗 Подключить Threads", callback_data="action:connect_threads")],
            [InlineKeyboardButton(text="📸 Анализ профиля", callback_data="action:analyze_profile")],
            [InlineKeyboardButton(text="🔍 Разбор чужой ленты", callback_data="action:feed_analysis")],
            [InlineKeyboardButton(text="🎁 Пригласить друга", callback_data="action:invite")],
            [InlineKeyboardButton(text="👤 Мой профиль", callback_data="action:profile")],
            [InlineKeyboardButton(text="💎 Подписка", callback_data="action:subscription")],
        ]
    )


async def show_main_menu(message: Message) -> None:
    user_id = message.from_user.id
    used = await count_today_generations(user_id)

    if user_id == config.admin_telegram_id:
        remaining_str = "∞ (admin)"
    else:
        remaining_str = f"{max(0, DAILY_LIMIT - used)}/{DAILY_LIMIT}"

    await message.answer(
        f"<b>Главное меню</b>\n\n"
        f"Осталось генераций сегодня: <b>{remaining_str}</b>\n\n"
        f"Что делаем?",
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
        "<b>Твой профиль</b>\n\n"
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
                f"Осталось дней: <b>{days_left}</b>\n\n"
                f"Когда срок подойдёт к концу — пришлю напоминание о продлении."
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
