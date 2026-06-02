"""Свободный чат с Gemini Pro для партнёров (+админа).

Сценарий: партнёр кидает посты конкурентов, обсуждает с ботом, как их
доработать, и собирает свои варианты. Один непрерывный чат на юзера,
история хранится в БД (переживает рестарт). Кнопка «🆕 Новый чат»
сбрасывает контекст.

Модель: gemini-3-pro-preview + thinking (см. gemini_service.partner_chat_reply).
Доступ: только партнёры (есть запись в partner_links) и админ.
"""
from __future__ import annotations

import html
import logging

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

from config import config
from database import (
    add_partner_chat_message,
    clear_partner_chat,
    count_partner_chat_messages,
    get_partner_chat_history,
    get_user,
    is_partner,
)
from gemini_service import partner_chat_reply

router = Router()
log = logging.getLogger(__name__)

# Сколько последних сообщений отправляем в модель (контроль стоимости).
_HISTORY_LIMIT = 40
# Лимит длины одного сообщения Telegram.
_TG_LIMIT = 4000


class PartnerChatStates(StatesGroup):
    chatting = State()


async def _can_access(user_id: int) -> bool:
    return user_id == config.admin_telegram_id or await is_partner(user_id)


def _chat_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="🆕 Новый чат", callback_data="pchat:new")],
            [InlineKeyboardButton(text="⬅️ Выйти", callback_data="pchat:exit")],
        ]
    )


_INTRO = (
    "💬 <b>Чат с Gemini Pro</b>\n\n"
    "Гибкий режим для доработки постов. Кидай пост конкурента, который "
    "зацепил, — разберём, почему он работает, и соберём твой вариант. "
    "Можно обсуждать, спорить, просить переписать «короче / жёстче / "
    "больше истории».\n\n"
    "Я держу весь контекст диалога. История сохраняется между сессиями — "
    "нажми «🆕 Новый чат», чтобы начать с чистого листа.\n\n"
    "<i>Просто напиши сообщение.</i>"
)


def _split(text: str) -> list[str]:
    """Режет длинный ответ на куски под лимит Telegram."""
    text = text or ""
    if len(text) <= _TG_LIMIT:
        return [text]
    chunks: list[str] = []
    while text:
        if len(text) <= _TG_LIMIT:
            chunks.append(text)
            break
        cut = text.rfind("\n", 0, _TG_LIMIT)
        if cut <= 0:
            cut = _TG_LIMIT
        chunks.append(text[:cut])
        text = text[cut:].lstrip("\n")
    return chunks


@router.callback_query(F.data == "action:partner_chat")
async def start_chat(callback: CallbackQuery, state: FSMContext) -> None:
    user_id = callback.from_user.id
    if not await _can_access(user_id):
        await callback.answer("Доступно только партнёрам", show_alert=True)
        return

    await callback.answer()
    await state.set_state(PartnerChatStates.chatting)

    count = await count_partner_chat_messages(user_id)
    text = _INTRO
    if count:
        text += (
            f"\n\n📚 В этом чате уже <b>{count}</b> сообщений — продолжаем "
            "с того же места."
        )
    await callback.message.answer(text, reply_markup=_chat_keyboard())


@router.callback_query(F.data == "pchat:new")
async def new_chat(callback: CallbackQuery, state: FSMContext) -> None:
    user_id = callback.from_user.id
    if not await _can_access(user_id):
        await callback.answer("Доступно только партнёрам", show_alert=True)
        return
    await clear_partner_chat(user_id)
    await state.set_state(PartnerChatStates.chatting)
    await callback.answer("История очищена")
    await callback.message.answer(
        "🆕 Начали новый чат. Кидай пост или идею.",
        reply_markup=_chat_keyboard(),
    )


@router.callback_query(F.data == "pchat:exit")
async def exit_chat(callback: CallbackQuery, state: FSMContext) -> None:
    await callback.answer()
    await state.clear()
    await callback.message.answer(
        "Вышел из чата. История сохранена — вернёшься через "
        "/menu → 📝 Создание → 💬 Чат с Gemini.\n\nОткрыть меню: /menu"
    )


@router.message(Command("chat"))
async def cmd_chat(message: Message, state: FSMContext) -> None:
    user_id = message.from_user.id
    if not await _can_access(user_id):
        await message.answer("💬 Чат с Gemini доступен только партнёрам.")
        return
    await state.set_state(PartnerChatStates.chatting)
    count = await count_partner_chat_messages(user_id)
    text = _INTRO
    if count:
        text += f"\n\n📚 В этом чате уже <b>{count}</b> сообщений."
    await message.answer(text, reply_markup=_chat_keyboard())


@router.message(PartnerChatStates.chatting, F.text & ~F.text.startswith("/"))
async def on_chat_message(message: Message, state: FSMContext, bot: Bot) -> None:
    user_id = message.from_user.id
    # Защита: доступ мог измениться (сняли партнёрство).
    if not await _can_access(user_id):
        await state.clear()
        await message.answer("💬 Чат больше недоступен.")
        return

    user_text = (message.text or "").strip()
    if not user_text:
        return

    user = await get_user(user_id) or {}
    await add_partner_chat_message(user_id, "user", user_text)

    try:
        await bot.send_chat_action(message.chat.id, "typing")
    except Exception:  # noqa: BLE001 — индикатор не критичен
        pass

    history = await get_partner_chat_history(user_id, limit=_HISTORY_LIMIT)

    try:
        reply = await partner_chat_reply(user, history)
    except Exception as e:
        log.exception("Partner chat failed for user %s", user_id)
        await message.answer(
            "❌ Gemini не ответил. Попробуй ещё раз.\n\n"
            f"<code>{html.escape(type(e).__name__)}: "
            f"{html.escape(str(e))[:200]}</code>",
            reply_markup=_chat_keyboard(),
        )
        return

    if not reply:
        await message.answer(
            "❌ Пустой ответ от модели. Переформулируй запрос.",
            reply_markup=_chat_keyboard(),
        )
        return

    await add_partner_chat_message(user_id, "model", reply)

    chunks = _split(reply)
    for i, chunk in enumerate(chunks):
        # Клавиатуру вешаем только на последний кусок.
        # parse_mode=None: ответ модели — сырой текст, может содержать
        # <, >, &, которые сломали бы HTML-парсер Telegram.
        kb = _chat_keyboard() if i == len(chunks) - 1 else None
        await message.answer(chunk, reply_markup=kb, parse_mode=None)


@router.message(PartnerChatStates.chatting, ~F.text)
async def on_chat_non_text(message: Message) -> None:
    await message.answer(
        "Пока понимаю только текст. Вставь пост или идею текстом.",
        reply_markup=_chat_keyboard(),
    )
