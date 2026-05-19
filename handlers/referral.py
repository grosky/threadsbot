"""Реферальная программа: ссылка-приглашение + статистика + бонусы + UTM для админа."""
from __future__ import annotations

import logging
import re

from aiogram import Bot, F, Router
from aiogram.filters import Command, CommandObject
from aiogram.types import CallbackQuery, Message

from config import config
from database import (
    PARTNER_COMMISSION_PERCENT,
    REFERRAL_BONUS_DAYS,
    get_payments_detailed,
    get_referral_stats,
    get_revenue_stats,
    get_source_stats,
)

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
        f"За каждого друга, который оформит подписку, "
        f"тебе +<b>{REFERRAL_BONUS_DAYS} дней</b>.\n\n"
        "<b>Твоя ссылка:</b>\n"
        f"<code>{link}</code>\n\n"
        "<i>Скопируй и отправь любому — твой друг просто жмёт «Старт» в боте.</i>\n\n"
        "<b>Твоя статистика:</b>\n"
        f"• Приглашено: <b>{stats['invited']}</b>\n"
        f"• Оформили подписку: <b>{stats['rewarded']}</b>\n"
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


def _format_rub(kopecks: int) -> str:
    """5900 копеек → '59 ₽', 59000 → '590 ₽'."""
    rub = (kopecks or 0) / 100
    if rub == int(rub):
        return f"{int(rub):,}".replace(",", " ") + " ₽"
    return f"{rub:.2f}".replace(".", ",") + " ₽"


def _format_user(p: dict) -> str:
    """Формирует строку '@username' или 'Имя (ID)' или просто 'ID' для платежа."""
    username = p.get("username")
    first_name = p.get("first_name")
    user_id = p.get("user_id")

    if username:
        return f"@{username}"
    if first_name:
        return f"{first_name} (<code>{user_id}</code>)"
    return f"<code>{user_id}</code>"


def _format_date(iso_str: str) -> str:
    """'2026-05-19T14:07:..' → '19.05'."""
    if not iso_str:
        return "—"
    from datetime import datetime
    try:
        dt = datetime.fromisoformat(iso_str)
        return dt.strftime("%d.%m")
    except (ValueError, TypeError):
        return "—"


@router.message(Command("stats"))
async def cmd_stats(message: Message) -> None:
    """Партнёрская воронка для админа: клики → оплаты → комиссия."""
    log.info(
        "cmd_stats called: user_id=%s admin_id=%s is_admin=%s",
        message.from_user.id, config.admin_telegram_id,
        message.from_user.id == config.admin_telegram_id,
    )
    if not _is_admin(message.from_user.id):
        await message.answer("⚠️ /stats — только для админа.")
        return

    funnel = await get_source_stats(message.from_user.id)
    revenue = await get_revenue_stats(message.from_user.id)
    payments_detailed = await get_payments_detailed(message.from_user.id)

    # Группируем платежи по source
    payments_by_source: dict[str, list[dict]] = {}
    for p in payments_detailed:
        payments_by_source.setdefault(p["source"], []).append(p)

    if not funnel:
        await message.answer(
            "📊 <b>Партнёрская статистика</b>\n\n"
            "Пока никого не приглашено.\n\n"
            "Создай UTM-ссылку через /utm и начни раздавать партнёрам."
        )
        return

    # Объединяем воронку + revenue по source
    revenue_by_source = {r["source"]: r for r in revenue}

    total_clicks = sum(row["invited"] for row in funnel)
    total_revenue = sum(
        r.get("revenue_kopecks", 0) for r in revenue_by_source.values()
    )
    total_payers = sum(
        r.get("payers", 0) for r in revenue_by_source.values()
    )
    total_commission = sum(
        r.get("commission_kopecks", 0) for r in revenue_by_source.values()
    )

    overall_conv = (
        f"{(total_payers / total_clicks * 100):.1f}%"
        if total_clicks else "—"
    )

    lines = [
        f"📊 <b>Партнёрская статистика</b>",
        "",
        f"<b>Общий итог:</b>",
        f"Кликов: <b>{total_clicks}</b>",
        f"Оплатили: <b>{total_payers}</b> ({overall_conv})",
        f"Доход: <b>{_format_rub(total_revenue)}</b>",
        f"Комиссия партнёрам ({PARTNER_COMMISSION_PERCENT}%): "
        f"<b>{_format_rub(total_commission)}</b>",
        "",
        "<b>По источникам:</b>",
    ]

    for row in funnel:
        src = row["source"]
        clicks = row["invited"]
        rev = revenue_by_source.get(src, {})
        payers = rev.get("payers", 0)
        payments = rev.get("payments_count", 0)
        rev_kop = rev.get("revenue_kopecks", 0)
        comm_kop = rev.get("commission_kopecks", 0)

        conv = f"{(payers / clicks * 100):.1f}%" if clicks else "—"

        block = [
            f"\n<b><code>{src}</code></b>",
            f"  Кликов: {clicks}",
            f"  Оплатили: {payers} ({conv})",
        ]
        if payments > 1:
            block.append(f"  Платежей всего: {payments}")
        if rev_kop:
            block.append(f"  Доход: {_format_rub(rev_kop)}")
            block.append(f"  Комиссия партнёру: {_format_rub(comm_kop)}")

        # Список конкретных платежей по этому источнику (макс 10)
        payments_for_source = payments_by_source.get(src, [])
        if payments_for_source:
            block.append("  <i>Платежи:</i>")
            for p in payments_for_source[:10]:
                user_label = _format_user(p)
                amount = _format_rub(p.get("amount_kopecks", 0))
                date = _format_date(p.get("created_at", ""))
                block.append(f"  • {user_label} — {amount} ({date})")
            if len(payments_for_source) > 10:
                block.append(f"  …и ещё {len(payments_for_source) - 10}")

        lines.extend(block)

    text = "\n".join(lines)
    if len(text) > 4000:
        text = text[:4000] + "\n…(обрезано)"
    await message.answer(text)
