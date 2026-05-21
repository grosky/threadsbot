"""Сборщик вирусных постов из Threads по ключевым словам.

Работает в фоне: раз в COLLECT_INTERVAL_HOURS просыпается, берёт
admin'ский Threads-токен из БД и дёргает keyword_search для каждого
ключевого слова. Сохраняет топ постов в viral_posts.

Юзеры читают результат через команду /trends — никаких API-запросов
от их имени.

Если admin'ского токена в БД нет (админ ещё не подключил Threads) —
тихо логгирует предупреждение и ждёт следующей итерации. Это позволяет
выкатывать фичу безопасно до того как админ подключится.
"""
from __future__ import annotations

import asyncio
import logging
from datetime import datetime
from typing import Optional

from config import config
from database import (
    cleanup_old_viral_posts,
    get_threads_account,
    upsert_viral_post,
)
from threads_api import decrypt_token, keyword_search

log = logging.getLogger(__name__)

# Ключевые слова для сбора. Берутся из ниш наших юзеров + смежные темы.
# Слова на русском работают через keyword_search так же как на английском.
KEYWORDS: list[str] = [
    "маркетинг",
    "продажи",
    "threads",
    "тредс",
    "контент",
    "продвижение",
    "smm",
    "ии",
    "ai",
    "личный бренд",
    "бизнес",
    "предпринимательство",
    "фриланс",
    "продуктивность",
    "копирайтинг",
    "инвестиции",
    "стартап",
    "блогинг",
    "telegram",
]

# Сколько постов на ключ сохраняем (топ по replies_count из 25 возвращаемых)
TOP_PER_KEYWORD = 10

# Минимум комментариев чтобы пост вообще учитывался как «вирусный»
MIN_REPLIES_THRESHOLD = 3

# Интервал между сборами
COLLECT_INTERVAL_HOURS = 12

# Возраст постов которые считаем достаточно свежими (дни)
CLEANUP_RETENTION_DAYS = 30


def _parse_timestamp(value) -> Optional[datetime]:
    if not value:
        return None
    try:
        # Threads timestamp типа "2026-05-20T08:14:00+0000"
        clean = str(value).replace("Z", "+0000")
        return datetime.fromisoformat(clean.replace("+0000", "+00:00"))
    except (ValueError, TypeError):
        return None


async def _get_admin_token() -> Optional[str]:
    """Достаёт расшифрованный admin Threads-токен или None если не подключён."""
    if not config.admin_telegram_id:
        return None
    account = await get_threads_account(config.admin_telegram_id)
    if not account:
        return None
    try:
        return decrypt_token(account["access_token_encrypted"])
    except Exception:
        log.exception("Failed to decrypt admin Threads token")
        return None


async def collect_once() -> dict:
    """Одна итерация сбора. Возвращает stats: {keywords_done, posts_saved, errors}."""
    stats = {"keywords_done": 0, "posts_saved": 0, "errors": 0}

    token = await _get_admin_token()
    if not token:
        log.warning(
            "Viral collector: admin Threads token missing — skipping cycle. "
            "Connect admin's Threads account through the bot to enable."
        )
        return stats

    for keyword in KEYWORDS:
        try:
            posts = await keyword_search(token, keyword, limit=25, search_type="TOP")
        except Exception as e:
            log.warning("Viral collector: keyword_search failed for %r: %s", keyword, e)
            stats["errors"] += 1
            continue

        # Сортируем по replies_count убыв и берём топ-N
        with_engagement = [
            p for p in posts
            if (p.get("replies_count") or 0) >= MIN_REPLIES_THRESHOLD
        ]
        with_engagement.sort(
            key=lambda p: (p.get("replies_count") or 0),
            reverse=True,
        )
        top = with_engagement[:TOP_PER_KEYWORD]

        for p in top:
            try:
                await upsert_viral_post(
                    threads_id=str(p["id"]),
                    permalink=str(p.get("permalink", "")),
                    text=str(p.get("text", "")),
                    username=str(p.get("username", "")),
                    replies_count=int(p.get("replies_count") or 0),
                    posted_at=_parse_timestamp(p.get("timestamp")),
                    keyword=keyword,
                )
                stats["posts_saved"] += 1
            except Exception:
                log.exception("Failed to upsert viral post %s", p.get("id"))
                stats["errors"] += 1

        stats["keywords_done"] += 1

        # Лёгкий троттл между ключами чтобы не упереться в rate limit
        await asyncio.sleep(1.5)

    # Чистим старые
    try:
        deleted = await cleanup_old_viral_posts(days=CLEANUP_RETENTION_DAYS)
        if deleted:
            log.info("Viral collector: cleanup deleted %d old posts", deleted)
    except Exception:
        log.exception("Viral collector: cleanup failed")

    log.info(
        "Viral collector cycle done: keywords=%d posts=%d errors=%d",
        stats["keywords_done"], stats["posts_saved"], stats["errors"],
    )
    return stats


async def viral_collector_loop() -> None:
    """Фоновый цикл. Запускается из bot.py."""
    log.info(
        "Viral collector loop started, interval=%d hours, keywords=%d",
        COLLECT_INTERVAL_HOURS, len(KEYWORDS),
    )
    # Стартовая задержка чтобы не дёрнуть API сразу после деплоя
    await asyncio.sleep(60)
    while True:
        try:
            await collect_once()
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("Viral collector iteration crashed")
        await asyncio.sleep(COLLECT_INTERVAL_HOURS * 3600)
