"""Генерация постов через Gemini + карточки форматов + пост-актив-кнопки."""
from __future__ import annotations

import html
import logging
import time as _time
from typing import Optional

from aiogram import Bot, F, Router
from aiogram.filters import Command
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
    can_transform_today,
    can_use_free_trial,
    clear_style_memory,
    get_style_memory,
    get_user,
    has_access,
    is_subscription_active,
    log_generation,
    mark_free_trial_used,
    touch_streak,
    update_style_memory,
)
from gemini_service import (
    generate_posts,
    humanize_post,
    learn_style_from_feedback,
    transform_post,
)
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
    waiting_style_feedback = State()


class FreeTrialStates(StatesGroup):
    entering_topic = State()


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

    sub_active = await is_subscription_active(user_id)
    if not sub_active:
        await callback.answer()
        # Если free_trial ещё доступен — направляем на бесплатную кнопку
        # вместо того чтобы тратить её скрытно через обычный flow.
        if await can_use_free_trial(user_id):
            await callback.message.answer(
                "🔓 <b>Это по подписке.</b>\n\n"
                "Но у тебя есть <b>одна бесплатная генерация</b> — вернись в "
                "/menu → 📝 Создание → 🎁 «Сгенерить бесплатный пост».\n\n"
                "Там бот сделает тебе один длинный пост под нишу."
            )
        else:
            await send_subscription_required(callback.message, "Генерация постов")
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
    await callback.message.answer(
        "🎯 <b>Создание поста</b>\n\n"
        "Сначала — выбери длину:\n"
        "— <b>Короткий</b> — 1 пост, ≤ 450 символов, режется в ленте\n"
        "— <b>Развёрнутый тред</b> — длинный пост со структурой\n\n"
        "Бот сам подберёт <b>3 разных формата</b> и сделает по ним 3 варианта.",
        reply_markup=length_keyboard(),
    )
    await state.set_state(GenerateStates.choosing_length)


# ---------- БЕСПЛАТНАЯ ГЕНЕРАЦИЯ (одна, без выбора длины, всегда длинный тред) ----------

def free_topic_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="🎲 Удиви меня", callback_data="free_topic:surprise")],
            [InlineKeyboardButton(text="❌ Отмена", callback_data="free_topic:cancel")],
        ]
    )


@router.callback_query(F.data == "action:free_generate")
async def start_free_generation(callback: CallbackQuery, state: FSMContext) -> None:
    user_id = callback.from_user.id

    # Защита: если подписка активна — это не для тебя, иди через обычный flow
    if await is_subscription_active(user_id):
        await callback.answer()
        await callback.message.answer(
            "У тебя активная подписка — пользуйся обычной «🎯 Сгенерить пост»."
        )
        return

    # Защита: free trial уже использован
    if not await can_use_free_trial(user_id):
        await callback.answer()
        await send_subscription_required(callback.message, "Генерация постов")
        return

    user = await get_user(user_id)
    if not user or not user.get("onboarding_complete"):
        await callback.answer()
        await callback.message.answer(
            "Сначала нужно заполнить профиль. Запусти /start."
        )
        return

    await callback.answer()
    await callback.message.answer(
        "🎁 <b>Твоя бесплатная генерация</b>\n\n"
        "О чём писать первый пост? Напиши тему или сырую мысль:\n\n"
        "<i>Тема:</i> «как набрать первую тысячу подписчиков»\n"
        "<i>Сырая мысль:</i> «вчера в кафе подслушал как девушки обсуждали "
        "что курсы все одинаковые»\n\n"
        "Бот сделает один развёрнутый пост под твою нишу.\n"
        "Или жми «🎲 Удиви меня» — подберёт тему сам.",
        reply_markup=free_topic_keyboard(),
    )
    await state.set_state(FreeTrialStates.entering_topic)


