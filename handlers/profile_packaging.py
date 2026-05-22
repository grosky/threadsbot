"""Упаковка профиля с нуля — для новичков в Threads.

Флоу:
1. Юзер тапает «🆕 Упаковать профиль» (или сюда попадает из онбординга).
2. Гейтинг: подписка или free trial.
3. Q1 — что есть из соцсетей/сайтов (free text).
4. Q2 — как хочешь чтобы воспринимали (free text + /skip).
5. Генерация всех 4 блоков одним Gemini-запросом.
6. Показываем 4 карточки: имя / bio / ссылка / закреп — каждая с кнопкой 🔄.
7. Сохраняем в БД, перегенерация любого блока обновляет соответствующее поле.
"""
from __future__ import annotations

import html
import logging

from aiogram import F, Router
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.filters import Command
from aiogram.types import (
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
)

from database import (
    get_profile_pack,
    get_user,
    has_access,
    save_profile_pack,
    update_profile_pack_block,
)
from gemini_service import generate_profile_pack, regenerate_pack_block

router = Router()
log = logging.getLogger(__name__)


# ---------- FSM ----------

class PackagingStates(StatesGroup):
    asking_socials = State()
    asking_perception = State()


# ---------- Хранилка контекста для перегенераций ----------
#
# Чтобы кнопка 🔄 могла регенерить блок, нужны исходные ответы юзера
# (socials + perception). Храним их в БД отдельно — добавим в pack как
# служебные поля `_socials_input` / `_perception_input`.


# ---------- Keyboards ----------

def _skip_perception_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="⏭ Пропустить", callback_data="pack:skip_perception")],
            [InlineKeyboardButton(text="❌ Отмена", callback_data="pack:cancel")],
        ]
    )


def _cancel_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="❌ Отмена", callback_data="pack:cancel")],
        ]
    )


def _block_kb(block: str) -> InlineKeyboardMarkup:
    """Кнопка перегенерации одного блока упаковки."""
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(
                text="🔄 Перегенерить",
                callback_data=f"pack:regen:{block}",
            )],
        ]
    )


def _final_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="📋 Готово", callback_data="menu:main")],
        ]
    )


# ---------- ENTRY ----------

@router.callback_query(F.data == "action:pack_profile")
async def start_packaging(callback: CallbackQuery, state: FSMContext) -> None:
    user_id = callback.from_user.id

    from database import is_subscription_active, can_use_free_trial
    if not await is_subscription_active(user_id):
        await callback.answer()
        from .generation import send_subscription_required
        if await can_use_free_trial(user_id):
            await callback.message.answer(
                "🔓 <b>«Упаковка профиля» доступна только по подписке.</b>\n\n"
                "Но у тебя есть <b>одна бесплатная генерация</b> — вернись в "
                "/menu → 📝 Создание → 🎁 «Сгенерить бесплатный пост»."
            )
        else:
            await send_subscription_required(callback.message, "Упаковка профиля")
        return

    user = await get_user(user_id)
    if not user or not user.get("onboarding_complete"):
        await callback.answer()
        await callback.message.answer(
            "Сначала заполни профиль — /start."
        )
        return

    await callback.answer()
    await callback.message.answer(
        "🆕 <b>Упаковка профиля Threads</b>\n\n"
        "Соберу за тебя имя, bio, рекомендацию по ссылке и закреп — "
        "под твою нишу и ЦА.\n\n"
        "<b>Вопрос 1 из 2</b>\n\n"
        "Что у тебя уже есть из соцсетей или сайтов? "
        "Telegram-канал, Instagram, сайт, лендинг — кидай ссылки "
        "или коротко напиши что есть.\n\n"
        "Если совсем ничего — напиши «ничего».",
        reply_markup=_cancel_kb(),
    )
    await state.set_state(PackagingStates.asking_socials)


# ---------- Точка входа из онбординга ----------

async def start_packaging_from_onboarding(message: Message, state: FSMContext) -> None:
    """Запуск упаковки сразу после онбординга (без callback)."""
    await message.answer(
        "🆕 <b>Упаковка профиля Threads</b>\n\n"
        "Раз ты только начинаешь — давай соберу имя, bio, "
        "рекомендацию по ссылке и закреп под твою нишу.\n\n"
        "<b>Вопрос 1 из 2</b>\n\n"
        "Что у тебя уже есть из соцсетей или сайтов? "
        "Telegram-канал, Instagram, сайт, лендинг — кидай ссылки "
        "или коротко напиши что есть.\n\n"
        "Если совсем ничего — напиши «ничего».",
        reply_markup=_cancel_kb(),
    )
    await state.set_state(PackagingStates.asking_socials)


# ---------- Q1: соцсети ----------

