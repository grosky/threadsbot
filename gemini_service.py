"""Интеграция с Gemini API: инициализация клиента и async-генерация.

Использует новый google-genai SDK (legacy google-generativeai deprecated).
"""
from __future__ import annotations

import json
import logging

from google import genai
from google.genai import types

from config import config
from prompts import (
    FEED_ANALYSIS_PROMPT,
    FEED_ANALYSIS_SCHEMA,
    FORMAT_OPTIONS,
    PROFILE_ANALYSIS_PROMPT,
    PROFILE_ANALYSIS_SCHEMA,
    RESPONSE_SCHEMA,
    STORYTELLING_PROMPT,
    STORYTELLING_SCHEMA,
    SYSTEM_PROMPT,
    build_feed_analysis_message,
    build_profile_analysis_message,
    build_storytelling_message,
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

_MODEL_NAME = "gemini-2.5-flash"


async def generate_posts(
    profile: dict,
    format_name: str,
    topic: str | None = None,
) -> list[dict]:
    """Возвращает список из 3 вариантов поста.

    Каждый вариант — dict с ключами: id, hook_formula, angle_technique, post.
    """
    if format_name not in FORMAT_OPTIONS:
        raise ValueError(f"Неизвестный формат: {format_name}")

    user_msg = build_user_message(profile, format_name, topic)
    log.info(
        "Generating posts: user_id=%s format=%s topic=%s",
        profile.get("telegram_id"),
        format_name,
        topic or "—",
    )

    try:
        response = await _client.aio.models.generate_content(
            model=_MODEL_NAME,
            contents=user_msg,
            config=_GENERATION_CONFIG,
        )
        data = json.loads(response.text)
        variants = data["variants"]
        if not isinstance(variants, list) or len(variants) < 1:
            raise ValueError("Gemini вернул пустой список variants")
        return variants
    except json.JSONDecodeError:
        # Retry с явным напоминанием о формате
        log.warning("JSON decode failed, retrying with explicit reminder")
        retry_msg = user_msg + "\n\nВЕРНИ ТОЛЬКО ВАЛИДНЫЙ JSON БЕЗ ОБРАМЛЕНИЯ."
        response = await _client.aio.models.generate_content(
            model=_MODEL_NAME,
            contents=retry_msg,
            config=_GENERATION_CONFIG,
        )
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

    response = await _client.aio.models.generate_content(
        model=_MODEL_NAME,
        contents=[user_msg, image_part],
        config=_PROFILE_ANALYSIS_CONFIG,
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

    response = await _client.aio.models.generate_content(
        model=_MODEL_NAME,
        contents=[user_msg, audio_part],
        config=_STORYTELLING_CONFIG,
    )
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

    response = await _client.aio.models.generate_content(
        model=_MODEL_NAME,
        contents=user_msg,
        config=_FEED_ANALYSIS_CONFIG,
    )
    return json.loads(response.text)
