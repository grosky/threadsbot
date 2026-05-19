"""Онбординг профиля: 4-5 простых вопросов.

Q1: о себе и нише (niche + product одновременно)
Q2: о читателе и его болях (audience + pains)
Q3: tone of voice (кнопки)
Q4: куда вести трафик (product_link)
Q5 (опционально): результаты / social proof
"""
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
    can_use_free_trial,
    is_subscription_active,
    mark_onboarding_complete,
    update_profile_field,
)

from .menu import show_main_menu

router = Router()


class OnboardingStates(StatesGroup):
    niche = State()
    audience = State()
    tone = State()
    product_link = State()
    social_proof = State()
    offer_packaging = State()


TONE_LABELS = {
    "tone:friendly": "ДРУЖЕЛЮБНЫЙ",
    "tone:expert": "ЭКСПЕРТНЫЙ",
    "tone:provocative": "ПРОВОКАЦИОННЫЙ",
    "tone:humor": "С ЮМОРОМ",
}


def tone_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="🤗 Дружелюбно", callback_data="tone:friendly")],
            [InlineKeyboardButton(text="🎓 Экспертно", callback_data="tone:expert")],
            [InlineKeyboardButton(text="🔥 Провокационно", callback_data="tone:provocative")],
            [InlineKeyboardButton(text="😎 С юмором", callback_data="tone:humor")],
        ]
    )


# ---------- ENTRY ----------

async def start_onboarding(message: Message, state: FSMContext) -> None:
    """Запускает онбординг. Вызывается из start.py."""
    await message.answer(
        "<b>Вопрос 1 из 4</b>\n\n"
        "Расскажи коротко чем занимаешься.\n\n"
        "Например:\n"
        "— продаю курс по маркетингу для предпринимателей\n"
        "— делаю UX-дизайн для стартапов\n"
        "— веду блог про продуктивность с СДВГ\n\n"
        "Одним сообщением — профессия + что продаёшь (если продаёшь)."
    )
    await state.set_state(OnboardingStates.niche)


# ---------- Q1: НИША + ПРОДУКТ ----------

@router.message(OnboardingStates.niche, F.text & ~F.text.startswith("/"))
async def on_niche(message: Message, state: FSMContext) -> None:
    text = (message.text or "").strip()
    user_id = message.from_user.id

    # Дублируем в niche + product — Gemini получит одинаковую инфу,
    # пользователь не вводит дважды.
    await update_profile_field(user_id, "niche", text)
    await update_profile_field(user_id, "product", text)

    await message.answer(
        "<b>Вопрос 2 из 4</b>\n\n"
        "Для кого пишешь? Кто эти люди и что у них болит.\n\n"
        "Например:\n"
        "— фрилансеры 25-35, устали от нестабильности и боятся "
        "брать дороже\n"
        "— мамы в декрете, хотят вернуться к работе, но боятся "
        "что отстали от рынка\n\n"
        "Одним сообщением — кто + чего хочет / чего боится."
    )
    await state.set_state(OnboardingStates.audience)


# ---------- Q2: АУДИТОРИЯ + БОЛИ ----------

@router.message(OnboardingStates.audience, F.text & ~F.text.startswith("/"))
async def on_audience(message: Message, state: FSMContext) -> None:
    text = (message.text or "").strip()
    user_id = message.from_user.id

    # Дублируем в audience + pains
    await update_profile_field(user_id, "audience", text)
    await update_profile_field(user_id, "pains", text)

    await message.answer(
        "<b>Вопрос 3 из 4</b>\n\n"
        "Каким голосом писать посты?",
        reply_markup=tone_keyboard(),
    )
    await state.set_state(OnboardingStates.tone)


# ---------- Q3: TONE ----------

@router.callback_query(OnboardingStates.tone, F.data.startswith("tone:"))
async def on_tone(callback: CallbackQuery, state: FSMContext) -> None:
    tone_value = TONE_LABELS.get(callback.data, "ЭКСПЕРТНЫЙ")
    await update_profile_field(callback.from_user.id, "tone", tone_value)
    await callback.answer()
    await callback.message.edit_reply_markup(reply_markup=None)
    await callback.message.answer(
        f"Выбран: <b>{tone_value}</b>\n\n"
        "<b>Вопрос 4 из 4</b>\n\n"
        "Куда отправлять читателей в конце поста?\n"
        "Telegram, сайт, бот, страница в Threads — любая ссылка.\n\n"
        "Если ещё нет — пиши «нет»."
    )
    await state.set_state(OnboardingStates.product_link)


