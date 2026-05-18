"""Подключение Threads-аккаунта (OAuth) + публикация постов."""
from __future__ import annotations

import html
import logging
from datetime import datetime

from aiogram import F, Router
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.types import (
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
)

from config import config
from database import (
    delete_threads_account,
    get_pending_post,
    get_threads_account,
    is_subscription_active,
    log_threads_publication,
    mark_threads_post_sent,
    save_pending_post,
    touch_streak,
)
from threads_api import (
    THREADS_MAX_CHARS,
    build_auth_url,
    debug_token,
    decrypt_token,
    publish_text_post,
    split_for_threads,
)

router = Router()
log = logging.getLogger(__name__)

# Кэш для скорости (in-memory). Основное хранилище — БД (pending_posts),
# чтобы посты не пропадали при редеплое.
_cache: dict[tuple[int, str], str] = {}


async def remember_post(user_id: int, post_key: str, text: str) -> None:
    """Сохраняет текст поста для последующей публикации (БД + кэш)."""
    _cache[(user_id, post_key)] = text
    # Ограничиваем in-memory размер чтобы не утечь
    if len(_cache) > 500:
        # Удаляем 100 самых старых ключей
        oldest_keys = list(_cache.keys())[:100]
        for k in oldest_keys:
            _cache.pop(k, None)
    await save_pending_post(user_id, post_key, text)


async def get_post(user_id: int, post_key: str) -> str | None:
    """Достаёт текст: сначала из кэша, при miss — из БД."""
    cached = _cache.get((user_id, post_key))
    if cached is not None:
        return cached
    text = await get_pending_post(user_id, post_key)
    if text is not None:
        _cache[(user_id, post_key)] = text
    return text


def _expiry_status(token_expires_at: str) -> tuple[bool, int]:
    """Возвращает (валиден, дней до истечения)."""
    try:
        expires = datetime.fromisoformat(token_expires_at)
    except (ValueError, TypeError):
        return False, 0
    delta = expires - datetime.utcnow()
    return delta.total_seconds() > 0, max(0, delta.days)


@router.callback_query(F.data == "action:connect_threads")
async def show_connect_screen(callback: CallbackQuery) -> None:
    user_id = callback.from_user.id

    if not await is_subscription_active(user_id):
        await callback.answer()
        await callback.message.answer(
            "❌ Подписка неактивна. Активируй промокод через /start."
        )
        return

    if not config.threads_enabled:
        await callback.answer()
        await callback.message.answer(
            "⚠️ Подключение Threads пока не настроено на сервере. "
            "Скажи админу что нужно добавить META_APP_ID/SECRET в Railway."
        )
        return

    account = await get_threads_account(user_id)
    if account:
        valid, days_left = _expiry_status(account["token_expires_at"])
        username = account.get("threads_username") or "—"

        kb = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(
                text="🔄 Переподключить",
                callback_data="action:connect_threads_force",
            )],
            [InlineKeyboardButton(
                text="❌ Отключить Threads",
                callback_data="action:disconnect_threads",
            )],
        ])

        status = "✅ активен" if valid else "❌ истёк"
        await callback.answer()
        await callback.message.answer(
            f"🧵 <b>Threads подключён</b>\n\n"
            f"Аккаунт: <b>@{html.escape(username)}</b>\n"
            f"Токен: {status}, осталось <b>{days_left} дней</b>\n\n"
            "Под каждым сгенерированным постом доступна кнопка «📤 Опубликовать в Threads».",
            reply_markup=kb,
        )
        return

    # Не подключён — даём ссылку на OAuth + подробную инструкцию
    await callback.answer()
    auth_url = build_auth_url(user_id)
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🔗 Авторизоваться в Threads", url=auth_url)],
        [InlineKeyboardButton(
            text="❓ Что-то пошло не так",
            callback_data="action:connect_threads_help",
        )],
    ])
    await callback.message.answer(
        "🧵 <b>Подключение Threads — 3 шага</b>\n\n"
        "<b>1.</b> Жми кнопку ниже\n"
        "<b>2.</b> На странице Meta жми <b>«Allow»</b> (разрешить публиковать)\n"
        "<b>3.</b> Тебя перекинет на страницу «✅ Подключено» — закрывай и возвращайся в Telegram\n\n"
        "Бот сам пришлёт подтверждение что всё ок.\n\n"
        "⚠️ <b>Важно перед подключением:</b>\n"
        "— Обнови Threads-приложение до последней версии (без этого "
        "может не появиться приглашение от бота)\n"
        "— Если Threads заблокирован у тебя в стране — включи VPN\n\n"
        "<i>Ссылка живёт 10 минут.</i>",
        reply_markup=kb,
    )


