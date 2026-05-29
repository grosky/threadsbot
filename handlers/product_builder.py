"""«🚀 Создать продукт» — продуктовый консультант на базе Gemini.

Флоу:
1. Юзер тапает «🚀 Создать продукт» в меню «Создание».
2. Гейтинг: подписка (как и упаковка профиля).
3. Q1: «Расскажи в чём ты реально хорош» (свободный текст).
4. Q2: «Сколько времени готов вкладывать?» (3 кнопки).
5. Gemini выдаёт 5 идей с ценами и обоснованием.
6. Сохраняем в БД, юзер может вернуться через /myproducts.
"""
from __future__ import annotations

import html
import logging

from aiogram import F, Router
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import (
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
)

from database import (
    get_product_pack,
    get_user,
    is_subscription_active,
    save_product_pack,
)
from gemini_service import generate_product_ideas

router = Router()
log = logging.getLogger(__name__)


class ProductBuilderStates(StatesGroup):
    asking_expertise = State()
    asking_effort = State()


# ---------- Keyboards ----------

def _cancel_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="❌ Отмена", callback_data="product:cancel")],
        ]
    )


def _effort_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(
                text="⚡ Минимум — пассивные продукты",
                callback_data="product:effort:low",
            )],
            [InlineKeyboardButton(
                text="⚖️ Средне — несколько часов в день",
                callback_data="product:effort:medium",
            )],
            [InlineKeyboardButton(
                text="🔥 Полное вовлечение — работа с клиентами",
                callback_data="product:effort:high",
            )],
            [InlineKeyboardButton(text="❌ Отмена", callback_data="product:cancel")],
        ]
    )


def _final_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(
                text="🔄 Сгенерить ещё 5 идей",
                callback_data="product:regenerate",
            )],
            [InlineKeyboardButton(text="📋 В меню", callback_data="menu:main")],
        ]
    )


# ---------- ENTRY ----------

@router.callback_query(F.data == "action:build_product")
async def start_product_builder(callback: CallbackQuery, state: FSMContext) -> None:
    user_id = callback.from_user.id

    if not await is_subscription_active(user_id):
        from database import can_use_free_trial as _can_trial
        from .generation import send_subscription_required
        await callback.answer()
        if await _can_trial(user_id):
            await callback.message.answer(
                "🔓 <b>«Создать продукт» доступно только по подписке.</b>\n\n"
                "Но у тебя есть <b>одна бесплатная генерация поста</b> — "
                "вернись в /menu → 📝 Создание → 🎁 «Сгенерить бесплатный пост»."
            )
        else:
            await send_subscription_required(callback.message, "Создать продукт")
        return

    user = await get_user(user_id)
    if not user or not user.get("onboarding_complete"):
        await callback.answer()
        await callback.message.answer("Сначала заполни профиль — /start.")
        return

    await callback.answer()
    await callback.message.answer(
        "🚀 <b>Создать продукт</b>\n\n"
        "Помогу определиться что продавать, за сколько и как именно — "
        "под твою нишу, ЦА и опыт.\n\n"
        "<b>Вопрос 1 из 2</b>\n\n"
        "Расскажи в чём ты реально хорош. Что ты делал, чему можешь "
        "научить, какие есть результаты?\n\n"
        "<i>Например:</i>\n"
        "— 10 лет в маркетинге, выводил 50+ продуктов на рынок\n"
        "— 3 года развиваю свой YouTube канал с нуля до 100к\n"
        "— фотограф, 200 свадеб за карьеру\n\n"
        "Одним сообщением. Чем конкретнее — тем точнее идеи.",
        reply_markup=_cancel_kb(),
    )
    await state.set_state(ProductBuilderStates.asking_expertise)


# ---------- Q1: ЭКСПЕРТИЗА ----------

@router.message(ProductBuilderStates.asking_expertise, F.text & ~F.text.startswith("/"))
async def on_expertise(message: Message, state: FSMContext) -> None:
    expertise = (message.text or "").strip()
    await state.update_data(expertise=expertise)
    await message.answer(
        "<b>Вопрос 2 из 2</b>\n\n"
        "Сколько времени готов вкладывать в продукт?\n\n"
        "<i>Это определит тип продукта:</i>\n"
        "— Минимум → инфопродукты, шаблоны, чек-листы (пассивный доход)\n"
        "— Средне → курсы, обучающие программы\n"
        "— Максимум → коучинг, консультации, наставничество",
        reply_markup=_effort_kb(),
    )
    await state.set_state(ProductBuilderStates.asking_effort)


# ---------- Q2: УРОВЕНЬ ВОВЛЕЧЁННОСТИ ----------

@router.callback_query(
    ProductBuilderStates.asking_effort, F.data.startswith("product:effort:"),
)
async def on_effort(callback: CallbackQuery, state: FSMContext) -> None:
    effort = callback.data.split(":", 2)[2]
    if effort not in ("low", "medium", "high"):
        await callback.answer("Неизвестный выбор", show_alert=True)
        return

    await callback.answer()
    try:
        await callback.message.edit_reply_markup(reply_markup=None)
    except Exception:
        pass
    await _run_generation(
        callback.message, callback.from_user.id, effort, state,
    )


# ---------- Cancel ----------

@router.callback_query(F.data == "product:cancel")
async def cancel_product(callback: CallbackQuery, state: FSMContext) -> None:
    await state.clear()
    await callback.answer("Отменено")
    try:
        await callback.message.edit_reply_markup(reply_markup=None)
    except Exception:
        pass


# ---------- ГЕНЕРАЦИЯ ----------

