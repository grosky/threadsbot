"""Генерация постов через Gemini + карточки форматов + пост-актив-кнопки."""
from __future__ import annotations

import html
import logging
import time as _time

from aiogram import Bot, F, Router
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import (
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
)

from achievements import (
    GENERATION_RELATED,
    STREAK_RELATED,
    check_and_award,
)
from config import DAILY_LIMIT
from database import (
    can_generate_today,
    get_user,
    has_access,
    is_subscription_active,
    log_generation,
    mark_free_trial_used,
    touch_streak,
)
from gemini_service import generate_posts, transform_post
from prompts import FORMAT_DETAILS, FORMAT_OPTIONS

from .threads_connect import (
    get_post,
    post_actions_keyboard,
    remember_post,
)

router = Router()
log = logging.getLogger(__name__)


class GenerateStates(StatesGroup):
    choosing_format = State()
    entering_topic = State()
    waiting_refine_feedback = State()


# ---------- KEYBOARDS ----------

def formats_keyboard() -> InlineKeyboardMarkup:
    """Компактная сетка форматов 2-в-ряд + кнопка подробнее."""
    items = list(FORMAT_OPTIONS.items())
    rows = []
    for i in range(0, len(items), 2):
        row = [
            InlineKeyboardButton(text=items[i][1], callback_data=f"fmt:{items[i][0]}")
        ]
        if i + 1 < len(items):
            row.append(InlineKeyboardButton(
                text=items[i + 1][1], callback_data=f"fmt:{items[i + 1][0]}"
            ))
        rows.append(row)
    rows.append([InlineKeyboardButton(
        text="❓ Какой выбрать?", callback_data="fmt:help"
    )])
    rows.append([InlineKeyboardButton(text="❌ Отмена", callback_data="fmt:cancel")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def topic_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="🎲 Удиви меня", callback_data="topic:surprise")],
            [InlineKeyboardButton(text="❌ Отмена", callback_data="topic:cancel")],
        ]
    )


# ---------- ENTRY ----------

@router.callback_query(F.data == "action:generate")
async def start_generation(callback: CallbackQuery, state: FSMContext) -> None:
    user_id = callback.from_user.id

    access_ok, reason = await has_access(user_id)
    if not access_ok:
        await callback.answer()
        await callback.message.answer(
            "🔓 Бесплатная генерация уже использована.\n\n"
            "Оформи подписку чтобы продолжить — /start → «💎 Оформить подписку»."
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
            "Сначала нужно заполнить профиль. Запусти /start."
        )
        return

    await callback.answer()
    trial_note = (
        "\n\n<i>🎁 Это твоя бесплатная пробная генерация.</i>"
        if reason == "free_trial" else ""
    )
    await callback.message.answer(
        "🎯 <b>Выбери формат поста</b>\n\n"
        "Каждый формат бьёт по своему — нажми «❓ Какой выбрать?» если не уверен."
        + trial_note,
        reply_markup=formats_keyboard(),
    )
    await state.set_state(GenerateStates.choosing_format)


@router.callback_query(GenerateStates.choosing_format, F.data == "fmt:help")
async def show_formats_help(callback: CallbackQuery) -> None:
    """Карточки всех форматов с описанием, когда юзать, примером хука."""
    lines = ["<b>Форматы постов</b>", ""]
    for key, info in FORMAT_DETAILS.items():
        lines.extend([
            f"{info['emoji']} <b>{info['name']}</b>",
            info['tagline'] + ".",
            f"Когда: {info['when'].lower()}.",
            f"Пример: «{info['hook_example']}»",
            "",
        ])
    lines.append("Выбери формат ниже.")

    await callback.answer()
    text = "\n".join(lines)
    if len(text) > 4000:
        text = text[:4000] + "\n…(обрезано)"
    await callback.message.answer(text, reply_markup=formats_keyboard())


@router.callback_query(GenerateStates.choosing_format, F.data.startswith("fmt:"))
async def format_selected(callback: CallbackQuery, state: FSMContext) -> None:
    fmt = callback.data.split(":", 1)[1]

    if fmt == "cancel":
        await state.clear()
        await callback.answer("Отменено")
        await callback.message.edit_reply_markup(reply_markup=None)
        return

    if fmt == "help":
        return  # уже обработано выше

    if fmt not in FORMAT_OPTIONS:
        await callback.answer("Неизвестный формат", show_alert=True)
        return

    await state.update_data(format=fmt)
    await callback.answer()
    await callback.message.edit_reply_markup(reply_markup=None)

    info = FORMAT_DETAILS.get(fmt, {})
    name = info.get("name", FORMAT_OPTIONS[fmt])
    emoji = info.get("emoji", "🎯")

    await callback.message.answer(
        f"{emoji} <b>Формат:</b> {name}\n\n"
        "Напиши тему поста — или жми «🎲 Удиви меня», Gemini сам подберёт угол под твою нишу.",
        reply_markup=topic_keyboard(),
    )
    await state.set_state(GenerateStates.entering_topic)


