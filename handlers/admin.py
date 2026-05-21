"""Админские команды: общая статистика по боту."""
from __future__ import annotations

import logging

from aiogram import F, Router
from aiogram.filters import Command
from aiogram.types import Message

from config import config
from database import get_admin_overview

router = Router()
log = logging.getLogger(__name__)


def _is_admin(user_id: int) -> bool:
    return user_id == config.admin_telegram_id


def _format_rub(kopecks: int) -> str:
    """5900 копеек → '59 ₽', 59000 → '590 ₽'."""
    rub = (kopecks or 0) / 100
    if rub == int(rub):
        return f"{int(rub):,}".replace(",", " ") + " ₽"
    return f"{rub:.2f}".replace(".", ",") + " ₽"


@router.message(Command("admin"))
async def cmd_admin(message: Message) -> None:
    """Общая сводка по боту: юзеры, активность, оплаты."""
    if not _is_admin(message.from_user.id):
        return  # тихо игнорим — обычные юзеры не знают про эту команду

    stats = await get_admin_overview()

    conv_trial_to_sub = (
        f"{(stats['with_active_sub'] / stats['used_free_trial'] * 100):.1f}%"
        if stats["used_free_trial"] else "—"
    )
    onboarding_rate = (
        f"{(stats['onboarded'] / stats['total_users'] * 100):.1f}%"
        if stats["total_users"] else "—"
    )

    lines = [
        "📊 <b>Админская сводка</b>",
        "",
        "<b>Юзеры</b>",
        f"Всего зарегистрировано: <b>{stats['total_users']}</b>",
        f"Прошли онбординг: <b>{stats['onboarded']}</b> ({onboarding_rate})",
        f"Использовали free trial: <b>{stats['used_free_trial']}</b>",
        f"Активные подписки сейчас: <b>{stats['with_active_sub']}</b>",
        f"Конверсия trial → подписка: <b>{conv_trial_to_sub}</b>",
        "",
        "<b>Активность</b>",
        f"Генерили за 24ч: <b>{stats['active_24h']}</b>",
        f"Генерили за 7 дней: <b>{stats['active_7d']}</b>",
        "",
        "<b>Генерации</b>",
        f"Сегодня: <b>{stats['gens_today']}</b>",
        f"За 7 дней: <b>{stats['gens_7d']}</b>",
        f"За 30 дней: <b>{stats['gens_30d']}</b>",
        "",
        "<b>Оплаты</b>",
        f"Всего платежей: <b>{stats['payments_count']}</b>",
        f"Суммарный доход: <b>{_format_rub(stats['revenue_total_kopecks'])}</b>",
        "",
        "<b>Threads</b>",
        f"Подключённых аккаунтов: <b>{stats['threads_connected']}</b>",
    ]

    await message.answer("\n".join(lines))
