"""Голосовой сторителлинг: voice → живой пост через Gemini audio."""
from __future__ import annotations

import html
import io
import logging

from aiogram import Bot, F, Router
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, Message

from achievements import (
    GENERATION_RELATED,
    STREAK_RELATED,
    VOICE_RELATED,
    check_and_award,
)
from config import DAILY_LIMIT
from database import (
    can_generate_today,
    get_user,
    is_subscription_active,
    log_generation,
    touch_streak,
)
from gemini_service import generate_storytelling_from_voice

from .threads_connect import post_actions_keyboard, remember_post

router = Router()
log = logging.getLogger(__name__)

# Жёсткий потолок длительности голосового — защищает от случайных часовых аудио.
MAX_VOICE_SECONDS = 5 * 60


class StorytellingStates(StatesGroup):
    waiting_for_voice = State()


@router.callback_query(F.data == "action:storytelling")
async def start_storytelling(callback: CallbackQuery, state: FSMContext) -> None:
    user_id = callback.from_user.id

    if not await is_subscription_active(user_id):
        await callback.answer()
        await callback.message.answer(
            "❌ Подписка неактивна. Активируй промокод через /start."
        )
        return

    can_gen, used = await can_generate_today(user_id)
    if not can_gen:
        await callback.answer()
        await callback.message.answer(
            f"⏳ Дневной лимит исчерпан ({DAILY_LIMIT}/{DAILY_LIMIT}).\n\n"
            "Возвращайся завтра — счётчик обнуляется в 00:00 UTC."
        )
        return

    user = await get_user(user_id)
    if not user or not user.get("onboarding_complete"):
        await callback.answer()
        await callback.message.answer(
            "Сначала пройди онбординг — пост будет учитывать твою нишу и ЦА. "
            "Запусти /start."
        )
        return

    await callback.answer()
    await callback.message.answer(
        "🎙 <b>Голосовой сторителлинг</b>\n\n"
        "Запиши голосовое — расскажи историю, идею или случай как другу за кофе. "
        "Не структурируй, не редактируй — просто говори.\n\n"
        "Я услышу и соберу из этого живой пост по правилам сторителлинга:\n"
        "• цепкий хук в первой строке\n"
        "• конкретные детали (имена, цифры, места)\n"
        "• подробная боль, краткое решение\n"
        "• без инфостиля и буллетов\n\n"
        f"<i>Ограничение: до {MAX_VOICE_SECONDS // 60} минут. "
        f"Использует 1 из {DAILY_LIMIT - used} оставшихся генераций сегодня.</i>\n\n"
        "<i>Отмена — /menu</i>"
    )
    await state.set_state(StorytellingStates.waiting_for_voice)


@router.message(StorytellingStates.waiting_for_voice, F.voice)
async def voice_received(message: Message, state: FSMContext, bot: Bot) -> None:
    user_id = message.from_user.id
    voice = message.voice

    if voice.duration and voice.duration > MAX_VOICE_SECONDS:
        await message.answer(
            f"Голосовое слишком длинное ({voice.duration}с). "
            f"Максимум {MAX_VOICE_SECONDS}с — попробуй ужать главную мысль."
        )
        return

    # Двойная проверка лимита
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

    status_msg = await message.answer("🧠 Слушаю голосовое и пишу пост... ~15-25 секунд")

    try:
        buf = io.BytesIO()
        await bot.download(voice, destination=buf)
        audio_bytes = buf.getvalue()
    except Exception as e:
        log.exception("Voice download failed for user %s", user_id)
        await status_msg.edit_text(
            "❌ Не удалось скачать голосовое. Попробуй ещё раз.\n\n"
            f"<code>{html.escape(type(e).__name__)}</code>"
        )
        await state.clear()
        return

    mime = voice.mime_type or "audio/ogg"

    try:
        result = await generate_storytelling_from_voice(profile, audio_bytes, mime)
    except Exception as e:
        log.exception("Storytelling generation failed for user %s", user_id)
        await status_msg.edit_text(
            "❌ Gemini не справился. Попробуй ещё раз через минуту.\n\n"
            f"<code>{html.escape(type(e).__name__)}: {html.escape(str(e))[:200]}</code>"
        )
        await state.clear()
        return

    await log_generation(user_id, "storytelling_voice", None)
    await status_msg.delete()

    heard = html.escape(str(result.get("heard", "—")))
    hook = html.escape(str(result.get("hook_line", "—")))
    raw_post = str(result.get("post", "—"))
    post = html.escape(raw_post)

    await message.answer(
        f"🎧 <b>Что я услышал:</b>\n<i>{heard}</i>\n\n"
        f"🪝 <b>Хук:</b>\n<i>{hook}</i>"
    )

    import time as _time
    post_key = f"v{int(_time.time())}"
    await remember_post(user_id, post_key, raw_post)

    full = f"📝 <b>Пост</b>\n\n{post}"
    if len(full) > 4000:
        full = full[:4000] + "\n\n…(обрезано)"
    await message.answer(full, reply_markup=post_actions_keyboard(post_key))

    # Стрик + ачивки
    await touch_streak(user_id)
    await check_and_award(
        user_id, bot,
        codes=GENERATION_RELATED + VOICE_RELATED + STREAK_RELATED,
    )

    _, used_after = await can_generate_today(user_id)
    remaining = max(0, DAILY_LIMIT - used_after)
    await message.answer(
        f"✅ Готово. Осталось сегодня: <b>{remaining}/{DAILY_LIMIT}</b>\n\n"
        f"Хочешь ещё — запиши новое голосовое или /menu."
    )
    await state.clear()