@router.callback_query(GenerateStates.entering_topic, F.data == "topic:surprise")
async def topic_surprise(callback: CallbackQuery, state: FSMContext, bot: Bot) -> None:
    await callback.answer()
    await callback.message.edit_reply_markup(reply_markup=None)
    data = await state.get_data()
    await _do_generate(
        callback.message, callback.from_user.id, data["format"], None, state, bot
    )


@router.callback_query(GenerateStates.entering_topic, F.data == "topic:cancel")
async def topic_cancel(callback: CallbackQuery, state: FSMContext) -> None:
    await state.clear()
    await callback.answer("Отменено")
    await callback.message.edit_reply_markup(reply_markup=None)


@router.message(GenerateStates.entering_topic, F.text)
async def topic_entered(message: Message, state: FSMContext, bot: Bot) -> None:
    data = await state.get_data()
    topic = (message.text or "").strip()
    await _do_generate(message, message.from_user.id, data["format"], topic, state, bot)


# ---------- ОСНОВНАЯ ГЕНЕРАЦИЯ ----------

async def _do_generate(
    message: Message,
    user_id: int,
    format_name: str,
    topic: str | None,
    state: FSMContext,
    bot: Bot,
) -> None:
    # Двойная проверка доступа
    access_ok, reason = await has_access(user_id)
    if not access_ok:
        await message.answer(
            "🔓 Доступ закончился. /start → «💎 Оформить подписку»."
        )
        await state.clear()
        return

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

    status_msg = await message.answer("🧠 Думаю... ~10-15 секунд")

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

    batch = int(_time.time())
    fmt_info = FORMAT_DETAILS.get(format_name, {})
    fmt_emoji = fmt_info.get("emoji", "🎯")
    fmt_name = fmt_info.get("name", FORMAT_OPTIONS.get(format_name, format_name))

    for v in variants:
        raw_post = str(v.get("post", ""))
        safe_post = html.escape(raw_post)
        variant_id = v.get("id", "?")
        technique = html.escape(str(v.get("angle_technique", "—")))

        header = (
            f"{fmt_emoji} <b>Вариант {variant_id}</b> · {fmt_name}\n"
            f"<i>Угол: {technique}</i>"
        )
        full_text = f"{header}\n\n{safe_post}"
        if len(full_text) > 4000:
            full_text = full_text[:4000] + "\n\n…(обрезано)"

        post_key = f"g{batch}_{variant_id}"
        await remember_post(user_id, post_key, raw_post)

        await message.answer(full_text, reply_markup=post_actions_keyboard(post_key))

    # Стрик + ачивки
    await touch_streak(user_id)
    await check_and_award(user_id, bot, codes=GENERATION_RELATED + STREAK_RELATED)

    # Если это был free trial — помечаем использованным и показываем paywall
    if reason == "free_trial":
        await mark_free_trial_used(user_id)
        await _send_paywall_after_trial(message)
        await state.clear()
        return

    _, used_after = await can_generate_today(user_id)
    if user_id == _admin_id():
        suffix = "∞ (admin)"
    else:
        suffix = f"{max(0, DAILY_LIMIT - used_after)}/{DAILY_LIMIT}"
    await message.answer(
        f"✅ Готово. Осталось сегодня: <b>{suffix}</b>\n\n"
        f"💡 Подсказка: под каждым вариантом есть «🔥 Жёстче», «😌 Мягче», «✏️ Доработать».\n\n"
        f"Жми /menu чтобы вернуться."
    )
    await state.clear()


async def _send_paywall_after_trial(message: Message) -> None:
    """Показываем paywall после первой бесплатной генерации."""
    from config import config as _cfg
    rows = []
    if _cfg.tribute_buy_button_enabled:
        rows.append([InlineKeyboardButton(
            text="💎 Оформить подписку",
            url=_cfg.tribute_subscription_url,
        )])
    kb = InlineKeyboardMarkup(inline_keyboard=rows) if rows else None

    perks = [
        "✅ <b>4 генерации в день</b> в 5 форматах",
        "✅ <b>Голосовой сторителлинг</b>",
        "✅ Анализ профиля и чужих лент",
        "✅ Доработка постов (жёстче / мягче / по фидбеку)",
    ]
    if _cfg.threads_publish_enabled:
        perks.insert(2, "✅ <b>Авто-публикация в Threads</b>")
    perks_text = "\n".join(perks)

    await message.answer(
        "🎉 <b>Это была твоя бесплатная генерация</b>\n\n"
        "Понравилось? Оформи подписку и получи:\n\n"
        f"{perks_text}\n\n"
        "Можно отменить в любой момент.",
        reply_markup=kb,
    )


def _admin_id() -> int:
    from config import config
    return config.admin_telegram_id


# ---------- ПОСТ-АКТИВЫ: ЖЁСТЧЕ / МЯГЧЕ ----------

@router.callback_query(F.data.startswith("post:harder:"))
async def make_harder(callback: CallbackQuery, bot: Bot) -> None:
    await _transform(callback, bot, "Перепиши пост ЖЁСТЧЕ. Добавь провокации, прямых формулировок, не бойся задеть.")


