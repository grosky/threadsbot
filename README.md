# Threads AI Posts Bot

Telegram-бот для генерации вирусных постов в Threads на основе профиля автора.
Использует Gemini 2.5 Flash для генерации, aiogram 3.x как фреймворк, SQLite как хранилище.

## Возможности MVP

- Активация по промокоду (30 дней по умолчанию)
- Онбординг профиля автора (8 полей, ~5 минут)
- Генерация 3 вариантов поста под 5 форматов
- Лимит 4 генерации в день на юзера (reset в 00:00 UTC)
- Структурированный JSON-ответ от Gemini с гарантией формата
- Команда `/promo` для админа — генерирует новые промокоды

## Структура проекта

```
threads_bot/
├── bot.py              # entry point
├── config.py           # настройки из .env
├── database.py         # SQLite + aiosqlite
├── prompts.py          # SYSTEM_PROMPT + user message builder
├── gemini_service.py   # клиент Gemini API
├── handlers/
│   ├── __init__.py     # setup_routers
│   ├── start.py        # /start, активация промокода, /promo
│   ├── onboarding.py   # FSM для 8 вопросов
│   ├── menu.py         # главное меню, профиль, подписка
│   └── generation.py   # генерация постов
├── requirements.txt
├── .env.example
├── Procfile            # для Railway
└── .gitignore
```

## Установка локально

```bash
cd threads_bot
python -m venv venv
source venv/bin/activate    # на Windows: venv\Scripts\activate
pip install -r requirements.txt
cp .env.example .env
# отредактируй .env, вставь свои ключи
python bot.py
```

### Где взять ключи

- **BOT_TOKEN** — у [@BotFather](https://t.me/BotFather) в Telegram (`/newbot`)
- **GEMINI_API_KEY** — на [aistudio.google.com/app/apikey](https://aistudio.google.com/app/apikey) (бесплатный tier есть)
- **ADMIN_TELEGRAM_ID** — твой Telegram ID, получить можно у [@userinfobot](https://t.me/userinfobot)

## Деплой на Railway

1. Создай новый проект в Railway, подключи репозиторий
2. В Variables добавь все переменные из `.env.example`
3. Railway сам определит Python и запустит `python bot.py` через Procfile
4. Залогинься в бот, отправь `/promo` — получишь первый промокод

**Важно про SQLite на Railway:** файл `bot.db` хранится на эфемерном диске и может потеряться при редеплое. Для прода подключи Railway Postgres addon и перепиши `database.py` на `asyncpg` (или `SQLAlchemy + asyncpg`).

## Первый запуск (рабочий flow)

1. Запусти `python bot.py` локально
2. Открой своего бота в Telegram, отправь `/start`
3. Бот попросит промокод — но у тебя его ещё нет
4. Отправь `/promo` от того же аккаунта (твой ID = ADMIN_TELEGRAM_ID) — получишь код
5. Отправь `/start` снова, введи полученный код
6. Пройди онбординг (8 вопросов)
7. В главном меню жми "Сгенерить пост" → выбери формат → опиши тему или жми "Удиви меня"
8. Получи 3 варианта поста

## Стоимость в проде (Gemini 2.5 Flash)

- ~$0.006 за одну генерацию (3 варианта)
- 4 генерации × 30 дней = ~$0.72/мес на активного юзера
- При тарифе 590₽ — маржа ~90% на Gemini-составляющей

## Что добавить дальше

- [ ] **Tribute integration** — webhooks для auto-renewal подписки
- [ ] **Кнопка "Сделать жёстче"** — переписать выбранный вариант с другим тоном
- [ ] **Daily push** — APScheduler + "тема дня" утром
- [ ] **Редактирование отдельных полей профиля** (сейчас только полный re-onboarding)
- [ ] **История генераций** — `/history`, последние 10 постов
- [ ] **Postgres** вместо SQLite для прода
- [ ] **Аналитика для админа** — DAU, MAU, conversion на продление
- [ ] **Защита от race condition** при параллельных нажатиях — счётчик через `INSERT ... RETURNING`
- [ ] **Уведомление об истечении** — за 3 дня до конца подписки

## Лицензия

MIT, делай что хочешь.
