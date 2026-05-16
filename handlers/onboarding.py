"""FSM для онбординга профиля автора (8 вопросов)."""
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

from database import mark_onboarding_complete, update_profile_field

from .menu import show_main_menu

router = Router()


class OnboardingStates(StatesGroup):
    niche = State()
    audience = State()
    product = State()
    product_link = State()
    tone = State()
    facts = State()
    pains = State()
    social_proof = State()


TONE_LABELS = {
    "tone:friendly": "ДРУЖЕЛЮБНЫЙ",
    "tone:expert": "ЭКСПЕРТНЫЙ",
    "tone:provocative": "ПРОВОКАЦИОННЫЙ",
    "tone:humor": "С ЮМОРОМ",
}


def tone_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="🤗 Дружелюбный", callback_data="tone:friendly")],
            [InlineKeyboardButton(text="🎓 Экспертный", callback_data="tone:expert")],
            [InlineKeyboardButton(text="🔥 Провокационный", callback_data="tone:provocative")],
            [InlineKeyboardButton(text="😎 С юмором", callback_data="tone:humor")],
        ]
    )


async def start_onboarding(message: Message, state: FSMContext) -> None:
    """Запускает FSM онбординга. Вызывается из start.py после активации."""
    await message.answer(
        "<b>Вопрос 1 из 8: Ниша</b>\n\n"
        "В какой нише ты работаешь? Например: маркетинг, психология, "
        "криптовалюты, продуктивность, обучение языкам.\n\n"
        "Напиши коротко:"
    )
    await state.set_state(OnboardingStates.niche)


@router.message(OnboardingStates.niche, F.text)
async def on_niche(message: Message, state: FSMContext) -> None:
    await update_profile_field(
        message.from_user.id, "niche", (message.text or "").strip()
    )
    await message.answer(
        "<b>Вопрос 2 из 8: Целевая аудитория</b>\n\n"
        "Кто твоя ЦА? Опиши в 1-2 предложениях.\n\n"
        "<i>Пример: «Предприниматели 25-40 лет с оборотом 1-10 млн ₽/мес, "
        "которые устали платить за рекламу.»</i>"
    )
    await state.set_state(OnboardingStates.audience)


@router.message(OnboardingStates.audience, F.text)
async def on_audience(message: Message, state: FSMContext) -> None:
    await update_profile_field(
        message.from_user.id, "audience", (message.text or "").strip()
    )
    await message.answer(
        "<b>Вопрос 3 из 8: Продукт / цель аккаунта</b>\n\n"
        "Что ты продаёшь или зачем растишь аккаунт?\n\n"
        "<i>Пример: «Продаю курс по построению автоворонок в Telegram»</i>"
    )
    await state.set_state(OnboardingStates.product)


@router.message(OnboardingStates.product, F.text)
async def on_product(message: Message, state: FSMContext) -> None:
    await update_profile_field(
        message.from_user.id, "product", (message.text or "").strip()
    )
    await message.answer(
        "<b>Вопрос 4 из 8: Ссылка на продукт</b>\n\n"
        "Куда будут вести посты? Telegram-канал, бот, лендинг — любая ссылка.\n\n"
        "<i>Пример: <code>t.me/your_bot</code></i>"
    )
    await state.set_state(OnboardingStates.product_link)


@router.message(OnboardingStates.product_link, F.text)
async def on_product_link(message: Message, state: FSMContext) -> None:
    await update_profile_field(
        message.from_user.id, "product_link", (message.text or "").strip()
    )
    await message.answer(
        "<b>Вопрос 5 из 8: Tone of voice</b>\n\n"
        "Какой тон ближе твоему стилю?",
        reply_markup=tone_keyboard(),
    )
    await state.set_state(OnboardingStates.tone)


@router.callback_query(OnboardingStates.tone, F.data.startswith("tone:"))
async def on_tone(callback: CallbackQuery, state: FSMContext) -> None:
    tone_value = TONE_LABELS.get(callback.data, "ЭКСПЕРТНЫЙ")
    await update_profile_field(callback.from_user.id, "tone", tone_value)
    await callback.answer()
    await callback.message.edit_reply_markup(reply_markup=None)
    await callback.message.answer(
        f"Выбран: <b>{tone_value}</b>\n\n"
        "<b>Вопрос 6 из 8: Личные факты для сторителлинга</b> <i>(можно /skip)</i>\n\n"
        "Напиши 3-5 фактов о себе — сильно улучшит качество историй.\n\n"
        "<i>Пример: «Бывший SMM-щик, провалил 3 запуска прежде чем нашёл схему. "
        "СДВГ, обожаю автоматизировать всё подряд.»</i>"
    )
    await state.set_state(OnboardingStates.facts)


@router.message(OnboardingStates.facts, Command("skip"))
async def on_facts_skip(message: Message, state: FSMContext) -> None:
    await _ask_pains(message, state)


@router.message(OnboardingStates.facts, F.text)
async def on_facts(message: Message, state: FSMContext) -> None:
    await update_profile_field(
        message.from_user.id, "facts", (message.text or "").strip()
    )
    await _ask_pains(message, state)


async def _ask_pains(message: Message, state: FSMContext) -> None:
    await message.answer(
        "<b>Вопрос 7 из 8: Главные боли ЦА</b>\n\n"
        "Что больше всего болит у твоей аудитории? Перечисли 3-5 болей.\n\n"
        "<i>Пример: «Лиды дорогие. Рекламные кабинеты блокируют. Нет системы. "
        "Воронки протекают на каждом шаге.»</i>"
    )
    await state.set_state(OnboardingStates.pains)


@router.message(OnboardingStates.pains, F.text)
async def on_pains(message: Message, state: FSMContext) -> None:
    await update_profile_field(
        message.from_user.id, "pains", (message.text or "").strip()
    )
    await message.answer(
        "<b>Вопрос 8 из 8: Social proof</b> <i>(можно /skip)</i>\n\n"
        "Цифры или результаты для нативной продажи?\n\n"
        "<i>Пример: «Помог 47 клиентам выйти на 500к+ ₽/мес чистой прибыли»</i>"
    )
    await state.set_state(OnboardingStates.social_proof)


@router.message(OnboardingStates.social_proof, Command("skip"))
async def on_social_proof_skip(message: Message, state: FSMContext) -> None:
    await _complete_onboarding(message, state)


@router.message(OnboardingStates.social_proof, F.text)
async def on_social_proof(message: Message, state: FSMContext) -> None:
    await update_profile_field(
        message.from_user.id, "social_proof", (message.text or "").strip()
    )
    await _complete_onboarding(message, state)


async def _complete_onboarding(message: Message, state: FSMContext) -> None:
    await mark_onboarding_complete(message.from_user.id)
    await state.clear()
    await message.answer(
        "🎉 <b>Профиль настроен!</b>\n\n"
        "Теперь можно генерить посты. Тыкай в кнопку ниже:"
    )
    await show_main_menu(message)
