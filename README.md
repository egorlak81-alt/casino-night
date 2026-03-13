# 🎰 Casino Night

<div align="center">

![Casino Night Banner](https://img.shields.io/badge/Casino-Night-gold?style=for-the-badge&logo=telegram&logoColor=white&labelColor=1a0e00&color=c8a84b)

**Telegram Mini App — казино прямо в мессенджере**

[![Live Demo](https://img.shields.io/badge/▶%20Играть-@Casino__n1ght__bot-blue?style=for-the-badge&logo=telegram)](https://t.me/Casino_n1ght_bot)
[![GitHub Pages](https://img.shields.io/badge/GitHub-Pages-222?style=for-the-badge&logo=github)](https://egorlak81-alt.github.io/casino-night/)
[![Railway](https://img.shields.io/badge/Backend-Railway-blueviolet?style=for-the-badge&logo=railway)](https://casino-night-production.up.railway.app)

</div>

---

## 🃏 Игры

| Игра | Режим | Описание |
|------|-------|----------|
| 🂡 **Блэкджек** | 1 игрок | Набери 21 — классика казино |
| 🃏 **Покер** | 2–6 игроков | Texas Hold'em с реальными ставками |
| 🪙 **Орёл & Решка** | 1 игрок | Бросай монету, угадывай |
| 🂮 **Дурак** | 2–6 игроков | Подкидной дурак на деньги |

Мультиплеер работает через **серверные комнаты** с HTTP-поллингом — без WebSocket, без PeerJS.

---

## 🛠 Стек

```
Frontend      → Vanilla JS + HTML/CSS (single file, no framework)
Backend       → Python Flask + psycopg2
Database      → PostgreSQL (Railway)
Bot           → python-telegram-bot 20.7 (webhook mode)
Hosting       → Railway (backend) + GitHub Pages (frontend)
```

---

## 🚀 Архитектура

```
Telegram App
     │
     ▼
GitHub Pages (index.html)
     │  apiFetch()
     ▼
Railway Flask API ──── PostgreSQL
     │
     ▼
Telegram Bot (webhook)
```

**Поток запуска:**
1. Пользователь открывает бота → `/start` → кнопка «Играть»
2. Telegram передаёт `tg_id` через `initData` / URL параметр
3. `GET /api/me?tg_id=X` — сервер автоматически создаёт игрока если нет
4. Меню открывается с реальным ником и балансом

---

## ⚙️ API Endpoints

| Метод | Endpoint | Описание |
|-------|----------|----------|
| `GET` | `/api/me?tg_id=X` | Профиль (автосоздаёт игрока) |
| `POST` | `/api/update` | Изменить баланс |
| `GET` | `/api/balance?tg_id=X` | Быстрый баланс + ник |
| `GET` | `/api/top` | Топ-10 игроков |
| `GET` | `/api/rooms` | Список комнат |
| `POST` | `/api/rooms/join` | Войти в комнату |
| `POST` | `/api/rooms/leave` | Покинуть комнату |
| `GET` | `/api/rooms/poll` | Polling состояния |
| `POST` | `/api/rooms/push` | Хост → клиенты |
| `POST` | `/api/rooms/action` | Клиент → хост |

---

## 🗄 База данных

```sql
CREATE TABLE players (
    tg_id       BIGINT PRIMARY KEY,
    username    TEXT DEFAULT '',
    nickname    TEXT DEFAULT '',
    balance     INTEGER DEFAULT 1000,
    total_won   INTEGER DEFAULT 0,
    total_lost  INTEGER DEFAULT 0,
    games_played INTEGER DEFAULT 0,
    last_daily  TIMESTAMP DEFAULT NULL,
    last_bonus  TIMESTAMP DEFAULT NULL,
    created_at  TIMESTAMP DEFAULT NOW()
);
```

---

## 🤖 Команды бота

| Команда | Описание |
|---------|----------|
| `/start` | Приветствие + кнопка «Играть» |
| `/play` | Открыть казино |
| `/balance` | Текущий баланс |
| `/daily` | Ежедневный бонус +$500 (раз в 24ч) |
| `/top` | Таблица лидеров |
| `/setnick <ник>` | Установить никнейм |

---

## 🏃 Запуск локально

### Требования
- Python 3.11+
- PostgreSQL

### Установка

```bash
git clone https://github.com/egorlak81-alt/casino-night.git
cd casino-night
pip install -r requirements.txt
```

### Переменные окружения

```env
BOT_TOKEN=your_telegram_bot_token
ADMIN_ID=your_telegram_id
DATABASE_URL=postgresql://user:pass@host/db
GAME_URL=https://your-frontend-url
RAILWAY_PUBLIC_DOMAIN=your-domain.up.railway.app
```

### Запуск

```bash
python bot.py
```

---

## 📁 Структура

```
casino-night/
├── index.html        # Всё приложение — один файл
├── bot.py            # Flask API + Telegram бот
├── requirements.txt  # Python зависимости
├── Dockerfile        # Docker образ
├── sw.js             # Service Worker (без кэша)
└── runtime.txt       # python-3.11.0
```

---

## 💡 Особенности реализации

- **Single-file frontend** — весь JS/CSS/HTML в одном `index.html`
- **Без фреймворков** — vanilla JS, никаких React/Vue
- **Авто-регистрация** — `/api/me` создаёт игрока при первом входе
- **CORS без preflight** — GET-запросы без `Content-Type` заголовка
- **Мультиплеер через polling** — каждые ~1.5с опрос сервера вместо WebSocket

---

<div align="center">

Сделано с ❤️ и ♠️ · [@Casino_n1ght_bot](https://t.me/Casino_n1ght_bot)

</div>

