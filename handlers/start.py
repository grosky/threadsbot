"""/start, активация промокода, /promo для админа."""
from __future__ import annotations

import logging

from aiogram import Bot, Router
from aiogram.filters import Command, CommandObject, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import Message, ReplyKeyboardRemove

from config import config
from database import (
    activate_promocode,
    consume_referral_reward,
    create_promocode,
    create_user,
    get_user,
    is_subscription_active,
    set_referrer_if_new,
)

from .menu import show_main_menu
from .onboarding import start_onboarding

router = Router()
log = logging.getLogger(__name__)


class StartStates(StatesGroup):
    waiting_promocode = State()


def _parse_referrer_id(payload: str | None) -> int | None:
    """Извлекает referrer_id из deep-link payload вида 'ref_12345'."""
    if not payload or not payload.startswith("ref_"):
        return None
    try:
        return int(payload[4:])
    except ValueError:
        return None


@router.message(CommandStart())
async def handle_start(
    message: Message, command: CommandObject, state: FSMContext
) -> None:
    await state.clear()
    user_tg = message.from_user

    # Создаём юзера ДО привязки реферера (для гонок при первом /start)
    await create_user(user_tg.id, user_tg.username, user_tg.first_name or "")

    referrer_id = _parse_referrer_id(command.args)
    if referrer_id:
        linked = await set_referrer_if_new(user_tg.id, referrer_id)
        if linked:
            log.info("Linked referral: invitee=%s referrer=%s", user_tg.id, referrer_id)

    user = await get_user(user_tg.id)

    if not await is_subscription_active(user_tg.id):
        await message.answer(
            f"Привет, {user_tg.first_name or 'друг'}!\n\n"
            "Это AI-генератор вирусных постов для Threads. "
            "Для активации введи промокод (приходит в чеке после покупки промт-пака):",
            reply_markup=ReplyKeyboardRemove(),
        )
        await state.set_state(StartStates.waiting_promocode)
        return

    # Подписка активна
    if not user or not user.get("onboarding_complete"):
        await message.answer(
            f"С возвращением, {user_tg.first_name or 'друг'}! Подписка активна.\n\n"
            "Прежде чем начать генерить — давай настроим профиль (~5 минут)."
        )
        await start_onboarding(message, state)
    else:
        await show_main_menu(message)


@router.message(Command("promo"))
async def handle_promo_command(message: Message, state: FSMContext) -> None:
    """Только для админа: генерирует новый промокод на 30 дней."""
    if message.from_user.id != config.admin_telegram_id:
        return
    code = await create_promocode(duration_days=30)
    await message.answer(
        f"Новый промокод на 30 дней:\n\n<code>{code}</code>\n\n"
        "Передай покупателю — он введёт его после /start."
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
        await message.answer(f"❌ {msg}\n\nПопробуй ещё раз или напиши автору.")
        return

    await message.answer(f"✅ {msg}\n\nТеперь настроим твой профиль.")

    # Начисляем реферальный бонус приглашающему (если есть и ещё не выплачен)
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
            # Не падаем если реферер заблокировал бота / удалил аккаунт
            log.warning(
                "Failed to notify referrer %s: %s",
                reward["referrer_id"], e,
            )

    await state.clear()
    await start_onboarding(message, state)