@router.message(StorytellingStates.waiting_for_voice, F.audio)
async def audio_file_received(message: Message, state: FSMContext, bot: Bot) -> None:
    """Загруженный аудиофайл (audio) — тоже принимаем, обрабатываем как voice."""
    audio = message.audio
    if audio.duration and audio.duration > MAX_VOICE_SECONDS:
        await message.answer(
            f"Аудио слишком длинное ({audio.duration}с). "
            f"Максимум {MAX_VOICE_SECONDS}с."
        )
        return

    user_id = message.from_user.id
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

    status_msg = await message.answer("🧠 Слушаю аудио и пишу пост... ~15-25 секунд")
    try:
        buf = io.BytesIO()
        await bot.download(audio, destination=buf)
        audio_bytes = buf.getvalue()
        mime = audio.mime_type or "audio/mpeg"
        result = await generate_storytelling_from_voice(profile, audio_bytes, mime)
    except Exception as e:
        log.exception("Storytelling (audio file) failed for user %s", user_id)
        await status_msg.edit_text(
            "❌ Не удалось обработать аудио.\n\n"
            f"<code>{html.escape(type(e).__name__)}: {html.escape(str(e))[:200]}</code>"
        )
        await state.clear()
        return

    await log_generation(user_id, "storytelling_audio", None)
    await status_msg.delete()

    heard = html.escape(str(result.get("heard", "—")))
    hook = html.escape(str(result.get("hook_line", "—")))
    raw_post = str(result.get("post", "—"))
    post = html.escape(raw_post)

    await message.answer(
        f"<b>🎧 Что я услышал:</b>\n<i>{heard}</i>\n\n"
        f"<b>🪝 Хук:</b>\n<i>{hook}</i>"
    )

    import time as _time
    post_key = f"a{int(_time.time())}"
    await remember_post(user_id, post_key, raw_post)

    full = f"<b>📝 Пост</b>\n\n━━━━━━━━━━━━━━━━━\n\n{post}"
    if len(full) > 4000:
        full = full[:4000] + "\n\n…(обрезано)"
    await message.answer(full, reply_markup=post_actions_keyboard(post_key))

    _, used_after = await can_generate_today(user_id)
    remaining = max(0, DAILY_LIMIT - used_after)
    await message.answer(
        f"✅ Готово. Осталось сегодня: <b>{remaining}/{DAILY_LIMIT}</b>"
    )
    await state.clear()


@router.message(StorytellingStates.waiting_for_voice)
async def wrong_input(message: Message) -> None:
    """Текст/фото/документ в режиме ожидания голосового."""
    await message.answer(
        "Жду <b>голосовое сообщение</b> 🎙\n\n"
        "Записать: зажми микрофон в Telegram. Если передумал — /menu."
    )
