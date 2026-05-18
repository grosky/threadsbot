"""Tribute webhook handler — приём оплат через @tribute Telegram bot.

Tribute шлёт POST на наш endpoint при событиях:
- new_subscription / cancelled_subscription
- new_digital_product
- new_donation

Подпись: header `trbt-signature` = HMAC-SHA256(body, api_key).

Docs: https://wiki.tribute.tg/for-content-creators/api-documentation/webhooks
"""
from __future__ import annotations

import hashlib
import hmac
import json
import logging
from typing import Optional

from aiogram import Bot
from aiohttp import web

from config import config
from database import (
    consume_referral_reward,
    create_user,
    extend_subscription_days,
    log_payment,
)

log = logging.getLogger(__name__)


def verify_signature(body: bytes, signature_header: str) -> bool:
    """Проверяет подпись trbt-signature.

    Tribute использует HMAC-SHA256 с API-ключом. Поддерживаем формат hex
    (с префиксом sha256= и без — на всякий случай).
    """
    if not signature_header or not config.tribute_api_key:
        return False

    expected = hmac.new(
        config.tribute_api_key.encode(),
        body,
        hashlib.sha256,
    ).hexdigest()

    # Снимаем возможный префикс «sha256=»
    received = signature_header
    if received.startswith("sha256="):
        received = received[len("sha256="):]

    return hmac.compare_digest(expected, received.lower())


def _extract_telegram_user_id(payload: dict) -> Optional[int]:
    """Пытается достать telegram user_id из payload.

    Tribute payload структура может варьироваться — пробуем разные пути.
    """
    # Прямой ключ
    for key in ("telegram_user_id", "user_id", "tg_user_id", "telegram_id"):
        v = payload.get(key)
        if v:
            try:
                return int(v)
            except (TypeError, ValueError):
                pass

    # Внутри nested user object
    user = payload.get("user") or payload.get("buyer") or payload.get("customer") or {}
    if isinstance(user, dict):
        for key in ("telegram_user_id", "user_id", "tg_user_id", "telegram_id", "id"):
            v = user.get(key)
            if v:
                try:
                    return int(v)
                except (TypeError, ValueError):
                    pass

    return None


def _extract_amount_and_currency(payload: dict) -> tuple[Optional[int], str]:
    """Извлекает сумму платежа в копейках/центах и валюту.

    Tribute шлёт сумму либо в целых рублях, либо в копейках. Нормализуем в копейки.
    """
    # Прямые поля
    raw_amount = None
    for key in ("amount", "price", "sum", "total"):
        v = payload.get(key)
        if v is not None:
            raw_amount = v
            break

    currency = (payload.get("currency") or payload.get("currency_code") or "RUB").upper()

    if raw_amount is None:
        return None, currency

    try:
        # Если float — считаем что это рубли, переводим в копейки
        if isinstance(raw_amount, float):
            return int(round(raw_amount * 100)), currency
        amount_int = int(raw_amount)
        # Эвристика: если число подозрительно маленькое (<10000), это рубли;
        # если большое (>=10000) — уже копейки. 590 рублей = 59000 копеек —
        # граница нормальная.
        # Tribute обычно шлёт в копейках, поэтому дефолт: копейки
        return amount_int, currency
    except (TypeError, ValueError):
        return None, currency


def _extract_period_days(payload: dict) -> int:
    """Сколько дней начислить за подписку.

    Tribute обычно шлёт период в payload (period: monthly/yearly или days).
    Дефолт — 30 дней если не распарсили.
    """
    # Прямое число дней
    days = payload.get("period_days") or payload.get("days")
    if days:
        try:
            return int(days)
        except (TypeError, ValueError):
            pass

    # Текстовый период
    period = (payload.get("period") or payload.get("interval") or "").lower()
    if period in ("monthly", "month", "1m"):
        return 30
    if period in ("quarterly", "3m"):
        return 90
    if period in ("yearly", "annual", "year", "1y"):
        return 365
    if period in ("weekly", "week"):
        return 7

    # Дефолт: месяц
    return 30


async def handle_tribute_webhook(request: web.Request) -> web.Response:
    """Endpoint POST /webhooks/tribute — принимает события от Tribute."""
    body = await request.read()

    if not config.tribute_enabled:
        log.warning("Tribute webhook received but TRIBUTE_API_KEY not set")
        return web.json_response({"error": "tribute_disabled"}, status=503)

    signature = request.headers.get("trbt-signature", "")
    if not verify_signature(body, signature):
        log.warning(
            "Tribute webhook: invalid signature (got=%s body_len=%d)",
            signature[:32], len(body),
        )
        return web.json_response({"error": "invalid_signature"}, status=401)

    try:
        event = json.loads(body)
    except json.JSONDecodeError:
        log.exception("Tribute webhook: invalid JSON body=%r", body[:500])
        return web.json_response({"error": "invalid_json"}, status=400)

    event_type = event.get("name") or event.get("type") or event.get("event") or ""
    payload = event.get("payload") or event.get("data") or event
    log.info("Tribute event received: type=%s keys=%s", event_type, list(event.keys()))

    # Полный дамп payload для отладки реальных событий (тестовый event пустой)
    if event_type:
        try:
            dump = json.dumps(event, ensure_ascii=False)[:1500]
            log.info("Tribute full payload: %s", dump)
        except Exception:
            pass

    bot: Bot = request.app["bot"]

    # ----- Маршрутизация событий -----

    if event_type in ("new_subscription", "subscription_created", "subscription_renewed"):
        return await _handle_subscription(bot, payload)

    if event_type in ("new_digital_product", "digital_product_purchased"):
        # Цифровой продукт = одноразовая покупка (например, разовый промо-доступ)
        return await _handle_digital_product(bot, payload)

    if event_type in ("cancelled_subscription", "subscription_cancelled"):
        return await _handle_cancellation(bot, payload)

    if event_type in ("new_donation",):
        return await _handle_donation(bot, payload)

    log.info("Tribute: ignoring event type=%s", event_type)
    return web.json_response({"status": "ignored", "type": event_type})


