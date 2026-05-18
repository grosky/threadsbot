"""Threads API клиент: OAuth + публикация постов.

Использует официальный graph.threads.net API.
Документация: https://developers.facebook.com/docs/threads
"""
from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import logging
import secrets
import time
from datetime import datetime, timedelta
from typing import Optional
from urllib.parse import urlencode

import aiohttp
from cryptography.fernet import Fernet

from config import config

# Threads limit на один пост = 500 символов. Берём с запасом под numerование.
THREADS_MAX_CHARS = 480

log = logging.getLogger(__name__)

# Endpoints
AUTH_URL = "https://threads.net/oauth/authorize"
TOKEN_URL = "https://graph.threads.net/oauth/access_token"
LONG_LIVED_TOKEN_URL = "https://graph.threads.net/access_token"
GRAPH_BASE = "https://graph.threads.net/v1.0"

# Scopes для публикации
SCOPES = "threads_basic,threads_content_publish"

# State token TTL (защита от устаревших OAuth-сессий)
STATE_TTL_SECONDS = 600  # 10 минут


# ---------- ШИФРОВАНИЕ ТОКЕНОВ ----------

def _get_fernet() -> Fernet:
    """Возвращает Fernet с ключом из конфига."""
    if not config.encryption_key:
        raise RuntimeError("ENCRYPTION_KEY не задан — невозможно шифровать токены")
    return Fernet(config.encryption_key.encode())


def encrypt_token(token: str) -> bytes:
    return _get_fernet().encrypt(token.encode())


def decrypt_token(encrypted: bytes) -> str:
    return _get_fernet().decrypt(encrypted).decode()


# ---------- STATE TOKEN (CSRF защита) ----------

def build_state(user_id: int) -> str:
    """Создаёт подписанный state для OAuth.

    Формат: user_id:timestamp:hmac
    HMAC берётся от app_secret — никто кроме нас не может подделать.
    """
    payload = f"{user_id}:{int(time.time())}"
    sig = hmac.new(
        config.meta_app_secret.encode(),
        payload.encode(),
        hashlib.sha256,
    ).hexdigest()[:16]
    return f"{payload}:{sig}"


def verify_state(state: str) -> Optional[int]:
    """Возвращает user_id если state валиден, иначе None."""
    parts = state.split(":")
    if len(parts) != 3:
        return None
    user_id_str, ts_str, sig = parts
    try:
        user_id = int(user_id_str)
        ts = int(ts_str)
    except ValueError:
        return None

    # Проверка TTL
    if time.time() - ts > STATE_TTL_SECONDS:
        log.warning("State expired: user_id=%s age=%ds", user_id, time.time() - ts)
        return None

    # Проверка подписи
    expected_sig = hmac.new(
        config.meta_app_secret.encode(),
        f"{user_id}:{ts}".encode(),
        hashlib.sha256,
    ).hexdigest()[:16]
    if not hmac.compare_digest(sig, expected_sig):
        log.warning("State signature mismatch: user_id=%s", user_id)
        return None

    return user_id


# ---------- OAUTH FLOW ----------

def build_auth_url(user_id: int) -> str:
    """URL на который надо отправить юзера для авторизации в Meta."""
    params = {
        "client_id": config.meta_app_id,
        "redirect_uri": config.meta_redirect_uri,
        "scope": SCOPES,
        "response_type": "code",
        "state": build_state(user_id),
    }
    return f"{AUTH_URL}?{urlencode(params)}"


async def exchange_code_for_token(code: str) -> dict:
    """Code → short-lived token (1 час).

    Возвращает {access_token, user_id}.
    """
    async with aiohttp.ClientSession() as session:
        async with session.post(
            TOKEN_URL,
            data={
                "client_id": config.meta_app_id,
                "client_secret": config.meta_app_secret,
                "grant_type": "authorization_code",
                "redirect_uri": config.meta_redirect_uri,
                "code": code,
            },
        ) as resp:
            text = await resp.text()
            if resp.status != 200:
                raise RuntimeError(f"Token exchange failed [{resp.status}]: {text}")
            return json.loads(text)


async def exchange_for_long_lived(short_token: str) -> dict:
    """Short-lived (1ч) → long-lived (60 дней).

    Возвращает {access_token, token_type, expires_in}.
    """
    async with aiohttp.ClientSession() as session:
        async with session.get(
            LONG_LIVED_TOKEN_URL,
            params={
                "grant_type": "th_exchange_token",
                "client_secret": config.meta_app_secret,
                "access_token": short_token,
            },
        ) as resp:
            text = await resp.text()
            if resp.status != 200:
                raise RuntimeError(f"Long-lived exchange failed [{resp.status}]: {text}")
            return json.loads(text)


