"""SQLite-хранилище через aiosqlite. Юзеры, промокоды, генерации.

Заметки:
- На Railway SQLite живёт во временном диске — для прод-нагрузки переходи на Postgres.
- Все запросы async. row_factory = aiosqlite.Row даёт dict-доступ к результатам.
"""
import logging
import secrets
from datetime import datetime, timedelta, timezone
from typing import Optional

import aiosqlite

from config import DAILY_LIMIT, config

log = logging.getLogger(__name__)

SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
    telegram_id INTEGER PRIMARY KEY,
    username TEXT,
    first_name TEXT,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    subscription_expires_at TIMESTAMP,
    onboarding_complete INTEGER DEFAULT 0,
    niche TEXT,
    audience TEXT,
    product TEXT,
    product_link TEXT,
    tone TEXT,
    facts TEXT,
    pains TEXT,
    social_proof TEXT
);

CREATE TABLE IF NOT EXISTS promocodes (
    code TEXT PRIMARY KEY,
    duration_days INTEGER NOT NULL,
    used_by INTEGER,
    used_at TIMESTAMP,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY (used_by) REFERENCES users(telegram_id)
);

CREATE TABLE IF NOT EXISTS generations (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER NOT NULL,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    format TEXT,
    topic TEXT,
    FOREIGN KEY (user_id) REFERENCES users(telegram_id)
);

CREATE INDEX IF NOT EXISTS idx_generations_user_date
    ON generations(user_id, created_at);

CREATE TABLE IF NOT EXISTS profile_analyses (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER NOT NULL,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    overall_score INTEGER,
    FOREIGN KEY (user_id) REFERENCES users(telegram_id)
);

CREATE INDEX IF NOT EXISTS idx_profile_analyses_user_date
    ON profile_analyses(user_id, created_at);

CREATE TABLE IF NOT EXISTS feed_analyses (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER NOT NULL,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    posts_count INTEGER,
    FOREIGN KEY (user_id) REFERENCES users(telegram_id)
);

CREATE INDEX IF NOT EXISTS idx_feed_analyses_user_date
    ON feed_analyses(user_id, created_at);

CREATE TABLE IF NOT EXISTS referrals (
    invitee_id INTEGER PRIMARY KEY,
    referrer_id INTEGER NOT NULL,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    rewarded_at TIMESTAMP,
    bonus_days INTEGER,
    FOREIGN KEY (invitee_id) REFERENCES users(telegram_id),
    FOREIGN KEY (referrer_id) REFERENCES users(telegram_id)
);

CREATE INDEX IF NOT EXISTS idx_referrals_referrer ON referrals(referrer_id);

CREATE TABLE IF NOT EXISTS threads_accounts (
    user_id INTEGER PRIMARY KEY,
    threads_user_id TEXT NOT NULL,
    threads_username TEXT,
    access_token_encrypted BLOB NOT NULL,
    token_expires_at TIMESTAMP,
    connected_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    last_post_at TIMESTAMP,
    FOREIGN KEY (user_id) REFERENCES users(telegram_id)
);

CREATE TABLE IF NOT EXISTS pending_posts (
    user_id INTEGER NOT NULL,
    post_key TEXT NOT NULL,
    text TEXT NOT NULL,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (user_id, post_key)
);

CREATE INDEX IF NOT EXISTS idx_pending_posts_created
    ON pending_posts(created_at);

CREATE TABLE IF NOT EXISTS user_streaks (
    user_id INTEGER PRIMARY KEY,
    current_streak INTEGER DEFAULT 0,
    best_streak INTEGER DEFAULT 0,
    last_active_date DATE,
    FOREIGN KEY (user_id) REFERENCES users(telegram_id)
);

CREATE TABLE IF NOT EXISTS achievements (
    user_id INTEGER NOT NULL,
    code TEXT NOT NULL,
    unlocked_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (user_id, code),
    FOREIGN KEY (user_id) REFERENCES users(telegram_id)
);

CREATE TABLE IF NOT EXISTS threads_post_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER NOT NULL,
    posted_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    posts_count INTEGER DEFAULT 1,
    FOREIGN KEY (user_id) REFERENCES users(telegram_id)
);

CREATE INDEX IF NOT EXISTS idx_threads_post_log_user
    ON threads_post_log(user_id, posted_at);

CREATE TABLE IF NOT EXISTS payments (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER NOT NULL,
    amount_kopecks INTEGER,
    currency TEXT DEFAULT 'RUB',
    period_days INTEGER,
    source TEXT,
    event_type TEXT,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY (user_id) REFERENCES users(telegram_id)
);

CREATE INDEX IF NOT EXISTS idx_payments_source ON payments(source);
CREATE INDEX IF NOT EXISTS idx_payments_user ON payments(user_id);

CREATE TABLE IF NOT EXISTS partner_links (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    partner_telegram_id INTEGER NOT NULL,
    source TEXT NOT NULL UNIQUE,
    link TEXT NOT NULL,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY (partner_telegram_id) REFERENCES users(telegram_id)
);

CREATE INDEX IF NOT EXISTS idx_partner_links_partner
    ON partner_links(partner_telegram_id);

