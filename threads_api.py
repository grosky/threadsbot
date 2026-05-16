"""Threads API клиент: OAuth + публикация постов.

Использует официальный graph.threads.net API.
Документация: https://developers.facebook.com/docs/threads
"""
from __future__ import annotations

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


# ---------- ПУБЛИКАЦИЯ ПОСТА ----------

async def publish_text_post(
    threads_user_id: str, access_token: str, text: str
) -> str:
    """Публикует текстовый пост в Threads.

    Двухступенчатый процесс:
    1. POST /{user-id}/threads — создать media container
    2. POST /{user-id}/threads_publish — опубликовать

    Возвращает permalink на опубликованный пост.
    """
    async with aiohttp.ClientSession() as session:
        # Шаг 1: создаём контейнер
        async with session.post(
            f"{GRAPH_BASE}/{threads_user_id}/threads",
            params={
                "media_type": "TEXT",
                "text": text,
                "access_token": access_token,
            },
        ) as resp:
            text_resp = await resp.text()
            if resp.status != 200:
                raise RuntimeError(
                    f"Create container failed [{resp.status}]: {text_resp}"
                )
            container = json.loads(text_resp)
            container_id = container["id"]

        # Threads рекомендует ждать ~30 секунд между шагами для медиа,
        # для текста можно сразу. Делаем небольшой sleep для надёжности.
        # asyncio.sleep здесь не нужен — текст обрабатывается быстро.

        # Шаг 2: публикуем
        async with session.post(
            f"{GRAPH_BASE}/{threads_user_id}/threads_publish",
            params={
                "creation_id": container_id,
                "access_token": access_token,
            },
        ) as resp:
            text_resp = await resp.text()
            if resp.status != 200:
                raise RuntimeError(
                    f"Publish failed [{resp.status}]: {text_resp}"
                )
            published = json.loads(text_resp)
            post_id = published["id"]

        # Получаем permalink
        async with session.get(
            f"{GRAPH_BASE}/{post_id}",
            params={
                "fields": "permalink",
                "access_token": access_token,
            },
        ) as resp:
            if resp.status == 200:
                data = json.loads(await resp.text())
                return data.get("permalink", f"https://www.threads.net/")

    return f"https://www.threads.net/"