async def refresh_long_lived(token: str) -> dict:
    """Продление long-lived токена ещё на 60 дней.

    Можно вызывать когда токену осталось <30 дней.
    """
    async with aiohttp.ClientSession() as session:
        async with session.get(
            "https://graph.threads.net/refresh_access_token",
            params={
                "grant_type": "th_refresh_token",
                "access_token": token,
            },
        ) as resp:
            text = await resp.text()
            if resp.status != 200:
                raise RuntimeError(f"Refresh failed [{resp.status}]: {text}")
            return json.loads(text)


async def debug_token(user_token: str) -> dict:
    """Многоступенчатый дебаг токена.

    1. /me на graph.threads.net — проверка что токен в принципе работает
    2. /threads — попытка создать тестовый контейнер для проверки content_publish
    3. debug_token на graph.facebook.com (если работает)

    Возвращает агрегированный отчёт.
    """
    report: dict = {}
    app_token = f"{config.meta_app_id}|{config.meta_app_secret}"

    async with aiohttp.ClientSession() as session:
        # 1) /me — должен работать с threads_basic
        async with session.get(
            f"{GRAPH_BASE}/me",
            params={"fields": "id,username", "access_token": user_token},
        ) as resp:
            body = await resp.text()
            report["me_status"] = resp.status
            report["me_body"] = body[:400]
            if resp.status == 200:
                report["me_data"] = json.loads(body)

        # 2) Попытка создать тестовый контейнер (не публикуя)
        threads_user_id = report.get("me_data", {}).get("id")
        if threads_user_id:
            async with session.post(
                f"{GRAPH_BASE}/{threads_user_id}/threads",
                params={
                    "media_type": "TEXT",
                    "text": "test container — will not publish",
                    "access_token": user_token,
                },
            ) as resp:
                body = await resp.text()
                report["create_status"] = resp.status
                report["create_body"] = body[:400]

        # 3) debug_token на graph.facebook.com (запасной путь)
        async with session.get(
            "https://graph.facebook.com/debug_token",
            params={"input_token": user_token, "access_token": app_token},
        ) as resp:
            body = await resp.text()
            report["fb_debug_status"] = resp.status
            report["fb_debug_body"] = body[:600]

    return report


async def get_me(access_token: str) -> dict:
    """Получить инфо о подключённом Threads-аккаунте.

    Возвращает {id, username, ...}.
    """
    async with aiohttp.ClientSession() as session:
        async with session.get(
            f"{GRAPH_BASE}/me",
            params={
                "fields": "id,username,name,threads_profile_picture_url",
                "access_token": access_token,
            },
        ) as resp:
            text = await resp.text()
            if resp.status != 200:
                raise RuntimeError(f"Get me failed [{resp.status}]: {text}")
            return json.loads(text)


# ---------- РАЗБИЕНИЕ ТЕКСТА НА ТРЕД ----------

def split_for_threads(text: str, max_len: int = THREADS_MAX_CHARS) -> list[str]:
    """Разбивает длинный текст на куски ≤ max_len по естественным границам.

    Приоритет точек разрыва:
    1. Двойной перенос (абзацы)
    2. Одинарный перенос
    3. Конец предложения (. ! ?)
    4. Граница слова
    5. Жёсткий обрез (последний случай)

    Сохраняет читабельность — не режет посреди слова если возможно.
    """
    text = text.strip()
    if len(text) <= max_len:
        return [text]

    chunks: list[str] = []
    remaining = text

    while len(remaining) > max_len:
        # Берём кусок длины max_len и ищем где безопасно резать
        window = remaining[:max_len]

        cut_at = -1
        # 1) Абзац
        idx = window.rfind("\n\n")
        if idx > max_len // 3:
            cut_at = idx
            advance = idx + 2
        else:
            # 2) Одиночный перенос
            idx = window.rfind("\n")
            if idx > max_len // 3:
                cut_at = idx
                advance = idx + 1
            else:
                # 3) Конец предложения
                best = -1
                for marker in (". ", "! ", "? ", "; "):
                    pos = window.rfind(marker)
                    if pos > best:
                        best = pos
                        marker_len = len(marker)
                if best > max_len // 3:
                    cut_at = best + marker_len
                    advance = cut_at
                else:
                    # 4) Граница слова
                    idx = window.rfind(" ")
                    if idx > max_len // 3:
                        cut_at = idx
                        advance = idx + 1
                    else:
                        # 5) Жёсткий обрез
                        cut_at = max_len
                        advance = max_len

        chunks.append(remaining[:cut_at].strip())
        remaining = remaining[advance:].strip()

    if remaining:
        chunks.append(remaining)

    return chunks


