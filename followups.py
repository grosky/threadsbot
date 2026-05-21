"""Догревочная цепочка — 3 сообщения после /start, если юзер не оплатил.

Тайминги от момента /start (или повторного /start без активной подписки):
  - +15 минут — «Ты здесь?»
  - +1 час — кейс с реальной веткой автора
  - +3 часа — кейс залетевшей ветки юзера + список фич

Логика:
  - Background-loop проверяет кандидатов раз в 60 секунд.
  - Для каждого юзера смотрит какие биты в followup_sent_mask не выставлены.
  - Если пора слать и опоздание < 2 часов — шлём.
  - Если опоздание ≥ 2 часов — выставляем бит без отправки (юзер уже остыл, спам выглядит странно).
  - При оплате (Tribute) — cancel_followups() выставляет mask=7, цикл их игнорит.
  - При 403 от Telegram (юзер заблокировал бот) — cancel_followups() тихо обрывает.
"""
from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from datetime import datetime
from typing import Optional

from aiogram import Bot
from aiogram.exceptions import TelegramForbiddenError, TelegramRetryAfter
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

from config import config
from database import (
    cancel_followups,
    get_followup_candidates,
    is_subscription_active,
    mark_followup_sent,
)

log = logging.getLogger(__name__)


@dataclass
class FollowupMessage:
    position: int          # 0 / 1 / 2
    delay_seconds: int     # сколько ждать от followup_start_at
    text: str              # HTML

    @property
    def bit_mask(self) -> int:
        return 1 << self.position


FOLLOWUP_SCHEDULE: list[FollowupMessage] = [
    FollowupMessage(
        position=0,
        delay_seconds=15 * 60,
        text=(
            "👀 <b>Ты здесь?</b>\n\n"
            "Вижу что ты остановился в шаге от того чтобы попробовать бот, "
            "но так и не сгенерил первый пост.\n\n"
            "Если думаешь что это «очередное сложное обучение» — выдохни. "
            "Это <i>не курс</i>. Это AI-помощник который пишет посты за тебя.\n\n"
            "Никаких промтов вставлять в ChatGPT, никаких настроек. "
            "Заходишь → жмёшь «Сгенерить» → 3 готовых поста через 30 секунд → "
            "копируешь лучший → публикуешь.\n\n"
            "Первый результат можешь увидеть прямо сегодня вечером, "
            "потратив 15 минут.\n\n"
            "<b>Заверши начатое по кнопке ниже.</b>"
        ),
    ),
    FollowupMessage(
        position=1,
        delay_seconds=60 * 60,
        text=(
            "🚀 <b>2500 подписчиков за 2 дня, тратя 15 минут в день.</b>\n\n"
            "И это никакая не сказка — реальный кейс юзера бота.\n\n"
            "Что лучше доказывает что инструмент работает, чем самокейс? "
            "Сапожник с сапогами 👢\n\n"
            "Ветка залетела на <b>150к просмотров</b>, привела <b>2500 человек</b> "
            "и пачку новых клиентов. На написание ушло ~10 минут — "
            "бот сам выбрал формат, выдал 3 варианта, юзер выбрал "
            "лучший и опубликовал.\n\n"
            "<a href=\"https://www.threads.com/@mattgrsk/post/DVGegHAjN54\">👉 Посмотреть пост</a>\n\n"
            "Те же инструменты сейчас доступны тебе одним тапом.\n\n"
            "<b>Кликай по кнопке ниже и повтори результат у себя.</b>"
        ),
    ),
    FollowupMessage(
        position=2,
        delay_seconds=3 * 60 * 60,
        text=(
            "🔥 <b>Залетела ветка у пользователя нашего бота!</b>\n\n"
            "Один из юзеров бота опубликовал ветку — собрал охваты и привёл "
            "себе подписчиков в Threads.\n\n"
            "<a href=\"https://www.threads.com/@ira.neprobeauty/post/DYaYuTaDjta\">👉 Посмотреть пост</a>\n\n"
            "Ты тоже можешь быть в числе людей, кто собирает охваты "
            "и стабильно растёт в Threads. Для этого не нужны:\n"
            "— огромный блог\n"
            "— съёмки рилс\n"
            "— ежедневные прогревы\n\n"
            "Только бот + 15 минут в день.\n\n"
            "Внутри:\n"
            "— 🎯 Генерация постов в 3 разных форматах за один тап\n"
            "— 🎙 Голосовой сторителлинг — наговариваешь идею, бот собирает живой пост\n"
            "— 📸 Анализ упаковки твоего профиля\n"
            "— 🔍 Разбор чужих лент\n\n"
            "<b>Забирай инструменты и систему.</b>"
        ),
    ),
]


