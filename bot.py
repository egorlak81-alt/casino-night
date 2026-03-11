import os
import logging
import threading
from datetime import datetime, timedelta
from urllib.parse import urlparse

import pg8000.native
from flask import Flask, request, jsonify
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, WebAppInfo
from telegram.ext import Application, CommandHandler, ContextTypes

logging.basicConfig(level=logging.INFO)
log = logging.getLogger(__name__)

# Глобальные переменные — заполняются в main()
BOT_TOKEN    = ""
DATABASE_URL = ""
ADMIN_ID     = 0
GAME_URL     = ""

def get_conn():
    u = urlparse(DATABASE_URL)
    return pg8000.native.Connection(
        host=u.hostname,
        port=u.port or 5432,
        database=u.path.lstrip("/"),
        user=u.username,
        password=u.password,
        ssl_context=True
    )

def qone(sql, **kw):
    c = get_conn()
    try:
        rows = c.run(sql, **kw)
        return rows[0] if rows else None
    finally:
        c.close()

def qall(sql, **kw):
    c = get_conn()
    try:
        return c.run(sql, **kw)
    finally:
        c.close()

def qexec(sql, **kw):
    c = get_conn()
    try:
        c.run(sql, **kw)
    finally:
        c.close()

def init_db():
    c = get_conn()
    try:
        c.run("""CREATE TABLE IF NOT EXISTS players (
            tg_id        BIGINT PRIMARY KEY,
            username     TEXT DEFAULT '',
            first_name   TEXT DEFAULT 'Игрок',
            balance      INTEGER DEFAULT 1000,
            total_won    INTEGER DEFAULT 0,
            total_lost   INTEGER DEFAULT 0,
            games_played INTEGER DEFAULT 0,
            last_daily   TIMESTAMP DEFAULT NULL,
            created_at   TIMESTAMP DEFAULT NOW()
        )""")
    finally:
        c.close()
    log.info("DB ready")

def ensure_player(tg_id, username, first_name):
    qexec("INSERT INTO players (tg_id,username,first_name) VALUES (:i,:u,:n) ON CONFLICT (tg_id) DO NOTHING",
          i=tg_id, u=username, n=first_name)

def get_balance(tg_id):
    r = qone("SELECT balance FROM players WHERE tg_id=:i", i=tg_id)
    return r[0] if r else 0

def add_balance(tg_id, delta):
    qexec("""UPDATE players SET
        balance=balance+:d,
        total_won=total_won+(CASE WHEN :d>0 THEN :d ELSE 0 END),
        total_lost=total_lost+(CASE WHEN :d<0 THEN :d*-1 ELSE 0 END),
        games_played=games_played+1
        WHERE tg_id=:i""", d=delta, i=tg_id)
    return get_balance(tg_id)

def get_last_daily(tg_id):
    r = qone("SELECT last_daily FROM players WHERE tg_id=:i", i=tg_id)
    return r[0] if r else None

def set_last_daily(tg_id):
    qexec("UPDATE players SET last_daily=NOW() WHERE tg_id=:i", i=tg_id)

def get_top():
    return qall("SELECT first_name,balance,games_played FROM players ORDER BY balance DESC LIMIT 10")

# ── FLASK ──
app = Flask(__name__)

@app.route("/health")
def health(): return "ok"

@app.route("/api/register", methods=["POST"])
def api_register():
    d = request.get_json() or {}
    tg_id = d.get("tg_id")
    if not tg_id: return jsonify({"error":"no tg_id"}), 400
    ensure_player(tg_id, d.get("username",""), d.get("first_name","Игрок"))
    return jsonify({"balance": get_balance(tg_id), "name": d.get("first_name","Игрок")})

@app.route("/api/balance")
def api_balance():
    tg_id = request.args.get("tg_id", type=int)
    if not tg_id: return jsonify({"error":"no tg_id"}), 400
    return jsonify({"balance": get_balance(tg_id)})

@app.route("/api/update", methods=["POST"])
def api_update():
    d = request.get_json() or {}
    tg_id = d.get("tg_id"); delta = d.get("delta", 0)
    if not tg_id: return jsonify({"error":"no tg_id"}), 400
    return jsonify({"balance": add_balance(tg_id, delta)})

@app.route("/api/top")
def api_top():
    rows = get_top() or []
    return jsonify([{"name":r[0],"balance":r[1],"games":r[2]} for r in rows])

