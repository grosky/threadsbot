"""Реферальная программа: ссылка-приглашение + статистика + бонусы + UTM для админа."""
from __future__ import annotations

import logging
import re

from aiogram import Bot, F, Router
from aiogram.filters import Command, CommandObject
from aiogram.types import CallbackQuery, Message

from config import config
from database import REFERRAL_BONUS_DAYS, get_referral_stats, get_source_stats

router = Router()
log = logging.getLogger(__name__)

# UTM-источник: только латиница/цифры/подчёркивание/дефис, до 30 символов
_SOURCE_RE = re.compile(r"^[a-z0-9_\-]{1,30}$")


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


# ---------- UTM-ГЕНЕРАТОР И СТАТИСТИКА (только для админа) ----------

def _is_admin(user_id: int) -> bool:
    return user_id == config.admin_telegram_id


@router.message(Command("utm"))
async def cmd_utm(message: Message, command: CommandObject, bot: Bot) -> None:
    """Генерирует ref-ссылку с UTM-меткой источника.

    Использование: /utm partner_anna
    Возвращает: t.me/aithreadbot?start=ref_<admin_id>_partner_anna
    """
    if not _is_admin(message.from_user.id):
        return

    source = (command.args or "").strip().lower()
    if not source:
        await message.answer(
            "🔗 <b>Генератор UTM-ссылок</b>\n\n"
            "Использование: <code>/utm название_источника</code>\n\n"
            "Примеры:\n"
            "<code>/utm partner_anna</code> — для конкретного партнёра\n"
            "<code>/utm tg_channel</code> — для своего канала\n"
            "<code>/utm vk_ads</code> — для рекламной кампании\n\n"
            "Каждый источник = отдельная строчка в /stats. "
            "Так видишь откуда лиды и кто приносит оплаты."
        )
        return

    if not _SOURCE_RE.match(source):
        await message.answer(
            "❌ Источник должен быть до 30 символов, только латиница, "
            "цифры, <code>_</code> или <code>-</code>.\n\n"
            "Например: <code>partner_anna</code>, <code>tg-channel</code>, "
            "<code>blog_post1</code>"
        )
        return

    me = await bot.get_me()
    link = f"https://t.me/{me.username}?start=ref_{message.from_user.id}_{source}"

    await message.answer(
        f"🔗 <b>Ссылка для источника</b> <code>{source}</code>\n\n"
        f"<code>{link}</code>\n\n"
        f"<i>Скопируй и раздавай — каждый клик/активация будет считаться "
        f"под этим источником в /stats.</i>"
    )


@router.message(Command("stats"))
async def cmd_stats(message: Message) -> None:
    """Воронка по UTM-источникам для админа."""
    if not _is_admin(message.from_user.id):
        return

    stats = await get_source_stats(message.from_user.id)

    if not stats:
        await message.answer(
            "📊 <b>Статистика рефералов</b>\n\n"
            "Пока никого не приглашено.\n\n"
            "Создай UTM-ссылку через /utm и начни раздавать."
        )
        return

    total_invited = sum(row["invited"] for row in stats)
    total_rewarded = sum(row["rewarded"] for row in stats)
    total_bonus = sum(row["bonus_days"] for row in stats)

    lines = [
        "📊 <b>Статистика рефералов</b>",
        "",
        f"Всего приглашено: <b>{total_invited}</b>",
        f"Активировали: <b>{total_rewarded}</b>",
        f"Бонус-дней получено: <b>{total_bonus}</b>",
        "",
        "<b>По источникам:</b>",
    ]

    for row in stats:
        src = row["source"]
        invited = row["invited"]
        rewarded = row["rewarded"]
        bonus = row["bonus_days"]
        conv = f"{(rewarded / invited * 100):.0f}%" if invited else "—"

        lines.append(
            f"\n<code>{src}</code>"
            f"\n  Кликов: {invited} · Активировали: {rewarded} ({conv})"
            f"\n  Бонус-дней: {bonus}"
        )

    text = "\n".join(lines)
    if len(text) > 4000:
        text = text[:4000] + "\n…(обрезано)"
    await message.answer(text)