@router.callback_query(FreeTrialStates.entering_topic, F.data == "free_topic:surprise")
async def free_topic_surprise(callback: CallbackQuery, state: FSMContext, bot: Bot) -> None:
    await callback.answer()
    await callback.message.edit_reply_markup(reply_markup=None)
    await _do_free_generate(callback.message, callback.from_user.id, None, state, bot)


@router.callback_query(FreeTrialStates.entering_topic, F.data == "free_topic:cancel")
async def free_topic_cancel(callback: CallbackQuery, state: FSMContext) -> None:
    await state.clear()
    await callback.answer("Отменено")
    try:
        await callback.message.edit_reply_markup(reply_markup=None)
    except Exception:
        pass


@router.message(FreeTrialStates.entering_topic, F.text & ~F.text.startswith("/"))
async def free_topic_entered(message: Message, state: FSMContext, bot: Bot) -> None:
    topic = (message.text or "").strip()
    await _do_free_generate(message, message.from_user.id, topic, state, bot)


async def _do_free_generate(
    message: Message,
    user_id: int,
    topic: str | None,
    state: FSMContext,
    bot: Bot,
) -> None:
    """Один длинный пост под нишу + сразу paywall. Без вариантов, без выбора длины."""
    # Двойная проверка
    if await is_subscription_active(user_id):
        await message.answer("У тебя активная подписка — используй /menu → 🎯 Сгенерить пост.")
        await state.clear()
        return
    if not await can_use_free_trial(user_id):
        await send_subscription_required(message, "Генерация постов")
        await state.clear()
        return

    profile = await get_user(user_id)
    if not profile:
        await message.answer("Профиль не найден. Запусти /start.")
        await state.clear()
        return

    status_msg = await message.answer("🧠 Пишу и довожу до ума... ~40-60 секунд")

    async def _progress(text: str) -> None:
        try:
            await status_msg.edit_text(text)
        except Exception:
            pass

    try:
        variants = await generate_posts(
            profile, topic, length="long", on_progress=_progress,
        )
    except Exception as e:
        log.exception("Free generation failed for user %s", user_id)
        await status_msg.edit_text(
            "❌ Что-то пошло не так. Попробуй ещё раз через минуту.\n\n"
            f"<code>{html.escape(type(e).__name__)}: {html.escape(str(e))[:200]}</code>"
        )
        await state.clear()
        return

    await log_generation(user_id, "auto_long", topic)
    await status_msg.delete()

    if not variants:
        await message.answer("❌ Gemini вернул пустой ответ. Попробуй ещё раз через /menu.")
        await state.clear()
        return

    # Показываем ЛУЧШИЙ из 3 по оценке зрителя (_interest_score) — остальные «за подпиской».
    # Если оценок нет (деградация конвейера) — берём первый.
    v = max(variants, key=lambda x: x.get("_interest_score", 0))
    raw_post = str(v.get("post", ""))
    safe_post = html.escape(raw_post)
    technique = html.escape(str(v.get("angle_technique", "—")))
    format_key = (v.get("format") or "").strip().lower()
    fmt_info = FORMAT_DETAILS.get(format_key, {})
    fmt_emoji = fmt_info.get("emoji", "🎯")
    fmt_name = fmt_info.get("name", FORMAT_OPTIONS.get(format_key, format_key) or "—")

    header = (
        f"{fmt_emoji} <b>Твой пост</b> · {fmt_name}\n"
        f"<i>Угол: {technique}</i>"
    )
    full_text = f"{header}\n\n{safe_post}"
    if len(full_text) > 4000:
        full_text = full_text[:4000] + "\n\n…(обрезано)"

    # post_actions_keyboard в free режиме не имеем смысла — кнопки доработки требуют подписки.
    # Поэтому показываем без них, отправляем сразу paywall ниже.
    await message.answer(full_text)

    # Стрик + ачивки
    await touch_streak(user_id)
    await check_and_award(user_id, bot, codes=GENERATION_RELATED + STREAK_RELATED)

    # Помечаем trial использованным + paywall
    await mark_free_trial_used(user_id)
    await _send_paywall_after_trial(message)
    await state.clear()


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

    status_msg = await message.answer("🧠 Пишу и довожу до ума... ~40-60 секунд")

    async def _progress(text: str) -> None:
        try:
            await status_msg.edit_text(text)
        except Exception:
            pass

    try:
        variants = await generate_posts(
            profile, topic, length=length, on_progress=_progress,
        )
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


