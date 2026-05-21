"""Админские команды: общая статистика по боту + управление партнёрами."""
from __future__ import annotations

import logging
import re

from aiogram import Bot, F, Router
from aiogram.exceptions import TelegramForbiddenError
from aiogram.filters import Command, CommandObject
from aiogram.types import Message

from config import config
from database import (
    add_partner_link,
    find_partner_by_source,
    find_user_by_username,
    get_admin_overview,
    get_user,
)
from viral_collector import collect_once

router = Router()
log = logging.getLogger(__name__)

# UTM-источник: латиница/цифры/_/-, до 30 символов (как в /utm)
_SOURCE_RE = re.compile(r"^[a-z0-9_\-]{1,30}$")


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


# ---------- /make_partner ----------

@router.message(Command("make_partner"))
async def cmd_make_partner(
    message: Message, command: CommandObject, bot: Bot
) -> None:
    """Регистрирует партнёра с собственной UTM-ссылкой.

    Использование: /make_partner <source> <@username | user_id>
    Пример: /make_partner anna_blog @anna_2024
    Пример: /make_partner partner_misha 123456789
    """
    if not _is_admin(message.from_user.id):
        return

    args = (command.args or "").strip()
    if not args:
        await message.answer(
            "🤝 <b>Регистрация партнёра</b>\n\n"
            "Использование: <code>/make_partner &lt;source&gt; &lt;@username | user_id&gt;</code>\n\n"
            "Примеры:\n"
            "<code>/make_partner anna_blog @anna_2024</code>\n"
            "<code>/make_partner partner_misha 123456789</code>\n\n"
            "Партнёр должен сначала запустить бот через /start "
            "чтобы попасть в БД."
        )
        return

    parts = args.split(maxsplit=1)
    if len(parts) != 2:
        await message.answer(
            "❌ Нужны 2 аргумента: source и @username/ID.\n\n"
            "Пример: <code>/make_partner anna_blog @anna_2024</code>"
        )
        return

    source, target = parts[0].lower(), parts[1].strip()

    if not _SOURCE_RE.match(source):
        await message.answer(
            "❌ Source должен быть до 30 символов: латиница, цифры, "
            "<code>_</code> или <code>-</code>.\n\n"
            "Например: <code>anna_blog</code>, <code>partner-misha</code>"
        )
        return

    existing = await find_partner_by_source(source)
    if existing:
        await message.answer(
            f"❌ Source <code>{source}</code> уже занят. "
            f"Привязан к партнёру <code>{existing['partner_telegram_id']}</code>."
        )
        return

    # Резолв партнёра: либо @username, либо user_id
    partner = None
    if target.startswith("@") or not target.lstrip("-").isdigit():
        partner = await find_user_by_username(target)
        resolve_hint = f"username «{target}»"
    else:
        try:
            partner_id = int(target)
        except ValueError:
            partner_id = None
        if partner_id:
            partner = await get_user(partner_id)
        resolve_hint = f"ID {target}"

    if not partner:
        await message.answer(
            f"❌ Партнёр {resolve_hint} не найден в БД.\n\n"
            f"Попроси его запустить бот через /start — после этого попробуй снова."
        )
        return

    partner_id = int(partner["telegram_id"])

    me = await bot.get_me()
    link = f"https://t.me/{me.username}?start=ref_{partner_id}_{source}"

    await add_partner_link(partner_id, source, link)

    # Уведомляем партнёра
    partner_notified = False
    try:
        await bot.send_message(
            chat_id=partner_id,
            text=(
                "🤝 <b>Тебя добавили партнёром Lazy Threads!</b>\n\n"
                "За каждого приглашённого юзера, который оформит подписку, "
                f"ты получаешь <b>30% от платежа</b>.\n\n"
                f"<b>Твоя ссылка</b> (источник <code>{source}</code>):\n"
                f"<code>{link}</code>\n\n"
                "Раздавай её — каждый клик и оплата будут отслеживаться. "
                "Статистика — команда /stats."
            ),
        )
        partner_notified = True
    except TelegramForbiddenError:
        log.warning("Cannot notify partner %s — they blocked the bot", partner_id)
    except Exception:
        log.exception("Failed to notify partner %s", partner_id)

    # Подтверждение админу
    notify_status = (
        "✅ Партнёр уведомлён в личке."
        if partner_notified
        else "⚠️ Партнёр не уведомлён — он не открывал чат с ботом. "
             "Скинь ему ссылку вручную."
    )

    username_str = f" (@{partner['username']})" if partner.get("username") else ""
    await message.answer(
        f"🤝 <b>Партнёр зарегистрирован</b>\n\n"
        f"ID: <code>{partner_id}</code>{username_str}\n"
        f"Source: <code>{source}</code>\n\n"
        f"<b>Ссылка:</b>\n<code>{link}</code>\n\n"
        f"{notify_status}"
    )


# ---------- /refresh_trends ----------

@router.message(Command("refresh_trends"))
async def cmd_refresh_trends(message: Message) -> None:
    """Форс-обновление кэша виральных постов. Только для админа."""
    if not _is_admin(message.from_user.id):
        return

    status = await message.answer(
        "🧠 Запускаю сбор виральных постов... "
        "Это займёт ~30-60 секунд (зависит от числа ключевых слов)."
    )
    try:
        stats = await collect_once()
    except Exception as e:
        log.exception("Manual viral collect failed")
        await status.edit_text(
            f"❌ Не получилось: <code>{type(e).__name__}: {str(e)[:200]}</code>"
        )
        return

    await status.edit_text(
        "✅ <b>Сбор завершён</b>\n\n"
        f"Ключевых слов обработано: <b>{stats['keywords_done']}</b>\n"
        f"Постов сохранено/обновлено: <b>{stats['posts_saved']}</b>\n"
        f"Ошибок: <b>{stats['errors']}</b>\n\n"
        "Проверь через /trends что появилось."
    )
