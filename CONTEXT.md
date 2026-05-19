# Контекст проекта — для нового чата с Claude

Файл-памятка чтобы новая сессия Claude быстро вошла в контекст. Читать целиком перед любой работой.

---

## Что это за продукт

**Lazy Threads** — Telegram-бот @aithreadbot, AI-помощник для авторов Threads:
- Генерация виральных постов (5 форматов: манифест, разбор, контринтуитивный, история, метод_известного)
- Голосовой сторителлинг (voice → готовый пост через Gemini audio)
- Анализ упаковки профиля по скрину
- Разбор чужих лент (юзер кидает 3-10 постов → бот находит паттерны и адаптирует под нишу)
- Доработка постов: жёстче / мягче / по фидбеку
- Авто-публикация в Threads (за фича-флагом, ждёт App Review)
- Подписка через Tribute с автопродлением
- Реферальная программа + UTM-трекинг + партнёрская воронка с комиссией 30%

## Стек

- **Python 3.13** на Railway
- **aiogram 3.x** — Telegram Bot framework
- **aiohttp** — HTTP-сервер для OAuth callback и Tribute webhook
- **aiosqlite** — БД (SQLite на Railway Volume `/data`)
- **Google Gemini API** через `google-genai` SDK
- **Cryptography (Fernet)** — шифрование Threads токенов
- Деплой: Railway auto-deploy от push в main
- Платежи: Tribute (через @tribute_app в Telegram)

## Инфраструктура

| Что | Где |
|---|---|
| Production URL | `worker-production-5690.up.railway.app` |
| GitHub | `github.com/grosky/threadsbot` |
| Telegram bot | `@aithreadbot` ("Ленивый THREADS") |
| Tribute subscription | `https://t.me/tribute/app?startapp=sVs1` |
| Privacy / ToS | `grosky.github.io/threadsbot/{privacy,terms}.html` |

## Env vars в Railway

```
BOT_TOKEN              = Telegram Bot Token
GEMINI_API_KEY         = Gemini API (на paid tier через ai.studio с auto-reload)
ADMIN_TELEGRAM_ID      = 709345953
DATABASE_PATH          = /data/bot.db
ENCRYPTION_KEY         = Fernet ключ для Threads токенов
META_APP_ID            = 4420621271550202
META_APP_SECRET        = (в Railway, не в чате)
META_REDIRECT_URI      = https://worker-production-5690.up.railway.app/auth/threads/callback
TRIBUTE_API_KEY        = (в Railway)
TRIBUTE_SUBSCRIPTION_URL = https://t.me/tribute/app?startapp=sVs1
THREADS_PUBLISH_ENABLED = (не задана = false) — флаг включения публикации после App Review
```

## Структура кода

```
threads_bot/
├── bot.py                  # Entry point — aiogram polling + aiohttp server
├── config.py               # Config dataclass + env loading + feature flags
├── database.py             # aiosqlite, схема, миграции, все CRUD
├── prompts.py              # Все промты для Gemini (SYSTEM_PROMPT, PROFILE_ANALYSIS, ...)
├── gemini_service.py       # Gemini client с retry + fallback на 2.5-flash
├── threads_api.py          # OAuth + публикация через graph.threads.net
├── oauth_server.py         # aiohttp routes для OAuth callback + Tribute webhook
├── tribute_webhook.py      # Handler для Tribute payments (продление подписок)
├── achievements.py         # 9 ачивок + проверка условий
├── handlers/
│   ├── __init__.py         # setup_routers
│   ├── start.py            # /start, welcome screen, /code (скрытая), /promo (admin)
│   ├── onboarding.py       # 4-вопросный онбординг профиля
│   ├── menu.py             # Иерархическое меню: Создание / Аналитика / Настройки
│   ├── generation.py       # Главный флоу генерации (длина → тема → 3 варианта)
│   ├── brainstorm.py       # «💡 Идеи для постов» — Gemini генерит 10 идей
│   ├── storytelling.py     # Голосовой сторителлинг
│   ├── custom_post.py      # «Свой пост» (скрыто за threads_publish_enabled)
│   ├── threads_connect.py  # OAuth + публикация (скрыто за threads_publish_enabled)
│   ├── profile_analysis.py # Анализ профиля по скрину
│   ├── feed_analysis.py    # Разбор чужой ленты
│   └── referral.py         # /invite, /utm, /stats — реферальная программа
├── docs/                   # GitHub Pages: index.html, privacy.html, terms.html
├── APP_REVIEW.md           # Готовые тексты + чек-лист для подачи на Meta App Review
├── TODO.md                 # Приоритизированный список задач
└── CONTEXT.md              # Этот файл
```