def _subscription_keyboard() -> Optional[InlineKeyboardMarkup]:
    from config import config as _cfg
    if not _cfg.tribute_buy_button_enabled:
        return None
    return InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(
            text="💎 Оформить подписку",
            url=_cfg.tribute_subscription_url,
        )
    ]])


def _paywall_perks_text() -> str:
    """Актуальный список фич которые открывает подписка."""
    from config import config as _cfg
    perks = [
        "✅ <b>4 поста в день</b> — каждая генерация выдаёт 3 разных варианта формата",
        "✅ <b>Доработка постов</b> — жёстче / мягче / 🫶 очеловечить / по фидбеку",
        "✅ <b>🎙 Голосовой сторителлинг</b> — наговариваешь идею, бот собирает живой пост",
        "✅ <b>📸 Анализ упаковки твоего профиля</b> по скриншоту",
        "✅ <b>🔍 Разбор чужих лент</b> — паттерны под твою нишу",
        "✅ <b>🆕 Упаковка профиля с нуля</b> — имя, bio, ссылка, закреп",
        "✅ <b>💡 10 идей под нишу</b> за один тап",
        "✅ Все будущие фичи бесплатно",
    ]
    if _cfg.threads_publish_enabled:
        perks.insert(0, "✅ <b>📤 Авто-публикация в Threads</b> в один тап")
    return "\n".join(perks)


async def _send_paywall_after_trial(message: Message) -> None:
    """Показываем paywall после первой бесплатной генерации."""
    await message.answer(
        "🎉 <b>Это была твоя бесплатная генерация</b>\n\n"
        "Если зашло — открой остальное по подписке:\n\n"
        f"{_paywall_perks_text()}\n\n"
        "Подписка $5/мес. Отмена в любой момент через @tribute.",
        reply_markup=_subscription_keyboard(),
    )


async def send_subscription_required(
    message: Message, feature_label: str = ""
) -> None:
    """Короткий paywall: «функция по подписке» + кнопка.

    feature_label — название конкретной фичи («Идеи для постов» и т.п.).
    Если пусто — общий текст.
    """
    if feature_label:
        text = (
            f"💎 <b>«{feature_label}» доступна только по подписке.</b>\n\n"
            "Жми кнопку ниже, чтобы оформить."
        )
    else:
        text = (
            "💎 <b>Эта функция доступна только по подписке.</b>\n\n"
            "Жми кнопку ниже, чтобы оформить."
        )
    await message.answer(text, reply_markup=_subscription_keyboard())


# Алиас для обратной совместимости внутри модуля
_send_subscription_required = send_subscription_required


def _admin_id() -> int:
    from config import config
    return config.admin_telegram_id


def _transform_warning_suffix(used_after: int) -> str:
    """Раньше показывали «осталось N доработок». Теперь не показываем — юзер
    просто упрётся в потолок если переборщит. Конкретные цифры лимита намеренно
    нигде не светим."""
    return ""


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
            "Лимит доработок на сегодня исчерпан. Возвращайся завтра — "
            "сбрасывается в 00:00 UTC.",
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
            "Лимит доработок на сегодня исчерпан. Возвращайся завтра — "
            "сбрасывается в 00:00 UTC.",
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
            "⏳ Лимит доработок на сегодня исчерпан. "
            "Возвращайся завтра — сбрасывается в 00:00 UTC."
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


# ---------- ОБУЧЕНИЕ СТИЛЮ: «🎓 Обучить модель под себя» ----------

def _mystyle_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="🗑 Сбросить обучение", callback_data="style:clear"),
    ]])