# ---------- Q4: PRODUCT LINK ----------

@router.message(OnboardingStates.product_link, F.text & ~F.text.startswith("/"))
async def on_product_link(message: Message, state: FSMContext) -> None:
    text = (message.text or "").strip()
    if text.lower() in ("нет", "no", "—", "-"):
        text = ""
    await update_profile_field(message.from_user.id, "product_link", text)

    await message.answer(
        "<b>Бонус-вопрос</b> <i>(можно /skip)</i>\n\n"
        "Конкретные результаты или цифры для убедительности постов?\n\n"
        "Например:\n"
        "— помог 47 клиентам выйти на 500к+ ₽/мес\n"
        "— 12 лет в индустрии\n"
        "— набрал 12к подписчиков в Threads за 2 недели\n\n"
        "Делает посты в разы убедительнее. Если нет — жми /skip."
    )
    await state.set_state(OnboardingStates.social_proof)


# ---------- Q5 (OPTIONAL): SOCIAL PROOF ----------

@router.message(OnboardingStates.social_proof, Command("skip"))
async def on_social_proof_skip(message: Message, state: FSMContext) -> None:
    await _complete_onboarding(message, state)


@router.message(OnboardingStates.social_proof, F.text & ~F.text.startswith("/"))
async def on_social_proof(message: Message, state: FSMContext) -> None:
    text = (message.text or "").strip()
    await update_profile_field(message.from_user.id, "social_proof", text)
    # facts оставляем пустым — мы его слили в social_proof по умолчанию,
    # Gemini обработает корректно даже без personal facts
    await _complete_onboarding(message, state)


# ---------- COMPLETION ----------

def _packaging_offer_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(
                text="🆕 Помоги упаковать",
                callback_data="onboard:pack_yes",
            )],
            [InlineKeyboardButton(
                text="Уже всё готово, пропустить",
                callback_data="onboard:pack_no",
            )],
        ]
    )


async def _complete_onboarding(message: Message, state: FSMContext) -> None:
    user_id = message.from_user.id
    await mark_onboarding_complete(user_id)

    # Спрашиваем — упаковать ли профиль с нуля
    await message.answer(
        "<b>Профиль настроен.</b>\n\n"
        "Сначала скажи — у тебя <b>уже есть профиль в Threads</b> с именем, "
        "bio, аватаркой и закрепом? Или ты с нуля?\n\n"
        "Если с нуля — соберу за тебя имя, bio, ссылку и закреп под твою нишу.",
        reply_markup=_packaging_offer_keyboard(),
    )
    await state.set_state(OnboardingStates.offer_packaging)


@router.callback_query(OnboardingStates.offer_packaging, F.data == "onboard:pack_yes")
async def on_pack_yes(callback: CallbackQuery, state: FSMContext) -> None:
    from .profile_packaging import start_packaging_from_onboarding
    await callback.answer()
    try:
        await callback.message.edit_reply_markup(reply_markup=None)
    except Exception:
        pass
    await start_packaging_from_onboarding(callback.message, state)


@router.callback_query(OnboardingStates.offer_packaging, F.data == "onboard:pack_no")
async def on_pack_no(callback: CallbackQuery, state: FSMContext) -> None:
    await callback.answer()
    try:
        await callback.message.edit_reply_markup(reply_markup=None)
    except Exception:
        pass
    await _finalize_onboarding_without_packaging(callback.message, state)


async def _finalize_onboarding_without_packaging(message: Message, state: FSMContext) -> None:
    user_id = message.from_user.id
    await state.clear()

    sub_active = await is_subscription_active(user_id)
    trial_available = await can_use_free_trial(user_id)

    if not sub_active and trial_available:
        from config import config as _cfg
        sub_lines = [
            "— 4 генерации в день",
            "— Анализ профиля и чужих лент",
            "— Доработка постов (жёстче / мягче / по фидбеку)",
        ]
        if _cfg.threads_publish_enabled:
            sub_lines.insert(1, "— Авто-публикация в Threads")
        sub_text = "\n".join(sub_lines)

        await message.answer(
            "Окей.\n\n"
            "Что доступно бесплатно (одна попытка):\n"
            "— Сгенерить пост в одном из 5 форматов\n"
            "— Голосовой сторителлинг — наговариваешь, "
            "бот собирает живой пост\n\n"
            "Что откроется после подписки:\n"
            f"{sub_text}\n\n"
            "Жми «Меню» → «Создание» чтобы начать."
        )
    else:
        await message.answer("Открой меню и начнём:")
    await show_main_menu(message)
