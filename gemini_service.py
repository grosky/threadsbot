"""Интеграция с Gemini API: инициализация клиента и async-генерация.

Использует новый google-genai SDK (legacy google-generativeai deprecated).
"""
from __future__ import annotations

import asyncio
import json
import logging

from google import genai
from google.genai import types

from config import config
from prompts import (
    FEED_ANALYSIS_PROMPT,
    FEED_ANALYSIS_SCHEMA,
    IDEAS_PROMPT,
    IDEAS_SCHEMA,
    PROFILE_ANALYSIS_PROMPT,
    PROFILE_ANALYSIS_SCHEMA,
    PROFILE_PACKAGING_BIOS_SCHEMA,
    PROFILE_PACKAGING_FULL_SCHEMA,
    PROFILE_PACKAGING_LINK_SCHEMA,
    PROFILE_PACKAGING_NAMES_SCHEMA,
    PROFILE_PACKAGING_PINNED_SCHEMA,
    PROFILE_PACKAGING_PROMPT,
    RESPONSE_SCHEMA,
    STORYTELLING_PROMPT,
    STORYTELLING_SCHEMA,
    SYSTEM_PROMPT,
    TRANSFORM_PROMPT,
    TRANSFORM_SCHEMA,
    build_feed_analysis_message,
    build_ideas_user_message,
    build_profile_analysis_message,
    build_profile_packaging_message,
    build_storytelling_message,
    build_transform_message,
    build_user_message,
)

log = logging.getLogger(__name__)

# Один клиент на всё приложение.
_client = genai.Client(api_key=config.gemini_api_key)

_GENERATION_CONFIG = types.GenerateContentConfig(
    system_instruction=SYSTEM_PROMPT,
    temperature=0.95,
    response_mime_type="application/json",
    response_schema=RESPONSE_SCHEMA,
)

_PROFILE_ANALYSIS_CONFIG = types.GenerateContentConfig(
    system_instruction=PROFILE_ANALYSIS_PROMPT,
    temperature=0.4,
    response_mime_type="application/json",
    response_schema=PROFILE_ANALYSIS_SCHEMA,
)

_FEED_ANALYSIS_CONFIG = types.GenerateContentConfig(
    system_instruction=FEED_ANALYSIS_PROMPT,
    temperature=0.6,
    response_mime_type="application/json",
    response_schema=FEED_ANALYSIS_SCHEMA,
)

_STORYTELLING_CONFIG = types.GenerateContentConfig(
    system_instruction=STORYTELLING_PROMPT,
    temperature=0.85,
    response_mime_type="application/json",
    response_schema=STORYTELLING_SCHEMA,
)

_TRANSFORM_CONFIG = types.GenerateContentConfig(
    system_instruction=TRANSFORM_PROMPT,
    temperature=0.8,
    response_mime_type="application/json",
    response_schema=TRANSFORM_SCHEMA,
)

_IDEAS_CONFIG = types.GenerateContentConfig(
    system_instruction=IDEAS_PROMPT,
    temperature=1.0,  # хочется разнообразия в идеях
    response_mime_type="application/json",
    response_schema=IDEAS_SCHEMA,
)


def _packaging_config(target: str) -> "types.GenerateContentConfig":
    """Конфиг для упаковки профиля — схема меняется в зависимости от блока."""
    schema_map = {
        "all": PROFILE_PACKAGING_FULL_SCHEMA,
        "names": PROFILE_PACKAGING_NAMES_SCHEMA,
        "bios": PROFILE_PACKAGING_BIOS_SCHEMA,
        "link": PROFILE_PACKAGING_LINK_SCHEMA,
        "pinned": PROFILE_PACKAGING_PINNED_SCHEMA,
    }
    return types.GenerateContentConfig(
        system_instruction=PROFILE_PACKAGING_PROMPT,
        temperature=0.9,
        response_mime_type="application/json",
        response_schema=schema_map.get(target, PROFILE_PACKAGING_FULL_SCHEMA),
    )

