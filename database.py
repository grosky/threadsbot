"""SQLite-хранилище через aiosqlite. Юзеры, промокоды, генерации.

Заметки:
- На Railway SQLite живёт во временном диске — для прод-нагрузки переходи на Postgres.
- Все запросы async. row_factory = aiosqlite.Row даёт dict-доступ к результатам.
"""
import secrets
from datetime import datetime, timedelta, timezone
from typing import Optional

import aiosqlite

from config import DAILY_LIMIT, config

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
"""

# Бонус приглашающему за каждого реферала, активировавшего промокод
REFERRAL_BONUS_DAYS = 30

# Whitelist полей профиля (защита от SQL-инъекций в update_profile_field)
PROFILE_FIELDS = {
    "niche", "audience", "product", "product_link",
    "tone", "facts", "pains", "social_proof",
}


async def init_db() -> None:
    """Создаёт таблицы при первом запуске."""
    async with aiosqlite.connect(config.database_path) as db:
        await db.executescript(SCHEMA)
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

async def count_today_generations(telegram_id: int) -> int:
    """Считает генерации за сегодня (UTC). Reset в 00:00 UTC."""
    async with aiosqlite.connect(config.database_path) as db:
        async with db.execute(
            "SELECT COUNT(*) FROM generations "
            "WHERE user_id = ? AND date(created_at) = date('now')",
            (telegram_id,),
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

async def set_referrer_if_new(invitee_id: int, referrer_id: int) -> bool:
    """Привязывает реферера к юзеру. Возвращает True если связь создана впервые.

    Защищено от:
    - self-referral (юзер по своей ссылке)
    - повторной перезаписи (юзер пришёл по другой ссылке после первой)
    - несуществующего реферера
    """
    if invitee_id == referrer_id:
        return False

    async with aiosqlite.connect(config.database_path) as db:
        db.row_factory = aiosqlite.Row

        # Реферер должен существовать
        async with db.execute(
            "SELECT 1 FROM users WHERE telegram_id = ?", (referrer_id,)
        ) as cur:
            if not await cur.fetchone():
                return False

        # Уже есть связь? Не перезаписываем.
        async with db.execute(
            "SELECT 1 FROM referrals WHERE invitee_id = ?", (invitee_id,)
        ) as cur:
            if await cur.fetchone():
                return False

        await db.execute(
            "INSERT INTO referrals (invitee_id, referrer_id) VALUES (?, ?)",
            (invitee_id, referrer_id),
        )
        await db.commit()
    return True


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