# ---------- ПУБЛИКАЦИЯ ПОСТА (single + chain) ----------

async def _create_and_publish(
    session: aiohttp.ClientSession,
    threads_user_id: str,
    access_token: str,
    text: str,
    reply_to_id: Optional[str] = None,
) -> str:
    """Один пост: создать container → опубликовать. Возвращает post_id.

    reply_to_id=None — новый отдельный пост.
    reply_to_id=<post_id> — реплай (для цепочки тредов).

    Для реплая передаём параметры как multipart/form-data (per Threads API docs).
    """
    fields = {
        "media_type": "TEXT",
        "text": text,
        "access_token": access_token,
    }
    if reply_to_id:
        fields["reply_to_id"] = reply_to_id

    # FormData = multipart/form-data, как требует Threads API для реплаев
    form = aiohttp.FormData()
    for k, v in fields.items():
        form.add_field(k, v)

    async with session.post(
        f"{GRAPH_BASE}/{threads_user_id}/threads",
        data=form,
    ) as resp:
        body = await resp.text()
        if resp.status != 200:
            raise RuntimeError(f"Create container failed [{resp.status}]: {body}")
        container = json.loads(body)
        container_id = container["id"]

    # Threads рекомендует ждать ~30с между create и publish для надёжной обработки.
    # Но для текста часто хватает 3-5 секунд. Берём баланс.
    await asyncio.sleep(5)

    publish_form = aiohttp.FormData()
    publish_form.add_field("creation_id", container_id)
    publish_form.add_field("access_token", access_token)

    async with session.post(
        f"{GRAPH_BASE}/{threads_user_id}/threads_publish",
        data=publish_form,
    ) as resp:
        body = await resp.text()
        if resp.status != 200:
            raise RuntimeError(f"Publish failed [{resp.status}]: {body}")
        published = json.loads(body)
        return str(published["id"])


async def _get_permalink(
    session: aiohttp.ClientSession,
    access_token: str,
    post_id: str,
) -> str:
    """Достаёт permalink опубликованного поста."""
    async with session.get(
        f"{GRAPH_BASE}/{post_id}",
        params={"fields": "permalink", "access_token": access_token},
    ) as resp:
        if resp.status == 200:
            data = json.loads(await resp.text())
            return data.get("permalink") or "https://www.threads.net/"
    return "https://www.threads.net/"


async def publish_text_post(
    threads_user_id: str, access_token: str, text: str
) -> dict:
    """Публикует текст в Threads.

    Если текст укладывается в 500 символов — одиночный пост.
    Если длиннее — родная цепочка тредов через reply_to_id.

    Возвращает {permalink, posts_count}.
    """
    chunks = split_for_threads(text)
    log.info(
        "Publishing to Threads: user=%s chunks=%d total_chars=%d",
        threads_user_id, len(chunks), len(text),
    )

    for i, chunk in enumerate(chunks):
        preview = chunk[:80].replace("\n", "\\n")
        log.info(
            "Chunk %d/%d (len=%d): %s%s",
            i + 1, len(chunks), len(chunk), preview,
            "…" if len(chunk) > 80 else "",
        )

    async with aiohttp.ClientSession() as session:
        first_post_id: Optional[str] = None
        prev_post_id: Optional[str] = None

        for i, chunk in enumerate(chunks):
            try:
                post_id = await _create_and_publish(
                    session=session,
                    threads_user_id=threads_user_id,
                    access_token=access_token,
                    text=chunk,
                    reply_to_id=prev_post_id,  # цепочка реплаев = thread
                )
            except Exception as e:
                preview = chunk[:120].replace("\n", " ")
                raise RuntimeError(
                    f"Chunk {i+1}/{len(chunks)} (len={len(chunk)}) failed. "
                    f"Preview: «{preview}…» — {e}"
                ) from e

            if i == 0:
                first_post_id = post_id
            prev_post_id = post_id

            # Между публикацией предыдущего и созданием следующего реплая —
            # пауза, чтобы Meta успел проиндексировать пост и принять его как
            # parent для reply_to_id.
            if i < len(chunks) - 1:
                await asyncio.sleep(10)

        permalink = await _get_permalink(session, access_token, first_post_id)

    return {"permalink": permalink, "posts_count": len(chunks)}