async def _run_generation(
    message: Message,
    user_id: int,
    effort: str,
    state: FSMContext,
) -> None:
    data = await state.get_data()
    expertise = data.get("expertise", "") or ""

    if not await is_subscription_active(user_id):
        await message.answer("🔓 Подписка истекла во время заполнения. /start → подписка.")
        await state.clear()
        return

    profile = await get_user(user_id)
    if not profile:
        await message.answer("Профиль не найден. /start.")
        await state.clear()
        return

    status_msg = await message.answer(
        "🧠 Подбираю продукты под твой профиль... ~30-40 секунд"
    )

    try:
        pack = await generate_product_ideas(profile, expertise, effort)
    except Exception as e:
        log.exception("Product builder failed for user %s", user_id)
        await status_msg.edit_text(
            "❌ Что-то пошло не так. Попробуй ещё раз через минуту.\n\n"
            f"<code>{html.escape(type(e).__name__)}: {html.escape(str(e))[:200]}</code>"
        )
        await state.clear()
        return

    # Сохраняем + входной контекст для regenerate
    pack["_expertise"] = expertise
    pack["_effort"] = effort
    await save_product_pack(user_id, pack)

    await status_msg.delete()
    await _send_pack(message, pack)
    await state.clear()


async def _send_pack(message: Message, pack: dict) -> None:
    """Шлёт сводку + 5 идей карточками."""
    summary = str(pack.get("summary", "")).strip()
    if summary:
        await message.answer(
            "🚀 <b>Готово. Вот 5 идей под твой профиль.</b>\n\n"
            f"<i>{html.escape(summary)}</i>"
        )

    ideas = pack.get("ideas", [])
    for idea in ideas:
        await message.answer(_format_idea(idea))

    await message.answer(
        "Это всё. Идеи сохранены — посмотреть снова можно командой /myproducts.\n\n"
        "Если хочется альтернативы — жми «🔄 Сгенерить ещё 5 идей».",
        reply_markup=_final_kb(),
    )


TIER_LABELS = {
    "tripwire": "💥 Tripwire (импульс)",
    "low": "📦 Low-ticket",
    "mid": "📚 Mid-ticket",
    "high": "🎯 High-ticket",
    "premium": "👑 Premium",
}


def _format_idea(idea: dict) -> str:
    """Карточка одной продуктовой идеи."""
    name = html.escape(str(idea.get("name", "—")))
    tier_key = (idea.get("tier") or "").lower()
    tier = TIER_LABELS.get(tier_key, tier_key.title() or "—")
    product_type = html.escape(str(idea.get("type", "—")))
    price = idea.get("price_rub", 0)
    try:
        price_str = f"{int(price):,}".replace(",", " ") + " ₽"
    except (ValueError, TypeError):
        price_str = "—"

    inside_items = idea.get("what_inside") or []
    inside_lines = "\n".join(
        f"— {html.escape(str(item))}" for item in inside_items
    )

    for_whom = html.escape(str(idea.get("for_whom", "—")))
    why_price = html.escape(str(idea.get("why_this_price", "—")))
    how_sell = html.escape(str(idea.get("how_to_sell", "—")))
    effort = html.escape(str(idea.get("effort_for_author", "—")))

    return (
        f"<b>{name}</b>\n"
        f"{tier} · {product_type} · <b>{price_str}</b>\n\n"
        f"<b>Что внутри:</b>\n{inside_lines}\n\n"
        f"<b>Кому:</b> {for_whom}\n\n"
        f"<b>Почему такая цена:</b> {why_price}\n\n"
        f"<b>Как продавать:</b> {how_sell}\n\n"
        f"<b>Сколько вкладывать:</b> {effort}"
    )


# ---------- РЕГЕНЕРАЦИЯ ----------

@router.callback_query(F.data == "product:regenerate")
async def regenerate_pack(callback: CallbackQuery) -> None:
    user_id = callback.from_user.id

    if not await is_subscription_active(user_id):
        await callback.answer("Подписка неактивна", show_alert=True)
        return

    pack = await get_product_pack(user_id)
    if not pack:
        await callback.answer(
            "Сначала запусти через меню — Создание → 🚀 Создать продукт.",
            show_alert=True,
        )
        return

    profile = await get_user(user_id)
    if not profile:
        await callback.answer("Профиль не найден", show_alert=True)
        return

    expertise = pack.get("_expertise", "") or ""
    effort = pack.get("_effort", "medium") or "medium"

    await callback.answer("🧠 Генерю ещё 5 идей...")
    status_msg = await callback.message.answer(
        "🧠 Подбираю ещё 5 идей... ~30-40 секунд"
    )

    try:
        new_pack = await generate_product_ideas(profile, expertise, effort)
    except Exception as e:
        log.exception("Product regen failed: user=%s", user_id)
        await status_msg.edit_text(
            "❌ Не получилось. Попробуй ещё раз через минуту.\n\n"
            f"<code>{html.escape(type(e).__name__)}: {html.escape(str(e))[:200]}</code>"
        )
        return

    # Сохраняем + сохраняем входной контекст
    new_pack["_expertise"] = expertise
    new_pack["_effort"] = effort
    await save_product_pack(user_id, new_pack)

    await status_msg.delete()
    await _send_pack(callback.message, new_pack)


# ---------- /myproducts — посмотреть последние идеи ----------

@router.message(Command("myproducts"))
async def cmd_myproducts(message: Message) -> None:
    pack = await get_product_pack(message.from_user.id)
    if not pack:
        await message.answer(
            "У тебя пока нет сохранённых продуктовых идей.\n\n"
            "Зайди в /menu → «📝 Создание» → «🚀 Создать продукт»."
        )
        return
    await _send_pack(message, pack)
