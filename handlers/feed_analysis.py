"""Разбор чужой ленты: юзер шлёт 3-10 постов, Gemini находит паттерны и даёт идеи."""
from __future__ import annotations

import html
import logging

from aiogram import F, Router
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import (
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
)

from database import (
    can_analyze_feed_today,
    get_user,
    is_subscription_active,
    log_feed_analysis,
)
from gemini_service import analyze_feed

router = Router()
log = logging.getLogger(__name__)

MIN_POSTS = 3
MAX_POSTS = 10
MIN_POST_LENGTH = 50  # символов — отсекаем «ок», «👍» и прочий мусор


class FeedAnalysisStates(StatesGroup):
    collecting_posts = State()


def _collecting_keyboard(count: int) -> InlineKeyboardMarkup:
    rows = []
    if count >= MIN_POSTS:
        rows.append([InlineKeyboardButton(
            text=f"✅ Готово, разобрать ({count})",
            callback_data="feed:analyze",
        )])
    rows.append([InlineKeyboardButton(text="❌ Отмена", callback_data="feed:cancel")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


@router.callback_query(F.data == "action:feed_analysis")
async def start_feed_analysis(callback: CallbackQuery, state: FSMContext) -> None:
    user_id = callback.from_user.id

    if not await is_subscription_active(user_id):
        from database import can_use_free_trial as _can_trial
        from .generation import send_subscription_required
        await callback.answer()
        if await _can_trial(user_id):
            await callback.message.answer(
                "🔓 <b>«Разбор чужих лент» доступен только по подписке.</b>\n\n"
                "Но у тебя есть <b>одна бесплатная генерация</b> — вернись в "
                "/menu → 📝 Создание → 🎁 «Сгенерить бесплатный пост»."
            )
        else:
            await send_subscription_required(callback.message, "Разбор чужих лент")
        return

    user = await get_user(user_id)
    if not user or not user.get("onboarding_complete"):
        await callback.answer()
        await callback.message.answer(
            "Сначала пройди онбординг — разбор адаптирует находки под твою нишу. "
            "Запусти /start."
        )
        return

    if not await can_analyze_feed_today(user_id):
        await callback.answer()
        await callback.message.answer(
            "⏳ Разбор ленты доступен раз в сутки. Возвращайся завтра "
            "(счётчик обнуляется в 00:00 UTC)."
        )
        return

    await callback.answer()
    await state.set_state(FeedAnalysisStates.collecting_posts)
    await state.update_data(posts=[])
    await callback.message.answer(
        "🔍 <b>Разбор чужой ленты</b>\n\n"
        f"Скинь от <b>{MIN_POSTS}</b> до <b>{MAX_POSTS}</b> постов одного автора — "
        "каждый отдельным сообщением (можно по очереди).\n\n"
        "Я найду паттерны (хуки, формулы, CTA), объясню что цепляет, "
        "и предложу 3 идеи постов <b>под твою нишу</b> на их механиках.\n\n"
        f"Минимум {MIN_POSTS} поста, дальше станет доступна кнопка «Готово».\n\n"
        "<i>Чтобы отменить — /menu</i>"
    )


@router.message(FeedAnalysisStates.collecting_posts, F.text & ~F.text.startswith("/"))
async def collect_post(message: Message, state: FSMContext) -> None:
    text = (message.text or "").strip()

    if len(text) < MIN_POST_LENGTH:
        await message.answer(
            f"Этот пост слишком короткий (нужно от {MIN_POST_LENGTH} символов). "
            "Скинь полный текст."
        )
        return

    data = await state.get_data()
    posts: list[str] = list(data.get("posts") or [])

    if len(posts) >= MAX_POSTS:
        await message.answer(
            f"Максимум {MAX_POSTS} постов. Жми «Готово» — разберу что уже есть."
        )
        return

    posts.append(text)
    await state.update_data(posts=posts)

    preview = text[:60].replace("\n", " ")
    if len(text) >= 60:
        preview += "…"

    await message.answer(
        f"✓ Принял <b>пост {len(posts)}/{MAX_POSTS}</b>: <i>{html.escape(preview)}</i>\n\n"
        + (
            "Кидай следующий или жми «Готово»."
            if len(posts) >= MIN_POSTS
            else f"Ещё минимум {MIN_POSTS - len(posts)} — и появится кнопка «Готово»."
        ),
        reply_markup=_collecting_keyboard(len(posts)),
    )


@router.callback_query(FeedAnalysisStates.collecting_posts, F.data == "feed:cancel")
async def cancel(callback: CallbackQuery, state: FSMContext) -> None:
    await state.clear()
    await callback.answer("Отменено")
    await callback.message.edit_reply_markup(reply_markup=None)


@router.callback_query(FeedAnalysisStates.collecting_posts, F.data == "feed:analyze")
async def trigger_analysis(callback: CallbackQuery, state: FSMContext) -> None:
    user_id = callback.from_user.id
    data = await state.get_data()
    posts: list[str] = list(data.get("posts") or [])

    if len(posts) < MIN_POSTS:
        await callback.answer(
            f"Нужно хотя бы {MIN_POSTS} поста", show_alert=True
        )
        return

    # Двойная проверка лимита (race на параллельных запусках)
    if not await can_analyze_feed_today(user_id):
        await callback.answer("Лимит исчерпан", show_alert=True)
        await callback.message.answer("⏳ Лимит на сегодня уже исчерпан.")
        await state.clear()
        return

    profile = await get_user(user_id)
    if not profile:
        await callback.answer("Профиль не найден", show_alert=True)
        await state.clear()
        return

    await callback.answer()
    await callback.message.edit_reply_markup(reply_markup=None)
    status_msg = await callback.message.answer(
        f"🧠 Анализирую {len(posts)} постов... ~15-25 секунд"
    )

    try:
        report = await analyze_feed(profile, posts)
    except Exception as e:
        log.exception("Feed analysis failed for user %s", user_id)
        await status_msg.edit_text(
            "❌ Gemini не справился. Попробуй ещё раз через минуту.\n\n"
            f"<code>{html.escape(type(e).__name__)}: {html.escape(str(e))[:200]}</code>"
        )
        await state.clear()
        return

    await log_feed_analysis(user_id, len(posts))
    await status_msg.delete()

    # Один большой отчёт может не влезть в 4096 — режем на 2 сообщения
    for chunk in _format_report(report, posts):
        await callback.message.answer(chunk)

    await state.clear()


def _format_report(report: dict, posts: list[str]) -> list[str]:
    """JSON-отчёт → 1-2 сообщения для Telegram (с учётом лимита 4096)."""
    best_idx = int(report.get("best_post_index", 0))

    # --- Сообщение 1: общая картина + паттерны + разбор по постам ---
    parts = [
        "<b>🔍 Разбор ленты</b>",
        "",
        f"<b>Профиль ленты:</b> {html.escape(str(report.get('summary', '—')))}",
        "",
    ]

    if best_idx and 1 <= best_idx <= len(posts):
        parts.append(f"<b>👑 Самый сильный — пост #{best_idx}</b>")
        parts.append(html.escape(str(report.get("best_post_why", "—"))))
        parts.append("")

    patterns = report.get("patterns") or []
    if patterns:
        parts.append("<b>🧬 Паттерны автора:</b>")
        for p in patterns:
            name = html.escape(str(p.get("name", "—")))
            desc = html.escape(str(p.get("description", "—")))
            parts.append(f"• <b>{name}</b> — {desc}")
        parts.append("")

    breakdowns = report.get("post_breakdowns") or []
    if breakdowns:
        parts.append("<b>📋 По постам:</b>")
        for b in sorted(breakdowns, key=lambda x: int(x.get("index", 0))):
            idx = b.get("index", "?")
            preview = html.escape(str(b.get("preview", "—")))
            formula = html.escape(str(b.get("hook_formula", "—")))
            works = html.escape(str(b.get("what_works", "—")))
            parts.append(f"\n<b>#{idx}</b> <i>{preview}</i>")
            parts.append(f"  Хук: {formula}")
            parts.append(f"  Цепляет: {works}")
            weakness = b.get("weakness")
            if weakness and str(weakness).lower() not in ("null", "none", "—", ""):
                parts.append(f"  Провал: {html.escape(str(weakness))}")

    msg1 = "\n".join(parts)

    # --- Сообщение 2: идеи под нишу читателя ---
    ideas = report.get("ideas_for_you") or []
    idea_parts = ["<b>💡 3 идеи под твою нишу</b>", ""]
    for i, idea in enumerate(ideas, 1):
        title = html.escape(str(idea.get("title", "—")))
        hook = html.escape(str(idea.get("hook", "—")))
        why = html.escape(str(idea.get("explanation", "—")))
        idea_parts.append(f"<b>{i}. {title}</b>")
        idea_parts.append(f"<i>Хук:</i> «{hook}»")
        idea_parts.append(f"<i>Почему:</i> {why}")
        idea_parts.append("")
    idea_parts.append(
        "Хочешь развернуть в полноценный пост? /menu → «🎯 Сгенерить пост»."
    )
    msg2 = "\n".join(idea_parts)

    chunks = []
    for raw in (msg1, msg2):
        if len(raw) > 4000:
            raw = raw[:4000] + "\n\n…(обрезано)"
        chunks.append(raw)
    return chunks


@router.message(Command("menu"), FeedAnalysisStates.collecting_posts)
async def exit_via_menu(message: Message, state: FSMContext) -> None:
    """Команда /menu внутри FSM — выходим из состояния, дальше отработает menu-роутер."""
    await state.clear()
