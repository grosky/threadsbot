# Meta App Review — чек-лист и материалы

Документ для подачи Threadsbot на App Review в Meta. Содержит:
- Подготовку (что включить и где)
- Тексты для полей в Meta dashboard
- Скрипт демо-видео
- Чек-лист подачи

---

## 1. Подготовка хостинга политик (5 минут)

GitHub Pages бесплатно отдаёт HTML по HTTPS — идеально для Privacy и Terms.

### Включение GitHub Pages

1. Открой [github.com/grosky/threadsbot/settings/pages](https://github.com/grosky/threadsbot/settings/pages)
2. **Source**: `Deploy from a branch`
3. **Branch**: `main`, folder: `/docs`
4. **Save**
5. Через ~1 минуту страница станет доступна

### Финальные URL (вставлять в Meta dashboard)

- **Сайт продукта**: `https://grosky.github.io/threadsbot/`
- **Privacy Policy**: `https://grosky.github.io/threadsbot/privacy.html`
- **Terms of Service**: `https://grosky.github.io/threadsbot/terms.html`

Если поменяешь username/имя репо — поменяй URL соответственно.

---

## 2. Заполнение Meta dashboard

### App Settings → Basic

| Поле | Значение |
|---|---|
| **Display Name** | `Lazy Threads` |
| **Namespace** | (любой, например `lazythreads`) |
| **App Domains** | `grosky.github.io` |
| **Privacy Policy URL** | `https://grosky.github.io/threadsbot/privacy.html` |
| **Terms of Service URL** | `https://grosky.github.io/threadsbot/terms.html` |
| **User Data Deletion** | `https://grosky.github.io/threadsbot/privacy.html#data-deletion` (или Telegram-контакт) |
| **Category** | `Productivity` или `Business and Pages` |
| **App Icon** | 1024×1024 PNG (см. ниже) |

### App Icon

Сделай минимальный логотип 1024×1024 PNG. Варианты:
- Можно через [Canva](https://canva.com) → шаблон Square Logo
- Или сгенерь через любой AI-генератор изображений с промтом: `minimal flat logo, threads icon stylized, dark blue and white, 1024x1024`
- Главное: чисто, без шрифта Threads (чтобы не было нарушения брендбука Meta)

---

## 3. Permissions and Features (Threads API → Use Case)

Для каждого scope нужно описание в духе «как именно я его использую».

### threads_basic

**Permission description (for app review):**
```
We use threads_basic to identify the user after they authenticate via OAuth.
Specifically, we call GET /me with fields=id,username to display the connected
account name in our bot's UI (e.g. «Threads: @username») and to associate
generated content with the correct user when they tap «Publish».

The user information is stored encrypted at rest and shown only to the user
themselves inside our Telegram bot. We do not display this information
publicly anywhere.
```

### threads_content_publish

**Permission description:**
```
We use threads_content_publish to publish posts to the user's Threads account
on their behalf — but only when the user explicitly taps the «Publish to
Threads» button inside our Telegram bot.

Flow:
1. The user generates a post inside the bot using AI, OR types it manually
2. The user reviews the generated text in Telegram
3. The user taps «📤 Publish to Threads»
4. We call POST /me/threads + POST /me/threads_publish
5. For long posts (>500 chars) we split into a thread chain using reply_to_id

We never publish without explicit user action. We never auto-post. We do not
have a scheduling feature at this time.
```

---

## 4. Демо-видео — скрипт записи

Meta требует видео-демонстрацию каждого permission в действии. Сделай **2 коротких ролика** (или один общий 2-3 минуты).

### Что снимать (общая последовательность)

Запиши экран телефона или браузера. **Покажи ПОЛНЫЙ цикл от OAuth до публикации.**

**Сценарий (2-3 минуты):**

1. **(0:00–0:15)** Открой Telegram → `@aithreadbot` → отправь `/menu`. Голосом или субтитрами:
   > «This is Lazy Threads, an AI assistant for Threads content creators.»

2. **(0:15–0:35)** Тапни «📝 Создание» → «Сгенерить пост» → выбери формат → получи 3 варианта. Покажи на экране:
   > «The bot generates posts using Google Gemini based on the user's niche.»

3. **(0:35–1:00)** Вернись в `/menu` → «⚙️ Настройки» → «🔗 Threads» → тапни «Подключить».
   > «Now I'll connect my Threads account using OAuth.»
   
   Перейди на страницу Meta → покажи consent screen с двумя permissions:
   > «The app requests threads_basic and threads_content_publish permissions.»
   
   Тапни Allow → вернись в бот → покажи сообщение «Threads подключён, @username».
   > «threads_basic was used here to fetch the user's profile and display it.»

4. **(1:00–1:30)** Под сгенерированным постом тапни «📤 Опубликовать в Threads».
   > «Now I'll publish this post. The bot uses threads_content_publish to create and publish via the official API.»
   
   Покажи loading → success message с ссылкой на пост.

5. **(1:30–2:00)** Открой Threads-приложение / threads.net → покажи свежеопубликованный пост.
   > «Here's the post live in Threads. Everything was triggered by an explicit user action — there's no auto-posting.»

### Тех. требования к видео

- Формат: MP4, MOV или любой стандартный
- Длительность: до 3 минут
- Разрешение: 720p или выше
- Звук опционален — лучше с короткими надписями-субтитрами на английском
- Загрузить на YouTube (Unlisted) или Vimeo, дать ссылку Meta

### Что снимать НЕ нужно

- Внутренний код / Railway dashboard
- Личные сообщения / приватные данные
- Без обходных путей / VPN не в кадре

---

## 5. Use Case Description (для подачи)

В Meta dashboard → Submit for Review будет поле описания всего use case. Текст:

```
Lazy Threads is an AI-powered Telegram bot that helps content creators
generate and publish posts to Threads.

The user flow:
1. The user signs up for the bot via Telegram and completes an 8-question
   onboarding (niche, target audience, product, tone of voice).
2. The user connects their Threads account via OAuth, granting threads_basic
   and threads_content_publish.
3. The user generates posts via AI (Google Gemini) inside the bot, OR types
   their own post.
4. The user explicitly taps «Publish to Threads» to publish.

The Threads API is used as follows:
- threads_basic: GET /me to fetch the connected user's id and username for
  display purposes inside the bot.
- threads_content_publish: POST /me/threads + POST /me/threads_publish to
  create and publish text-only posts. Long posts (>500 chars) are split into
  native Threads chains using the reply_to_id parameter.

There is NO auto-posting, NO scheduling, NO automated triggers. Every
publication is initiated by an explicit user action.

We do not collect, store or display data from other Threads users —
exclusively the authenticated user's own profile info.

Access tokens are stored encrypted at rest using symmetric Fernet
encryption. We are using long-lived tokens with the standard 60-day TTL.

The bot operates as a service for individual creators. Each user authorises
their own Threads account independently. Subscriptions are paid (~$5/month
equivalent) and limit daily generations.
```

---

## 6. Submission checklist (порядок действий)

- [ ] Включить GitHub Pages в `grosky/threadsbot` (Settings → Pages → main / docs)
- [ ] Открыть `https://grosky.github.io/threadsbot/` в браузере — убедиться что страница работает
- [ ] То же для `privacy.html` и `terms.html`
- [ ] Создать 1024×1024 PNG иконку
- [ ] В Meta dashboard → **App Settings → Basic**:
  - [ ] Display name: Lazy Threads
  - [ ] App icon загрузить
  - [ ] Privacy Policy URL
  - [ ] Terms of Service URL
  - [ ] User Data Deletion URL
  - [ ] App Domains: grosky.github.io
- [ ] **Threads API → Use Cases → Configure**:
  - [ ] threads_basic permission description (см. выше)
  - [ ] threads_content_publish permission description
- [ ] Записать демо-видео по сценарию выше
- [ ] Загрузить видео на YouTube/Vimeo как Unlisted
- [ ] **Submit for Review** в Meta dashboard:
  - [ ] Use case description
  - [ ] Demo video URL
  - [ ] Test user credentials (создать в Meta — для рецензента)
- [ ] Дождаться ответа (3-10 рабочих дней)

---

## 7. Что может вернуть Meta

Типичные правки которые приходят:

- **«Demo video doesn't show permission in action»** — пересними чётко показывая шаг авторизации и публикации
- **«Privacy Policy missing X»** — добавим в `privacy.html` что просят
- **«App icon contains restricted elements»** — переделай иконку без логотипа Meta/Threads
- **«User data deletion URL not working»** — добавь отдельную секцию в Privacy с инструкциями
- **«Use case description too vague»** — дополним описание конкретикой

Меньше шанс на отказ — больше конкретики в описаниях, чище демо-видео.

---

## 8. Полезные ссылки

- [Threads API docs](https://developers.facebook.com/docs/threads)
- [App Review process](https://developers.facebook.com/docs/app-review)
- [Permissions Reference](https://developers.facebook.com/docs/permissions)
