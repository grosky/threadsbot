"""/start — welcome screen, free trial flow, paywall, промокод как secondary path."""
from __future__ import annotations

import logging

from aiogram import Bot, F, Router
from aiogram.filters import Command, CommandObject, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import (
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
    ReplyKeyboardRemove,
)

from achievements import REFERRAL_RELATED, check_and_award
from config import config
from database import (
    activate_promocode,
    can_use_free_trial,
    consume_referral_reward,
    create_promocode,
    create_user,
    get_user,
    has_access,
    is_subscription_active,
    set_referrer_if_new,
)

from .menu import show_main_menu
from .onboarding import start_onboarding

router = Router()
log = logging.getLogger(__name__)


class StartStates(StatesGroup):
    waiting_promocode = State()


# ---------- DEEP LINK ----------

def _parse_referrer_payload(payload: str | None) -> tuple[int | None, str | None]:
    """Парсит deep-link payload.

    Форматы:
    - 'ref_12345' → (12345, None)
    - 'ref_12345_partner_anna' → (12345, 'partner_anna')
    Возвращает (referrer_id, source).
    """
    if not payload or not payload.startswith("ref_"):
        return None, None
    rest = payload[4:]
    # Если есть подчёркивание после ID — это source
    parts = rest.split("_", 1)
    try:
        ref_id = int(parts[0])
    except ValueError:
        return None, None
    source = parts[1] if len(parts) > 1 and parts[1] else None
    return ref_id, source


# ---------- WELCOME + PAYWALL ----------

def welcome_keyboard(show_trial: bool = True) -> InlineKeyboardMarkup:
    rows = []
    if show_trial:
        rows.append([InlineKeyboardButton(
            text="🎯 Попробовать бесплатно",
            callback_data="welcome:try",
        )])
    if config.tribute_buy_button_enabled:
        rows.append([InlineKeyboardButton(
            text="💎 Оформить подписку",
            url=config.tribute_subscription_url,
        )])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def _build_welcome_text() -> str:
    """Welcome для новых юзеров: продающий текст с кейсами + план + бонусы.

    Последняя строка списка бонусов («авто-публикация») появляется только когда
    публикация в Threads разрешена (после Meta App Review).
    """
    lines = [
        "<b>Добро пожаловать!</b>",
        "Ты уже сегодня можешь сделать первый вирусный пост в Threads.",
        "",
        "Используя этого бота пользователи добиваются таких результатов:",
        "— <b>12 000 подписчиков за 2 недели</b>, тратя 15 мин/день",
        "— 2500 подписчиков и продажи на 600к в нише недвижимости",
        "— 1000 в Telegram-канал эксперта по продажам",
        "— 1800 лидов в воронку в нише дизайна",
        "",
        "<b>Вот как это работает:</b>",
        "1. Расскажешь о себе — 4 коротких вопроса",
        "2. Жмёшь «Сгенерить» — бот выдаёт 3 варианта в разных форматах",
        "3. Копируешь лучший → публикуешь в Threads (15 минут в день)",
        "4. Получаешь подписчиков и продажи в Threads, Telegram пассивно",
        "",
        "<b>А ещё внутри:</b>",
        "— 🎙 Голосовой сторителлинг — наговариваешь идею, бот собирает живой пост",
        "— 📸 Анализ упаковки твоего профиля по скриншоту",
        "— 🔍 Разбор чужих лент — находит паттерны и адаптирует под тебя",
        "— 🆕 Упаковка профиля с нуля: имя, bio, ссылка, закреп",
        "— 🔥 Доработка постов: жёстче / мягче / по фидбеку",
        "— 💡 10 идей для постов под твою нишу за один тап",
    ]
    if config.threads_publish_enabled:
        lines.append("— 📤 Авто-публикация в Threads в один тап")
    lines.extend([
        "",
        "🎁 <b>Первая генерация — бесплатно.</b>",
        "Жми кнопку ниже 👇",
    ])
    return "\n".join(lines)


def _build_paywall_text() -> str:
    lines = [
        "🔓 <b>Бесплатная генерация уже использована</b>",
        "",
        "Чтобы продолжить, оформи подписку. Получишь:",
        "",
        "✅ <b>4 генерации в день</b> в 5 форматах",
        "✅ <b>Голосовой сторителлинг</b> — наговариваешь, бот собирает пост",
    ]
    if config.threads_publish_enabled:
        lines.append("✅ <b>Авто-публикация в Threads</b> в один тап")
    lines.extend([
        "✅ <b>Анализ профиля и чужих лент</b>",
        "✅ Все будущие фичи бесплатно",
        "",
        "Можно отменить в любой момент через @tribute.",
    ])
    return "\n".join(lines)


# Старая константа оставлена для совместимости / тестов
PAYWALL_TEXT = (
    "🔓 Бесплатная генерация уже использована"
)


async def _show_welcome(message: Message, user_id: int) -> None:
    trial_ok = await can_use_free_trial(user_id)
    if trial_ok:
        text = _build_welcome_text()
        kb = welcome_keyboard(show_trial=True)
    else:
        text = _build_paywall_text()
        kb = welcome_keyboard(show_trial=False)
    await message.answer(text, reply_markup=kb)


# ---------- /start ----------

