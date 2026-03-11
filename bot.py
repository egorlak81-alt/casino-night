import os, logging, threading, time
from datetime import datetime, timedelta
from urllib.parse import urlparse

import pg8000.native
from flask import Flask, request, jsonify
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, WebAppInfo
from telegram.ext import Application, CommandHandler, ContextTypes

logging.basicConfig(level=logging.INFO)
log = logging.getLogger(__name__)

BOT_TOKEN = ""; DATABASE_URL = ""; ADMIN_ID = 0; GAME_URL = ""

# ── DB ──
def get_conn():
    u = urlparse(DATABASE_URL)
    return pg8000.native.Connection(
        host=u.hostname, port=u.port or 5432,
        database=u.path.lstrip("/"), user=u.username,
        password=u.password, ssl_context=True
    )

def qone(sql, **kw):
    c = get_conn()
    try: rows = c.run(sql, **kw); return rows[0] if rows else None
    finally: c.close()

def qall(sql, **kw):
    c = get_conn()
    try: return c.run(sql, **kw)
    finally: c.close()

def qexec(sql, **kw):
    c = get_conn()
    try: c.run(sql, **kw)
    finally: c.close()

def init_db():
    c = get_conn()
    try:
        c.run("""CREATE TABLE IF NOT EXISTS players (
            tg_id BIGINT PRIMARY KEY, username TEXT DEFAULT '',
            first_name TEXT DEFAULT 'Игрок', balance INTEGER DEFAULT 1000,
            total_won INTEGER DEFAULT 0, total_lost INTEGER DEFAULT 0,
            games_played INTEGER DEFAULT 0, last_daily TIMESTAMP DEFAULT NULL,
            created_at TIMESTAMP DEFAULT NOW()
        )""")
    finally: c.close()
    log.info("DB ready")

def ensure_player(tg_id, username, first_name):
    qexec("INSERT INTO players (tg_id,username,first_name) VALUES (:i,:u,:n) ON CONFLICT (tg_id) DO NOTHING",
          i=tg_id, u=username, n=first_name)

def get_balance(tg_id):
    r = qone("SELECT balance FROM players WHERE tg_id=:i", i=tg_id)
    return r[0] if r else 0

def add_balance(tg_id, delta):
    qexec("""UPDATE players SET balance=balance+:d,
        total_won=total_won+(CASE WHEN :d>0 THEN :d ELSE 0 END),
        total_lost=total_lost+(CASE WHEN :d<0 THEN :d*-1 ELSE 0 END),
        games_played=games_played+1 WHERE tg_id=:i""", d=delta, i=tg_id)
    return get_balance(tg_id)

def get_last_daily(tg_id):
    r = qone("SELECT last_daily FROM players WHERE tg_id=:i", i=tg_id)
    return r[0] if r else None

def set_last_daily(tg_id):
    qexec("UPDATE players SET last_daily=NOW() WHERE tg_id=:i", i=tg_id)

def get_top():
    return qall("SELECT first_name,balance,games_played FROM players ORDER BY balance DESC LIMIT 10")

# ── ROOMS ──
ROOMS_LOCK = threading.Lock()
COUNTDOWN_SECS = 90
MAX_PLAYERS = {'poker': 6, 'durak': 4}

def _make_room(game, name):
    return {
        'game': game, 'name': name,
        'players': [],       # [{id, name, last_ping}]
        'host_id': None,
        'messages': [],      # host→guests: [{seq, data}]
        'actions': [],       # guests→host: [{seq, from_id, data}]
        'msg_seq': 0, 'act_seq': 0,
        'countdown_start': None,
        'started': False,
    }

ROOMS = {}
for i in range(1, 6):
    ROOMS[f'poker_{i}'] = _make_room('poker', f'Покер · Стол {i}')
    ROOMS[f'durak_{i}'] = _make_room('durak', f'Дурак · Стол {i}')

def _get_countdown(r):
    if not r['countdown_start']: return None
    if r['started']: return 0
    remaining = max(0, COUNTDOWN_SECS - int(time.time() - r['countdown_start']))
    return remaining

def _push_msg(r, msg):
    r['msg_seq'] += 1
    r['messages'].append({'seq': r['msg_seq'], 'data': msg})
    if len(r['messages']) > 200: r['messages'] = r['messages'][-200:]