@router.callback_query(F.data.startswith("post:learn:"))
async def start_learn(callback: CallbackQuery, state: FSMContext) -> None:
    """Юзер хочет обучить модель на основе ОС об этом посте."""
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
        await callback.answer("Обучение доступно по подписке", show_alert=True)
        return

    await state.update_data(learn_post_key=post_key)
    await state.set_state(GenerateStates.waiting_style_feedback)
    await callback.answer()
    await callback.message.answer(
        "🎓 <b>Обучи бота под свой вкус</b>\n\n"
        "Напиши свободным текстом, что в этом посте <b>нравится</b>, а что <b>нет</b>. "
        "Бот запомнит твои предпочтения по стилю и будет учитывать их в следующих постах.\n\n"
        "<i>Примеры:</i>\n"
        "• «слишком пафосно и много капса, пиши спокойнее»\n"
        "• «не начинай с вопроса, не люблю»\n"
        "• «нравятся короткие абзацы, так и продолжай»\n"
        "• «убери слово инсайт и подобный сленг»\n\n"
        "<i>Отмена — /menu</i>"
    )


@router.message(GenerateStates.waiting_style_feedback, F.text & ~F.text.startswith("/"))
async def apply_learn(message: Message, state: FSMContext) -> None:
    feedback = (message.text or "").strip()
    user_id = message.from_user.id
    data = await state.get_data()
    post_key = data.get("learn_post_key")
    original = await get_post(user_id, post_key) if post_key else None

    await state.clear()

    if not feedback:
        return
    if not original:
        await message.answer("Текст поста потерян. Сгенерируй заново и попробуй ещё раз.")
        return
    if not await is_subscription_active(user_id):
        await message.answer("❌ Обучение доступно по подписке.")
        return

    current = await get_style_memory(user_id)

    status_msg = await message.answer("🧠 Запоминаю твой стиль...")
    try:
        result = await learn_style_from_feedback(current, feedback, original)
    except Exception as e:
        log.exception("Style learning failed for user %s", user_id)
        await status_msg.edit_text(
            f"❌ Не получилось запомнить. <code>{html.escape(type(e).__name__)}: "
            f"{html.escape(str(e))[:200]}</code>"
        )
        return

    new_memory = str(result.get("memory", "")).strip()
    learned = str(result.get("learned", "Запомнил твои правки.")).strip()

    if new_memory:
        await update_style_memory(user_id, new_memory)

    await status_msg.delete()
    await message.answer(
        "✅ <b>Готово, обучился.</b>\n\n"
        f"<i>{html.escape(learned)}</i>\n\n"
        "Применю в следующих генерациях. Посмотреть или сбросить всё "
        "выученное — команда /mystyle.",
        reply_markup=_mystyle_keyboard(),
    )


@router.message(Command("mystyle"))
async def cmd_mystyle(message: Message) -> None:
    """Показать накопленную память стиля + дать сбросить."""
    memory = await get_style_memory(message.from_user.id)
    if not memory:
        await message.answer(
            "🎓 <b>Память стиля пока пуста.</b>\n\n"
            "Под любым сгенерированным постом жми «🎓 Обучить модель под себя» "
            "и напиши, что нравится, а что нет — бот начнёт подстраиваться под твой вкус."
        )
        return
    await message.answer(
        "🎓 <b>Что бот про тебя выучил</b>\n\n"
        "Эти правила он учитывает в каждой генерации:\n\n"
        f"{html.escape(memory)}\n\n"
        "<i>Хочешь начать с чистого листа — жми кнопку.</i>",
        reply_markup=_mystyle_keyboard(),
    )


@router.callback_query(F.data == "style:clear")
async def clear_learned_style(callback: CallbackQuery) -> None:
    await clear_style_memory(callback.from_user.id)
    await callback.answer("Память стиля сброшена")
    try:
        await callback.message.edit_reply_markup(reply_markup=None)
    except Exception:
        pass
    await callback.message.answer(
        "🧹 Готово — обнулил выученный стиль. Дальше можешь обучить заново."
    )