## Что работает в проде сейчас (на момент переезда в новый чат)

| Фича | Статус |
|---|---|
| Welcome + free trial + paywall | ✅ |
| Онбординг 4 вопроса | ✅ |
| Генерация постов (короткие/длинные, авто-выбор формата) | ✅ |
| «💡 Идеи для постов» (брейншторм) | ✅ |
| Голосовой сторителлинг | ✅ |
| Анализ профиля по скрину | ✅ |
| Разбор чужой ленты | ✅ |
| Доработка постов (жёстче/мягче/refine) | ✅ |
| Tribute оплаты + автопродление | ✅ |
| Реферальная программа | ✅ |
| UTM-трекинг + /utm + /stats | ✅ |
| 9 ачивок + стрики | ✅ |
| Скрытая команда /code для админских раздач | ✅ |
| Gemini 3 Flash с fallback на 2.5 Flash | ✅ |
| Paid tier Gemini (AI Studio prepaid credits) | ✅ |
| **Threads публикация** | 🔒 за флагом `THREADS_PUBLISH_ENABLED`, ждёт App Review |

## Главные правила UX (из договорённостей с юзером)

- **Эмодзи**: 1 эмодзи в шапке сообщения, не больше. Без эмодзи на каждом bullet.
- **Bullets**: через `—`, без иконок.
- **Без `━━━` разделителей** — пустая строка справляется.
- **Жирный** для акцентов, не эмодзи.
- **«ты»** везде, не «вы».
- **Слово «промокод» НЕ показывать юзерам** — заменено на «оформить подписку». Скрытая команда `/code` для активации.
- **Короткие посты НЕ должны содержать CTA/ссылки** — только хук + добивка.

## Известные проблемы / нюансы

1. **Threads публикация** — заблокирована Meta до App Review. Кнопки и подменю скрыты через `config.threads_publish_enabled`. Код полностью сохранён и работает — флипнуть флаг в Railway → всё включится.

2. **Gemini 3 Flash Preview** — может выдавать 503 в часы пик. Сделан retry+fallback на 2.5 Flash в `gemini_service._call_with_fallback()`.

3. **AI Studio billing ≠ Cloud Billing**. $300 Google Cloud credits НЕ применяются к AI Studio. Используем prepaid в AI Studio ($10 пополнено, auto-reload включен).

4. **Bot DB переживает редеплои** — Railway Volume `/data` смонтирован. Миграции через PRAGMA в `database._migrate_users_columns` и `_migrate_referrals_columns`.

5. **Юзер из РФ** — Threads в РФ заблокирован. Регистрация Meta dev-аккаунта прошла через виртуальный номер из Бразилии + VPN. Целевая аудитория = русскоязычные за пределами РФ + люди с VPN.

## Что ОСТАЛОСЬ до полноценного запуска

См. `TODO.md`. Главное:
1. **App Review** для Meta — иконка 1024×1024 + демо-видео 2-3 мин. Без него Threads-публикация только для добавленных вручную тестеров.
2. **End-to-end тест** со 2-го tg-аккаунта (выявить баги что не видны админу).
3. **Запуск** — посты в свои соц.сети + сбор первых платных юзеров.

## Что НЕ делать без обсуждения

- **Не выпиливать феатуры** без явного запроса — даже если кажется лишним, всё на каких-то этапах согласовано.
- **Не менять модель Gemini** на Pro без явного запроса — экономика расчитана на Flash.
- **Не включать `THREADS_PUBLISH_ENABLED`** пока App Review не одобрен — иначе юзеры будут видеть «invite required» ошибку.
- **Не возвращать слово «промокод»** в публичный UI.
- **Не возвращать выбор формата** в UI — рандомизация форматов согласована.

## Стиль работы с юзером

- **Краткость и конкретика** — он быстро устаёт от длинных ответов.
- **Не подвешивать вопросами** — лучше принять решение и сделать, чем гонять туда-сюда.
- **Честность про ограничения** — если что-то не работает по причине Meta/Telegram/Gemini — сказать прямо и предложить workaround.
- **Не предлагать сторонние сервисы** без явной необходимости.
- **Code-first** — после обсуждения сразу коммит + push (Railway auto-deploy).
