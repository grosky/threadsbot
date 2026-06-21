"""Интеграция с Gemini API: инициализация клиента и async-генерация.

Использует новый google-genai SDK (legacy google-generativeai deprecated).
"""
from __future__ import annotations

import asyncio
import json
import logging
from typing import Awaitable, Callable

from google import genai
from google.genai import types

from config import config
from prompts import (
    FEED_ANALYSIS_PROMPT,
    FEED_ANALYSIS_SCHEMA,
    HUMANIZE_PROMPT,
    HUMANIZE_SCHEMA,
    IDEAS_PROMPT,
    IDEAS_SCHEMA,
    PROFILE_ANALYSIS_PROMPT,
    PROFILE_ANALYSIS_SCHEMA,
    PROFILE_PACKAGING_BIOS_SCHEMA,
    PROFILE_PACKAGING_FULL_SCHEMA,
    PROFILE_PACKAGING_LINK_SCHEMA,
    PROFILE_PACKAGING_NAMES_SCHEMA,
    POST_AUDIT_PROMPT,
    POST_AUDIT_SCHEMA,
    POST_REVISE_PROMPT,
    POST_REVISE_SCHEMA,
    PROFILE_PACKAGING_PINNED_SCHEMA,
    PROFILE_PACKAGING_PROMPT,
    PRODUCT_BUILDER_PROMPT,
    READER_REACTION_PROMPT,
    READER_REACTION_SCHEMA,
    PRODUCT_BUILDER_SCHEMA,
    RESPONSE_SCHEMA,
    STORYTELLING_PROMPT,
    STORYTELLING_SCHEMA,
    STYLE_LEARN_PROMPT,
    STYLE_LEARN_SCHEMA,
    SYSTEM_PROMPT,
    TRANSFORM_PROMPT,
    TRANSFORM_SCHEMA,
    build_feed_analysis_message,
    build_humanize_message,
    build_ideas_user_message,
    build_profile_analysis_message,
    build_product_builder_message,
    build_profile_packaging_message,
    build_audit_message,
    build_partner_chat_system,
    build_reader_message,
    build_revise_message,
    build_storytelling_message,
    build_style_learn_message,
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

# Humanize: больше temperature для более вариативного «живого» голоса
_PRODUCT_BUILDER_CONFIG = types.GenerateContentConfig(
    system_instruction=PRODUCT_BUILDER_PROMPT,
    temperature=0.85,
    response_mime_type="application/json",
    response_schema=PRODUCT_BUILDER_SCHEMA,
)

_HUMANIZE_CONFIG = types.GenerateContentConfig(
    system_instruction=HUMANIZE_PROMPT,
    temperature=1.0,
    response_mime_type="application/json",
    response_schema=HUMANIZE_SCHEMA,
)

_IDEAS_CONFIG = types.GenerateContentConfig(
    system_instruction=IDEAS_PROMPT,
    temperature=1.0,  # хочется разнообразия в идеях
    response_mime_type="application/json",
    response_schema=IDEAS_SCHEMA,
)

# Обучение стилю: дистилляция фидбека в правила — нужна точность, не креатив.
_STYLE_LEARN_CONFIG = types.GenerateContentConfig(
    system_instruction=STYLE_LEARN_PROMPT,
    temperature=0.3,
    response_mime_type="application/json",
    response_schema=STYLE_LEARN_SCHEMA,
)

# Quality-конвейер: аудит (критик), взгляд зрителя (читатель), доработка (редактор).
_AUDIT_CONFIG = types.GenerateContentConfig(
    system_instruction=POST_AUDIT_PROMPT,
    temperature=0.2,  # детерминированная оценка
    response_mime_type="application/json",
    response_schema=POST_AUDIT_SCHEMA,
)
_READER_CONFIG = types.GenerateContentConfig(
    system_instruction=READER_REACTION_PROMPT,
    temperature=0.6,  # живая, но стабильная реакция
    response_mime_type="application/json",
    response_schema=READER_REACTION_SCHEMA,
)
_REVISE_CONFIG = types.GenerateContentConfig(
    system_instruction=POST_REVISE_PROMPT,
    temperature=0.85,  # креативный рерайт
    response_mime_type="application/json",
    response_schema=POST_REVISE_SCHEMA,
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
# Гибкий чат для партнёров — самая сильная модель + thinking.
_CHAT_MODEL = "gemini-3-pro-preview"

# Quality-конвейер: цикл доработки до «зрителю интересно».
_INTEREST_TARGET = 9   # порог оценки интереса зрителя (0-10), при котором выходим
_MAX_REFINE_ITERS = 2  # макс. итераций цикла зритель->доработка
_STALL_PATIENCE = 1    # стоп, если балл не вырос столько итераций подряд


def _high_thinking_config():
    """ThinkingConfig для Pro-чата, устойчивый к версии SDK.

    Gemini 3 управляет рассуждением через thinking_level ('high'),
    более старые сборки SDK — через thinking_budget. Пробуем по
    очереди; если параметр не поддерживается — возвращаем None
    (модель решит сама, фича не падает).
    """
    for kwargs in ({"thinking_level": "high"}, {"thinking_budget": 8192}):
        try:
            return types.ThinkingConfig(**kwargs)
        except Exception:  # noqa: BLE001 — несовместимый параметр SDK
            continue
    return None


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
    model: str | None = None,
    fallback_model: str | None = None,
) -> "types.GenerateContentResponse":
    """Вызов Gemini с автоматическим retry и fallback на стабильную модель.

    1. Пробуем основную модель (по умолчанию _MODEL_NAME)
    2. При 503 — ждём retry_delay и пробуем ещё раз
    3. Если опять 503 — переключаемся на fallback (по умолчанию _FALLBACK_MODEL)
    """
    primary = model or _MODEL_NAME
    fallback = fallback_model or _FALLBACK_MODEL
    try:
        return await _client.aio.models.generate_content(
            model=primary, contents=contents, config=config_obj,
        )
    except Exception as e:
        if not _is_503(e):
            raise
        log.warning("Gemini %s 503, retrying after %ss: %s", primary, retry_delay, e)
        await asyncio.sleep(retry_delay)

        try:
            return await _client.aio.models.generate_content(
                model=primary, contents=contents, config=config_obj,
            )
        except Exception as e2:
            if not _is_503(e2):
                raise
            log.warning(
                "Gemini %s still 503, falling back to %s: %s",
                primary, fallback, e2,
            )
            return await _client.aio.models.generate_content(
                model=fallback, contents=contents, config=config_obj,
            )


# ---------- QUALITY-КОНВЕЙЕР ----------

async def _emit(on_progress, text: str) -> None:
    """Шлёт веху прогресса юзеру; ошибка прогресса не валит генерацию."""
    if on_progress is None:
        return
    try:
        await on_progress(text)
    except Exception:  # noqa: BLE001 — прогресс не критичен
        pass


async def _safe(coro, fallback, label: str):
    """Выполняет этап конвейера; при сбое логирует и деградирует к fallback."""
    try:
        return await coro
    except Exception as e:  # noqa: BLE001
        log.warning("pipeline stage %s failed, degrading: %s", label, e)
        return fallback


async def audit_post(post: str, fmt: str, profile: dict, length: str) -> dict:
    """Этап АУДИТ: критик оценивает пост по рубрике (модель Pro)."""
    msg = build_audit_message(post, fmt, profile, length)
    resp = await _call_with_fallback(
        msg, _AUDIT_CONFIG, model=_CHAT_MODEL, fallback_model=_MODEL_NAME,
    )
    return json.loads(resp.text)


async def reader_react(post: str, profile: dict, length: str) -> dict:
    """Этап ВЗГЛЯД ЗРИТЕЛЯ: живой читатель ленты оценивает интерес (модель Pro)."""
    msg = build_reader_message(post, profile, length)
    resp = await _call_with_fallback(
        msg, _READER_CONFIG, model=_CHAT_MODEL, fallback_model=_MODEL_NAME,
    )
    return json.loads(resp.text)


async def revise_post(
    post: str, fmt: str, audit: dict, reaction: dict, profile: dict, length: str,
) -> dict:
    """Этап ДОРАБОТКА: редактор переписывает проблемные места (модель Flash)."""
    msg = build_revise_message(post, fmt, audit, reaction, profile, length)
    resp = await _call_with_fallback(msg, _REVISE_CONFIG)
    return json.loads(resp.text)


# Нейтральные fallback-значения этапов (деградация без падения генерации).
_NEUTRAL_AUDIT = {
    "scores": {}, "problems": [],
    "fabricated_stats": False, "moral_ending": False, "needs_fix": False,
}


def _neutral_reaction(score: int) -> dict:
    return {
        "interest_score": score, "hook_grab": True, "what_grabbed": "",
        "boring_spots": [], "would_scroll_past": False, "comment": "",
    }


async def refine_variant(variant: dict, profile: dict, length: str) -> dict:
    """Прогоняет один вариант: аудит + взгляд зрителя -> цикл доработки.

    Возвращает {**variant, "post": лучшая_версия, "_interest_score", "_iterations"}.
    Всегда отдаёт рабочий вариант — при сбоях этапов деградирует к лучшему из имеющихся.
    """
    fmt = variant.get("format", "")
    best_post = variant.get("post", "") or ""
    if not best_post:
        return variant

    # АУДИТ + ВЗГЛЯД ЗРИТЕЛЯ параллельно (узлы независимы).
    audit, reaction = await asyncio.gather(
        _safe(audit_post(best_post, fmt, profile, length), dict(_NEUTRAL_AUDIT), "audit"),
        _safe(reader_react(best_post, profile, length), _neutral_reaction(_INTEREST_TARGET), "reader"),
    )
    best_score = int(reaction.get("interest_score", 0) or 0)
    initial_score = best_score
    critical = bool(audit.get("fabricated_stats") or audit.get("moral_ending"))
    iterations = 0

    # Ранний выход: пост чистый и зрителю интересно.
    if not critical and not audit.get("needs_fix") and best_score >= _INTEREST_TARGET:
        log.info(
            "refine id=%s fmt=%s: ранний выход, интерес=%s/10 (черновик чист)",
            variant.get("id"), fmt, best_score,
        )
        return {**variant, "post": best_post, "_interest_score": best_score, "_iterations": 0}

    # Цикл доработки: держим версию с максимальным баллом (доработка может ухудшить).
    stall = 0
    for i in range(_MAX_REFINE_ITERS):
        iterations = i + 1
        revised = await _safe(
            revise_post(best_post, fmt, audit, reaction, profile, length),
            {"post": best_post}, "revise",
        )
        new_post = (revised.get("post") or "").strip() or best_post

        reaction = await _safe(
            reader_react(new_post, profile, length),
            _neutral_reaction(best_score), "reader",
        )
        new_score = int(reaction.get("interest_score", best_score) or best_score)

        if new_score >= best_score:
            best_post, best_score = new_post, new_score
            stall = 0
        else:
            stall += 1

        if best_score >= _INTEREST_TARGET:
            break
        if stall > _STALL_PATIENCE:
            break

    # Финальные ворота: критичные нарушения (выдуманные цифры / мораль) не должны остаться.
    gate_fired = False
    final_audit = await _safe(
        audit_post(best_post, fmt, profile, length), dict(_NEUTRAL_AUDIT), "audit",
    )
    if final_audit.get("fabricated_stats") or final_audit.get("moral_ending"):
        gate_fired = True
        fixed = await _safe(
            revise_post(best_post, fmt, final_audit, reaction, profile, length),
            {"post": best_post}, "revise",
        )
        best_post = (fixed.get("post") or "").strip() or best_post

    log.info(
        "refine id=%s fmt=%s: интерес %s->%s/10, итераций=%s, финальные_ворота=%s",
        variant.get("id"), fmt, initial_score, best_score, iterations, gate_fired,
    )
    return {**variant, "post": best_post, "_interest_score": best_score, "_iterations": iterations}


async def generate_posts(
    profile: dict,
    topic: str | None = None,
    length: str = "long",
    on_progress: Callable[[str], Awaitable[None]] | None = None,
) -> list[dict]:
    """3 варианта поста, прогнанных через quality-конвейер.

    ДРАФТ (Gemini рисует 3 архетипа) -> для КАЖДОГО варианта конкурентно
    АУДИТ + ВЗГЛЯД ЗРИТЕЛЯ -> цикл доработки, пока интерес зрителя < порога.
    Формат возврата прежний: list[dict] с ключами post/format/id/... (плюс
    служебные _interest_score/_iterations, которые хэндлеры игнорируют).

    on_progress — опц. async-колбэк для вех прогресса (конвейер долгий).
    length="short" — каждый вариант ≤ 450 символов; "long" — развёрнутый.
    """
    user_msg = build_user_message(profile, topic, length=length)
    log.info(
        "Generating posts: user_id=%s length=%s topic=%s",
        profile.get("telegram_id"),
        length,
        topic or "—",
    )

    # --- ЭТАП 1: ДРАФТ (как раньше, с retry на битый JSON) ---
    await _emit(on_progress, "🧠 Пишу черновик...")
    try:
        response = await _call_with_fallback(user_msg, _GENERATION_CONFIG)
        draft = json.loads(response.text)["variants"]
    except json.JSONDecodeError:
        log.warning("JSON decode failed, retrying with explicit reminder")
        retry_msg = user_msg + "\n\nВЕРНИ ТОЛЬКО ВАЛИДНЫЙ JSON БЕЗ ОБРАМЛЕНИЯ."
        response = await _call_with_fallback(retry_msg, _GENERATION_CONFIG)
        draft = json.loads(response.text)["variants"]

    if not isinstance(draft, list) or len(draft) < 1:
        raise ValueError("Gemini вернул пустой список variants")

    # --- ЭТАПЫ 2-5: конкурентный refine всех вариантов ---
    await _emit(on_progress, "🔍 Проверяю по чек-листу и читаю глазами зрителя...")
    try:
        refined = await asyncio.gather(
            *[refine_variant(v, profile, length) for v in draft],
            return_exceptions=True,
        )
    except Exception as e:  # noqa: BLE001 — тотальная деградация к черновику
        log.warning("refine pipeline failed entirely, returning draft: %s", e)
        return draft

    # Поэлементная деградация: упавший вариант заменяем его черновиком.
    out = [r if isinstance(r, dict) else v for v, r in zip(draft, refined)]

    log.info(
        "Pipeline готов: user=%s len=%s итоги(id,интерес,итер)=%s",
        profile.get("telegram_id"), length,
        [(v.get("id"), v.get("_interest_score"), v.get("_iterations")) for v in out],
    )
    await _emit(on_progress, "✨ Довожу до 10/10...")
    return out


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


async def humanize_post(profile: dict, original_post: str) -> dict:
    """Переписывает AI-пост в живой голос реального автора.

    Возвращает {post, summary}. Использует отдельный системный промт с
    правилами «нормального взрослого автора» — длинные предложения с запятыми,
    личная история с конкретикой, без рваных коротких предложений подряд,
    без опечаток/капса/мата.
    """
    user_msg = build_humanize_message(original_post, profile)
    log.info(
        "Humanizing post: user=%s len=%d",
        profile.get("telegram_id"), len(original_post),
    )
    response = await _call_with_fallback(user_msg, _HUMANIZE_CONFIG)
    return json.loads(response.text)


async def learn_style_from_feedback(
    current_memory: str,
    feedback: str,
    post: str,
) -> dict:
    """Дистиллирует обратную связь автора в обновлённые правила стиля.

    Возвращает {memory, learned}: memory — полный обновлённый список правил
    (его сохраняем в users.style_memory), learned — что нового выучено
    (показываем автору для подтверждения).
    """
    user_msg = build_style_learn_message(current_memory, feedback, post)
    log.info(
        "Learning style from feedback: feedback='%s' mem_len=%d",
        feedback[:80], len(current_memory or ""),
    )
    response = await _call_with_fallback(user_msg, _STYLE_LEARN_CONFIG)
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


async def generate_product_ideas(
    profile: dict,
    expertise_text: str,
    effort_level: str,
) -> dict:
    """Генерирует 5 продуктовых идей под автора с ценами и обоснованием.

    Возвращает dict с ключами: summary, ideas (list of 5 dicts).
    """
    user_msg = build_product_builder_message(profile, expertise_text, effort_level)
    log.info(
        "Generating product ideas: user_id=%s effort=%s niche=%s",
        profile.get("telegram_id"),
        effort_level,
        (profile.get("niche") or "—")[:60],
    )
    response = await _call_with_fallback(user_msg, _PRODUCT_BUILDER_CONFIG)
    return json.loads(response.text)


async def partner_chat_reply(profile: dict, history: list[dict]) -> str:
    """Свободный многоходовый чат с Gemini Pro (+thinking) для партнёров.

    profile — словарь автора (ниша/ЦА/tone/product/style_memory).
    history — [{role: 'user'|'model', content: str}, ...] в хронологии,
    последним должно идти свежее сообщение пользователя.

    Возвращает текст ответа (без JSON-схемы — живой разговор).
    """
    contents = [
        types.Content(
            role=("model" if m.get("role") == "model" else "user"),
            parts=[types.Part(text=m.get("content") or "")],
        )
        for m in history
        if (m.get("content") or "").strip()
    ]

    config_obj = types.GenerateContentConfig(
        system_instruction=build_partner_chat_system(profile),
        temperature=0.9,
        thinking_config=_high_thinking_config(),
    )

    log.info(
        "Partner chat: user_id=%s turns=%s",
        profile.get("telegram_id"),
        len(contents),
    )
    response = await _call_with_fallback(
        contents,
        config_obj,
        model=_CHAT_MODEL,
        fallback_model=_MODEL_NAME,
    )
    return (response.text or "").strip()