# Gemini 3 Flash Preview (released Dec 2025) — заметно лучше 2.5 Flash,
# по качеству близок к 2.5 Pro, при этом в 2.5 раза дешевле Pro.
# Preview-модели иногда падают с 503 (high demand). На такой случай
# есть fallback на стабильную 2.5 Flash.
_MODEL_NAME = "gemini-3-flash-preview"
_FALLBACK_MODEL = "gemini-2.5-flash"


def _is_503(exc: Exception) -> bool:
    """Проверяет 503/UNAVAILABLE/высокую нагрузку."""
    msg = str(exc).lower()
    return any(
        s in msg for s in (
            "503", "unavailable", "high demand", "overloaded", "rate"
        )
    )


async def _call_with_fallback(
    contents,
    config_obj,
    *,
    retry_delay: float = 2.0,
) -> "types.GenerateContentResponse":
    """Вызов Gemini с автоматическим retry и fallback на стабильную модель.

    1. Пробуем _MODEL_NAME
    2. При 503 — ждём retry_delay и пробуем ещё раз
    3. Если опять 503 — переключаемся на _FALLBACK_MODEL
    """
    try:
        return await _client.aio.models.generate_content(
            model=_MODEL_NAME, contents=contents, config=config_obj,
        )
    except Exception as e:
        if not _is_503(e):
            raise
        log.warning("Gemini %s 503, retrying after %ss: %s", _MODEL_NAME, retry_delay, e)
        await asyncio.sleep(retry_delay)

        try:
            return await _client.aio.models.generate_content(
                model=_MODEL_NAME, contents=contents, config=config_obj,
            )
        except Exception as e2:
            if not _is_503(e2):
                raise
            log.warning(
                "Gemini %s still 503, falling back to %s: %s",
                _MODEL_NAME, _FALLBACK_MODEL, e2,
            )
            return await _client.aio.models.generate_content(
                model=_FALLBACK_MODEL, contents=contents, config=config_obj,
            )


async def generate_posts(
    profile: dict,
    topic: str | None = None,
    length: str = "long",
) -> list[dict]:
    """Возвращает список из 3 вариантов поста в РАЗНЫХ форматах.

    Gemini сам выбирает 3 формата (манифест / разбор / контринтуитивный /
    история / метод_известного) — юзер не указывает.

    length="short" — каждый вариант ≤ 450 символов (один пост в Threads).
    length="long" — полноценный развёрнутый пост (1500-2500 символов).
    """
    user_msg = build_user_message(profile, topic, length=length)
    log.info(
        "Generating posts: user_id=%s length=%s topic=%s",
        profile.get("telegram_id"),
        length,
        topic or "—",
    )

    try:
        response = await _call_with_fallback(user_msg, _GENERATION_CONFIG)
        data = json.loads(response.text)
        variants = data["variants"]
        if not isinstance(variants, list) or len(variants) < 1:
            raise ValueError("Gemini вернул пустой список variants")
        return variants
    except json.JSONDecodeError:
        # Retry с явным напоминанием о формате
        log.warning("JSON decode failed, retrying with explicit reminder")
        retry_msg = user_msg + "\n\nВЕРНИ ТОЛЬКО ВАЛИДНЫЙ JSON БЕЗ ОБРАМЛЕНИЯ."
        response = await _call_with_fallback(retry_msg, _GENERATION_CONFIG)
        return json.loads(response.text)["variants"]


async def analyze_profile(
    profile: dict,
    image_bytes: bytes,
    mime_type: str = "image/jpeg",
) -> dict:
    """Анализ упаковки профиля по скриншоту шапки. Возвращает структурированный отчёт."""
    user_msg = build_profile_analysis_message(profile)
    log.info(
        "Analyzing profile screenshot: user_id=%s mime=%s size=%d",
        profile.get("telegram_id"),
        mime_type,
        len(image_bytes),
    )

    image_part = types.Part.from_bytes(data=image_bytes, mime_type=mime_type)

    response = await _call_with_fallback(
        [user_msg, image_part], _PROFILE_ANALYSIS_CONFIG,
    )
    return json.loads(response.text)