def _cleanup_room(r):
    now = time.time()
    before = len(r['players'])
    r['players'] = [p for p in r['players'] if now - p['last_ping'] < 35]
    if len(r['players']) != before:
        if not r['players']:
            # Reset room fully
            r['host_id'] = None; r['messages'] = []; r['actions'] = []
            r['msg_seq'] = 0; r['act_seq'] = 0
            r['countdown_start'] = None; r['started'] = False
        else:
            if r['host_id'] not in [p['id'] for p in r['players']]:
                r['host_id'] = r['players'][0]['id']
            if len(r['players']) < 2:
                r['countdown_start'] = None

# ── FLASK ──
app = Flask(__name__)

@app.after_request
def cors(resp):
    resp.headers['Access-Control-Allow-Origin'] = '*'
    resp.headers['Access-Control-Allow-Headers'] = 'Content-Type'
    resp.headers['Access-Control-Allow-Methods'] = 'GET,POST,OPTIONS'
    return resp

@app.route("/health")
def health(): return "ok"

@app.route("/api/rooms")
def api_rooms():
    game = request.args.get("game", "")
    with ROOMS_LOCK:
        result = []
        for rid, r in ROOMS.items():
            _cleanup_room(r)
            if game and r['game'] != game: continue
            cd = _get_countdown(r)
            result.append({
                'id': rid, 'name': r['name'], 'game': r['game'],
                'players': len(r['players']),
                'max': MAX_PLAYERS[r['game']],
                'started': r['started'],
                'countdown': cd,
            })
    return jsonify(result)

@app.route("/api/rooms/join", methods=["POST","OPTIONS"])
def api_rooms_join():
    if request.method == "OPTIONS": return jsonify({}), 200
    d = request.get_json() or {}
    rid = d.get("room_id"); uid = d.get("uid"); name = d.get("name", "Игрок")
    if not rid or not uid: return jsonify({"error":"missing"}), 400
    with ROOMS_LOCK:
        r = ROOMS.get(rid)
        if not r: return jsonify({"error":"no room"}), 404
        if r['started']: return jsonify({"error":"Игра уже идёт"}), 400
        mx = MAX_PLAYERS[r['game']]
        existing = next((p for p in r['players'] if p['id'] == uid), None)
        if not existing and len(r['players']) >= mx:
            return jsonify({"error":"Стол заполнен"}), 400
        # Remove old entry, re-add
        r['players'] = [p for p in r['players'] if p['id'] != uid]
        r['players'].append({'id': uid, 'name': name, 'last_ping': time.time()})
        if not r['host_id']: r['host_id'] = uid
        if len(r['players']) >= 2 and not r['countdown_start']:
            r['countdown_start'] = time.time()
        _push_msg(r, {'type': 'pl', 'players': [p['name'] for p in r['players']]})
        return jsonify({
            'ok': True,
            'is_host': r['host_id'] == uid,
            'players': [p['name'] for p in r['players']],
            'countdown': _get_countdown(r),
            'msg_seq': r['msg_seq'], 'act_seq': r['act_seq'],
        })

@app.route("/api/rooms/leave", methods=["POST","OPTIONS"])
def api_rooms_leave():
    if request.method == "OPTIONS": return jsonify({}), 200
    d = request.get_json() or {}
    rid = d.get("room_id"); uid = d.get("uid")
    with ROOMS_LOCK:
        r = ROOMS.get(rid)
        if not r: return jsonify({"ok": True})
        r['players'] = [p for p in r['players'] if p['id'] != uid]
        if r['host_id'] == uid:
            r['host_id'] = r['players'][0]['id'] if r['players'] else None
        if len(r['players']) < 2: r['countdown_start'] = None
        if not r['players']:
            r['messages']=[]; r['actions']=[]; r['msg_seq']=0; r['act_seq']=0
            r['started']=False; r['host_id']=None
        else:
            _push_msg(r, {'type': 'pl', 'players': [p['name'] for p in r['players']]})
    return jsonify({"ok": True})

@app.route("/api/rooms/poll")
def api_rooms_poll():
    rid = request.args.get("room_id")
    uid = request.args.get("uid")
    since_msg = int(request.args.get("since_msg", 0))
    since_act = int(request.args.get("since_act", 0))
    is_host = request.args.get("is_host") == "1"
    with ROOMS_LOCK:
        r = ROOMS.get(rid)
        if not r: return jsonify({"error":"no room"}), 404
        # Update ping
        for p in r['players']:
            if p['id'] == uid: p['last_ping'] = time.time()
        _cleanup_room(r)
        # Auto-start when countdown done
        cd = _get_countdown(r)
        if cd == 0 and not r['started'] and len(r['players']) >= 2:
            r['started'] = True
        result = {
            'players': [p['name'] for p in r['players']],
            'is_host': r['host_id'] == uid,
            'countdown': cd,
            'started': r['started'],
            'host_id': r['host_id'],
        }
        if is_host:
            new_acts = [a for a in r['actions'] if a['seq'] > since_act]
            result['actions'] = new_acts
            result['act_seq'] = r['act_seq']
        else:
            new_msgs = [m for m in r['messages'] if m['seq'] > since_msg]
            result['messages'] = new_msgs
            result['msg_seq'] = r['msg_seq']
    return jsonify(result)