# Если опоздание превышает порог — пропускаем сообщение (юзер остыл).
LATE_THRESHOLD_SECONDS = 2 * 60 * 60

# Как часто просыпается фоновый loop.
LOOP_INTERVAL_SECONDS = 60


def _subscribe_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[[
            InlineKeyboardButton(
                text="💎 Оформить подписку",
                url=config.tribute_subscription_url,
            ),
        ]]
    )


def _parse_start_at(value) -> Optional[datetime]:
    if not value:
        return None
    if isinstance(value, datetime):
        return value
    try:
        return datetime.fromisoformat(str(value))
    except (ValueError, TypeError):
        return None


async def _send_followup(bot: Bot, user_id: int, msg: FollowupMessage) -> bool:
    """Отправляет одно сообщение. Возвращает True если успешно, False если юзер
    заблокировал бот (TelegramForbiddenError) — тогда тихо отменяем всю цепочку.
    """
    try:
        await bot.send_message(
            chat_id=user_id,
            text=msg.text,
            reply_markup=_subscribe_keyboard(),
            disable_web_page_preview=False,
        )
        return True
    except TelegramForbiddenError:
        log.info("Followup: user %s blocked the bot, cancelling chain", user_id)
        await cancel_followups(user_id)
        return False
    except TelegramRetryAfter as e:
        log.warning("Followup: flood control, sleeping %ds", e.retry_after)
        await asyncio.sleep(e.retry_after)
        return False
    except Exception:
        log.exception("Followup: send failed for user %s position %s", user_id, msg.position)
        return False


async def _process_user(bot: Bot, row: dict) -> None:
    """Для одного юзера: посмотреть какие биты не выставлены, и какие пора."""
    user_id = row["telegram_id"]
    start_at = _parse_start_at(row.get("followup_start_at"))
    if start_at is None:
        return

    mask = int(row.get("followup_sent_mask") or 0)

    # Если у юзера активная подписка — обрываем (на случай если webhook не прилетел вовремя).
    if await is_subscription_active(user_id):
        await cancel_followups(user_id)
        return

    elapsed = (datetime.utcnow() - start_at).total_seconds()

    for msg in FOLLOWUP_SCHEDULE:
        if mask & msg.bit_mask:
            continue  # уже отправлено
        if elapsed < msg.delay_seconds:
            break  # рано — и следующие тем более рано (они идут по возрастанию delay)

        overdue = elapsed - msg.delay_seconds
        if overdue > LATE_THRESHOLD_SECONDS:
            # Сильно опоздали — пропускаем без отправки (юзер уже остыл).
            log.info(
                "Followup: skipping pos=%s for user=%s, overdue=%ds",
                msg.position, user_id, int(overdue),
            )
            await mark_followup_sent(user_id, msg.position)
            continue

        sent_ok = await _send_followup(bot, user_id, msg)
        if not sent_ok:
            # 403 или ошибка — выходим из цикла этого юзера, не пытаемся слать следующие сейчас.
            return
        await mark_followup_sent(user_id, msg.position)
        log.info("Followup: sent pos=%s to user=%s", msg.position, user_id)


async def followup_loop(bot: Bot) -> None:
    """Главный фоновый цикл. Запускается из bot.py при старте."""
    log.info("Followup loop started, interval=%ds", LOOP_INTERVAL_SECONDS)
    while True:
        try:
            candidates = await get_followup_candidates()
            for row in candidates:
                await _process_user(bot, row)
        except Exception:
            log.exception("Followup loop iteration crashed")
        await asyncio.sleep(LOOP_INTERVAL_SECONDS)
