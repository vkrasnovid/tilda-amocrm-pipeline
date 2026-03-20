# Tilda → AmoCRM Pipeline

Сервис интеграции: автоматически принимает заявки с форм Tilda, создаёт сделки в AmoCRM и запускает email-цепочку из трёх писем. При ответе клиента цепочка останавливается и менеджер получает уведомление в Telegram.

---

## Содержание

1. [Описание проекта](#1-описание-проекта)
2. [Архитектура](#2-архитектура)
3. [Быстрый старт](#3-быстрый-старт)
4. [Конфигурация](#4-конфигурация)
5. [API endpoints](#5-api-endpoints)
6. [Подключение Tilda](#6-подключение-tilda)
7. [Подключение AmoCRM](#7-подключение-amocrm)
8. [Email шаблоны](#8-email-шаблоны)
9. [Мониторинг и логи](#9-мониторинг-и-логи)
10. [Troubleshooting](#10-troubleshooting)

---

## 1. Описание проекта

Сервис решает задачу автоматической обработки лидов с сайта:

- **Приём заявок** — Tilda отправляет данные формы (имя, email, телефон) на webhook-endpoint сервиса.
- **AmoCRM** — автоматически создаётся контакт и сделка на нужном этапе воронки. Дубли по email объединяются с существующим контактом.
- **Email-цепочка** — клиент получает три письма: приветствие сразу, напоминание через 2 дня, спецпредложение через 5 дней.
- **Отслеживание ответов** — IMAP-polling каждые 5 минут ищет ответные письма. При ответе цепочка останавливается, менеджер получает уведомление в Telegram.

### Стек технологий

| Слой | Технология |
|---|---|
| Web-фреймворк | FastAPI + Uvicorn |
| Очередь задач | Celery 5 + Redis 7 |
| Планировщик | Celery Beat |
| База данных | SQLite + aiosqlite (SQLAlchemy 2, Alembic) |
| Email (отправка) | aiosmtplib |
| Email (получение) | aioimaplib |
| HTTP-клиент | httpx (async) |
| Telegram | aiogram 3.x |
| Контейнеризация | Docker + Docker Compose |
| Конфигурация | pydantic-settings |

---

## 2. Архитектура

```
┌──────────┐   POST /webhook/tilda   ┌─────────────────────────────────┐
│  Tilda   │ ──────────────────────► │  app  (FastAPI, port 8000)      │
│  Forms   │                         │                                 │
└──────────┘                         │  validate → upsert lead → DB   │
                                     │  enqueue Celery tasks           │
┌──────────┐                         └────────────────┬────────────────┘
│  AmoCRM  │ ◄── create_amocrm_deal ─────────────────┤
│  REST API│                                          │ Redis (broker)
└──────────┘                         ┌────────────────▼────────────────┐
                                     │  worker  (Celery)               │
┌──────────┐                         │  - create_amocrm_deal           │
│   SMTP   │ ◄── send_email ─────────│  - schedule_email_chain         │
│  Server  │                         │  - send_email (step 1/2/3)      │
└──────────┘                         │  - imap_poll_inbox              │
                                     │  - notify_telegram              │
┌──────────┐                         └────────────────┬────────────────┘
│  IMAP    │ ◄── imap_poll_inbox ────────────────────┤
│  Server  │   (every 5 min via beat)                 │
└──────────┘                         ┌────────────────▼────────────────┐
                                     │  beat  (Celery Beat)            │
┌──────────┐                         │  schedules IMAP polling         │
│ Telegram │ ◄── notify_telegram ────└─────────────────────────────────┘
│  Bot API │
└──────────┘                         ┌─────────────────────────────────┐
                                     │  SQLite  /data/db.sqlite3       │
                                     │  (Docker volume: sqlite-data)   │
                                     └─────────────────────────────────┘
```

### Сервисы Docker Compose

| Сервис | Образ | Порт | Роль |
|---|---|---|---|
| `redis` | redis:7-alpine | внутренний | Брокер Celery + result backend |
| `app` | ./Dockerfile | 8000 | FastAPI HTTP-сервер |
| `worker` | ./Dockerfile | — | Celery worker (очереди: default, email, amocrm, telegram) |
| `beat` | ./Dockerfile | — | Celery Beat — планировщик IMAP-поллинга |

**Порядок запуска:** `redis` → `app` → `worker`, `beat`

---

## 3. Быстрый старт

### Требования

- Docker >= 24
- Docker Compose >= 2.20

### Шаги

```bash
# 1. Клонировать репозиторий
git clone <repository-url>
cd tilda-amocrm-pipeline

# 2. Создать файл конфигурации
cp .env.example .env

# 3. Заполнить обязательные переменные в .env (см. раздел 4)
nano .env

# 4. Запустить
docker compose up -d --build

# 5. Проверить работу
curl http://localhost:8000/health
```

Ожидаемый ответ:

```json
{
  "status": "ok",
  "db": "ok",
  "redis": "ok",
  "version": "1.0.0"
}
```

### Просмотр логов

```bash
# Все сервисы
docker compose logs -f

# Только приложение
docker compose logs -f app

# Только воркер
docker compose logs -f worker
```

### Остановка

```bash
docker compose down           # остановить, сохранить данные
docker compose down -v        # остановить и удалить volumes (ВНИМАНИЕ: удалит БД)
```

---

## 4. Конфигурация

Все настройки передаются через файл `.env`. Скопируйте `.env.example` и заполните значения.

### Приложение

| Переменная | По умолчанию | Описание |
|---|---|---|
| `APP_HOST` | `0.0.0.0` | Адрес, на котором слушает Uvicorn |
| `APP_PORT` | `8000` | Порт HTTP-сервера |
| `APP_SECRET_KEY` | — | Случайная строка 32 байта (hex) для внутренней подписи |
| `LOG_LEVEL` | `INFO` | Уровень логирования: DEBUG, INFO, WARNING, ERROR |

### Безопасность webhook

| Переменная | По умолчанию | Описание |
|---|---|---|
| `TILDA_WEBHOOK_SECRET` | **обязательно** | Shared secret для верификации HMAC-SHA256 подписи от Tilda |

### Admin API

| Переменная | По умолчанию | Описание |
|---|---|---|
| `ADMIN_API_TOKEN` | **обязательно** | Bearer-токен для доступа к `/admin/*` endpoints |

### База данных

| Переменная | По умолчанию | Описание |
|---|---|---|
| `DATABASE_URL` | `sqlite+aiosqlite:////data/db.sqlite3` | URL подключения к SQLite |

### Redis / Celery

| Переменная | По умолчанию | Описание |
|---|---|---|
| `REDIS_URL` | `redis://redis:6379/0` | URL Redis для health check |
| `CELERY_BROKER_URL` | `redis://redis:6379/0` | Брокер сообщений Celery |
| `CELERY_RESULT_BACKEND` | `redis://redis:6379/1` | Хранилище результатов задач |

### AmoCRM

| Переменная | По умолчанию | Описание |
|---|---|---|
| `AMOCRM_BASE_URL` | — | URL аккаунта: `https://your-subdomain.amocrm.ru` |
| `AMOCRM_CLIENT_ID` | — | OAuth2 Client ID из настроек интеграции |
| `AMOCRM_CLIENT_SECRET` | — | OAuth2 Client Secret |
| `AMOCRM_REDIRECT_URI` | — | Redirect URI, зарегистрированный в интеграции |
| `AMOCRM_ACCESS_TOKEN` | — | Первичный access token (обновляется автоматически) |
| `AMOCRM_REFRESH_TOKEN` | — | Refresh token для обновления access token |
| `AMOCRM_PIPELINE_ID` | `0` | ID воронки, в которой создаются сделки |
| `AMOCRM_STAGE_ID` | `0` | ID этапа воронки («Первичный контакт») |

### SMTP (исходящая почта)

| Переменная | По умолчанию | Описание |
|---|---|---|
| `SMTP_HOST` | `smtp.example.com` | Хост SMTP-сервера |
| `SMTP_PORT` | `587` | Порт SMTP |
| `SMTP_USERNAME` | — | Логин для аутентификации |
| `SMTP_PASSWORD` | — | Пароль |
| `SMTP_FROM_NAME` | `Company Name` | Имя отправителя в поле «От» |
| `SMTP_FROM_EMAIL` | `noreply@example.com` | Email отправителя |
| `SMTP_MODE` | `starttls` | Режим TLS: `starttls` (587), `ssl` (465), `plain` (25) |

### IMAP (входящая почта)

| Переменная | По умолчанию | Описание |
|---|---|---|
| `IMAP_HOST` | `imap.example.com` | Хост IMAP-сервера |
| `IMAP_PORT` | `993` | Порт IMAP |
| `IMAP_USERNAME` | — | Логин для аутентификации |
| `IMAP_PASSWORD` | — | Пароль |
| `IMAP_MAILBOX` | `INBOX` | Папка для мониторинга |
| `IMAP_POLL_INTERVAL_SECONDS` | `300` | Интервал опроса в секундах (5 минут) |

### Telegram

| Переменная | По умолчанию | Описание |
|---|---|---|
| `TELEGRAM_BOT_TOKEN` | — | Токен бота от @BotFather |
| `TELEGRAM_MANAGER_CHAT_ID` | `0` | Числовой ID чата/пользователя менеджера |

---

## 5. API endpoints

### POST /webhook/tilda

Принимает данные формы Tilda. Требует заголовок `X-Tilda-Signature` (HMAC-SHA256).

**Лимит:** 30 запросов / минуту с одного IP.

**Поддерживаемые Content-Type:** `application/json`, `application/x-www-form-urlencoded`

**Тело запроса:**

```json
{
  "name": "Иван Иванов",
  "email": "ivan@example.com",
  "phone": "+79001234567",
  "formid": "tilda-form-123"
}
```

| Поле | Обязательно | Описание |
|---|---|---|
| `name` | да | Имя клиента |
| `email` | да | Email клиента (ключ дедупликации) |
| `phone` | нет | Телефон клиента |
| `formid` | нет | ID формы Tilda |

**Ответы:**

| Код | Описание |
|---|---|
| `200` | `{"status": "ok", "lead_id": 42}` |
| `401` | Неверная или отсутствующая подпись HMAC |
| `422` | Ошибка валидации (отсутствует email или name) |
| `429` | Превышен лимит запросов |
| `500` | Ошибка базы данных |

---

### GET /health

Проверка состояния сервиса. Авторизация не требуется.

**Ответ 200:**

```json
{
  "status": "ok",
  "db": "ok",
  "redis": "ok",
  "version": "1.0.0"
}
```

---

### GET /admin/leads

Список лидов с фильтрацией и пагинацией.

**Авторизация:** `Authorization: Bearer <ADMIN_API_TOKEN>`

**Лимит:** 60 запросов / минуту.

**Query-параметры:**

| Параметр | По умолчанию | Описание |
|---|---|---|
| `chain_status` | — | Фильтр по статусу цепочки: `active`, `stopped`, `completed` |
| `amocrm_status` | — | Фильтр по статусу AmoCRM: `pending`, `created`, `failed` |
| `limit` | `50` | Количество записей (макс. 200) |
| `offset` | `0` | Смещение для пагинации |

**Пример:**

```bash
curl -H "Authorization: Bearer <token>" \
  "http://localhost:8000/admin/leads?chain_status=active&limit=10"
```

---

### GET /admin/leads/{id}

Получить один лид со всеми email-событиями.

**Авторизация:** `Authorization: Bearer <ADMIN_API_TOKEN>`

**Ответ 200:** объект лида с полем `email_events` — массив событий отправки писем.

**Ответ 404:** лид не найден.

---

## 6. Подключение Tilda

### Шаг 1: Получить URL webhook

Ваш webhook URL:

```
https://<your-domain>/webhook/tilda
```

Если запускаете локально для тестирования, используйте ngrok:

```bash
ngrok http 8000
# Скопируйте HTTPS URL вида: https://xxxx.ngrok.io/webhook/tilda
```

### Шаг 2: Настроить форму в Tilda

1. Откройте **Tilda** → выберите проект → **Настройки сайта** → **Формы**.
2. Или: откройте блок с формой → **Настройки блока** → **После отправки** → **Webhook**.
3. Укажите URL: `https://<your-domain>/webhook/tilda`.
4. Метод: **POST**.

### Шаг 3: Настроить подпись (рекомендуется)

1. В настройках webhook Tilda найдите поле **Secret key** (или аналогичное).
2. Установите произвольную строку — это и есть `TILDA_WEBHOOK_SECRET`.
3. Скопируйте это значение в `.env`:
   ```
   TILDA_WEBHOOK_SECRET=ваш-секретный-ключ
   ```

### Шаг 4: Маппинг полей формы

Сервис ожидает следующие имена полей:

| Поле Tilda | Имя в сервисе | Обязательно |
|---|---|---|
| Email | `email` | да |
| Имя | `name` | да |
| Телефон | `phone` | нет |
| ID формы (авто) | `formid` | нет |

Убедитесь, что поля формы в Tilda имеют соответствующие системные имена.

### Шаг 5: Проверка

Отправьте тестовую форму. В логах приложения появится:

```
[webhook] Received lead: email=test@example.com name=Test User form=...
[webhook] Lead upserted: lead_id=1 (new=True)
```

---

## 7. Подключение AmoCRM

### Шаг 1: Создать интеграцию

1. Войдите в AmoCRM → **Настройки** → **Интеграции** → **Создать интеграцию**.
2. Тип: **OAuth 2.0 (private)**.
3. Укажите Redirect URI — например, `https://your-domain.com/oauth2/callback` (может быть любым валидным URL).
4. Сохраните. Скопируйте **Client ID** и **Client Secret**.

### Шаг 2: Получить токены

Для получения первоначальных токенов выполните OAuth2 Authorization Code Flow:

```
GET https://your-subdomain.amocrm.ru/oauth2/access_token
```

Либо воспользуйтесь официальным SDK или Postman-коллекцией AmoCRM.

После успешной авторизации вы получите `access_token` и `refresh_token`.

### Шаг 3: Узнать Pipeline ID и Stage ID

1. Откройте AmoCRM → **Сделки** → нужная воронка.
2. URL вида: `https://your-subdomain.amocrm.ru/leads/pipeline/XXXXXX` — число в URL это `AMOCRM_PIPELINE_ID`.
3. Для получения `AMOCRM_STAGE_ID` используйте API:

```bash
curl -H "Authorization: Bearer <access_token>" \
  "https://your-subdomain.amocrm.ru/api/v4/leads/pipelines/<pipeline_id>/statuses"
```

Найдите статус «Первичный контакт» и скопируйте его `id`.

### Шаг 4: Заполнить .env

```dotenv
AMOCRM_BASE_URL=https://your-subdomain.amocrm.ru
AMOCRM_CLIENT_ID=xxxxxxxx-xxxx-xxxx-xxxx-xxxxxxxxxxxx
AMOCRM_CLIENT_SECRET=xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx
AMOCRM_REDIRECT_URI=https://your-domain.com/oauth2/callback
AMOCRM_ACCESS_TOKEN=<полученный access token>
AMOCRM_REFRESH_TOKEN=<полученный refresh token>
AMOCRM_PIPELINE_ID=123456
AMOCRM_STAGE_ID=654321
```

### Автообновление токенов

Сервис автоматически обновляет `access_token` при получении ответа `401` от AmoCRM API. Обновлённые токены сохраняются в файл `/data/amocrm_tokens.json` на Docker-volume. Файловая блокировка предотвращает состояние гонки при параллельном обновлении несколькими воркерами.

---

## 8. Email шаблоны

Шаблоны хранятся в директории `templates/`:

```
templates/
  email_step_1.html   # Письмо 1: Приветствие (отправляется сразу)
  email_step_2.html   # Письмо 2: Напоминание (через 48 часов)
  email_step_3.html   # Письмо 3: Спецпредложение (через 120 часов)
```

### Доступные переменные

В каждом шаблоне доступны две переменные Jinja2:

| Переменная | Значение |
|---|---|
| `{{ name }}` | Имя клиента из формы Tilda |
| `{{ email }}` | Email клиента |

**Пример использования:**

```html
<p>Здравствуйте, <strong>{{ name }}</strong>!</p>
<p>Мы свяжемся с вами по адресу {{ email }}.</p>
```

### Как кастомизировать

1. Откройте нужный файл шаблона, например `templates/email_step_1.html`.
2. Измените текст, цвета, структуру HTML по своему усмотрению.
3. Сохраните переменные `{{ name }}` и `{{ email }}` — они подставляются при отправке.
4. Перезапустите воркер для применения изменений:
   ```bash
   docker compose restart worker
   ```

### Важные замечания

- Шаблоны рендерятся с **включённым Jinja2 autoescaping** — это защита от XSS.
- Заголовок `List-Unsubscribe` добавляется автоматически при отправке.
- Не используйте в шаблонах пользовательский ввод вне переменных `{{ name }}` и `{{ email }}`.

---

## 9. Мониторинг и логи

### Health check

```bash
curl http://localhost:8000/health
```

Возвращает статус подключения к БД и Redis. Используется Docker Compose для проверки готовности `app` перед запуском `worker` и `beat`.

### Просмотр логов

```bash
# Все сервисы в реальном времени
docker compose logs -f

# Конкретный сервис
docker compose logs -f app
docker compose logs -f worker
docker compose logs -f beat

# Последние 100 строк
docker compose logs --tail=100 worker
```

### Формат логов

```
[имя_модуля] LEVEL сообщение
```

Примеры:
```
[webhook] INFO Received lead: email=ivan@example.com name=Иван form=123
[webhook] INFO Lead upserted: lead_id=42 (new=True)
[amocrm] INFO Contact created: contact_id=9876
[amocrm] INFO Deal created: deal_id=5432
[email_chain] INFO Email step=1 sent to ivan@example.com
[imap] INFO IMAP poll: checked=12 matched=1
[telegram] INFO Telegram notification sent for lead_id=42
```

### Уровень логирования

Изменяется в `.env`:

```dotenv
LOG_LEVEL=DEBUG   # подробные логи (включая запросы к AmoCRM)
LOG_LEVEL=INFO    # стандартный режим
LOG_LEVEL=WARNING # только предупреждения и ошибки
```

После изменения: `docker compose restart app worker beat`

### Мониторинг IMAP-поллинга

Каждый запуск IMAP-задачи записывается в таблицу `imap_poll_log`. Посмотреть через admin API:

```bash
# Последние лиды с активными цепочками
curl -H "Authorization: Bearer <token>" \
  "http://localhost:8000/admin/leads?chain_status=active"

# Лиды, у которых цепочка остановлена (клиент ответил)
curl -H "Authorization: Bearer <token>" \
  "http://localhost:8000/admin/leads?chain_status=stopped"
```

---

## 10. Troubleshooting

### Webhook возвращает 401

**Причина:** неверная HMAC-подпись.

**Решение:**
- Убедитесь, что `TILDA_WEBHOOK_SECRET` в `.env` совпадает с секретом, указанным в настройках Tilda.
- Перезапустите `app`: `docker compose restart app`.
- Если Tilda не поддерживает подпись, временно для тестирования можно отправить запрос напрямую с заголовком `X-Tilda-Signature`.

---

### Сделки не создаются в AmoCRM

**Причина:** проблемы с токенами или неверные параметры.

**Диагностика:**
```bash
docker compose logs worker | grep amocrm
```

**Частые причины:**
- Истёк refresh token → нужно получить новые токены вручную (повторить Шаг 2 раздела 7).
- Неверный `AMOCRM_PIPELINE_ID` или `AMOCRM_STAGE_ID` → проверьте значения через API.
- Неверный `AMOCRM_BASE_URL` → убедитесь, что указан правильный поддомен.

---

### Письма не отправляются

**Диагностика:**
```bash
docker compose logs worker | grep email
```

**Частые причины:**
- Неверный `SMTP_HOST`, `SMTP_PORT`, `SMTP_USERNAME`, `SMTP_PASSWORD`.
- Неправильный `SMTP_MODE`: для порта 587 → `starttls`, для 465 → `ssl`, для 25 → `plain`.
- SMTP-сервер требует App Password (например, Gmail) вместо основного пароля.
- Брандмауэр блокирует исходящий SMTP — проверьте настройки сети.

---

### IMAP-поллинг не останавливает цепочку

**Диагностика:**
```bash
docker compose logs worker | grep imap
docker compose logs beat
```

**Частые причины:**
- Сервис `beat` не запущен — проверьте: `docker compose ps`.
- Email отправителя в ответном письме не совпадает точно с email в таблице `leads` (регистр, пробелы).
- Неверные IMAP-настройки → проверьте `IMAP_HOST`, `IMAP_PORT`, `IMAP_USERNAME`, `IMAP_PASSWORD`.
- IMAP-сервер требует двухфакторную аутентификацию → создайте App Password.

---

### Telegram-уведомления не приходят

**Диагностика:**
```bash
docker compose logs worker | grep telegram
```

**Частые причины:**
- Бот не добавлен в чат / не инициирован диалог (`/start` не отправлен боту).
- Неверный `TELEGRAM_MANAGER_CHAT_ID` — получите ID через бота `@userinfobot`.
- Неверный `TELEGRAM_BOT_TOKEN` — проверьте у `@BotFather`.

---

### Сервис не запускается: "TILDA_WEBHOOK_SECRET must be set"

**Причина:** в `.env` оставлены значения по умолчанию `change-me`.

**Решение:** установите реальные значения для `TILDA_WEBHOOK_SECRET` и `ADMIN_API_TOKEN`.

---

### Пересоздание БД (сброс данных)

> **Внимание:** это удалит все лиды и историю.

```bash
docker compose down -v       # удалить volumes
docker compose up -d --build # пересоздать
```

---

### Проверка состояния очереди Celery

```bash
# Активные задачи
docker compose exec worker celery -A app.celery_app inspect active

# Зарезервированные задачи
docker compose exec worker celery -A app.celery_app inspect reserved

# Статистика воркера
docker compose exec worker celery -A app.celery_app inspect stats
```
