"""Брейншторм идей для постов.

Юзер тапает «💡 Идеи для постов» → бот генерит 10 свежих идей под нишу.
Каждая идея — отдельная кнопка. Тап → идея становится темой в обычном
флоу генерации (длина → 3 варианта в 3 разных форматах).
"""
from __future__ import annotations

import html
import logging
import time as _time

from aiogram import Bot, F, Router
from aiogram.fsm.context import FSMContext
from aiogram.types import (
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
)

from database import (
    can_generate_today,
    get_pending_post,
    get_user,
    has_access,
    is_subscription_active,
    save_pending_post,
)
from gemini_service import generate_ideas

from .generation import GenerateStates, length_keyboard

router = Router()
log = logging.getLogger(__name__)


@router.callback_query(F.data == "action:ideas")
async def start_ideas(callback: CallbackQuery, state: FSMContext) -> None:
    user_id = callback.from_user.id

    if not await is_subscription_active(user_id):
        await callback.answer()
        from database import can_use_free_trial as _can_trial
        from .generation import send_subscription_required
        if await _can_trial(user_id):
            await callback.message.answer(
                "🔓 <b>«Идеи для постов» доступны только по подписке.</b>\n\n"
                "Но у тебя есть <b>одна бесплатная генерация</b> — вернись в "
                "/menu → 📝 Создание → 🎁 «Сгенерить бесплатный пост»."
            )
        else:
            await send_subscription_required(callback.message, "Идеи для постов")
        return

    user = await get_user(user_id)
    if not user or not user.get("onboarding_complete"):
        await callback.answer()
        await callback.message.answer(
            "Сначала пройди онбординг — без него идеи будут общими. Запусти /start."
        )
        return

    await callback.answer()
    status = await callback.message.answer(
        "🧠 Генерю 10 идей под твою нишу... ~10 секунд"
    )

    try:
        ideas = await generate_ideas(user)
    except Exception as e:
        log.exception("Ideas generation failed for user %s", user_id)
        await status.edit_text(
            f"❌ Не получилось. Попробуй ещё раз.\n\n"
            f"<code>{html.escape(type(e).__name__)}: {html.escape(str(e))[:200]}</code>"
        )
        return

    if not ideas:
        await status.edit_text("❌ Gemini вернул пустой список идей. Попробуй ещё раз.")
        return

    await status.delete()

    # Сохраняем каждую идею в БД с уникальным ключом, callback ссылается на этот ключ.
    # 24h TTL — потом протухнет автоматически.
    batch = int(_time.time())
    rows = []
    text_lines = ["💡 <b>10 идей для постов под твою нишу</b>", ""]

    for idx, idea in enumerate(ideas[:10], start=1):
        text = str(idea.get("text", "")).strip()
        if not text:
            continue

        idea_key = f"idea_{batch}_{idx}"
        await save_pending_post(user_id, idea_key, text)

        # Текст идеи в сообщении (для контекста)
        text_lines.append(f"<b>{idx}.</b> {html.escape(text)}")

        # Кнопка — компактно, только номер
        rows.append([InlineKeyboardButton(
            text=f"✍️ Развить идею {idx}",
            callback_data=f"idea:{batch}:{idx}",
        )])

    text_lines.append("")
    text_lines.append(
        "<i>Тапни любую кнопку чтобы развернуть идею в пост.</i>"
    )

    kb = InlineKeyboardMarkup(inline_keyboard=rows)
    text = "\n".join(text_lines)
    if len(text) > 4000:
        text = text[:4000] + "\n…(обрезано)"
    await callback.message.answer(text, reply_markup=kb)


@router.callback_query(F.data.startswith("idea:"))
async def use_idea(callback: CallbackQuery, state: FSMContext) -> None:
    """Юзер тапнул на идею — берём её текст и стартуем генерацию через флоу длины."""
    user_id = callback.from_user.id
    parts = callback.data.split(":")
    if len(parts) != 3:
        await callback.answer("Битый callback", show_alert=True)
        return

    _, batch, idx = parts
    idea_key = f"idea_{batch}_{idx}"
    idea_text = await get_pending_post(user_id, idea_key)

    if not idea_text:
        await callback.answer(
            "Идея потеряна (старше 24 часов). Запроси новые через «💡 Идеи».",
            show_alert=True,
        )
        return

    if not await is_subscription_active(user_id):
        from .generation import send_subscription_required
        await callback.answer()
        await send_subscription_required(callback.message, "Развитие идей в посты")
        return

    can_gen, _ = await can_generate_today(user_id)
    if not can_gen:
        await callback.answer("Лимит исчерпан, возвращайся завтра", show_alert=True)
        return

    # Сохраняем тему в FSM и идём прямо на шаг выбора длины
    await state.update_data(topic=idea_text)
    await state.set_state(GenerateStates.choosing_length)

    await callback.answer()
    await callback.message.answer(
        f"✍️ <b>Идея:</b>\n<i>{html.escape(idea_text)}</i>\n\n"
        "Выбери длину:",
        reply_markup=length_keyboard(),
    )