async def _handle_subscription(bot: Bot, payload: dict) -> web.Response:
    user_id = _extract_telegram_user_id(payload)
    if not user_id:
        log.warning("Tribute subscription: no telegram_user_id in payload=%s", payload)
        return web.json_response({"error": "no_user_id"}, status=400)

    await create_user(user_id, username=None, first_name="")

    days = _extract_period_days(payload)
    amount, currency = _extract_amount_and_currency(payload)
    new_expires = await extend_subscription_days(user_id, days)

    # Логируем платёж для партнёрской статистики
    await log_payment(
        user_id=user_id,
        amount_kopecks=amount,
        currency=currency,
        period_days=days,
        event_type="subscription",
    )

    log.info(
        "Tribute subscription: user=%s amount=%s %s days=%d expires=%s",
        user_id, amount, currency, days, new_expires.isoformat(),
    )

    # Если юзер пришёл по реферальной ссылке — даём бонус приглашающему
    reward = await consume_referral_reward(user_id)
    if reward:
        try:
            await bot.send_message(
                reward["referrer_id"],
                "🎁 <b>Твой друг оформил подписку!</b>\n\n"
                f"Тебе начислено <b>+{reward['bonus_days']} дней</b>.\n"
                f"Действует до: <b>{reward['new_expires_at'].strftime('%d.%m.%Y')}</b>",
            )
        except Exception as e:
            log.warning("Failed to notify referrer %s: %s", reward["referrer_id"], e)
        try:
            from achievements import REFERRAL_RELATED, check_and_award
            await check_and_award(reward["referrer_id"], bot, codes=REFERRAL_RELATED)
        except Exception:
            log.exception("Referral achievement check failed")

    try:
        await bot.send_message(
            user_id,
            "💎 <b>Оплата прошла!</b>\n\n"
            f"Подписка продлена на <b>{days} дней</b>.\n"
            f"Действует до: <b>{new_expires.strftime('%d.%m.%Y')}</b>\n\n"
            "Жми /menu чтобы начать работу.",
        )
    except Exception as e:
        log.warning("Failed to notify user %s about subscription: %s", user_id, e)

    return web.json_response({"status": "ok", "user_id": user_id, "days": days})


async def _handle_digital_product(bot: Bot, payload: dict) -> web.Response:
    """Цифровой продукт = разовая покупка. Можно использовать как «trial-доступ»."""
    user_id = _extract_telegram_user_id(payload)
    if not user_id:
        log.warning("Tribute digital product: no telegram_user_id in payload=%s", payload)
        return web.json_response({"error": "no_user_id"}, status=400)

    await create_user(user_id, username=None, first_name="")

    days = _extract_period_days(payload)
    amount, currency = _extract_amount_and_currency(payload)
    new_expires = await extend_subscription_days(user_id, days)

    await log_payment(
        user_id=user_id,
        amount_kopecks=amount,
        currency=currency,
        period_days=days,
        event_type="digital_product",
    )

    log.info(
        "Tribute digital product: user=%s amount=%s %s days=%d",
        user_id, amount, currency, days,
    )

    try:
        await bot.send_message(
            user_id,
            "✅ <b>Покупка получена</b>\n\n"
            f"Доступ к боту: <b>{days} дней</b>.\n"
            f"Действует до: <b>{new_expires.strftime('%d.%m.%Y')}</b>\n\n"
            "Жми /menu чтобы начать.",
        )
    except Exception as e:
        log.warning("Failed to notify user %s about purchase: %s", user_id, e)

    return web.json_response({"status": "ok", "user_id": user_id, "days": days})


async def _handle_cancellation(bot: Bot, payload: dict) -> web.Response:
    """Юзер отменил автопродление. Подписка доработает до конца периода."""
    user_id = _extract_telegram_user_id(payload)
    if not user_id:
        return web.json_response({"error": "no_user_id"}, status=400)

    log.info("Tribute cancellation: user=%s", user_id)
    try:
        await bot.send_message(
            user_id,
            "ℹ️ <b>Автопродление отключено</b>\n\n"
            "Подписка продолжит работать до конца оплаченного периода.\n"
            "Чтобы возобновить — оплати ещё раз через Tribute.",
        )
    except Exception as e:
        log.warning("Failed to notify user %s about cancellation: %s", user_id, e)

    return web.json_response({"status": "ok"})


async def _handle_donation(bot: Bot, payload: dict) -> web.Response:
    """Донат — просто говорим спасибо, подписку не трогаем."""
    user_id = _extract_telegram_user_id(payload)
    log.info("Tribute donation: user=%s payload=%s", user_id, payload)
    if user_id:
        try:
            await bot.send_message(user_id, "❤️ Спасибо за поддержку!")
        except Exception:
            pass
    return web.json_response({"status": "ok"})
