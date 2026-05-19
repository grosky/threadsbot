"""Публикация своего поста (написанного руками) в Threads."""
from __future__ import annotations

import html
import logging
import time

from aiogram import F, Router
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import (
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
)

from config import config
from database import (
    get_threads_account,
    is_subscription_active,
)
from threads_api import split_for_threads

from .threads_connect import remember_post

router = Router()
log = logging.getLogger(__name__)


class CustomPostStates(StatesGroup):
    waiting_for_text = State()


@router.callback_query(F.data == "action:custom_post")
async def start_custom_post(callback: CallbackQuery, state: FSMContext) -> None:
    user_id = callback.from_user.id

    if not config.threads_publish_enabled:
        await callback.answer("Эта фича скоро будет доступна", show_alert=True)
        return

    if not await is_subscription_active(user_id):
        await callback.answer()
        await callback.message.answer(
            "🔓 «Свой пост» доступен только по подписке.\n"
            "/start → «💎 Оформить подписку»."
        )
        return

    account = await get_threads_account(user_id)
    if not account:
        await callback.answer()
        await callback.message.answer(
            "Сначала подключи Threads через /menu → «🔗 Подключить Threads»."
        )
        return

    await callback.answer()
    await state.set_state(CustomPostStates.waiting_for_text)
    await callback.message.answer(
        "✍️ <b>Свой пост в Threads</b>\n\n"
        "Отправь мне текст поста — я опубликую его в твой <b>@"
        f"{html.escape(account.get('threads_username') or 'threads')}</b>.\n\n"
        "📏 Если длиннее 480 символов — разрежу на тред (цепочка реплаев).\n\n"
        "<i>Отмена — /menu</i>"
    )


@router.message(CustomPostStates.waiting_for_text, F.text & ~F.text.startswith("/"))
async def receive_custom_text(message: Message, state: FSMContext) -> None:
    text = (message.text or "").strip()
    if not text:
        await message.answer("Пустой пост — попробуй ещё раз.")
        return

    if len(text) > 5000:
        await message.answer(
            f"Слишком длинно ({len(text)} символов). Threads вряд ли вытянет тред "
            "из 10+ постов. Ужми до 5000 символов."
        )
        return

    # Кладём в кэш (быстро) и сразу показываем превью с подтверждением
    post_key = f"c{int(time.time())}"
    await remember_post(message.from_user.id, post_key, text)

    chunks = split_for_threads(text)
    if len(chunks) == 1:
        plan = "одним постом"
    else:
        plan = f"тредом из <b>{len(chunks)}</b> постов"

    preview = text[:300]
    if len(text) > 300:
        preview += "…"

    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(
            text="✅ Опубликовать",
            callback_data=f"publish:threads:{post_key}",
        )],
        [InlineKeyboardButton(
            text="❌ Отмена",
            callback_data="custom:cancel",
        )],
    ])

    await state.clear()
    await message.answer(
        f"📋 <b>Превью</b> · {len(text)} символов · уйдёт {plan}\n\n"
        f"━━━━━━━━━━━━━━━━━\n\n"
        f"{html.escape(preview)}",
        reply_markup=kb,
    )


@router.callback_query(F.data == "custom:cancel")
async def cancel_custom(callback: CallbackQuery, state: FSMContext) -> None:
    await state.clear()
    await callback.answer("Отменено")
    await callback.message.edit_reply_markup(reply_markup=None)


@router.message(CustomPostStates.waiting_for_text)
async def wrong_input(message: Message) -> None:
    """Не-текст в режиме ожидания поста."""
    await message.answer(
        "Жду <b>текст</b> поста. Если передумал — /menu."
    )
