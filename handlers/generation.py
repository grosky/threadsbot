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
from config import DAILY_LIMIT, TRANSFORM_DAILY_LIMIT, TRANSFORM_WARNING_AT
from database import (
    can_generate_today,
    can_transform_today,
    get_user,
    has_access,
    is_subscription_active,
    log_generation,
    mark_free_trial_used,
    touch_streak,
)
from gemini_service import generate_posts, humanize_post, transform_post
from prompts import FORMAT_DETAILS, FORMAT_OPTIONS

from .threads_connect import (
    get_post,
    post_actions_keyboard,
    remember_post,
)

router = Router()
log = logging.getLogger(__name__)


class GenerateStates(StatesGroup):
    choosing_length = State()
    entering_topic = State()
    waiting_refine_feedback = State()


# ---------- KEYBOARDS ----------

def length_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(
                text="📝 Короткий — 1 пост",
                callback_data="len:short",
            )],
            [InlineKeyboardButton(
                text="🧵 Развёрнутый тред",
                callback_data="len:long",
            )],
            [InlineKeyboardButton(text="❌ Отмена", callback_data="len:cancel")],
        ]
    )


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
        "🎯 <b>Создание поста</b>\n\n"
        "Сначала — выбери длину:\n"
        "— <b>Короткий</b> — 1 пост, ≤ 450 символов, режется в ленте\n"
        "— <b>Развёрнутый тред</b> — длинный пост со структурой\n\n"
        "Бот сам подберёт <b>3 разных формата</b> и сделает по ним 3 варианта."
        + trial_note,
        reply_markup=length_keyboard(),
    )
    await state.set_state(GenerateStates.choosing_length)


@router.callback_query(GenerateStates.choosing_length, F.data.startswith("len:"))
async def length_selected(callback: CallbackQuery, state: FSMContext, bot: Bot) -> None:
    choice = callback.data.split(":", 1)[1]
    if choice == "cancel":
        await state.clear()
        await callback.answer("Отменено")
        await callback.message.edit_reply_markup(reply_markup=None)
        return

    if choice not in ("short", "long"):
        await callback.answer("Неизвестный выбор", show_alert=True)
        return

    await state.update_data(length=choice)
    await callback.answer()
    await callback.message.edit_reply_markup(reply_markup=None)

    # Если тема уже задана заранее (юзер пришёл через брейншторм) —
    # пропускаем шаг ввода и сразу генерим.
    data = await state.get_data()
    preset_topic = data.get("topic")
    if preset_topic:
        await state.update_data(topic=None)  # очищаем чтобы не залипало
        await _do_generate(
            callback.message, callback.from_user.id,
            preset_topic, choice, state, bot,
        )
        return

    label = "Короткий пост" if choice == "short" else "Развёрнутый тред"
    await callback.message.answer(
        f"📏 <b>{label}</b>\n\n"
        "Напиши <b>тему</b> или <b>сырую мысль/наблюдение</b>:\n\n"
        "<i>Тема:</i> «как набрать первую тысячу подписчиков»\n"
        "<i>Сырая мысль:</i> «вчера в кафе подслушал как девушки обсуждали "
        "что курсы все одинаковые»\n\n"
        "Бот разберётся и сделает 3 разных поста.\n"
        "Или жми «🎲 Удиви меня» — подберёт тему сам.",
        reply_markup=topic_keyboard(),
    )
    await state.set_state(GenerateStates.entering_topic)


@router.callback_query(GenerateStates.entering_topic, F.data == "topic:surprise")
async def topic_surprise(callback: CallbackQuery, state: FSMContext, bot: Bot) -> None:
    await callback.answer()
    await callback.message.edit_reply_markup(reply_markup=None)
    data = await state.get_data()
    await _do_generate(
        callback.message, callback.from_user.id,
        None, data.get("length", "long"), state, bot,
    )


@router.callback_query(GenerateStates.entering_topic, F.data == "topic:cancel")
async def topic_cancel(callback: CallbackQuery, state: FSMContext) -> None:
    await state.clear()
    await callback.answer("Отменено")
    await callback.message.edit_reply_markup(reply_markup=None)


@router.message(GenerateStates.entering_topic, F.text & ~F.text.startswith("/"))
async def topic_entered(message: Message, state: FSMContext, bot: Bot) -> None:
    data = await state.get_data()
    topic = (message.text or "").strip()
    await _do_generate(
        message, message.from_user.id,
        topic, data.get("length", "long"), state, bot,
    )


# ---------- ОСНОВНАЯ ГЕНЕРАЦИЯ ----------

