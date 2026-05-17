"""Ачивки: определения + логика разблокировки + уведомления.

Тихая геймификация — ачивка выпадает один раз, шлёт одно сообщение,
не назойлива. Стрик показывается в шапке меню.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Awaitable, Callable, Optional

from aiogram import Bot

from database import (
    count_successful_referrals,
    count_threads_publications,
    count_user_generations,
    count_voice_storytellings,
    get_streak,
    has_achievement,
    unlock_achievement,
)

log = logging.getLogger(__name__)


@dataclass
class Achievement:
    code: str
    emoji: str
    name: str
    description: str
    # Async-функция, проверяющая выполнено ли условие
    check: Callable[[int], Awaitable[bool]]


# ---------- Определения ачивок ----------

async def _check_first_generation(uid: int) -> bool:
    return await count_user_generations(uid) >= 1


async def _check_ten_generations(uid: int) -> bool:
    return await count_user_generations(uid) >= 10


async def _check_first_publish(uid: int) -> bool:
    return await count_threads_publications(uid) >= 1


async def _check_ten_publishes(uid: int) -> bool:
    return await count_threads_publications(uid) >= 10


async def _check_voice_5(uid: int) -> bool:
    return await count_voice_storytellings(uid) >= 5


async def _check_streak_7(uid: int) -> bool:
    return await get_streak(uid) >= 7


async def _check_streak_30(uid: int) -> bool:
    return await get_streak(uid) >= 30


async def _check_first_referral(uid: int) -> bool:
    return await count_successful_referrals(uid) >= 1


async def _check_three_referrals(uid: int) -> bool:
    return await count_successful_referrals(uid) >= 3


ACHIEVEMENTS: list[Achievement] = [
    Achievement(
        code="first_generation",
        emoji="✍️",
        name="Первый пост",
        description="Сгенерил первый пост в боте",
        check=_check_first_generation,
    ),
    Achievement(
        code="ten_generations",
        emoji="🎯",
        name="Серийный автор",
        description="10 сгенерированных постов",
        check=_check_ten_generations,
    ),
    Achievement(
        code="first_publish",
        emoji="📤",
        name="Дебют в Threads",
        description="Опубликовал первый пост в Threads",
        check=_check_first_publish,
    ),
    Achievement(
        code="ten_publishes",
        emoji="🚀",
        name="Десятка",
        description="10 публикаций в Threads",
        check=_check_ten_publishes,
    ),
    Achievement(
        code="voice_5",
        emoji="🎙",
        name="Голос автора",
        description="5 голосовых сторителлингов",
        check=_check_voice_5,
    ),
    Achievement(
        code="streak_7",
        emoji="🔥",
        name="Неделя в строю",
        description="7 дней активности подряд",
        check=_check_streak_7,
    ),
    Achievement(
        code="streak_30",
        emoji="💎",
        name="Месяц в строю",
        description="30 дней активности подряд",
        check=_check_streak_30,
    ),
    Achievement(
        code="first_referral",
        emoji="🎁",
        name="Сарафан запущен",
        description="Первый друг активировал промокод",
        check=_check_first_referral,
    ),
    Achievement(
        code="three_referrals",
        emoji="🌟",
        name="Амбассадор",
        description="3 друга активировали подписку",
        check=_check_three_referrals,
    ),
]

ACHIEVEMENTS_BY_CODE: dict[str, Achievement] = {a.code: a for a in ACHIEVEMENTS}


# ---------- Логика разблокировки ----------

async def check_and_award(
    user_id: int,
    bot: Optional[Bot] = None,
    codes: Optional[list[str]] = None,
) -> list[Achievement]:
    """Проверяет указанные коды (или все) и разблокирует те, что выполнены.

    Возвращает список РАЗБЛОКИРОВАННЫХ только что ачивок.
    Если bot передан — шлёт уведомление каждый раз.
    """
    candidates = (
        [ACHIEVEMENTS_BY_CODE[c] for c in codes if c in ACHIEVEMENTS_BY_CODE]
        if codes
        else ACHIEVEMENTS
    )

    newly_unlocked: list[Achievement] = []
    for ach in candidates:
        if await has_achievement(user_id, ach.code):
            continue
        try:
            if not await ach.check(user_id):
                continue
        except Exception:
            log.exception("Achievement check failed: %s for user %s", ach.code, user_id)
            continue

        if await unlock_achievement(user_id, ach.code):
            newly_unlocked.append(ach)

    if bot and newly_unlocked:
        for ach in newly_unlocked:
            try:
                await bot.send_message(
                    user_id,
                    f"🏆 <b>Новая ачивка!</b>\n\n"
                    f"{ach.emoji} <b>{ach.name}</b>\n"
                    f"<i>{ach.description}</i>",
                )
            except Exception as e:
                log.warning("Failed to send achievement to user %s: %s", user_id, e)

    return newly_unlocked


# Часто используемые наборы — для вызова из соответствующих хендлеров
GENERATION_RELATED = ["first_generation", "ten_generations"]
VOICE_RELATED = ["voice_5"]
PUBLISH_RELATED = ["first_publish", "ten_publishes"]
STREAK_RELATED = ["streak_7", "streak_30"]
REFERRAL_RELATED = ["first_referral", "three_referrals"]
