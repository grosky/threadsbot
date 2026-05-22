"""Анализ упаковки профиля по скриншоту через Gemini Vision."""
from __future__ import annotations

import html
import io
import logging

from aiogram import Bot, F, Router
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, Message

from database import (
    can_analyze_profile_today,
    get_user,
    is_subscription_active,
    log_profile_analysis,
)
from gemini_service import analyze_profile

router = Router()
log = logging.getLogger(__name__)


class ProfileAnalysisStates(StatesGroup):
    waiting_for_screenshot = State()


@router.callback_query(F.data == "action:analyze_profile")
async def start_profile_analysis(callback: CallbackQuery, state: FSMContext) -> None:
    user_id = callback.from_user.id

    if not await is_subscription_active(user_id):
        from database import can_use_free_trial as _can_trial
        from .generation import send_subscription_required
        await callback.answer()
        if await _can_trial(user_id):
            await callback.message.answer(
                "🔓 <b>«Анализ профиля» доступен только по подписке.</b>\n\n"
                "Но у тебя есть <b>одна бесплатная генерация</b> — вернись в "
                "/menu → 📝 Создание → 🎁 «Сгенерить бесплатный пост»."
            )
        else:
            await send_subscription_required(callback.message, "Анализ профиля")
        return

    user = await get_user(user_id)
    if not user or not user.get("onboarding_complete"):
        await callback.answer()
        await callback.message.answer(
            "Сначала пройди онбординг — анализ профиля опирается на твою нишу и ЦА. "
            "Запусти /start."
        )
        return

    if not await can_analyze_profile_today(user_id):
        await callback.answer()
        await callback.message.answer(
            "⏳ Анализ профиля доступен раз в сутки. Возвращайся завтра "
            "(счётчик обнуляется в 00:00 UTC)."
        )
        return

    await callback.answer()
    await callback.message.answer(
        "📸 <b>Анализ упаковки профиля</b>\n\n"
        "Скинь скриншот шапки своего профиля в Threads: "
        "аватар, имя, био и (если влезает) первые посты.\n\n"
        "Я разберу упаковку по чек-листу и предложу конкретные правки.\n\n"
        "<i>Чтобы отменить — /menu</i>"
    )
    await state.set_state(ProfileAnalysisStates.waiting_for_screenshot)


@router.message(ProfileAnalysisStates.waiting_for_screenshot, F.photo)
async def screenshot_received(message: Message, state: FSMContext, bot: Bot) -> None:
    user_id = message.from_user.id

    # Двойная проверка лимита (race на параллельных нажатиях)
    if not await can_analyze_profile_today(user_id):
        await message.answer("⏳ Лимит исчерпан, возвращайся завтра.")
        await state.clear()
        return

    profile = await get_user(user_id)
    if not profile:
        await message.answer("Профиль не найден. Запусти /start.")
        await state.clear()
        return

    # Берём самое большое разрешение из присланных
    photo = message.photo[-1]
    status_msg = await message.answer("🧠 Изучаю упаковку... ~15-20 секунд")

    try:
        buf = io.BytesIO()
        await bot.download(photo, destination=buf)
        image_bytes = buf.getvalue()
    except Exception as e:
        log.exception("Photo download failed for user %s", user_id)
        await status_msg.edit_text(
            "❌ Не удалось скачать фото. Попробуй ещё раз.\n\n"
            f"<code>{html.escape(type(e).__name__)}</code>"
        )
        await state.clear()
        return

    try:
        report = await analyze_profile(profile, image_bytes, mime_type="image/jpeg")
    except Exception as e:
        log.exception("Profile analysis failed for user %s", user_id)
        await status_msg.edit_text(
            "❌ Gemini не справился. Попробуй ещё раз через минуту.\n\n"
            f"<code>{html.escape(type(e).__name__)}: {html.escape(str(e))[:200]}</code>"
        )
        await state.clear()
        return

    await log_profile_analysis(user_id, int(report.get("overall_score", 0)))
    await status_msg.delete()

    await message.answer(_format_report(report))
    await state.clear()


@router.message(ProfileAnalysisStates.waiting_for_screenshot)
async def wrong_input(message: Message) -> None:
    """Если юзер шлёт текст/документ вместо фото."""
    await message.answer(
        "Жду именно <b>фото</b> (скриншот шапки профиля). "
        "Если передумал — жми /menu."
    )


def _format_report(report: dict) -> str:
    """Превращает JSON-отчёт Gemini в красивое сообщение для Telegram."""
    score = int(report.get("overall_score", 0))
    score_bar = "🟩" * score + "⬜️" * (10 - score)

    parts = [
        f"<b>📊 Анализ упаковки: {score}/10</b>",
        score_bar,
        "",
        f"<b>Вердикт:</b> {html.escape(str(report.get('verdict', '—')))}",
        "",
    ]

    works = report.get("what_works") or []
    if works:
        parts.append("<b>✅ Что зашло:</b>")
        for item in works:
            parts.append(f"• {html.escape(str(item))}")
        parts.append("")

    breaks = report.get("what_breaks") or []
    if breaks:
        parts.append("<b>❌ Что не работает:</b>")
        for item in breaks:
            element = html.escape(str(item.get("element", "—")))
            problem = html.escape(str(item.get("problem", "—")))
            parts.append(f"• <b>{element}</b> — {problem}")
        parts.append("")

    fixes = report.get("fixes") or []
    if fixes:
        # Сортируем по priority (1 = самое важное)
        fixes_sorted = sorted(fixes, key=lambda f: int(f.get("priority", 99)))
        parts.append("<b>🛠 Конкретные правки:</b>")
        for i, fix in enumerate(fixes_sorted, 1):
            action = html.escape(str(fix.get("action", "—")))
            parts.append(f"{i}. {action}")
        parts.append("")

    bio = report.get("bio_suggestion")
    if bio:
        parts.append("<b>✍️ Вариант био:</b>")
        parts.append(f"<i>{html.escape(str(bio))}</i>")
        parts.append("")

    name = report.get("name_suggestion")
    if name:
        parts.append("<b>🪪 Вариант имени:</b>")
        parts.append(f"<i>{html.escape(str(name))}</i>")
        parts.append("")

    parts.append("Следующий анализ — через 24 часа. /menu чтобы вернуться.")

    text = "\n".join(parts)
    # Telegram limit 4096
    if len(text) > 4000:
        text = text[:4000] + "\n\n…(обрезано)"
    return text