@router.message(CommandStart())
async def handle_start(
    message: Message, command: CommandObject, state: FSMContext
) -> None:
    await state.clear()
    user_tg = message.from_user

    await create_user(user_tg.id, user_tg.username, user_tg.first_name or "")

    referrer_id, source = _parse_referrer_payload(command.args)
    if referrer_id:
        linked = await set_referrer_if_new(user_tg.id, referrer_id, source)
        if linked:
            log.info(
                "Linked referral: invitee=%s referrer=%s source=%s",
                user_tg.id, referrer_id, source,
            )

    user = await get_user(user_tg.id)
    sub_active = await is_subscription_active(user_tg.id)

    # 1. Подписка активна — сразу в меню (или дозаполнить онбординг)
    if sub_active:
        if not user or not user.get("onboarding_complete"):
            await message.answer(
                f"С возвращением, {user_tg.first_name or 'друг'}! Подписка активна.\n\n"
                "Прежде чем начать — давай настроим профиль (~5 минут)."
            )
            await start_onboarding(message, state)
        else:
            await show_main_menu(message)
        return

    # 2. Нет подписки — welcome screen или paywall
    await _show_welcome(message, user_tg.id)


# ---------- WELCOME CALLBACKS ----------

@router.callback_query(F.data == "welcome:try")
async def start_free_trial(callback: CallbackQuery, state: FSMContext) -> None:
    """Бесплатная пробная генерация. Если онбординга нет — сначала пройти его."""
    user_id = callback.from_user.id
    await callback.answer()

    if not await can_use_free_trial(user_id):
        await callback.message.answer(
            "Бесплатная генерация уже использована.",
            reply_markup=welcome_keyboard(show_trial=False),
        )
        return

    user = await get_user(user_id)
    if not user or not user.get("onboarding_complete"):
        await callback.message.answer(
            "🚀 <b>Поехали!</b>\n\n"
            "Сначала расскажи о себе — без этого посты будут общими.\n"
            "4 коротких вопроса, ~2 минуты.\n\n"
            "После онбординга сразу сгенерим первый бесплатный пост."
        )
        await start_onboarding(callback.message, state)
    else:
        # Онбординг уже пройден — сразу к меню (там можно жать «Сгенерить»)
        await callback.message.answer(
            "🎁 <b>Один бесплатный пост — твой</b>\n\n"
            "Открой меню и жми «🎯 Сгенерить» — это будет твоя бесплатная генерация."
        )
        await show_main_menu(callback.message)


@router.callback_query(F.data == "welcome:promo")
async def start_promo_input(callback: CallbackQuery, state: FSMContext) -> None:
    """Скрытый callback — кнопка убрана из UI, но осталась для совместимости."""
    await callback.answer()
    await callback.message.answer(
        "Введи код одним сообщением:",
        reply_markup=ReplyKeyboardRemove(),
    )
    await state.set_state(StartStates.waiting_promocode)


@router.message(Command("code"))
async def cmd_code(message: Message, state: FSMContext) -> None:
    """Скрытая команда активации кода. Юзеры узнают её только от админа."""
    await state.clear()
    await message.answer(
        "Введи код одним сообщением:",
        reply_markup=ReplyKeyboardRemove(),
    )
    await state.set_state(StartStates.waiting_promocode)


# ---------- ПРОМОКОД ----------

@router.message(Command("promo"))
async def handle_promo_command(message: Message, state: FSMContext) -> None:
    """Только для админа: генерирует новый код на 30 дней."""
    if message.from_user.id != config.admin_telegram_id:
        return
    code = await create_promocode(duration_days=30)
    await message.answer(
        f"Новый код на 30 дней:\n\n<code>{code}</code>\n\n"
        "<b>Инструкция для получателя:</b>\n"
        f"«Открой @aithreadbot → отправь /code → введи код <code>{code}</code>»\n\n"
        "<i>Юзеры не видят слово «промокод» нигде в интерфейсе — "
        "только те кому ты дашь команду /code знают что она существует.</i>"
    )


@router.message(StartStates.waiting_promocode)
async def handle_promocode_input(
    message: Message, state: FSMContext, bot: Bot
) -> None:
    code = (message.text or "").strip().upper()
    if not code:
        await message.answer("Промокод не может быть пустым. Попробуй ещё раз:")
        return

    user_id = message.from_user.id
    ok, msg = await activate_promocode(code, user_id)
    if not ok:
        kb = InlineKeyboardMarkup(inline_keyboard=[[
            InlineKeyboardButton(
                text="🔙 Назад к выбору",
                callback_data="welcome:back",
            ),
        ]])
        await message.answer(f"❌ {msg}\n\nПопробуй ещё раз или вернись:", reply_markup=kb)
        return

    await message.answer(f"✅ {msg}")
    await state.clear()

    # Реферальный бонус приглашающему
    reward = await consume_referral_reward(user_id)
    if reward:
        try:
            await bot.send_message(
                reward["referrer_id"],
                "🎁 <b>Твой друг активировал подписку!</b>\n\n"
                f"Тебе начислено <b>+{reward['bonus_days']} дней</b>.\n"
                f"Действует до: <b>{reward['new_expires_at'].strftime('%d.%m.%Y')}</b>",
            )
        except Exception as e:
            log.warning("Failed to notify referrer %s: %s", reward["referrer_id"], e)
        try:
            await check_and_award(reward["referrer_id"], bot, codes=REFERRAL_RELATED)
        except Exception:
            log.exception("Referral achievement check failed")

    # После активации — если онбординга нет, пройди его
    user = await get_user(user_id)
    if not user or not user.get("onboarding_complete"):
        await message.answer("Теперь настроим твой профиль.")
        await start_onboarding(message, state)
    else:
        await show_main_menu(message)


@router.callback_query(F.data == "welcome:back")
async def back_to_welcome(callback: CallbackQuery, state: FSMContext) -> None:
    await state.clear()
    await callback.answer()
    await _show_welcome(callback.message, callback.from_user.id)