@router.message(PackagingStates.asking_socials, F.text & ~F.text.startswith("/"))
async def on_socials(message: Message, state: FSMContext) -> None:
    socials = (message.text or "").strip()
    await state.update_data(socials=socials)
    await message.answer(
        "<b>Вопрос 2 из 2</b>\n\n"
        "Как хочешь чтобы тебя воспринимали в Threads? "
        "Одной фразой — например «как эксперт-практик», "
        "«как свой парень с провокационным мнением», "
        "«как тот кто упрощает сложное».\n\n"
        "Если без идей — жми «Пропустить», бот опирёся на онбординг.",
        reply_markup=_skip_perception_kb(),
    )
    await state.set_state(PackagingStates.asking_perception)


# ---------- Q2: восприятие ----------

@router.callback_query(PackagingStates.asking_perception, F.data == "pack:skip_perception")
async def perception_skip(callback: CallbackQuery, state: FSMContext) -> None:
    await callback.answer()
    await callback.message.edit_reply_markup(reply_markup=None)
    await _run_full_generation(callback.message, callback.from_user.id, "", state)


@router.message(PackagingStates.asking_perception, F.text & ~F.text.startswith("/"))
async def on_perception(message: Message, state: FSMContext) -> None:
    perception = (message.text or "").strip()
    await _run_full_generation(message, message.from_user.id, perception, state)


# ---------- /cancel ----------

@router.callback_query(F.data == "pack:cancel")
async def cancel_packaging(callback: CallbackQuery, state: FSMContext) -> None:
    await state.clear()
    await callback.answer("Отменено")
    try:
        await callback.message.edit_reply_markup(reply_markup=None)
    except Exception:
        pass


# ---------- ГЕНЕРАЦИЯ + ПОКАЗ ----------

async def _run_full_generation(
    message: Message,
    user_id: int,
    perception: str,
    state: FSMContext,
) -> None:
    data = await state.get_data()
    socials = data.get("socials", "") or ""

    # Двойная проверка доступа
    access_ok, _r = await has_access(user_id)
    if not access_ok:
        await message.answer("🔓 Доступ закончился. /start → «💎 Оформить подписку».")
        await state.clear()
        return

    profile = await get_user(user_id)
    if not profile:
        await message.answer("Профиль не найден. /start.")
        await state.clear()
        return

    status_msg = await message.answer("🧠 Собираю упаковку... ~20-30 секунд")

    try:
        pack = await generate_profile_pack(profile, socials, perception)
    except Exception as e:
        log.exception("Profile packaging failed for user %s", user_id)
        await status_msg.edit_text(
            "❌ Что-то пошло не так. Попробуй ещё раз через минуту.\n\n"
            f"<code>{html.escape(type(e).__name__)}: {html.escape(str(e))[:200]}</code>"
        )
        await state.clear()
        return

    # Сохраняем pack + исходные ответы (для перегенераций)
    pack["_socials_input"] = socials
    pack["_perception_input"] = perception
    await save_profile_pack(user_id, pack)

    await status_msg.delete()
    await _send_all_cards(message, pack)
    await state.clear()


async def _send_all_cards(message: Message, pack: dict) -> None:
    """Отправляет 4 карточки с результатами упаковки."""
    await message.answer(
        "✅ <b>Готово.</b> Вот твоя упаковка — 4 блока ниже.\n\n"
        "Под каждым есть «🔄 Перегенерить» если хочется альтернативу. "
        "Сохраняй в Threads вручную (бот пока не умеет менять профиль автоматически)."
    )

    await message.answer(_format_names(pack.get("names", [])), reply_markup=_block_kb("names"))
    await message.answer(_format_bios(pack.get("bios", [])), reply_markup=_block_kb("bios"))
    await message.answer(_format_link(pack.get("link_recommendation", "")), reply_markup=_block_kb("link"))
    await message.answer(_format_pinned(pack.get("pinned_posts", [])), reply_markup=_block_kb("pinned"))

    await message.answer(
        "Всё. Возвращайся в /menu когда упакуешь профиль — там тебя ждёт "
        "генерация постов и разбор лент.",
        reply_markup=_final_kb(),
    )


# ---------- Форматирование карточек ----------

def _format_names(names: list) -> str:
    lines = ["👤 <b>1/4 · Имя для отображения</b>", ""]
    if not names:
        lines.append("<i>Не удалось сгенерировать — попробуй перегенерить.</i>")
        return "\n".join(lines)
    for i, n in enumerate(names, 1):
        text = html.escape(str(n.get("text", "—")))
        logic = html.escape(str(n.get("logic", "")))
        lines.append(f"<b>Вариант {i}:</b> <code>{text}</code>")
        if logic:
            lines.append(f"<i>{logic}</i>")
        lines.append("")
    return "\n".join(lines).rstrip()