async def _do_generate(
    message: Message,
    user_id: int,
    topic: str | None,
    length: str,
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
        variants = await generate_posts(profile, topic, length=length)
    except Exception as e:
        log.exception("Generation failed for user %s", user_id)
        await status_msg.edit_text(
            "❌ Что-то пошло не так. Попробуй ещё раз через минуту.\n\n"
            f"<code>{html.escape(type(e).__name__)}: {html.escape(str(e))[:200]}</code>"
        )
        await state.clear()
        return

    await log_generation(user_id, f"auto_{length}", topic)
    await status_msg.delete()

    batch = int(_time.time())

    for v in variants:
        raw_post = str(v.get("post", ""))
        safe_post = html.escape(raw_post)
        variant_id = v.get("id", "?")
        technique = html.escape(str(v.get("angle_technique", "—")))

        # Формат теперь приходит из ответа Gemini, не задаётся юзером
        format_key = (v.get("format") or "").strip().lower()
        fmt_info = FORMAT_DETAILS.get(format_key, {})
        fmt_emoji = fmt_info.get("emoji", "🎯")
        fmt_name = fmt_info.get("name", FORMAT_OPTIONS.get(format_key, format_key) or "—")

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


def _transform_warning_suffix(used_after: int) -> str:
    """Возвращает приписку про оставшиеся доработки если приблизились к лимиту.

    used_after — сколько transform'ов уже сделано после текущего.
    """
    if used_after < TRANSFORM_WARNING_AT:
        return ""
    remaining = max(0, TRANSFORM_DAILY_LIMIT - used_after)
    if remaining == 0:
        return ""  # лимит уже исчерпан, отдельного предупреждения не нужно
    word = "доработка" if remaining == 1 else "доработки" if remaining < 5 else "доработок"
    return (
        f"\n\n<i>⚠️ Осталось <b>{remaining}</b> {word} на сегодня. "
        f"Лимит сбросится в 00:00 UTC.</i>"
    )


# ---------- ПОСТ-АКТИВЫ: ЖЁСТЧЕ / МЯГЧЕ / ОЧЕЛОВЕЧИТЬ ----------

@router.callback_query(F.data.startswith("post:harder:"))
async def make_harder(callback: CallbackQuery, bot: Bot) -> None:
    await _transform(callback, bot, "Перепиши пост ЖЁСТЧЕ. Добавь провокации, прямых формулировок, не бойся задеть.")


@router.callback_query(F.data.startswith("post:softer:"))
async def make_softer(callback: CallbackQuery, bot: Bot) -> None:
    await _transform(callback, bot, "Перепиши пост МЯГЧЕ. Убери агрессию, добавь эмпатии, ВЫ-форма.")


@router.callback_query(F.data.startswith("post:humanize:"))
async def make_humanize(callback: CallbackQuery, bot: Bot) -> None:
    """Переписывает пост в живой голос реального автора (отдельный системный промт)."""
    user_id = callback.from_user.id
    post_key = callback.data.split(":", 2)[2]

    original = await get_post(user_id, post_key)
    if not original:
        await callback.answer(
            "Текст поста потерян (старше 24 ч). Сгенерируй заново.",
            show_alert=True,
        )
        return

    if not await is_subscription_active(user_id):
        await callback.answer("Подписка неактивна", show_alert=True)
        return

    can_tr, _ = await can_transform_today(user_id)
    if not can_tr:
        await callback.answer(
            f"Лимит доработок исчерпан ({TRANSFORM_DAILY_LIMIT}/{TRANSFORM_DAILY_LIMIT}). "
            f"Сбросится в 00:00 UTC.",
            show_alert=True,
        )
        return

    profile = await get_user(user_id)
    if not profile:
        await callback.answer("Профиль не найден", show_alert=True)
        return

    await callback.answer("Очеловечиваю...")
    status_msg = await callback.message.answer(
        "🧠 Переписываю человеческим голосом... ~15-20 секунд"
    )

    try:
        result = await humanize_post(profile, original)
    except Exception as e:
        log.exception("Humanize failed for user %s", user_id)
        await status_msg.edit_text(
            f"❌ Не получилось. <code>{html.escape(type(e).__name__)}: "
            f"{html.escape(str(e))[:200]}</code>"
        )
        return

    await log_generation(user_id, "humanize", None)
    await status_msg.delete()

    new_post = str(result.get("post", ""))
    summary = str(result.get("summary", "переписал в живом голосе"))

    new_key = f"h{int(_time.time())}"
    await remember_post(user_id, new_key, new_post)

    _, used_after = await can_transform_today(user_id)
    warn = _transform_warning_suffix(used_after)

    safe_post = html.escape(new_post)
    header = f"🫶 <b>Очеловечено</b>\n<i>{html.escape(summary)}</i>\n\n"
    full = header + safe_post
    if len(full) > 4000:
        full = header + safe_post[:3700] + "\n…(обрезано)"
    await callback.message.answer(
        full + warn,
        reply_markup=post_actions_keyboard(new_key),
    )


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

    can_tr, _ = await can_transform_today(user_id)
    if not can_tr:
        await callback.answer(
            f"Лимит доработок исчерпан ({TRANSFORM_DAILY_LIMIT}/{TRANSFORM_DAILY_LIMIT}). "
            f"Сбросится в 00:00 UTC.",
            show_alert=True,
        )
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

    _, used_after = await can_transform_today(user_id)
    warn = _transform_warning_suffix(used_after)

    safe_post = html.escape(new_post)
    body = (
        f"🔄 <b>Переписано</b>\n<i>{html.escape(summary)}</i>\n\n{safe_post}"
        if len(safe_post) < 3500
        else f"🔄 <b>Переписано</b>\n\n{safe_post[:3700]}"
    )
    await callback.message.answer(
        body + warn,
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


@router.message(GenerateStates.waiting_refine_feedback, F.text & ~F.text.startswith("/"))
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

    can_tr, _ = await can_transform_today(user_id)
    if not can_tr:
        await message.answer(
            f"⏳ Лимит доработок исчерпан "
            f"({TRANSFORM_DAILY_LIMIT}/{TRANSFORM_DAILY_LIMIT}). "
            f"Сбросится в 00:00 UTC."
        )
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

    _, used_after = await can_transform_today(user_id)
    warn = _transform_warning_suffix(used_after)

    safe_post = html.escape(new_post)
    text = f"✏️ <b>Доработано</b>\n<i>{html.escape(summary)}</i>\n\n{safe_post}"
    if len(text) > 4000:
        text = text[:4000] + "\n…(обрезано)"

    await message.answer(text + warn, reply_markup=post_actions_keyboard(new_key))
