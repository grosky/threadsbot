"""Онбординг профиля: 4-5 простых вопросов.

Q1: о себе и нише (niche + product одновременно)
Q2: о читателе и его болях (audience + pains)
Q3: tone of voice (кнопки)
Q4: куда вести трафик (product_link)
Q5 (опционально): результаты / social proof
"""
from aiogram import F, Router
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
        "<b>Вопрос 1 из 3</b>\n\n"
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
        "<b>Вопрос 2 из 3</b>\n\n"
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
        "<b>Вопрос 3 из 3</b>\n\n"
        "Каким голосом писать посты?",
        reply_markup=tone_keyboard(),
    )
    await state.set_state(OnboardingStates.tone)


# ---------- Q3: TONE → ЗАВЕРШЕНИЕ ----------

@router.callback_query(OnboardingStates.tone, F.data.startswith("tone:"))
async def on_tone(callback: CallbackQuery, state: FSMContext) -> None:
    tone_value = TONE_LABELS.get(callback.data, "ЭКСПЕРТНЫЙ")
    await update_profile_field(callback.from_user.id, "tone", tone_value)
    await callback.answer()
    await callback.message.edit_reply_markup(reply_markup=None)
    # Раньше тут были ещё 2 вопроса (ссылка + social_proof). Убраны для
    # сокращения drop-off на онбординге. Эти поля остаются пустыми в БД —
    # юзер может добавить через профиль позже.
    # ВАЖНО: callback.message.from_user — это бот, поэтому user_id берём
    # из callback.from_user явно.
    await _complete_onboarding(
        callback.message, state, user_id=callback.from_user.id,
    )


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


async def _complete_onboarding(
    message: Message, state: FSMContext, user_id: int | None = None,
) -> None:
    # user_id передаётся явно из callback-хэндлеров, иначе берём из message.
    # Это критично потому что message от CallbackQuery — это сообщение бота,
    # message.from_user.id вернёт ID бота, а не юзера.
    if user_id is None:
        user_id = message.from_user.id
    await mark_onboarding_complete(user_id)

    sub_active = await is_subscription_active(user_id)
    trial_available = await can_use_free_trial(user_id)

    # Упаковка профиля доступна только по подписке. Free-trial юзерам
    # сразу указываем на бесплатную генерацию поста — это их главное действие.
    if not sub_active and trial_available:
        await _finalize_onboarding_without_packaging(message, state, user_id=user_id)
        return

    # Подписчикам предлагаем упаковку профиля
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
    await _finalize_onboarding_without_packaging(
        callback.message, state, user_id=callback.from_user.id,
    )


async def _finalize_onboarding_without_packaging(
    message: Message, state: FSMContext, user_id: int | None = None,
) -> None:
    # См. комментарий в _complete_onboarding про message от CallbackQuery.
    if user_id is None:
        user_id = message.from_user.id
    await state.clear()

    sub_active = await is_subscription_active(user_id)
    trial_available = await can_use_free_trial(user_id)

    if not sub_active and trial_available:
        # Сразу запускаем выбор темы для бесплатной генерации, не показываем
        # меню — убираем 3-4 лишних клика. Юзер только что прошёл онбординг,
        # горячий — нужно выдать вау-результат как можно быстрее.
        from .generation import FreeTrialStates, free_topic_keyboard
        await message.answer(
            "<b>Профиль настроен. Сразу делаю первый пост.</b>\n\n"
            "🎁 О чём писать? Напиши тему или сырую мысль:\n\n"
            "<i>Тема:</i> «как набрать первую тысячу подписчиков»\n"
            "<i>Сырая мысль:</i> «вчера в кафе подслушал как девушки "
            "обсуждали что курсы все одинаковые»\n\n"
            "Бот сделает один развёрнутый пост под твою нишу.\n"
            "Или жми «🎲 Удиви меня» — подберёт тему сам.",
            reply_markup=free_topic_keyboard(),
        )
        await state.set_state(FreeTrialStates.entering_topic)
        return

    await message.answer("Открой меню и начнём:")
    await show_main_menu(message)