-- Свободный чат с Gemini (для партнёров + админа): доработка постов.
-- Один непрерывный чат на юзера, role = 'user' | 'model'.
CREATE TABLE IF NOT EXISTS partner_chat_messages (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER NOT NULL,
    role TEXT NOT NULL,
    content TEXT NOT NULL,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX IF NOT EXISTS idx_partner_chat_user
    ON partner_chat_messages(user_id, id);

CREATE TABLE IF NOT EXISTS viral_posts (
    threads_id TEXT PRIMARY KEY,
    permalink TEXT NOT NULL,
    text TEXT,
    username TEXT,
    replies_count INTEGER DEFAULT 0,
    posted_at TIMESTAMP,
    keyword TEXT NOT NULL,
    fetched_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX IF NOT EXISTS idx_viral_keyword_posted
    ON viral_posts(keyword, posted_at);
CREATE INDEX IF NOT EXISTS idx_viral_fetched_at
    ON viral_posts(fetched_at);
"""

# Стандартная комиссия партнёру в процентах от платежа.
# Если хочешь другую — поменять здесь, пересчитается во всех отчётах.
PARTNER_COMMISSION_PERCENT = 30

# Бонус приглашающему за каждого реферала, активировавшего промокод
REFERRAL_BONUS_DAYS = 30

# Whitelist полей профиля (защита от SQL-инъекций в update_profile_field)
PROFILE_FIELDS = {
    "niche", "audience", "product", "product_link",
    "tone", "facts", "pains", "social_proof",
}


async def _migrate_users_columns(db: aiosqlite.Connection) -> None:
    """Добавляет колонки в users, если их ещё нет (для совместимости с прод-БД)."""
    async with db.execute("PRAGMA table_info(users)") as cur:
        cols = {row[1] for row in await cur.fetchall()}
    if "free_trial_used" not in cols:
        await db.execute(
            "ALTER TABLE users ADD COLUMN free_trial_used INTEGER DEFAULT 0"
        )
    if "profile_pack_json" not in cols:
        await db.execute(
            "ALTER TABLE users ADD COLUMN profile_pack_json TEXT"
        )
    if "ab_variant" not in cols:
        # A/B тест: 'A' = с free trial (текущая модель),
        # 'B' = без free trial (welcome → сразу paywall).
        # NULL = legacy юзеры до запуска теста, считаются как 'A'.
        await db.execute("ALTER TABLE users ADD COLUMN ab_variant TEXT")
    if "product_pack_json" not in cols:
        # Последние сгенерированные продуктовые идеи (5 шт + summary).
        await db.execute(
            "ALTER TABLE users ADD COLUMN product_pack_json TEXT"
        )
    if "followup_start_at" not in cols:
        await db.execute(
            "ALTER TABLE users ADD COLUMN followup_start_at TIMESTAMP"
        )
    if "followup_sent_mask" not in cols:
        await db.execute(
            "ALTER TABLE users ADD COLUMN followup_sent_mask INTEGER DEFAULT 0"
        )
    if "style_memory" not in cols:
        # Выученные правила стиля автора — копятся из обратной связи юзера
        # на сгенерированные посты, подмешиваются в промт следующих генераций.
        await db.execute("ALTER TABLE users ADD COLUMN style_memory TEXT")


async def _migrate_referrals_columns(db: aiosqlite.Connection) -> None:
    """Добавляет колонку source в referrals (UTM-трекинг)."""
    async with db.execute("PRAGMA table_info(referrals)") as cur:
        cols = {row[1] for row in await cur.fetchall()}
    if "source" not in cols:
        await db.execute("ALTER TABLE referrals ADD COLUMN source TEXT")


async def _backfill_onboarding_complete(db: aiosqlite.Connection) -> None:
    """Чинит юзеров, прошедших онбординг до фикса #50.

    Старый баг: в callback-флоу (выбор tone кнопкой) onboarding_complete
    не выставлялся. Такие юзеры заполнили niche/audience/tone, но флаг
    остался 0 — и любой фича-гейт `if not onboarding_complete` выкидывает
    их обратно в онбординг. Восстанавливаем флаг по факту заполненного
    профиля. Идемпотентно: после первого прогона строк под условие нет.
    """
    cur = await db.execute(
        "UPDATE users SET onboarding_complete = 1 "
        "WHERE COALESCE(onboarding_complete, 0) = 0 "
        "  AND niche IS NOT NULL AND TRIM(niche) != '' "
        "  AND audience IS NOT NULL AND TRIM(audience) != '' "
        "  AND tone IS NOT NULL AND TRIM(tone) != ''"
    )
    if cur.rowcount:
        log.info("Backfill onboarding_complete: восстановлено %s юзеров", cur.rowcount)


async def init_db() -> None:
    """Создаёт таблицы при первом запуске + миграции колонок."""
    async with aiosqlite.connect(config.database_path) as db:
        await db.executescript(SCHEMA)
        await _migrate_users_columns(db)
        await _migrate_referrals_columns(db)
        await _backfill_onboarding_complete(db)
        await db.commit()


# ---------- USERS ----------

async def get_user(telegram_id: int) -> Optional[dict]:
    async with aiosqlite.connect(config.database_path) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            "SELECT * FROM users WHERE telegram_id = ?", (telegram_id,)
        ) as cur:
            row = await cur.fetchone()
            return dict(row) if row else None


async def create_user(
    telegram_id: int, username: Optional[str], first_name: str
) -> None:
    async with aiosqlite.connect(config.database_path) as db:
        await db.execute(
            "INSERT OR IGNORE INTO users (telegram_id, username, first_name) "
            "VALUES (?, ?, ?)",
            (telegram_id, username, first_name),
        )
        await db.commit()


async def update_profile_field(telegram_id: int, field: str, value: str) -> None:
    """Обновляет одно поле профиля. field из whitelist."""
    if field not in PROFILE_FIELDS:
        raise ValueError(f"Forbidden field: {field}")
    async with aiosqlite.connect(config.database_path) as db:
        await db.execute(
            f"UPDATE users SET {field} = ? WHERE telegram_id = ?",
            (value, telegram_id),
        )
        await db.commit()


async def mark_onboarding_complete(telegram_id: int) -> None:
    async with aiosqlite.connect(config.database_path) as db:
        await db.execute(
            "UPDATE users SET onboarding_complete = 1 WHERE telegram_id = ?",
            (telegram_id,),
        )
        await db.commit()


async def save_profile_pack(telegram_id: int, pack: dict) -> None:
    """Сохраняет упаковку профиля (имя/bio/ссылка/закрепы) в JSON-поле."""
    import json as _json
    async with aiosqlite.connect(config.database_path) as db:
        await db.execute(
            "UPDATE users SET profile_pack_json = ? WHERE telegram_id = ?",
            (_json.dumps(pack, ensure_ascii=False), telegram_id),
        )
        await db.commit()


async def get_profile_pack(telegram_id: int) -> Optional[dict]:
    """Возвращает сохранённую упаковку профиля или None."""
    import json as _json
    user = await get_user(telegram_id)
    if not user or not user.get("profile_pack_json"):
        return None
    try:
        return _json.loads(user["profile_pack_json"])
    except (ValueError, TypeError):
        return None


async def update_profile_pack_block(
    telegram_id: int, block_key: str, value
) -> None:
    """Обновляет один блок упаковки (names | bios | link_recommendation | pinned_posts).

    Если pack ещё не сохранён — создаёт минимальный.
    """
    allowed = {"names", "bios", "link_recommendation", "pinned_posts"}
    if block_key not in allowed:
        raise ValueError(f"Forbidden pack block: {block_key}")
    current = await get_profile_pack(telegram_id) or {}
    current[block_key] = value
    await save_profile_pack(telegram_id, current)


async def save_product_pack(telegram_id: int, pack: dict) -> None:
    """Сохраняет последний сгенерированный набор продуктовых идей."""
    import json as _json
    async with aiosqlite.connect(config.database_path) as db:
        await db.execute(
            "UPDATE users SET product_pack_json = ? WHERE telegram_id = ?",
            (_json.dumps(pack, ensure_ascii=False), telegram_id),
        )
        await db.commit()


async def get_product_pack(telegram_id: int) -> Optional[dict]:
    """Возвращает сохранённый набор продуктовых идей или None."""
    import json as _json
    user = await get_user(telegram_id)
    if not user or not user.get("product_pack_json"):
        return None
    try:
        return _json.loads(user["product_pack_json"])
    except (ValueError, TypeError):
        return None


# ---------- ПАМЯТЬ СТИЛЯ (обучение на обратной связи) ----------

async def get_style_memory(telegram_id: int) -> str:
    """Возвращает выученные правила стиля автора (или пустую строку)."""
    user = await get_user(telegram_id)
    if not user:
        return ""
    return (user.get("style_memory") or "").strip()


async def update_style_memory(telegram_id: int, memory: str) -> None:
    """Перезаписывает память стиля (модель сама мерджит/дедуплицирует)."""
    async with aiosqlite.connect(config.database_path) as db:
        await db.execute(
            "UPDATE users SET style_memory = ? WHERE telegram_id = ?",
            ((memory or "").strip(), telegram_id),
        )
        await db.commit()


async def clear_style_memory(telegram_id: int) -> None:
    """Сбрасывает память стиля автора."""
    await update_style_memory(telegram_id, "")


# ---------- A/B ТЕСТ (с free trial vs без) ----------

async def assign_ab_variant(telegram_id: int) -> str:
    """Назначает A/B вариант юзеру.

    A/B тест free vs no-free завершён 2026-05-26 — победила ветка A
    (free trial). B показала 0% конверсии на 95 юзерах, A — 4.9% на 326.
    Решение зафиксировано в STRATEGY.md.

    Теперь:
      - Все новые юзеры (ab_variant IS NULL) получают 'A'.
      - Старые B-юзеры (~95 человек на момент закрытия теста) остаются
        в 'B' и продолжают видеть welcome без free-кнопки. Если захочется
        их вернуть — миграция отдельной командой.

    Возвращает финальный вариант юзера.
    """
    user = await get_user(telegram_id)
    if user and user.get("ab_variant") in ("A", "B"):
        return user["ab_variant"]

    # Новые юзеры — всегда A
    async with aiosqlite.connect(config.database_path) as db:
        await db.execute(
            "UPDATE users SET ab_variant = ? WHERE telegram_id = ?",
            ("A", telegram_id),
        )
        await db.commit()
    return "A"


async def get_ab_metrics() -> dict:
    """Сводка по A/B тесту. Возвращает {A: {...}, B: {...}}.

    Для каждой ветки: total, onboarded, paid_now, conversion (%).
    Legacy юзеры (ab_variant NULL) считаются как A.
    """
    async with aiosqlite.connect(config.database_path) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            "SELECT "
            "  COALESCE(ab_variant, 'A') AS variant, "
            "  COUNT(*) AS total, "
            "  SUM(CASE WHEN onboarding_complete = 1 THEN 1 ELSE 0 END) AS onboarded, "
            "  SUM(CASE WHEN subscription_expires_at IS NOT NULL "
            "           AND subscription_expires_at > datetime('now') "
            "           THEN 1 ELSE 0 END) AS paid_now "
            "FROM users "
            "GROUP BY COALESCE(ab_variant, 'A')",
        ) as cur:
            rows = await cur.fetchall()

    out: dict = {"A": {}, "B": {}}
    for row in rows:
        variant = row["variant"]
        total = row["total"] or 0
        paid = row["paid_now"] or 0
        out[variant] = {
            "total": total,
            "onboarded": row["onboarded"] or 0,
            "paid_now": paid,
            "conversion": (paid / total * 100) if total else 0.0,
        }
    # Гарантируем что обе ветки в словаре есть, даже если 0 юзеров
    for v in ("A", "B"):
        if not out[v]:
            out[v] = {"total": 0, "onboarded": 0, "paid_now": 0, "conversion": 0.0}
    return out


# ---------- FOLLOWUP DRIP (3 сообщения после /start если не оплатил) ----------
#
# Маска: бит 0 = первое сообщение (15м), бит 1 = второе (1ч), бит 2 = третье (3ч).
# Маска = 7 → все три выставлены / цепочка прервана (оплата, блок, ручная отмена).

FOLLOWUP_DONE_MASK = 0b111  # все 3 бита выставлены


async def start_followup_timer(telegram_id: int) -> None:
    """Запускает таймер догрева: NOW() как точка отсчёта, маска обнулена."""
    async with aiosqlite.connect(config.database_path) as db:
        await db.execute(
            "UPDATE users SET followup_start_at = ?, followup_sent_mask = 0 "
            "WHERE telegram_id = ?",
            (datetime.utcnow().isoformat(), telegram_id),
        )
        await db.commit()


async def cancel_followups(telegram_id: int) -> None:
    """Прерывает цепочку догрева — больше ничего не отправляем."""
    async with aiosqlite.connect(config.database_path) as db:
        await db.execute(
            "UPDATE users SET followup_sent_mask = ? WHERE telegram_id = ?",
            (FOLLOWUP_DONE_MASK, telegram_id),
        )
        await db.commit()


async def mark_followup_sent(telegram_id: int, position: int) -> None:
    """Выставляет бит position (0/1/2) в маске."""
    if position not in (0, 1, 2):
        raise ValueError(f"Invalid followup position: {position}")
    async with aiosqlite.connect(config.database_path) as db:
        await db.execute(
            "UPDATE users SET followup_sent_mask = followup_sent_mask | ? "
            "WHERE telegram_id = ?",
            (1 << position, telegram_id),
        )
        await db.commit()


async def get_followup_candidates() -> list[dict]:
    """Возвращает юзеров у кого таймер запущен и цепочка не завершена.

    Фильтр по времени и битам делается в Python (проще и быстрее).
    """
    async with aiosqlite.connect(config.database_path) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            "SELECT telegram_id, followup_start_at, followup_sent_mask, "
            "       subscription_expires_at "
            "FROM users "
            "WHERE followup_start_at IS NOT NULL "
            "  AND COALESCE(followup_sent_mask, 0) < ?",
            (FOLLOWUP_DONE_MASK,),
        ) as cur:
            return [dict(row) for row in await cur.fetchall()]


async def is_subscription_active(telegram_id: int) -> bool:
    user = await get_user(telegram_id)
    if not user or not user.get("subscription_expires_at"):
        return False
    try:
        expires = datetime.fromisoformat(user["subscription_expires_at"])
    except (ValueError, TypeError):
        return False
    # naive datetimes хранятся как UTC
    return expires > datetime.utcnow()


async def can_use_free_trial(telegram_id: int) -> bool:
    """Доступна ли бесплатная пробная генерация (одна разовая)."""
    user = await get_user(telegram_id)
    if not user:
        return False
    # SQLite вернёт 0 или 1
    return not bool(user.get("free_trial_used"))


async def mark_free_trial_used(telegram_id: int) -> None:
    async with aiosqlite.connect(config.database_path) as db:
        await db.execute(
            "UPDATE users SET free_trial_used = 1 WHERE telegram_id = ?",
            (telegram_id,),
        )
        await db.commit()


async def has_access(telegram_id: int) -> tuple[bool, str]:
    """Имеет ли юзер доступ к платным фичам.

    Возвращает (access, reason):
    - (True, "subscription") — есть подписка
    - (True, "free_trial") — есть неиспользованный free trial
    - (False, "none") — нет доступа
    """
    if await is_subscription_active(telegram_id):
        return True, "subscription"
    if await can_use_free_trial(telegram_id):
        return True, "free_trial"
    return False, "none"


# ---------- PROMOCODES ----------

def _generate_promocode_string() -> str:
    """8 символов без неоднозначных (0/O/I/1)."""
    alphabet = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"
    return "".join(secrets.choice(alphabet) for _ in range(8))


async def create_promocode(duration_days: int = 30) -> str:
    code = _generate_promocode_string()
    async with aiosqlite.connect(config.database_path) as db:
        await db.execute(
            "INSERT INTO promocodes (code, duration_days) VALUES (?, ?)",
            (code, duration_days),
        )
        await db.commit()
    return code


async def extend_subscription_days(telegram_id: int, days: int) -> datetime:
    """Продлевает подписку на N дней.

    Если активна — добавляем к концу периода.
    Если истекла или нет — стартуем с сегодня.
    Возвращает новую дату истечения.
    """
    now = datetime.utcnow()
    user = await get_user(telegram_id)
    base = now
    if user and user.get("subscription_expires_at"):
        try:
            current = datetime.fromisoformat(user["subscription_expires_at"])
            if current > now:
                base = current
        except (ValueError, TypeError):
            pass
    new_expires = base + timedelta(days=days)

    async with aiosqlite.connect(config.database_path) as db:
        await db.execute(
            "UPDATE users SET subscription_expires_at = ? WHERE telegram_id = ?",
            (new_expires.isoformat(), telegram_id),
        )
        await db.commit()
    return new_expires


async def cancel_subscription_renewal(telegram_id: int) -> None:
    """Помечает что юзер отменил продление в Tribute.

    Подписку не трогаем — она доработает до конца оплаченного периода.
    Можно использовать для уведомлений «вы отменили, действует до X».
    """
    # Сейчас просто ничего не делаем кроме лога — для будущей фичи можно
    # добавить колонку cancellation_pending в users.
    pass


async def activate_promocode(code: str, telegram_id: int) -> tuple[bool, str]:
    """Активирует промокод. Возвращает (success, user_facing_message)."""
    async with aiosqlite.connect(config.database_path) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            "SELECT * FROM promocodes WHERE code = ?", (code,)
        ) as cur:
            promo = await cur.fetchone()

        if not promo:
            return False, "Промокод не найден"
        if promo["used_by"]:
            return False, "Промокод уже использован"

        duration = promo["duration_days"]
        now = datetime.utcnow()
        new_expires = now + timedelta(days=duration)

        # Если уже есть активная подписка — продлеваем от её конца
        current = await get_user(telegram_id)
        if current and current.get("subscription_expires_at"):
            try:
                current_expires = datetime.fromisoformat(
                    current["subscription_expires_at"]
                )
                if current_expires > now:
                    new_expires = current_expires + timedelta(days=duration)
            except (ValueError, TypeError):
                pass

        await db.execute(
            "UPDATE users SET subscription_expires_at = ? WHERE telegram_id = ?",
            (new_expires.isoformat(), telegram_id),
        )
        await db.execute(
            "UPDATE promocodes SET used_by = ?, used_at = ? WHERE code = ?",
            (telegram_id, now.isoformat(), code),
        )
        await db.commit()

    return True, f"Подписка активна до {new_expires.strftime('%d.%m.%Y')}"


# ---------- GENERATIONS / LIMITS ----------

# format'ы которые считаются «доработками», а не «новыми генерациями»
# (не списывают основной DAILY_LIMIT, имеют собственный TRANSFORM_DAILY_LIMIT)
TRANSFORM_KINDS = ("transform", "refine", "humanize")


async def count_today_generations(telegram_id: int) -> int:
    """Считает «новые» генерации за сегодня (UTC). Reset в 00:00 UTC.

    Доработки (TRANSFORM_KINDS) НЕ учитываются — у них свой лимит.
    """
    placeholders = ",".join("?" for _ in TRANSFORM_KINDS)
    async with aiosqlite.connect(config.database_path) as db:
        async with db.execute(
            f"SELECT COUNT(*) FROM generations "
            f"WHERE user_id = ? AND date(created_at) = date('now') "
            f"  AND format NOT IN ({placeholders})",
            (telegram_id, *TRANSFORM_KINDS),
        ) as cur:
            row = await cur.fetchone()
            return row[0] if row else 0


async def count_today_transforms(telegram_id: int) -> int:
    """Сколько доработок (жёстче/мягче/refine/humanize) юзер сделал сегодня."""
    placeholders = ",".join("?" for _ in TRANSFORM_KINDS)
    async with aiosqlite.connect(config.database_path) as db:
        async with db.execute(
            f"SELECT COUNT(*) FROM generations "
            f"WHERE user_id = ? AND date(created_at) = date('now') "
            f"  AND format IN ({placeholders})",
            (telegram_id, *TRANSFORM_KINDS),
        ) as cur:
            row = await cur.fetchone()
            return row[0] if row else 0


def _is_admin(telegram_id: int) -> bool:
    return telegram_id == config.admin_telegram_id


async def can_generate_today(telegram_id: int) -> tuple[bool, int]:
    """Возвращает (can_generate, used_today). Админ — безлимит."""
    used = await count_today_generations(telegram_id)
    if _is_admin(telegram_id):
        return True, used
    return used < DAILY_LIMIT, used


async def can_transform_today(telegram_id: int) -> tuple[bool, int]:
    """Возвращает (can_transform, used_transforms_today). Админ — безлимит."""
    from config import TRANSFORM_DAILY_LIMIT
    used = await count_today_transforms(telegram_id)
    if _is_admin(telegram_id):
        return True, used
    return used < TRANSFORM_DAILY_LIMIT, used


async def log_generation(
    telegram_id: int, format_name: str, topic: Optional[str]
) -> None:
    async with aiosqlite.connect(config.database_path) as db:
        await db.execute(
            "INSERT INTO generations (user_id, format, topic) VALUES (?, ?, ?)",
            (telegram_id, format_name, topic),
        )
        await db.commit()


# ---------- PROFILE ANALYSIS LIMITS ----------

async def count_today_profile_analyses(telegram_id: int) -> int:
    """Сколько анализов профиля юзер сделал сегодня (UTC)."""
    async with aiosqlite.connect(config.database_path) as db:
        async with db.execute(
            "SELECT COUNT(*) FROM profile_analyses "
            "WHERE user_id = ? AND date(created_at) = date('now')",
            (telegram_id,),
        ) as cur:
            row = await cur.fetchone()
            return row[0] if row else 0


async def can_analyze_profile_today(telegram_id: int) -> bool:
    """1 анализ в сутки на юзера. Reset в 00:00 UTC. Админ — безлимит."""
    if _is_admin(telegram_id):
        return True
    return await count_today_profile_analyses(telegram_id) < 1


async def log_profile_analysis(telegram_id: int, overall_score: int) -> None:
    async with aiosqlite.connect(config.database_path) as db:
        await db.execute(
            "INSERT INTO profile_analyses (user_id, overall_score) VALUES (?, ?)",
            (telegram_id, overall_score),
        )
        await db.commit()


# ---------- FEED ANALYSIS LIMITS ----------

async def count_today_feed_analyses(telegram_id: int) -> int:
    """Сколько разборов чужой ленты юзер сделал сегодня (UTC)."""
    async with aiosqlite.connect(config.database_path) as db:
        async with db.execute(
            "SELECT COUNT(*) FROM feed_analyses "
            "WHERE user_id = ? AND date(created_at) = date('now')",
            (telegram_id,),
        ) as cur:
            row = await cur.fetchone()
            return row[0] if row else 0


async def can_analyze_feed_today(telegram_id: int) -> bool:
    """1 разбор в сутки. Reset в 00:00 UTC. Админ — безлимит."""
    if _is_admin(telegram_id):
        return True
    return await count_today_feed_analyses(telegram_id) < 1


async def log_feed_analysis(telegram_id: int, posts_count: int) -> None:
    async with aiosqlite.connect(config.database_path) as db:
        await db.execute(
            "INSERT INTO feed_analyses (user_id, posts_count) VALUES (?, ?)",
            (telegram_id, posts_count),
        )
        await db.commit()


# ---------- REFERRALS ----------

async def set_referrer_if_new(
    invitee_id: int, referrer_id: int, source: Optional[str] = None
) -> bool:
    """Привязывает реферера к юзеру (опционально с UTM-источником).

    Защищено от:
    - self-referral (юзер по своей ссылке)
    - повторной перезаписи (юзер пришёл по другой ссылке после первой)
    - несуществующего реферера
    """
    if invitee_id == referrer_id:
        return False

    async with aiosqlite.connect(config.database_path) as db:
        db.row_factory = aiosqlite.Row

        async with db.execute(
            "SELECT 1 FROM users WHERE telegram_id = ?", (referrer_id,)
        ) as cur:
            if not await cur.fetchone():
                return False

        async with db.execute(
            "SELECT 1 FROM referrals WHERE invitee_id = ?", (invitee_id,)
        ) as cur:
            if await cur.fetchone():
                return False

        await db.execute(
            "INSERT INTO referrals (invitee_id, referrer_id, source) "
            "VALUES (?, ?, ?)",
            (invitee_id, referrer_id, source),
        )
        await db.commit()
    return True


async def get_source_stats(referrer_id: int) -> list[dict]:
    """Воронка по UTM-источникам для админа.

    Возвращает список:
    [{source, invited, rewarded, bonus_days}, ...]
    Отсортировано по invited DESC.
    """
    async with aiosqlite.connect(config.database_path) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            "SELECT "
            "  COALESCE(source, '(без UTM)') AS source, "
            "  COUNT(*) AS invited, "
            "  SUM(CASE WHEN rewarded_at IS NOT NULL THEN 1 ELSE 0 END) AS rewarded, "
            "  COALESCE(SUM(bonus_days), 0) AS bonus_days "
            "FROM referrals "
            "WHERE referrer_id = ? "
            "GROUP BY source "
            "ORDER BY invited DESC",
            (referrer_id,),
        ) as cur:
            rows = await cur.fetchall()
    return [dict(r) for r in rows]


# ---------- PAYMENTS / PARTNER REVENUE ----------

async def get_referral_source(invitee_id: int) -> Optional[str]:
    """Возвращает UTM-источник по которому юзер пришёл (или None)."""
    async with aiosqlite.connect(config.database_path) as db:
        async with db.execute(
            "SELECT source FROM referrals WHERE invitee_id = ?",
            (invitee_id,),
        ) as cur:
            row = await cur.fetchone()
            return row[0] if row and row[0] else None


async def log_payment(
    user_id: int,
    amount_kopecks: Optional[int],
    currency: str,
    period_days: int,
    event_type: str,
) -> None:
    """Сохраняет платёж + автоматически проставляет UTM-источник из referrals."""
    source = await get_referral_source(user_id)
    async with aiosqlite.connect(config.database_path) as db:
        await db.execute(
            "INSERT INTO payments "
            "(user_id, amount_kopecks, currency, period_days, source, event_type) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (user_id, amount_kopecks, currency, period_days, source, event_type),
        )
        await db.commit()


async def get_revenue_stats(referrer_id: int) -> list[dict]:
    """Финансовая воронка по UTM-источникам.

    JOIN payments с referrals чтобы посчитать только оплаты тех юзеров,
    которых данный реферер привёл.

    Возвращает список:
    [{source, payers, payments_count, revenue_kopecks, commission_kopecks}, ...]
    """
    async with aiosqlite.connect(config.database_path) as db:
        db.row_factory = aiosqlite.Row
        # Используем p.source явно — колонка source есть и в payments, и в referrals.
        # GROUP BY по той же выражению что в SELECT.
        async with db.execute(
            "SELECT "
            "  COALESCE(p.source, '(без UTM)') AS src, "
            "  COUNT(DISTINCT p.user_id) AS payers, "
            "  COUNT(*) AS payments_count, "
            "  COALESCE(SUM(p.amount_kopecks), 0) AS revenue_kopecks "
            "FROM payments p "
            "JOIN referrals r ON r.invitee_id = p.user_id "
            "WHERE r.referrer_id = ? "
            "GROUP BY COALESCE(p.source, '(без UTM)') "
            "ORDER BY revenue_kopecks DESC",
            (referrer_id,),
        ) as cur:
            rows = await cur.fetchall()

    result = []
    for r in rows:
        d = dict(r)
        # Переименовываем src обратно в source для совместимости с хендлерами
        d["source"] = d.pop("src")
        d["commission_kopecks"] = int(
            (d["revenue_kopecks"] or 0) * PARTNER_COMMISSION_PERCENT / 100
        )
        result.append(d)
    return result


async def get_payments_detailed(referrer_id: int) -> list[dict]:
    """Список индивидуальных платежей по всем UTM-источникам реферера.

    Для админ-отчёта: видно кто, сколько и когда заплатил по каждому источнику.

    Возвращает список:
    [{source, user_id, username, first_name, amount_kopecks, currency,
      period_days, created_at}, ...]
    Отсортировано по source, потом по дате DESC.
    """
    async with aiosqlite.connect(config.database_path) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            "SELECT "
            "  COALESCE(p.source, '(без UTM)') AS source, "
            "  p.user_id, "
            "  u.username, "
            "  u.first_name, "
            "  p.amount_kopecks, "
            "  p.currency, "
            "  p.period_days, "
            "  p.created_at, "
            "  p.event_type "
            "FROM payments p "
            "JOIN referrals r ON r.invitee_id = p.user_id "
            "LEFT JOIN users u ON u.telegram_id = p.user_id "
            "WHERE r.referrer_id = ? "
            "ORDER BY source, p.created_at DESC",
            (referrer_id,),
        ) as cur:
            rows = await cur.fetchall()
    return [dict(r) for r in rows]


async def consume_referral_reward(
    invitee_id: int, bonus_days: int = REFERRAL_BONUS_DAYS
) -> Optional[dict]:
    """Если у инвайти есть невыплаченный реферал — выдаёт реферреру bonus_days.

    Возвращает {referrer_id, bonus_days, new_expires_at} или None.
    Идемпотентно: повторный вызов на уже-выплаченном реферале вернёт None.
    """
    async with aiosqlite.connect(config.database_path) as db:
        db.row_factory = aiosqlite.Row

        async with db.execute(
            "SELECT referrer_id, rewarded_at FROM referrals WHERE invitee_id = ?",
            (invitee_id,),
        ) as cur:
            ref = await cur.fetchone()

        if not ref or ref["rewarded_at"]:
            return None

        referrer_id = ref["referrer_id"]

        # Текущая подписка реферрера (если активна — продлеваем от её конца)
        async with db.execute(
            "SELECT subscription_expires_at FROM users WHERE telegram_id = ?",
            (referrer_id,),
        ) as cur:
            row = await cur.fetchone()

        now = datetime.utcnow()
        base = now
        if row and row["subscription_expires_at"]:
            try:
                current = datetime.fromisoformat(row["subscription_expires_at"])
                if current > now:
                    base = current
            except (ValueError, TypeError):
                pass

        new_expires = base + timedelta(days=bonus_days)

        await db.execute(
            "UPDATE users SET subscription_expires_at = ? WHERE telegram_id = ?",
            (new_expires.isoformat(), referrer_id),
        )
        await db.execute(
            "UPDATE referrals SET rewarded_at = ?, bonus_days = ? WHERE invitee_id = ?",
            (now.isoformat(), bonus_days, invitee_id),
        )
        await db.commit()

    return {
        "referrer_id": referrer_id,
        "bonus_days": bonus_days,
        "new_expires_at": new_expires,
    }


# ---------- THREADS ACCOUNTS ----------

async def save_threads_account(
    user_id: int,
    threads_user_id: str,
    threads_username: Optional[str],
    access_token_encrypted: bytes,
    token_expires_at: datetime,
) -> None:
    """Сохраняет / перезаписывает подключение Threads-аккаунта юзера."""
    async with aiosqlite.connect(config.database_path) as db:
        await db.execute(
            "INSERT INTO threads_accounts "
            "(user_id, threads_user_id, threads_username, access_token_encrypted, "
            "token_expires_at, connected_at) "
            "VALUES (?, ?, ?, ?, ?, ?) "
            "ON CONFLICT(user_id) DO UPDATE SET "
            "threads_user_id = excluded.threads_user_id, "
            "threads_username = excluded.threads_username, "
            "access_token_encrypted = excluded.access_token_encrypted, "
            "token_expires_at = excluded.token_expires_at, "
            "connected_at = excluded.connected_at",
            (
                user_id,
                threads_user_id,
                threads_username,
                access_token_encrypted,
                token_expires_at.isoformat(),
                datetime.utcnow().isoformat(),
            ),
        )
        await db.commit()


async def get_threads_account(user_id: int) -> Optional[dict]:
    async with aiosqlite.connect(config.database_path) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            "SELECT * FROM threads_accounts WHERE user_id = ?", (user_id,)
        ) as cur:
            row = await cur.fetchone()
            return dict(row) if row else None


async def delete_threads_account(user_id: int) -> None:
    async with aiosqlite.connect(config.database_path) as db:
        await db.execute(
            "DELETE FROM threads_accounts WHERE user_id = ?", (user_id,)
        )
        await db.commit()


async def mark_threads_post_sent(user_id: int) -> None:
    async with aiosqlite.connect(config.database_path) as db:
        await db.execute(
            "UPDATE threads_accounts SET last_post_at = ? WHERE user_id = ?",
            (datetime.utcnow().isoformat(), user_id),
        )
        await db.commit()


# ---------- PENDING POSTS (для публикации в Threads) ----------

async def save_pending_post(user_id: int, post_key: str, text: str) -> None:
    """Сохраняет текст поста в БД для последующей публикации.

    Переживает редеплой бота (в отличие от in-memory кэша).
    TTL — 24 часа, чистится фоновой джобой.
    """
    async with aiosqlite.connect(config.database_path) as db:
        await db.execute(
            "INSERT OR REPLACE INTO pending_posts (user_id, post_key, text, created_at) "
            "VALUES (?, ?, ?, ?)",
            (user_id, post_key, text, datetime.utcnow().isoformat()),
        )
        await db.commit()


async def get_pending_post(user_id: int, post_key: str) -> Optional[str]:
    async with aiosqlite.connect(config.database_path) as db:
        async with db.execute(
            "SELECT text FROM pending_posts WHERE user_id = ? AND post_key = ?",
            (user_id, post_key),
        ) as cur:
            row = await cur.fetchone()
            return row[0] if row else None


async def cleanup_old_pending_posts() -> int:
    """Удаляет посты старше 24 часов. Возвращает кол-во удалённых."""
    async with aiosqlite.connect(config.database_path) as db:
        cur = await db.execute(
            "DELETE FROM pending_posts WHERE created_at < datetime('now', '-1 day')"
        )
        await db.commit()
        return cur.rowcount


# ---------- STREAKS ----------

async def touch_streak(telegram_id: int) -> dict:
    """Регистрирует активность за сегодня и обновляет стрик.

    Возвращает {current_streak, best_streak, is_new_day, prev_streak}.
    is_new_day = True если это первая активность за сегодня (для триггера ачивок).
    """
    today = datetime.utcnow().date()
    async with aiosqlite.connect(config.database_path) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            "SELECT current_streak, best_streak, last_active_date "
            "FROM user_streaks WHERE user_id = ?",
            (telegram_id,),
        ) as cur:
            row = await cur.fetchone()

        prev_streak = 0
        is_new_day = True

        if row is None:
            new_streak = 1
            new_best = 1
        else:
            prev_streak = row["current_streak"] or 0
            last_str = row["last_active_date"]
            last_date = None
            if last_str:
                try:
                    last_date = datetime.fromisoformat(last_str).date()
                except (ValueError, TypeError):
                    last_date = None

            if last_date == today:
                # Уже отметились сегодня — стрик не меняется
                is_new_day = False
                new_streak = prev_streak
            elif last_date and (today - last_date).days == 1:
                # Подряд — продлеваем
                new_streak = prev_streak + 1
            else:
                # Пропущен день — обнуление
                new_streak = 1

            new_best = max(row["best_streak"] or 0, new_streak)

        await db.execute(
            "INSERT INTO user_streaks (user_id, current_streak, best_streak, last_active_date) "
            "VALUES (?, ?, ?, ?) "
            "ON CONFLICT(user_id) DO UPDATE SET "
            "current_streak = excluded.current_streak, "
            "best_streak = excluded.best_streak, "
            "last_active_date = excluded.last_active_date",
            (telegram_id, new_streak, new_best, today.isoformat()),
        )
        await db.commit()

    return {
        "current_streak": new_streak,
        "best_streak": new_best,
        "is_new_day": is_new_day,
        "prev_streak": prev_streak,
    }


async def get_streak(telegram_id: int) -> int:
    """Текущий стрик. Если последняя активность не вчера/не сегодня — 0."""
    today = datetime.utcnow().date()
    async with aiosqlite.connect(config.database_path) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            "SELECT current_streak, last_active_date "
            "FROM user_streaks WHERE user_id = ?",
            (telegram_id,),
        ) as cur:
            row = await cur.fetchone()

    if not row:
        return 0
    last_str = row["last_active_date"]
    if not last_str:
        return 0
    try:
        last_date = datetime.fromisoformat(last_str).date()
    except (ValueError, TypeError):
        return 0

    diff = (today - last_date).days
    if diff > 1:
        return 0  # Стрик уже сломан
    return row["current_streak"] or 0


# ---------- ACHIEVEMENTS ----------

async def unlock_achievement(telegram_id: int, code: str) -> bool:
    """Разблокирует ачивку. Возвращает True если ачивка реально новая."""
    async with aiosqlite.connect(config.database_path) as db:
        try:
            await db.execute(
                "INSERT INTO achievements (user_id, code) VALUES (?, ?)",
                (telegram_id, code),
            )
            await db.commit()
            return True
        except aiosqlite.IntegrityError:
            # Уже разблокирована
            return False


async def get_user_achievements(telegram_id: int) -> list[str]:
    async with aiosqlite.connect(config.database_path) as db:
        async with db.execute(
            "SELECT code FROM achievements WHERE user_id = ? ORDER BY unlocked_at",
            (telegram_id,),
        ) as cur:
            rows = await cur.fetchall()
            return [r[0] for r in rows]


async def has_achievement(telegram_id: int, code: str) -> bool:
    async with aiosqlite.connect(config.database_path) as db:
        async with db.execute(
            "SELECT 1 FROM achievements WHERE user_id = ? AND code = ?",
            (telegram_id, code),
        ) as cur:
            return (await cur.fetchone()) is not None


async def log_threads_publication(telegram_id: int, posts_count: int = 1) -> int:
    """Логирует факт публикации в Threads. Возвращает общее число публикаций после."""
    async with aiosqlite.connect(config.database_path) as db:
        await db.execute(
            "INSERT INTO threads_post_log (user_id, posts_count) VALUES (?, ?)",
            (telegram_id, posts_count),
        )
        await db.commit()
        async with db.execute(
            "SELECT COUNT(*) FROM threads_post_log WHERE user_id = ?",
            (telegram_id,),
        ) as cur:
            row = await cur.fetchone()
            return row[0] if row else 0


async def count_threads_publications(telegram_id: int) -> int:
    """Сколько раз юзер успешно публиковал в Threads."""
    async with aiosqlite.connect(config.database_path) as db:
        async with db.execute(
            "SELECT COUNT(*) FROM threads_post_log WHERE user_id = ?",
            (telegram_id,),
        ) as cur:
            row = await cur.fetchone()
            return row[0] if row else 0


async def count_voice_storytellings(telegram_id: int) -> int:
    """Сколько голосовых сторителлингов сделал юзер."""
    async with aiosqlite.connect(config.database_path) as db:
        async with db.execute(
            "SELECT COUNT(*) FROM generations "
            "WHERE user_id = ? AND format IN ('storytelling_voice', 'storytelling_audio')",
            (telegram_id,),
        ) as cur:
            row = await cur.fetchone()
            return row[0] if row else 0


async def count_user_generations(telegram_id: int) -> int:
    """Общее число генераций юзера за всё время."""
    async with aiosqlite.connect(config.database_path) as db:
        async with db.execute(
            "SELECT COUNT(*) FROM generations WHERE user_id = ?",
            (telegram_id,),
        ) as cur:
            row = await cur.fetchone()
            return row[0] if row else 0


async def count_successful_referrals(telegram_id: int) -> int:
    """Сколько рефералов реально активировали промокод."""
    async with aiosqlite.connect(config.database_path) as db:
        async with db.execute(
            "SELECT COUNT(*) FROM referrals "
            "WHERE referrer_id = ? AND rewarded_at IS NOT NULL",
            (telegram_id,),
        ) as cur:
            row = await cur.fetchone()
            return row[0] if row else 0


async def get_referral_stats(referrer_id: int) -> dict:
    """Статистика приглашений: всего, активировано, бонус-дней получено."""
    async with aiosqlite.connect(config.database_path) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            "SELECT "
            "COUNT(*) AS invited, "
            "SUM(CASE WHEN rewarded_at IS NOT NULL THEN 1 ELSE 0 END) AS rewarded, "
            "COALESCE(SUM(bonus_days), 0) AS bonus_days_total "
            "FROM referrals WHERE referrer_id = ?",
            (referrer_id,),
        ) as cur:
            row = await cur.fetchone()

    return {
        "invited": (row["invited"] if row else 0) or 0,
        "rewarded": (row["rewarded"] if row else 0) or 0,
        "bonus_days_total": (row["bonus_days_total"] if row else 0) or 0,
    }


# ---------- PARTNER LINKS ----------

async def find_user_by_username(username: str) -> Optional[dict]:
    """Ищет юзера в БД по @username (без @). Username хранится без @."""
    clean = username.lstrip("@").strip()
    if not clean:
        return None
    async with aiosqlite.connect(config.database_path) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            "SELECT * FROM users WHERE LOWER(username) = LOWER(?)",
            (clean,),
        ) as cur:
            row = await cur.fetchone()
            return dict(row) if row else None


async def find_partner_by_source(source: str) -> Optional[dict]:
    async with aiosqlite.connect(config.database_path) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            "SELECT * FROM partner_links WHERE source = ?", (source,),
        ) as cur:
            row = await cur.fetchone()
            return dict(row) if row else None


async def add_partner_link(
    partner_telegram_id: int, source: str, link: str
) -> None:
    async with aiosqlite.connect(config.database_path) as db:
        await db.execute(
            "INSERT INTO partner_links (partner_telegram_id, source, link) "
            "VALUES (?, ?, ?)",
            (partner_telegram_id, source, link),
        )
        await db.commit()


async def get_partner_links(partner_telegram_id: int) -> list[dict]:
    async with aiosqlite.connect(config.database_path) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            "SELECT * FROM partner_links WHERE partner_telegram_id = ? "
            "ORDER BY created_at ASC",
            (partner_telegram_id,),
        ) as cur:
            return [dict(row) for row in await cur.fetchall()]


async def is_partner(telegram_id: int) -> bool:
    """True, если у юзера есть хотя бы одна партнёрская ссылка."""
    async with aiosqlite.connect(config.database_path) as db:
        async with db.execute(
            "SELECT 1 FROM partner_links WHERE partner_telegram_id = ? LIMIT 1",
            (telegram_id,),
        ) as cur:
            return await cur.fetchone() is not None


# ---------- PARTNER CHAT (свободный чат с Gemini) ----------

async def add_partner_chat_message(
    user_id: int, role: str, content: str
) -> None:
    """Сохраняет одно сообщение чата. role = 'user' | 'model'."""
    async with aiosqlite.connect(config.database_path) as db:
        await db.execute(
            "INSERT INTO partner_chat_messages (user_id, role, content) "
            "VALUES (?, ?, ?)",
            (user_id, role, content),
        )
        await db.commit()


async def get_partner_chat_history(
    user_id: int, limit: int = 40
) -> list[dict]:
    """Последние `limit` сообщений в хронологическом порядке.

    Возвращает [{role, content}, ...] от старых к новым.
    """
    async with aiosqlite.connect(config.database_path) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            "SELECT role, content FROM partner_chat_messages "
            "WHERE user_id = ? ORDER BY id DESC LIMIT ?",
            (user_id, limit),
        ) as cur:
            rows = [dict(r) for r in await cur.fetchall()]
    rows.reverse()
    return rows


async def count_partner_chat_messages(user_id: int) -> int:
    async with aiosqlite.connect(config.database_path) as db:
        async with db.execute(
            "SELECT COUNT(*) FROM partner_chat_messages WHERE user_id = ?",
            (user_id,),
        ) as cur:
            row = await cur.fetchone()
            return int(row[0]) if row and row[0] is not None else 0


async def clear_partner_chat(user_id: int) -> None:
    """Удаляет всю историю чата юзера (кнопка «Новый чат»)."""
    async with aiosqlite.connect(config.database_path) as db:
        await db.execute(
            "DELETE FROM partner_chat_messages WHERE user_id = ?",
            (user_id,),
        )
        await db.commit()


# ---------- VIRAL POSTS (кэш топовых веток через threads_keyword_search) ----------

async def upsert_viral_post(
    threads_id: str,
    permalink: str,
    text: str,
    username: str,
    replies_count: int,
    posted_at: Optional[datetime],
    keyword: str,
) -> None:
    """Сохраняет или обновляет пост в кэше виралок. Уникален по threads_id."""
    posted_iso = posted_at.isoformat() if posted_at else None
    async with aiosqlite.connect(config.database_path) as db:
        await db.execute(
            "INSERT INTO viral_posts "
            "(threads_id, permalink, text, username, replies_count, posted_at, keyword) "
            "VALUES (?, ?, ?, ?, ?, ?, ?) "
            "ON CONFLICT(threads_id) DO UPDATE SET "
            "  replies_count = excluded.replies_count, "
            "  fetched_at = CURRENT_TIMESTAMP",
            (threads_id, permalink, text, username, replies_count, posted_iso, keyword),
        )
        await db.commit()


async def get_viral_posts_by_keyword(
    keyword: str, limit: int = 5, max_age_days: int = 7
) -> list[dict]:
    """Возвращает топ постов по keyword за последние N дней, по replies_count."""
    cutoff = (datetime.utcnow() - timedelta(days=max_age_days)).isoformat()
    async with aiosqlite.connect(config.database_path) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            "SELECT * FROM viral_posts "
            "WHERE keyword = ? AND COALESCE(posted_at, fetched_at) >= ? "
            "ORDER BY replies_count DESC, posted_at DESC "
            "LIMIT ?",
            (keyword, cutoff, limit),
        ) as cur:
            return [dict(row) for row in await cur.fetchall()]


async def get_viral_keywords_available() -> list[str]:
    """Список keywords по которым уже есть посты в кэше."""
    async with aiosqlite.connect(config.database_path) as db:
        async with db.execute(
            "SELECT DISTINCT keyword FROM viral_posts ORDER BY keyword"
        ) as cur:
            return [row[0] for row in await cur.fetchall()]


async def cleanup_old_viral_posts(days: int = 30) -> int:
    """Удаляет посты старше N дней (по дате публикации). Возвращает кол-во удалённых."""
    cutoff = (datetime.utcnow() - timedelta(days=days)).isoformat()
    async with aiosqlite.connect(config.database_path) as db:
        cur = await db.execute(
            "DELETE FROM viral_posts WHERE COALESCE(posted_at, fetched_at) < ?",
            (cutoff,),
        )
        await db.commit()
        return cur.rowcount or 0


# ---------- ADMIN ANALYTICS ----------

async def get_admin_overview() -> dict:
    """Сводка по всему боту для админ-команды /admin.

    Возвращает dict с ключами:
      total_users, onboarded, with_active_sub, used_free_trial,
      active_24h, active_7d, gens_today, gens_7d, gens_30d,
      revenue_total_kopecks, payments_count, threads_connected
    """
    now = datetime.utcnow()
    iso_24h = (now - timedelta(hours=24)).isoformat()
    iso_7d = (now - timedelta(days=7)).isoformat()
    iso_30d = (now - timedelta(days=30)).isoformat()
    today_start = now.replace(hour=0, minute=0, second=0, microsecond=0).isoformat()

    async with aiosqlite.connect(config.database_path) as db:
        db.row_factory = aiosqlite.Row

        async def _scalar(query: str, params: tuple = ()) -> int:
            async with db.execute(query, params) as cur:
                row = await cur.fetchone()
                if not row:
                    return 0
                val = row[0]
                return int(val) if val is not None else 0

        total_users = await _scalar("SELECT COUNT(*) FROM users")
        onboarded = await _scalar(
            "SELECT COUNT(*) FROM users WHERE onboarding_complete = 1"
        )
        with_active_sub = await _scalar(
            "SELECT COUNT(*) FROM users WHERE subscription_expires_at > ?",
            (now.isoformat(),),
        )
        used_free_trial = await _scalar(
            "SELECT COUNT(*) FROM users WHERE COALESCE(free_trial_used, 0) = 1"
        )

        # «Активный» = были генерации
        active_24h = await _scalar(
            "SELECT COUNT(DISTINCT user_id) FROM generations WHERE created_at >= ?",
            (iso_24h,),
        )
        active_7d = await _scalar(
            "SELECT COUNT(DISTINCT user_id) FROM generations WHERE created_at >= ?",
            (iso_7d,),
        )

        gens_today = await _scalar(
            "SELECT COUNT(*) FROM generations WHERE created_at >= ?",
            (today_start,),
        )
        gens_7d = await _scalar(
            "SELECT COUNT(*) FROM generations WHERE created_at >= ?",
            (iso_7d,),
        )
        gens_30d = await _scalar(
            "SELECT COUNT(*) FROM generations WHERE created_at >= ?",
            (iso_30d,),
        )

        revenue_total_kopecks = await _scalar(
            "SELECT COALESCE(SUM(amount_kopecks), 0) FROM payments"
        )
        payments_count = await _scalar("SELECT COUNT(*) FROM payments")

        threads_connected = await _scalar(
            "SELECT COUNT(*) FROM threads_accounts"
        )

        # Рефералы: сколько всего приглашений, сколько разных людей
        # вообще кого-то пригласили, сколько приглашений довели до награды.
        referrals_total = await _scalar("SELECT COUNT(*) FROM referrals")
        referrers_distinct = await _scalar(
            "SELECT COUNT(DISTINCT referrer_id) FROM referrals"
        )
        referrals_rewarded = await _scalar(
            "SELECT COUNT(*) FROM referrals WHERE rewarded_at IS NOT NULL"
        )

    return {
        "total_users": total_users,
        "onboarded": onboarded,
        "with_active_sub": with_active_sub,
        "used_free_trial": used_free_trial,
        "active_24h": active_24h,
        "active_7d": active_7d,
        "gens_today": gens_today,
        "gens_7d": gens_7d,
        "gens_30d": gens_30d,
        "revenue_total_kopecks": revenue_total_kopecks,
        "payments_count": payments_count,
        "threads_connected": threads_connected,
        "referrals_total": referrals_total,
        "referrers_distinct": referrers_distinct,
        "referrals_rewarded": referrals_rewarded,
    }