# ── BOT HANDLERS ──
async def cmd_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    u = update.effective_user
    ensure_player(u.id, u.username or "", u.first_name or "Игрок")
    bal = get_balance(u.id)
    kb = [[InlineKeyboardButton("🎰 Играть", web_app=WebAppInfo(url=GAME_URL))]] if GAME_URL else []
    await update.message.reply_text(
        f"🎰 *Casino Night — {u.first_name}!*\n\n💰 Баланс: *${bal}*\n\n"
        "🃏 /play — открыть казино\n💵 /balance — баланс\n🎁 /daily — бонус $500\n👑 /top — топ",
        parse_mode="Markdown",
        reply_markup=InlineKeyboardMarkup(kb) if kb else None
    )

async def cmd_play(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    u = update.effective_user
    ensure_player(u.id, u.username or "", u.first_name or "Игрок")
    if not GAME_URL:
        await update.message.reply_text("⚙️ GAME_URL не настроен"); return
    kb = [[InlineKeyboardButton("🎰 Casino Night", web_app=WebAppInfo(url=GAME_URL))]]
    await update.message.reply_text("Нажми:", reply_markup=InlineKeyboardMarkup(kb))

async def cmd_balance(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    u = update.effective_user
    ensure_player(u.id, u.username or "", u.first_name or "Игрок")
    r = qone("SELECT balance,total_won,total_lost,games_played FROM players WHERE tg_id=:i", i=u.id)
    if not r: await update.message.reply_text("Напиши /start"); return
    await update.message.reply_text(
        f"💰 *Баланс: ${r[0]}*\n🎮 Игр: {r[3]}\n📈 Выиграно: ${r[1]}\n📉 Проиграно: ${r[2]}",
        parse_mode="Markdown")

async def cmd_daily(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    u = update.effective_user
    ensure_player(u.id, u.username or "", u.first_name or "Игрок")
    last = get_last_daily(u.id)
    if last:
        diff = datetime.utcnow() - last.replace(tzinfo=None)
        if diff < timedelta(hours=24):
            h = int((timedelta(hours=24) - diff).total_seconds() / 3600)
            await update.message.reply_text(f"⏳ Следующий бонус через *{h} ч.*", parse_mode="Markdown"); return
    add_balance(u.id, 500); set_last_daily(u.id)
    await update.message.reply_text(f"🎁 *+$500!*\n💰 Баланс: *${get_balance(u.id)}*", parse_mode="Markdown")

async def cmd_top(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    rows = get_top() or []
    medals = ["🥇","🥈","🥉","4️⃣","5️⃣","6️⃣","7️⃣","8️⃣","9️⃣","🔟"]
    lines = ["👑 *Топ Casino Night*\n"] + [f"{medals[i]} {r[0]} — *${r[1]}*" for i,r in enumerate(rows)]
    await update.message.reply_text("\n".join(lines), parse_mode="Markdown")

async def cmd_topup(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_ID: return
    args = ctx.args
    if len(args) < 2: await update.message.reply_text("/topup <tg_id> <сумма>"); return
    bal = add_balance(int(args[0]), int(args[1]))
    await update.message.reply_text(f"✅ Баланс: ${bal}")

def run_flask():
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 5000)))

def main():
    global BOT_TOKEN, DATABASE_URL, ADMIN_ID, GAME_URL

    # Читаем переменные строго здесь — с .strip() на случай лишних пробелов
    BOT_TOKEN    = os.environ.get("BOT_TOKEN",    "").strip()
    DATABASE_URL = os.environ.get("DATABASE_URL", "").strip()
    ADMIN_ID     = int(os.environ.get("ADMIN_ID", "0").strip())
    GAME_URL     = os.environ.get("GAME_URL",     "").strip()

    log.info(f"BOT_TOKEN set: {bool(BOT_TOKEN)}")
    log.info(f"DATABASE_URL set: {bool(DATABASE_URL)}")
    log.info(f"GAME_URL: {GAME_URL}")

    if not BOT_TOKEN:
        log.error("BOT_TOKEN not set! Проверь Variables в Railway.")
        return
    if not DATABASE_URL:
        log.error("DATABASE_URL not set!")
        return

    init_db()

    threading.Thread(target=run_flask, daemon=True).start()
    log.info("Flask started")

    application = Application.builder().token(BOT_TOKEN).build()
    async def error_handler(update, context):
        err = str(context.error)
        if 'Conflict' in err:
            log.warning("Conflict: another bot instance detected, retrying...")
        else:
            log.error(f"Error: {err}")
    application.add_error_handler(error_handler)

    for cmd, fn in [("start",cmd_start),("play",cmd_play),("balance",cmd_balance),
                    ("daily",cmd_daily),("top",cmd_top),("topup",cmd_topup)]:
        application.add_handler(CommandHandler(cmd, fn))

    log.info("Bot polling...")
    application.run_polling(
        allowed_updates=Update.ALL_TYPES,
        drop_pending_updates=True,
        close_loop=False
    )

if __name__ == "__main__":
    main()