def _format_bios(bios: list) -> str:
    lines = ["📝 <b>2/4 · Bio (до 160 символов)</b>", ""]
    if not bios:
        lines.append("<i>Не удалось сгенерировать — попробуй перегенерить.</i>")
        return "\n".join(lines)
    for i, b in enumerate(bios, 1):
        text = str(b.get("text", "—"))
        # Считаем символы сами на всякий случай
        real_chars = len(text)
        claimed = b.get("chars", real_chars)
        tone_note = html.escape(str(b.get("tone_note", "")))
        chars_label = f"{real_chars}/160"
        if real_chars > 160:
            chars_label += " ⚠️"
        elif real_chars != claimed:
            # Gemini посчитал криво — берём реальное
            pass
        lines.append(f"<b>Вариант {i}</b> · <i>{chars_label}</i>")
        lines.append(f"<code>{html.escape(text)}</code>")
        if tone_note:
            lines.append(f"<i>{tone_note}</i>")
        lines.append("")
    return "\n".join(lines).rstrip()


def _format_link(recommendation: str) -> str:
    lines = ["🔗 <b>3/4 · Куда вести ссылку</b>", ""]
    if not recommendation:
        lines.append("<i>Не удалось сгенерировать — попробуй перегенерить.</i>")
    else:
        lines.append(html.escape(recommendation))
    return "\n".join(lines)


def _format_pinned(pinned: list) -> str:
    lines = ["📌 <b>4/4 · Закреплённый пост</b>", ""]
    if not pinned:
        lines.append("<i>Не удалось сгенерировать — попробуй перегенерить.</i>")
        return "\n".join(lines)
    for i, p in enumerate(pinned, 1):
        fmt = html.escape(str(p.get("format", "—")))
        text = html.escape(str(p.get("text", "—")))
        lines.append(f"<b>Вариант {i}</b> · <i>формат: {fmt}</i>")
        lines.append("")
        lines.append(text)
        lines.append("")
        lines.append("─" * 10)
        lines.append("")
    # Подрезаем если слишком длинно (Telegram limit ~4096)
    full = "\n".join(lines).rstrip()
    if len(full) > 4000:
        full = full[:4000] + "\n\n…(обрезано)"
    return full


# ---------- ПЕРЕГЕНЕРАЦИЯ ----------

@router.callback_query(F.data.startswith("pack:regen:"))
async def regen_block(callback: CallbackQuery) -> None:
    block = callback.data.split(":", 2)[2]
    if block not in ("names", "bios", "link", "pinned"):
        await callback.answer("Неизвестный блок", show_alert=True)
        return

    user_id = callback.from_user.id

    access_ok, _r = await has_access(user_id)
    if not access_ok:
        await callback.answer(
            "Доступ закончился — оформи подписку.",
            show_alert=True,
        )
        return

    pack = await get_profile_pack(user_id)
    if not pack:
        await callback.answer(
            "Упаковка не найдена — запусти заново через меню.",
            show_alert=True,
        )
        return

    profile = await get_user(user_id)
    if not profile:
        await callback.answer("Профиль не найден", show_alert=True)
        return

    socials = pack.get("_socials_input", "") or ""
    perception = pack.get("_perception_input", "") or ""

    await callback.answer("🧠 Регенерю...")
    status_msg = await callback.message.answer("🔄 Перегенерирую блок...")

    try:
        result = await regenerate_pack_block(profile, socials, perception, block)
    except Exception as e:
        log.exception("Pack regen failed: user=%s block=%s", user_id, block)
        await status_msg.edit_text(
            "❌ Не получилось. Попробуй ещё раз через минуту.\n\n"
            f"<code>{html.escape(type(e).__name__)}: {html.escape(str(e))[:200]}</code>"
        )
        return

    # Сохраняем обновлённый блок в pack
    block_key_map = {
        "names": "names",
        "bios": "bios",
        "link": "link_recommendation",
        "pinned": "pinned_posts",
    }
    pack_key = block_key_map[block]
    new_value = result.get(pack_key)
    if new_value is None:
        await status_msg.edit_text(
            "❌ Gemini не вернул блок. Попробуй ещё раз."
        )
        return

    await update_profile_pack_block(user_id, pack_key, new_value)

    # Рендерим заново
    formatter_map = {
        "names": _format_names,
        "bios": _format_bios,
        "link": _format_link,
        "pinned": _format_pinned,
    }
    text = formatter_map[block](new_value)

    await status_msg.delete()
    await callback.message.answer(text, reply_markup=_block_kb(block))


# ---------- /mypack — посмотреть последнюю упаковку ----------

@router.message(Command("mypack"))
async def cmd_mypack(message: Message) -> None:
    pack = await get_profile_pack(message.from_user.id)
    if not pack:
        await message.answer(
            "У тебя пока нет сохранённой упаковки. "
            "Зайди в /menu → «📝 Создание» → «🆕 Упаковать профиль»."
        )
        return
    await _send_all_cards(message, pack)