@router.callback_query(F.data.startswith("post:softer:"))
async def make_softer(callback: CallbackQuery, bot: Bot) -> None:
    await _transform(callback, bot, "Перепиши пост МЯГЧЕ. Убери агрессию, добавь эмпатии, ВЫ-форма.")


async def _transform(callback: CallbackQuery, bot: Bot, instruction: str) -> None:
    user_id = callback.from_user.id
    post_key = callback.data.split(":", 2)[2]

    original = await get_post(user_id, post_key)
    if not original:
        await callback.answer("Текст поста потерян (старше 24 ч). Сгенерируй заново.", show_alert=True)
        return

    if not await is_subscription_active(user_id):
        await callback.answer("Подписка неактивна", show_alert=True)
        return

    can_gen, _ = await can_generate_today(user_id)
    if not can_gen:
        await callback.answer("Дневной лимит исчерпан", show_alert=True)
        return

    profile = await get_user(user_id)
    if not profile:
        await callback.answer("Профиль не найден", show_alert=True)
        return

    await callback.answer("Переписываю...")
    status_msg = await callback.message.answer("🧠 Переписываю... ~5-10 секунд")

    try:
        result = await transform_post(profile, original, instruction)
    except Exception as e:
        log.exception("Transform failed for user %s", user_id)
        await status_msg.edit_text(
            f"❌ Не получилось. <code>{html.escape(type(e).__name__)}: "
            f"{html.escape(str(e))[:200]}</code>"
        )
        return

    await log_generation(user_id, "transform", instruction[:30])
    await status_msg.delete()

    new_post = str(result.get("post", ""))
    summary = str(result.get("summary", "переписал"))

    new_key = f"t{int(_time.time())}"
    await remember_post(user_id, new_key, new_post)

    safe_post = html.escape(new_post)
    await callback.message.answer(
        f"🔄 <b>Переписано</b>\n<i>{html.escape(summary)}</i>\n\n{safe_post}"
        if len(safe_post) < 3500
        else f"🔄 <b>Переписано</b>\n\n{safe_post[:3700]}",
        reply_markup=post_actions_keyboard(new_key),
    )


# ---------- ПОСТ-АКТИВЫ: ДОРАБОТАТЬ ----------

@router.callback_query(F.data.startswith("post:refine:"))
async def start_refine(callback: CallbackQuery, state: FSMContext) -> None:
    post_key = callback.data.split(":", 2)[2]
    original = await get_post(callback.from_user.id, post_key)
    if not original:
        await callback.answer("Текст поста потерян. Сгенерируй заново.", show_alert=True)
        return

    await state.update_data(refine_post_key=post_key)
    await state.set_state(GenerateStates.waiting_refine_feedback)
    await callback.answer()
    await callback.message.answer(
        "✏️ <b>Что улучшить?</b>\n\n"
        "Напиши свободным текстом. Примеры:\n"
        "• «короче и добавь цифр»\n"
        "• «уберись от ИТ-сленга, ЦА — мамы»\n"
        "• «другой хук, без вопроса в начале»\n"
        "• «добавь в конец CTA на тг-канал»\n\n"
        "<i>Отмена — /menu</i>"
    )


@router.message(GenerateStates.waiting_refine_feedback, F.text)
async def apply_refine(message: Message, state: FSMContext, bot: Bot) -> None:
    feedback = (message.text or "").strip()
    if not feedback or feedback.startswith("/"):
        await state.clear()
        return

    user_id = message.from_user.id
    data = await state.get_data()
    post_key = data.get("refine_post_key")
    original = await get_post(user_id, post_key) if post_key else None

    await state.clear()

    if not original:
        await message.answer("Текст потерян. Сгенерируй заново.")
        return

    if not await is_subscription_active(user_id):
        await message.answer("❌ Подписка неактивна.")
        return

    can_gen, _ = await can_generate_today(user_id)
    if not can_gen:
        await message.answer("⏳ Лимит исчерпан.")
        return

    profile = await get_user(user_id)
    if not profile:
        await message.answer("Профиль не найден.")
        return

    status_msg = await message.answer("🧠 Дорабатываю... ~5-10 секунд")
    try:
        result = await transform_post(profile, original, feedback)
    except Exception as e:
        log.exception("Refine failed for user %s", user_id)
        await status_msg.edit_text(
            f"❌ Не получилось. <code>{html.escape(type(e).__name__)}: "
            f"{html.escape(str(e))[:200]}</code>"
        )
        return

    await log_generation(user_id, "refine", feedback[:60])
    await status_msg.delete()

    new_post = str(result.get("post", ""))
    summary = str(result.get("summary", "доработал"))

    new_key = f"r{int(_time.time())}"
    await remember_post(user_id, new_key, new_post)

    safe_post = html.escape(new_post)
    text = f"✏️ <b>Доработано</b>\n<i>{html.escape(summary)}</i>\n\n{safe_post}"
    if len(text) > 4000:
        text = text[:4000] + "\n…(обрезано)"

    await message.answer(text, reply_markup=post_actions_keyboard(new_key))