@router.callback_query(F.data == "action:connect_threads_help")
async def connect_threads_help(callback: CallbackQuery) -> None:
    """Расширенный гайд — что делать если падает 'invite not accepted' и т.п."""
    await callback.answer()
    await callback.message.answer(
        "🛟 <b>Если выдаёт ошибку при авторизации</b>\n\n"
        "<b>«User has not accepted the invite»</b>\n"
        "Твой Threads-аккаунт не принял приглашение тестировщика.\n\n"
        "Что делать:\n"
        "1. <b>Обнови Threads-приложение</b> до последней версии "
        "(в старых версиях вкладка с приглашениями просто не появляется)\n"
        "2. Открой Threads (с VPN если нужно)\n"
        "3. <b>Profile</b> → справа сверху <b>☰</b> → <b>Settings</b>\n"
        "4. <b>Account</b> → <b>Apps and Websites</b>\n"
        "5. Вкладка <b>Invites</b> → найди <b>Threadsbot</b> → жми <b>Accept</b>\n"
        "6. Если вкладки <b>Invites</b> нет — открой на десктопе "
        "<a href='https://www.threads.com/settings/account/apps'>"
        "threads.com/settings/account/apps</a> (там она всегда есть)\n"
        "7. Возвращайся в бот и пробуй подключение снова\n\n"
        "<b>«Session expired»</b>\n"
        "Ссылка живёт 10 минут. Запроси новую через /menu.\n\n"
        "<b>Страница Meta не открывается</b>\n"
        "Threads заблокирован в РФ — нужен VPN (страна = Бразилия / Турция / "
        "Латвия / любая не-РФ).\n\n"
        "<b>Что-то другое</b>\n"
        "Скрин ошибки автору → @grosky"
    )


@router.callback_query(F.data == "action:connect_threads_force")
async def force_reconnect(callback: CallbackQuery) -> None:
    """Кнопка «Переподключить» — выдаёт свежую OAuth-ссылку без удаления старой записи."""
    auth_url = build_auth_url(callback.from_user.id)
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🔗 Авторизоваться в Threads", url=auth_url)],
    ])
    await callback.answer()
    await callback.message.answer(
        "Жми кнопку — после новой авторизации старая запись перезапишется.",
        reply_markup=kb,
    )


@router.callback_query(F.data == "action:disconnect_threads")
async def disconnect(callback: CallbackQuery) -> None:
    await delete_threads_account(callback.from_user.id)
    await callback.answer("Отключено")
    await callback.message.answer(
        "🔌 Threads отключён. Данные удалены из бота.\n\n"
        "Если хочешь полностью отозвать доступ — также зайди в Threads → "
        "Settings → Account → Apps → Threadsbot → Remove."
    )


# ---------- ПУБЛИКАЦИЯ ----------

def publish_button(post_text_id: str) -> InlineKeyboardMarkup:
    """Старая кнопка-одиночка (оставлена для обратной совместимости).

    Лучше использовать post_actions_keyboard — с публикацией + жёстче/мягче/доработать.
    """
    return InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(
            text="📤 Опубликовать в Threads",
            callback_data=f"publish:threads:{post_text_id}",
        ),
    ]])


def post_actions_keyboard(post_key: str) -> InlineKeyboardMarkup:
    """Полный набор кнопок под сгенерированным/написанным постом.

    Используется и generation, и storytelling, и custom_post.
    Хендлеры post:harder / post:softer / post:refine живут в generation.py.
    """
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(
            text="📤 Опубликовать в Threads",
            callback_data=f"publish:threads:{post_key}",
        )],
        [
            InlineKeyboardButton(text="🔥 Жёстче", callback_data=f"post:harder:{post_key}"),
            InlineKeyboardButton(text="😌 Мягче", callback_data=f"post:softer:{post_key}"),
            InlineKeyboardButton(text="✏️ Доработать", callback_data=f"post:refine:{post_key}"),
        ],
    ])


async def _send_copy_instead_of_publish(
    callback: CallbackQuery, post_text: str
) -> None:
    """Для длинных постов: показываем подсказку «скопируй и опубликуй вручную»."""
    # Превью первой строки для контекста
    preview = post_text[:120].replace("\n", " ")
    if len(post_text) > 120:
        preview += "…"

    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🧵 Открыть Threads", url="https://www.threads.com/")],
    ])
    await callback.message.answer(
        f"📋 <b>Этот пост длиннее 500 символов — нужен тред</b>\n\n"
        f"Threads API пока не даёт мне публиковать треды для нашего бота "
        f"(ждём одобрение Meta, обычно 3-10 дней).\n\n"
        f"Скопируй текст ниже и опубликуй вручную через Threads — "
        f"родная функция «Новая ветка» сама разобьёт его на цепочку.\n\n"
        f"<i>Начало поста: «{html.escape(preview)}»</i>",
        reply_markup=kb,
    )
    # Шлём сам текст отдельным сообщением чтобы можно было быстро тапнуть «копировать»
    full_text = post_text
    if len(full_text) > 3900:
        full_text = full_text[:3900] + "\n\n…(обрезано до 4000 символов Telegram)"
    await callback.message.answer(f"<code>{html.escape(full_text)}</code>")