@app.route("/api/rooms/push", methods=["POST","OPTIONS"])
def api_rooms_push():
    if request.method == "OPTIONS": return jsonify({}), 200
    d = request.get_json() or {}
    rid = d.get("room_id"); uid = d.get("uid"); msg = d.get("msg")
    with ROOMS_LOCK:
        r = ROOMS.get(rid)
        if not r: return jsonify({"error":"no room"}), 404
        _push_msg(r, msg)
    return jsonify({"ok": True, "seq": r['msg_seq']})

@app.route("/api/rooms/action", methods=["POST","OPTIONS"])
def api_rooms_action():
    if request.method == "OPTIONS": return jsonify({}), 200
    d = request.get_json() or {}
    rid = d.get("room_id"); uid = d.get("uid"); action = d.get("action")
    with ROOMS_LOCK:
        r = ROOMS.get(rid)
        if not r: return jsonify({"error":"no room"}), 404
        r['act_seq'] += 1
        r['actions'].append({'seq': r['act_seq'], 'from_id': uid, 'data': action})
        if len(r['actions']) > 100: r['actions'] = r['actions'][-100:]
    return jsonify({"ok": True})

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

# ── BOT ──
async def cmd_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    u = update.effective_user
    ensure_player(u.id, u.username or "", u.first_name or "Игрок")
    bal = get_balance(u.id)
    kb = [[InlineKeyboardButton("🎰 Играть", web_app=WebAppInfo(url=GAME_URL))]] if GAME_URL else []
    await update.message.reply_text(
        f"🎰 *Casino Night — {u.first_name}!*\n\n💰 Баланс: *${bal}*\n\n"
        "🃏 /play — открыть казино\n💵 /balance — баланс\n🎁 /daily — бонус $500\n👑 /top — топ",
        parse_mode="Markdown", reply_markup=InlineKeyboardMarkup(kb) if kb else None)

async def cmd_play(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    u = update.effective_user
    ensure_player(u.id, u.username or "", u.first_name or "Игрок")
    if not GAME_URL: await update.message.reply_text("⚙️ GAME_URL не настроен"); return
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
    await update.message.reply_text(f"✅ Баланс: ${add_balance(int(args[0]), int(args[1]))}")

def run_flask():
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 5000)))

def main():
    global BOT_TOKEN, DATABASE_URL, ADMIN_ID, GAME_URL
    BOT_TOKEN    = os.environ.get("BOT_TOKEN",    "").strip()
    DATABASE_URL = os.environ.get("DATABASE_URL", "").strip()
    ADMIN_ID     = int(os.environ.get("ADMIN_ID", "0").strip())
    GAME_URL     = os.environ.get("GAME_URL",     "").strip()
    log.info(f"BOT_TOKEN set: {bool(BOT_TOKEN)}, DB set: {bool(DATABASE_URL)}, GAME_URL: {GAME_URL}")
    if not BOT_TOKEN: log.error("BOT_TOKEN not set!"); return
    if not DATABASE_URL: log.error("DATABASE_URL not set!"); return

    import asyncio, telegram
    async def reset_bot():
        bot = telegram.Bot(token=BOT_TOKEN)
        await bot.delete_webhook(drop_pending_updates=True)
        log.info("Webhook cleared")
    asyncio.run(reset_bot())

    init_db()
    threading.Thread(target=run_flask, daemon=True).start()
    log.info("Flask started")

    application = Application.builder().token(BOT_TOKEN).build()
    async def error_handler(update, context):
        err = str(context.error)
        if 'Conflict' in err: log.warning("Conflict: another instance")
        else: log.error(f"Error: {err}")
    application.add_error_handler(error_handler)
    for cmd, fn in [("start",cmd_start),("play",cmd_play),("balance",cmd_balance),
                    ("daily",cmd_daily),("top",cmd_top),("topup",cmd_topup)]:
        application.add_handler(CommandHandler(cmd, fn))
    log.info("Bot polling...")
    application.run_polling(allowed_updates=Update.ALL_TYPES, drop_pending_updates=True, close_loop=False)

if __name__ == "__main__":
    main()
