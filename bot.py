import os
import json
import logging
import threading
from datetime import datetime

import psycopg2
from psycopg2.extras import RealDictCursor
from flask import Flask, request, jsonify
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, WebAppInfo
from telegram.ext import Application, CommandHandler, ContextTypes, MessageHandler, filters

# ── CONFIG ──
BOT_TOKEN   = os.environ.get("BOT_TOKEN", "")
DATABASE_URL = os.environ.get("DATABASE_URL", "")
ADMIN_ID    = int(os.environ.get("ADMIN_ID", "0"))
GAME_URL    = os.environ.get("GAME_URL", "")   # GitHub Pages URL
SERVER_URL  = os.environ.get("RAILWAY_STATIC_URL", "")
STARTING_BALANCE = 1000

logging.basicConfig(level=logging.INFO)
log = logging.getLogger(__name__)

# ── DATABASE ──
def get_db():
    return psycopg2.connect(DATABASE_URL, cursor_factory=RealDictCursor)

def init_db():
    with get_db() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                CREATE TABLE IF NOT EXISTS players (
                    tg_id       BIGINT PRIMARY KEY,
                    username    TEXT,
                    first_name  TEXT,
                    balance     INTEGER DEFAULT 1000,
                    total_won   INTEGER DEFAULT 0,
                    total_lost  INTEGER DEFAULT 0,
                    games_played INTEGER DEFAULT 0,
                    created_at  TIMESTAMP DEFAULT NOW()
                )
            """)
            cur.execute("""
                CREATE TABLE IF NOT EXISTS transactions (
                    id          SERIAL PRIMARY KEY,
                    tg_id       BIGINT,
                    amount      INTEGER,
                    reason      TEXT,
                    created_at  TIMESTAMP DEFAULT NOW()
                )
            """)
        conn.commit()
    log.info("DB initialized")

def get_player(tg_id: int):
    with get_db() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT * FROM players WHERE tg_id = %s", (tg_id,))
            return cur.fetchone()

def create_player(tg_id: int, username: str, first_name: str):
    with get_db() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                INSERT INTO players (tg_id, username, first_name, balance)
                VALUES (%s, %s, %s, %s)
                ON CONFLICT (tg_id) DO NOTHING
                RETURNING *
            """, (tg_id, username, first_name, STARTING_BALANCE))
            conn.commit()

def update_balance(tg_id: int, delta: int, reason: str = ""):
    with get_db() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                UPDATE players
                SET balance = balance + %s,
                    total_won  = total_won  + CASE WHEN %s > 0 THEN %s ELSE 0 END,
                    total_lost = total_lost + CASE WHEN %s < 0 THEN ABS(%s) ELSE 0 END,
                    games_played = games_played + CASE WHEN %s != 0 THEN 1 ELSE 0 END
                WHERE tg_id = %s
                RETURNING balance
            """, (delta, delta, delta, delta, delta, delta, tg_id))
            row = cur.fetchone()
            if row:
                cur.execute(
                    "INSERT INTO transactions (tg_id, amount, reason) VALUES (%s, %s, %s)",
                    (tg_id, delta, reason)
                )
            conn.commit()
            return row["balance"] if row else None

def get_top(limit=10):
    with get_db() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT first_name, username, balance, games_played
                FROM players
                ORDER BY balance DESC
                LIMIT %s
            """, (limit,))
            return cur.fetchall()

# ── FLASK API ──
app = Flask(__name__)

@app.route("/api/balance", methods=["GET"])
def api_balance():
    tg_id = request.args.get("tg_id", type=int)
    if not tg_id:
        return jsonify({"error": "no tg_id"}), 400
    player = get_player(tg_id)
    if not player:
        return jsonify({"error": "player not found"}), 404
    return jsonify({"balance": player["balance"], "name": player["first_name"]})

@app.route("/api/update", methods=["POST"])
def api_update():
    data = request.get_json()
    tg_id  = data.get("tg_id")
    delta  = data.get("delta", 0)
    reason = data.get("reason", "game")
    if not tg_id:
        return jsonify({"error": "no tg_id"}), 400
    new_bal = update_balance(tg_id, delta, reason)
    if new_bal is None:
        return jsonify({"error": "player not found"}), 404
    return jsonify({"balance": new_bal})

@app.route("/api/register", methods=["POST"])
def api_register():
    data = request.get_json()
    tg_id      = data.get("tg_id")
    username   = data.get("username", "")
    first_name = data.get("first_name", "Игрок")
    if not tg_id:
        return jsonify({"error": "no tg_id"}), 400
    create_player(tg_id, username, first_name)
    player = get_player(tg_id)
    return jsonify({"balance": player["balance"], "name": player["first_name"]})

@app.route("/api/top", methods=["GET"])
def api_top():
    rows = get_top()
    return jsonify([dict(r) for r in rows])

@app.route("/health")
def health():
    return "ok"