async def generate_storytelling_from_voice(
    profile: dict,
    audio_bytes: bytes,
    mime_type: str = "audio/ogg",
) -> dict:
    """Превращает голосовое сообщение в сторителлинг-пост.

    Возвращает dict с ключами: heard, post, hook_line.
    """
    user_msg = build_storytelling_message(profile)
    log.info(
        "Storytelling from voice: user_id=%s mime=%s size=%d",
        profile.get("telegram_id"),
        mime_type,
        len(audio_bytes),
    )

    audio_part = types.Part.from_bytes(data=audio_bytes, mime_type=mime_type)

    response = await _call_with_fallback(
        [user_msg, audio_part], _STORYTELLING_CONFIG,
    )
    return json.loads(response.text)


async def transform_post(
    profile: dict, original_post: str, instruction: str
) -> dict:
    """Переписывает пост по инструкции (жёстче / мягче / свободный фидбек).

    Возвращает {post, summary}.
    """
    user_msg = build_transform_message(original_post, instruction, profile)
    log.info(
        "Transforming post: user=%s instruction='%s' len=%d",
        profile.get("telegram_id"), instruction[:60], len(original_post),
    )
    response = await _call_with_fallback(user_msg, _TRANSFORM_CONFIG)
    return json.loads(response.text)


async def generate_ideas(profile: dict) -> list[dict]:
    """Генерирует 10 идей для постов под профиль автора.

    Возвращает список dict'ов: [{id, text}, ...]
    """
    user_msg = build_ideas_user_message(profile)
    log.info(
        "Generating ideas: user_id=%s niche=%s",
        profile.get("telegram_id"),
        (profile.get("niche") or "—")[:60],
    )

    response = await _call_with_fallback(user_msg, _IDEAS_CONFIG)
    data = json.loads(response.text)
    return data.get("ideas", [])


async def generate_profile_pack(
    profile: dict,
    socials_text: str,
    perception_text: str,
) -> dict:
    """Генерирует полную упаковку профиля Threads: имя + bio + ссылка + закрепы.

    Возвращает dict: {names, bios, link_recommendation, pinned_posts}.
    """
    user_msg = build_profile_packaging_message(
        profile, socials_text, perception_text, target="all",
    )
    log.info(
        "Generating profile pack: user_id=%s niche=%s",
        profile.get("telegram_id"),
        (profile.get("niche") or "—")[:60],
    )
    response = await _call_with_fallback(user_msg, _packaging_config("all"))
    return json.loads(response.text)


async def regenerate_pack_block(
    profile: dict,
    socials_text: str,
    perception_text: str,
    target: str,
) -> dict:
    """Перегенерирует один блок упаковки: names | bios | link | pinned.

    Возвращает dict с одним полем соответствующим target.
    """
    if target not in ("names", "bios", "link", "pinned"):
        raise ValueError(f"Unknown packaging target: {target}")

    user_msg = build_profile_packaging_message(
        profile, socials_text, perception_text, target=target,
    )
    log.info(
        "Regenerating pack block: user_id=%s target=%s",
        profile.get("telegram_id"),
        target,
    )
    response = await _call_with_fallback(user_msg, _packaging_config(target))
    return json.loads(response.text)


async def analyze_feed(profile: dict, posts: list[str]) -> dict:
    """Разбор чужой ленты: 3-10 постов конкурента/референса.

    Возвращает структурированный отчёт с паттернами и идеями под нишу автора.
    """
    user_msg = build_feed_analysis_message(profile, posts)
    log.info(
        "Analyzing feed: user_id=%s posts_count=%d total_chars=%d",
        profile.get("telegram_id"),
        len(posts),
        sum(len(p) for p in posts),
    )

    response = await _call_with_fallback(user_msg, _FEED_ANALYSIS_CONFIG)
    return json.loads(response.text)
