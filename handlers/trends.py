"""Команда /trends — показывает топ виральных постов в Threads по категориям.

Данные предсобираются фоновым модулем viral_collector. Юзер выбирает
категорию → получает топ-5 постов со ссылками на оригиналы.
"""
from __future__ import annotations

import html
import logging
from datetime import datetime, timedelta

from aiogram import F, Router
from aiogram.filters import Command
from aiogram.types import (
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
)

from database import (
    get_viral_keywords_available,
    get_viral_posts_by_keyword,
)
from viral_collector import KEYWORDS

router = Router()
log = logging.getLogger(__name__)

# Сколько постов показываем за раз
POSTS_PER_CATEGORY = 5


def _trends_keyboard(available_keywords: list[str]) -> InlineKeyboardMarkup:
    """Клавиатура с категориями. Показываем только те где есть посты."""
    rows: list[list[InlineKeyboardButton]] = []
    row: list[InlineKeyboardButton] = []
    for kw in available_keywords:
        # Берём первое слово/часть для компактности кнопки
        label = kw if len(kw) <= 20 else kw[:17] + "…"
        row.append(InlineKeyboardButton(
            text=f"🔥 {label}",
            callback_data=f"trends:kw:{kw}",
        ))
        if len(row) == 2:
            rows.append(row)
            row = []
    if row:
        rows.append(row)
    return InlineKeyboardMarkup(inline_keyboard=rows)


def _format_post(post: dict, idx: int) -> str:
    """Один пост в выдаче — превью текста + цифры + ссылка."""
    text = (post.get("text") or "").strip()
    # Превью первых 280 символов, на разрыве строки если есть
    if len(text) > 280:
        cut = text[:280].rsplit("\n", 1)[0] or text[:280]
        preview = cut + "…"
    else:
        preview = text

    username = post.get("username") or "—"
    replies = int(post.get("replies_count") or 0)
    permalink = post.get("permalink") or ""

    posted_str = ""
    if post.get("posted_at"):
        try:
            dt = datetime.fromisoformat(str(post["posted_at"]).replace("Z", "+00:00"))
            delta = datetime.utcnow() - dt.replace(tzinfo=None)
            if delta.days >= 1:
                posted_str = f" · {delta.days} дн назад"
            else:
                hours = max(1, delta.seconds // 3600)
                posted_str = f" · {hours}ч назад"
        except (ValueError, TypeError):
            pass

    lines = [
        f"<b>{idx}.</b> @{html.escape(username)} · 💬 {replies}{posted_str}",
        "",
        html.escape(preview),
    ]
    if permalink:
        lines.append("")
        lines.append(f"<a href=\"{html.escape(permalink)}\">→ Открыть в Threads</a>")
    return "\n".join(lines)


@router.message(Command("trends"))
async def cmd_trends(message: Message) -> None:
    """Показывает категории с виральными постами."""
    await _show_categories(message)


@router.callback_query(F.data == "action:trends")
async def show_trends_action(callback: CallbackQuery) -> None:
    await callback.answer()
    await _show_categories(callback.message)


async def _show_categories(message: Message) -> None:
    available = await get_viral_keywords_available()
    if not available:
        await message.answer(
            "🔥 <b>Что залетает в Threads</b>\n\n"
            "База ещё собирается — загляни позже. "
            "Бот раз в 12 часов подтягивает свежие вирусные посты по нишам.\n\n"
            "<i>Если только что задеплоено — первый сбор запустится "
            "в течение пары минут.</i>"
        )
        return

    await message.answer(
        "🔥 <b>Что залетает в Threads</b>\n\n"
        "Выбери категорию — покажу топ-5 свежих веток по числу обсуждений "
        "за последние 7 дней. Можно открыть оригинал и посмотреть как написан хук.",
        reply_markup=_trends_keyboard(available),
    )


@router.callback_query(F.data.startswith("trends:kw:"))
async def show_category(callback: CallbackQuery) -> None:
    keyword = callback.data.split(":", 2)[2]
    await callback.answer()

    posts = await get_viral_posts_by_keyword(
        keyword, limit=POSTS_PER_CATEGORY, max_age_days=7
    )

    if not posts:
        await callback.message.answer(
            f"По категории <b>{html.escape(keyword)}</b> пока нет свежих "
            "вирусных веток. Попробуй другую."
        )
        return

    header = (
        f"🔥 <b>Топ-{len(posts)} в категории «{html.escape(keyword)}»</b>\n"
        f"<i>Отсортировано по числу обсуждений</i>"
    )
    await callback.message.answer(header)

    for idx, post in enumerate(posts, 1):
        text = _format_post(post, idx)
        # Telegram-лимит сообщения ~4096, наши посты ~500 символов с обёрткой
        if len(text) > 4000:
            text = text[:4000] + "\n…(обрезано)"
        await callback.message.answer(
            text,
            disable_web_page_preview=False,
        )

    await callback.message.answer(
        "<i>💡 Выбери идею и сгенерь свой пост по этой теме через /menu → Создание.</i>"
    )