# ── BOT HANDLERS ──
async def cmd_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    create_player(user.id, user.username or "", user.first_name or "Игрок")
    player = get_player(user.id)

    text = (
        f"🎰 *Добро пожаловать в Casino Night, {user.first_name}!*\n\n"
        f"💰 Ваш стартовый баланс: *${player['balance']}*\n\n"
        "Доступные команды:\n"
        "🃏 /play — Открыть казино\n"
        "💵 /balance — Мой баланс\n"
        "🎁 /daily — Ежедневный бонус\n"
        "👑 /top — Таблица лидеров\n"
        "❓ /help — Помощь"
    )

    keyboard = [[InlineKeyboardButton("🎰 Играть", web_app=WebAppInfo(url=GAME_URL))]]
    await update.message.reply_text(text, parse_mode="Markdown",
                                    reply_markup=InlineKeyboardMarkup(keyboard))

async def cmd_play(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    create_player(user.id, user.username or "", user.first_name or "Игрок")
    keyboard = [[InlineKeyboardButton("🎰 Открыть Casino Night", web_app=WebAppInfo(url=GAME_URL))]]
    await update.message.reply_text(
        "🃏 Нажми кнопку чтобы открыть казино:",
        reply_markup=InlineKeyboardMarkup(keyboard)
    )

async def cmd_balance(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    create_player(user.id, user.username or "", user.first_name or "Игрок")
    player = get_player(user.id)
    text = (
        f"💰 *Баланс: ${player['balance']}*\n\n"
        f"🎮 Игр сыграно: {player['games_played']}\n"
        f"📈 Выиграно всего: ${player['total_won']}\n"
        f"📉 Проиграно всего: ${player['total_lost']}"
    )
    await update.message.reply_text(text, parse_mode="Markdown")

async def cmd_daily(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    create_player(user.id, user.username or "", user.first_name or "Игрок")
    # Check last daily bonus
    with get_db() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT created_at FROM transactions
                WHERE tg_id = %s AND reason = 'daily'
                ORDER BY created_at DESC LIMIT 1
            """, (user.id,))
            last = cur.fetchone()

    if last:
        diff = datetime.now() - last["created_at"].replace(tzinfo=None)
        if diff.total_seconds() < 86400:
            hours_left = int((86400 - diff.total_seconds()) / 3600)
            await update.message.reply_text(
                f"⏳ Ежедневный бонус уже получен!\n"
                f"Следующий через *{hours_left} ч.*",
                parse_mode="Markdown"
            )
            return

    bonus = 500
    new_bal = update_balance(user.id, bonus, "daily")
    await update.message.reply_text(
        f"🎁 *Ежедневный бонус получен!*\n\n"
        f"➕ +${bonus}\n"
        f"💰 Баланс: *${new_bal}*",
        parse_mode="Markdown"
    )

async def cmd_top(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    rows = get_top(10)
    medals = ["🥇","🥈","🥉","4️⃣","5️⃣","6️⃣","7️⃣","8️⃣","9️⃣","🔟"]
    lines = ["👑 *Таблица лидеров Casino Night*\n"]
    for i, row in enumerate(rows):
        name = row["first_name"] or row["username"] or "Игрок"
        lines.append(f"{medals[i]} {name} — *${row['balance']}*")
    await update.message.reply_text("\n".join(lines), parse_mode="Markdown")

async def cmd_topup(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """Admin only: topup <tg_id> <amount>"""
    if update.effective_user.id != ADMIN_ID:
        await update.message.reply_text("❌ Нет доступа")
        return
    args = ctx.args
    if len(args) < 2:
        await update.message.reply_text("Использование: /topup <tg_id> <сумма>")
        return
    tg_id, amount = int(args[0]), int(args[1])
    new_bal = update_balance(tg_id, amount, "admin_topup")
    await update.message.reply_text(f"✅ Пополнено. Новый баланс: ${new_bal}")

async def cmd_help(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "🎰 *Casino Night — Помощь*\n\n"
        "/play — Открыть казино\n"
        "/balance — Мой баланс и статистика\n"
        "/daily — Ежедневный бонус $500\n"
        "/top — Топ-10 игроков\n\n"
        "💡 Баланс автоматически синхронизируется с игрой!",
        parse_mode="Markdown"
    )

# ── MAIN ──
def run_flask():
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port)

def main():
    init_db()

    # Run Flask in background thread
    t = threading.Thread(target=run_flask, daemon=True)
    t.start()

    # Run bot
    application = Application.builder().token(BOT_TOKEN).build()
    application.add_handler(CommandHandler("start",   cmd_start))
    application.add_handler(CommandHandler("play",    cmd_play))
    application.add_handler(CommandHandler("balance", cmd_balance))
    application.add_handler(CommandHandler("daily",   cmd_daily))
    application.add_handler(CommandHandler("top",     cmd_top))
    application.add_handler(CommandHandler("topup",   cmd_topup))
    application.add_handler(CommandHandler("help",    cmd_help))

    log.info("Bot started!")
    application.run_polling(allowed_updates=Update.ALL_TYPES)

if __name__ == "__main__":
    main()