@router.callback_query(F.data.startswith("publish:threads:"))
async def publish_to_threads(callback: CallbackQuery, state: FSMContext) -> None:
    user_id = callback.from_user.id
    post_key = callback.data.split(":", 2)[2]

    # Достаём текст поста: кэш → БД
    post_text = await get_post(user_id, post_key)

    if not post_text:
        await callback.answer(
            "Текст поста потерян. Скопируй текст вручную и используй "
            "✍️ Опубликовать свой пост",
            show_alert=True,
        )
        # Шлём подсказку с кнопкой
        kb = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(
                text="✍️ Опубликовать свой пост",
                callback_data="action:custom_post",
            )],
            [InlineKeyboardButton(
                text="🎯 Сгенерить новый",
                callback_data="action:generate",
            )],
        ])
        await callback.message.answer(
            "⚠️ <b>Текст этого поста уже нельзя достать</b> "
            "(старше 24 часов или был удалён).\n\n"
            "Что можно сделать:\n"
            "• <b>Понравился сам пост</b> → скопируй текст вверху сообщения, "
            "жми <b>«✍️ Опубликовать свой пост»</b>, вставь и публикуй\n"
            "• <b>Хочешь свежие варианты</b> → жми «🎯 Сгенерить новый»",
            reply_markup=kb,
        )
        return

    account = await get_threads_account(user_id)
    if not account:
        await callback.answer()
        await callback.message.answer(
            "Сначала подключи Threads через /menu → «🔗 Подключить Threads»."
        )
        return

    valid, _ = _expiry_status(account["token_expires_at"])
    if not valid:
        await callback.answer()
        await callback.message.answer(
            "🔑 Токен Threads истёк. Переподключись через /menu → «🔗 Подключить Threads»."
        )
        return

    await callback.answer("Публикую...")
    status_msg = await callback.message.answer(
        "Публикую в Threads... (длинный пост разобью на тред, ~10 сек на каждый пост)"
    )

    try:
        token = decrypt_token(account["access_token_encrypted"])
        result = await publish_text_post(
            threads_user_id=account["threads_user_id"],
            access_token=token,
            text=post_text,
        )
        await mark_threads_post_sent(user_id)
    except Exception as e:
        log.exception("Threads publish failed for user %s", user_id)
        await status_msg.edit_text(
            f"❌ Не удалось опубликовать.\n\n"
            f"<code>{html.escape(type(e).__name__)}: {html.escape(str(e))[:300]}</code>\n\n"
            "Если ошибка про токен — переподключи Threads через /menu."
        )
        return

    posts_count = result["posts_count"]
    permalink = result["permalink"]

    # Логируем публикацию для подсчёта ачивок
    await log_threads_publication(user_id, posts_count)
    # Стрик и ачивки
    await touch_streak(user_id)
    try:
        from achievements import (
            PUBLISH_RELATED,
            STREAK_RELATED,
            check_and_award,
        )
        from aiogram import Bot
        bot_instance: Bot = callback.bot
        await check_and_award(user_id, bot_instance, codes=PUBLISH_RELATED + STREAK_RELATED)
    except Exception:
        log.exception("Achievement check failed after publish")

    if posts_count == 1:
        suffix = "одним постом"
    else:
        suffix = f"тредом из <b>{posts_count}</b> постов"

    await status_msg.edit_text(
        f"✅ <b>Опубликовано в Threads</b> {suffix}\n\n"
        f"<a href='{html.escape(permalink)}'>Открыть в Threads ↗</a>"
    )


# ---------- /threads команда ----------

@router.message(Command("threads_dry"))
async def cmd_threads_dry(message: Message) -> None:
    """Dry-run сплита: показывает как бот режет текст БЕЗ публикации.

    Использование: ответ на сообщение с постом + /threads_dry,
    либо просто /threads_dry длинный текст здесь...
    """
    # Берём текст: либо из reply, либо из аргумента команды
    src = message.reply_to_message.text if message.reply_to_message else None
    if not src:
        # Текст после команды
        parts = (message.text or "").split(None, 1)
        src = parts[1] if len(parts) > 1 else None

    if not src:
        await message.answer(
            "Использование:\n"
            "• <b>Reply</b> на сообщение с постом + <code>/threads_dry</code>\n"
            "• Или <code>/threads_dry [текст поста сюда]</code>\n\n"
            "Покажу как бот разрежет текст для Threads — без публикации."
        )
        return

    chunks = split_for_threads(src)

    lines = [
        f"📐 <b>Dry-run сплита</b>",
        f"Всего: <b>{len(src)}</b> символов → <b>{len(chunks)}</b> кусков\n",
    ]
    for i, chunk in enumerate(chunks, 1):
        preview = chunk[:200]
        if len(chunk) > 200:
            preview += "…"
        lines.append(
            f"<b>━━━ Chunk {i}/{len(chunks)} (len={len(chunk)}) ━━━</b>\n"
            f"<code>{html.escape(preview)}</code>\n"
        )
    text = "\n".join(lines)
    if len(text) > 4000:
        text = text[:3900] + "\n\n…(обрезано)"
    await message.answer(text)


