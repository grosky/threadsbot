"""Генерация постов через Gemini."""
from __future__ import annotations

import html
import logging

from aiogram import F, Router
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import (
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
)

from config import DAILY_LIMIT
from database import (
    can_generate_today,
    get_user,
    is_subscription_active,
    log_generation,
)
from gemini_service import generate_posts
from prompts import FORMAT_OPTIONS

from .threads_connect import publish_button, remember_post

router = Router()
log = logging.getLogger(__name__)


class GenerateStates(StatesGroup):
    choosing_format = State()
    entering_topic = State()


def formats_keyboard() -> InlineKeyboardMarkup:
    rows = [
        [InlineKeyboardButton(text=label, callback_data=f"fmt:{key}")]
        for key, label in FORMAT_OPTIONS.items()
    ]
    rows.append([InlineKeyboardButton(text="❌ Отмена", callback_data="fmt:cancel")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def topic_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="🎲 Удиви меня", callback_data="topic:surprise")],
            [InlineKeyboardButton(text="❌ Отмена", callback_data="topic:cancel")],
        ]
    )


@router.callback_query(F.data == "action:generate")
async def start_generation(callback: CallbackQuery, state: FSMContext) -> None:
    user_id = callback.from_user.id

    # 1. Подписка активна?
    if not await is_subscription_active(user_id):
        await callback.answer()
        await callback.message.answer(
            "❌ Подписка неактивна. Активируй промокод через /start."
        )
        return

    # 2. Лимит на сегодня?
    can_gen, used = await can_generate_today(user_id)
    if not can_gen:
        await callback.answer()
        await callback.message.answer(
            f"⏳ Дневной лимит исчерпан ({DAILY_LIMIT}/{DAILY_LIMIT}).\n\n"
            "Возвращайся завтра — счётчик обнуляется в 00:00 UTC."
        )
        return

    # 3. Профиль заполнен?
    user = await get_user(user_id)
    if not user or not user.get("onboarding_complete"):
        await callback.answer()
        await callback.message.answer(
            "Сначала нужно заполнить профиль. Запусти /start."
        )
        return

    await callback.answer()
    await callback.message.answer(
        f"<b>Выбери формат поста</b>\n\n"
        f"Осталось сегодня: {DAILY_LIMIT - used}/{DAILY_LIMIT}",
        reply_markup=formats_keyboard(),
    )
    await state.set_state(GenerateStates.choosing_format)


@router.callback_query(GenerateStates.choosing_format, F.data.startswith("fmt:"))
async def format_selected(callback: CallbackQuery, state: FSMContext) -> None:
    fmt = callback.data.split(":", 1)[1]

    if fmt == "cancel":
        await state.clear()
        await callback.answer("Отменено")
        await callback.message.edit_reply_markup(reply_markup=None)
        return

    if fmt not in FORMAT_OPTIONS:
        await callback.answer("Неизвестный формат", show_alert=True)
        return

    await state.update_data(format=fmt)
    await callback.answer()
    await callback.message.edit_reply_markup(reply_markup=None)
    await callback.message.answer(
        f"<b>Формат:</b> {FORMAT_OPTIONS[fmt]}\n\n"
        "Напиши тему поста или жми «Удиви меня» — Gemini сам подберёт угол.",
        reply_markup=topic_keyboard(),
    )
    await state.set_state(GenerateStates.entering_topic)


@router.callback_query(GenerateStates.entering_topic, F.data == "topic:surprise")
async def topic_surprise(callback: CallbackQuery, state: FSMContext) -> None:
    await callback.answer()
    await callback.message.edit_reply_markup(reply_markup=None)
    data = await state.get_data()
    await _do_generate(
        callback.message, callback.from_user.id, data["format"], None, state
    )


@router.callback_query(GenerateStates.entering_topic, F.data == "topic:cancel")
async def topic_cancel(callback: CallbackQuery, state: FSMContext) -> None:
    await state.clear()
    await callback.answer("Отменено")
    await callback.message.edit_reply_markup(reply_markup=None)


@router.message(GenerateStates.entering_topic, F.text)
async def topic_entered(message: Message, state: FSMContext) -> None:
    data = await state.get_data()
    topic = (message.text or "").strip()
    await _do_generate(message, message.from_user.id, data["format"], topic, state)


async def _do_generate(
    message: Message,
    user_id: int,
    format_name: str,
    topic: str | None,
    state: FSMContext,
) -> None:
    """Сама генерация. Использует Gemini и шлёт результаты юзеру."""
    # Двойная проверка лимита (race-condition при параллельных нажатиях)
    can_gen, _ = await can_generate_today(user_id)
    if not can_gen:
        await message.answer("⏳ Лимит исчерпан, возвращайся завтра.")
        await state.clear()
        return

    profile = await get_user(user_id)
    if not profile:
        await message.answer("Профиль не найден. Запусти /start.")
        await state.clear()
        return

    status_msg = await message.answer("🧠 Gemini генерит варианты... ~10-15 секунд")

    try:
        variants = await generate_posts(profile, format_name, topic)
    except Exception as e:
        log.exception("Generation failed for user %s", user_id)
        await status_msg.edit_text(
            "❌ Что-то пошло не так. Попробуй ещё раз через минуту.\n\n"
            f"<code>{html.escape(type(e).__name__)}: {html.escape(str(e))[:200]}</code>"
        )
        await state.clear()
        return

    await log_generation(user_id, format_name, topic)
    await status_msg.delete()

    # Уникальный префикс на это сообщение (timestamp), чтобы post_key не конфликтовал
    # с предыдущими генерациями того же юзера.
    import time as _time
    batch = int(_time.time())

    # Каждый вариант — отдельным сообщением, текст поста экранируем
    for v in variants:
        raw_post = str(v.get("post", ""))
        safe_post = html.escape(raw_post)
        variant_id = v.get("id", "?")
        header = (
            f"<b>Вариант {variant_id}</b> · "
            f"формула {v.get('hook_formula', '?')} · "
            f"<i>{html.escape(str(v.get('angle_technique', '—')))}</i>"
        )
        # Telegram limit = 4096 chars. Пост обычно влезает, но на всякий — обрезаем.
        full_text = f"{header}\n\n━━━━━━━━━━━━━━━━━\n\n{safe_post}"
        if len(full_text) > 4000:
            full_text = full_text[:4000] + "\n\n…(обрезано)"

        post_key = f"g{batch}_{variant_id}"
        remember_post(user_id, post_key, raw_post)

        await message.answer(full_text, reply_markup=publish_button(post_key))

    # Финальное сообщение со статусом лимита
    _, used_after = await can_generate_today(user_id)
    remaining = max(0, DAILY_LIMIT - used_after)
    await message.answer(
        f"✅ Готово. Осталось сегодня: <b>{remaining}/{DAILY_LIMIT}</b>\n\n"
        f"Жми /menu чтобы вернуться."
    )
    await state.clear()
