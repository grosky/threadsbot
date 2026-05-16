"""Реферальная программа: ссылка-приглашение + статистика + бонусы."""
from __future__ import annotations

import logging

from aiogram import Bot, F, Router
from aiogram.filters import Command
from aiogram.types import CallbackQuery, Message

from database import REFERRAL_BONUS_DAYS, get_referral_stats

router = Router()
log = logging.getLogger(__name__)


def build_referral_payload(user_id: int) -> str:
    return f"ref_{user_id}"


async def _build_invite_text(bot: Bot, user_id: int) -> str:
    me = await bot.get_me()
    link = f"https://t.me/{me.username}?start={build_referral_payload(user_id)}"
    stats = await get_referral_stats(user_id)

    return (
        "🎁 <b>Приглашай друзей — получай дни подписки</b>\n\n"
        f"За каждого друга, который активирует промокод, "
        f"тебе +<b>{REFERRAL_BONUS_DAYS} дней</b> подписки.\n\n"
        "<b>Твоя ссылка:</b>\n"
        f"<code>{link}</code>\n\n"
        "<i>Скопируй и отправь любому — твой друг просто жмёт «Старт» в боте.</i>\n\n"
        "<b>Твоя статистика:</b>\n"
        f"• Приглашено: <b>{stats['invited']}</b>\n"
        f"• Активировали: <b>{stats['rewarded']}</b>\n"
        f"• Бонус-дней получено: <b>{stats['bonus_days_total']}</b>"
    )


@router.callback_query(F.data == "action:invite")
async def show_invite_screen(callback: CallbackQuery, bot: Bot) -> None:
    text = await _build_invite_text(bot, callback.from_user.id)
    await callback.answer()
    await callback.message.answer(text)


@router.message(Command("invite"))
async def cmd_invite(message: Message, bot: Bot) -> None:
    text = await _build_invite_text(bot, message.from_user.id)
    await message.answer(text)