@router.message(Command("threads_test"))
async def cmd_threads_test(message: Message) -> None:
    """Публикует заведомо чистый короткий пост — для диагностики content_publish.

    Если этот срабатывает — значит проблема в контенте обычных постов
    (ссылки, CAPS, длина). Если этот тоже падает — проблема в scope/app.
    """
    user_id = message.from_user.id
    account = await get_threads_account(user_id)
    if not account:
        await message.answer("Threads не подключён — нечего тестить.")
        return

    test_text = "test post from bot at " + datetime.utcnow().strftime("%H:%M:%S UTC")

    await message.answer(f"📤 Пробую опубликовать тестовый пост:\n\n<i>{html.escape(test_text)}</i>")
    try:
        token = decrypt_token(account["access_token_encrypted"])
        from threads_api import publish_text_post
        result = await publish_text_post(
            threads_user_id=account["threads_user_id"],
            access_token=token,
            text=test_text,
        )
        await message.answer(
            f"✅ Опубликовано!\n\n"
            f"<a href='{html.escape(result['permalink'])}'>Открыть пост ↗</a>\n\n"
            f"Если этот тест прошёл, а обычные посты падают — проблема в "
            f"контенте (ссылки, CAPS, длина)."
        )
    except Exception as e:
        await message.answer(
            f"❌ Тест провалился — значит проблема в scope/app, не в контенте.\n\n"
            f"<code>{html.escape(type(e).__name__)}: {html.escape(str(e))[:400]}</code>"
        )


@router.message(Command("threads_debug"))
async def cmd_threads_debug(message: Message) -> None:
    """Debug: проверяет какие scopes реально в токене юзера.

    Помогает диагностировать «Application does not have permission» —
    видим сразу есть ли threads_content_publish в гранте.
    """
    user_id = message.from_user.id
    account = await get_threads_account(user_id)
    if not account:
        await message.answer("Threads не подключён — нечего дебажить.")
        return

    try:
        token = decrypt_token(account["access_token_encrypted"])
        info = await debug_token(token)
    except Exception as e:
        await message.answer(
            f"❌ debug_token failed:\n<code>{html.escape(type(e).__name__)}: "
            f"{html.escape(str(e))[:400]}</code>"
        )
        return

    me_status = info.get("me_status")
    me_body = info.get("me_body", "")
    create_status = info.get("create_status")
    create_body = info.get("create_body", "")
    fb_status = info.get("fb_debug_status")
    fb_body = info.get("fb_debug_body", "")

    text = (
        "🔍 <b>Token debug report</b>\n\n"
        f"<b>1. GET /me</b> → status <code>{me_status}</code>\n"
        f"<code>{html.escape(str(me_body))}</code>\n\n"
        f"<b>2. POST /threads (test container)</b> → status <code>{create_status}</code>\n"
        f"<code>{html.escape(str(create_body))}</code>\n\n"
        f"<b>3. FB debug_token</b> → status <code>{fb_status}</code>\n"
        f"<code>{html.escape(str(fb_body))}</code>"
    )
    # Telegram limit
    if len(text) > 4000:
        text = text[:4000] + "\n…(обрезано)"
    await message.answer(text)


@router.message(Command("threads"))
async def cmd_threads(message: Message) -> None:
    """Алиас для меню подключения."""
    # Эмулируем callback — просто вызовем экран
    user_id = message.from_user.id
    if not config.threads_enabled:
        await message.answer("⚠️ Подключение Threads не настроено.")
        return

    account = await get_threads_account(user_id)
    if account:
        valid, days_left = _expiry_status(account["token_expires_at"])
        username = account.get("threads_username") or "—"
        status = "✅ активен" if valid else "❌ истёк"
        await message.answer(
            f"🧵 Threads: <b>@{html.escape(username)}</b> — {status}, "
            f"<b>{days_left} дней</b>"
        )
    else:
        auth_url = build_auth_url(user_id)
        kb = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="🔗 Подключить Threads", url=auth_url)],
        ])
        await message.answer(
            "🧵 Threads не подключён. Жми кнопку для авторизации:",
            reply_markup=kb,
        )
