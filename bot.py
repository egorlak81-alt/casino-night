import os, logging, time, threading
from datetime import datetime, timedelta

import psycopg2
from flask import Flask, request, jsonify
from flask_cors import CORS
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, WebAppInfo
from telegram.ext import Application, CommandHandler, ContextTypes

logging.basicConfig(level=logging.INFO)
log = logging.getLogger(__name__)

BOT_TOKEN    = os.environ.get("BOT_TOKEN",    "").strip()
DATABASE_URL = os.environ.get("DATABASE_URL", "").strip()
ADMIN_ID     = int(os.environ.get("ADMIN_ID", "0").strip() or "0")
GAME_URL     = os.environ.get("GAME_URL",     "").strip()
RAILWAY_URL  = os.environ.get("RAILWAY_PUBLIC_DOMAIN", "").strip()

# ── DB ──
def get_conn():
    return psycopg2.connect(DATABASE_URL, sslmode='require')

def qone(sql, p=()):
    c = get_conn()
    try: cur = c.cursor(); cur.execute(sql, p); return cur.fetchone()
    finally: c.close()

def qall(sql, p=()):
    c = get_conn()
    try: cur = c.cursor(); cur.execute(sql, p); return cur.fetchall()
    finally: c.close()

def qexec(sql, p=()):
    c = get_conn()
    try: cur = c.cursor(); cur.execute(sql, p); c.commit()
    finally: c.close()

def init_db():
    qexec("""CREATE TABLE IF NOT EXISTS players (
        tg_id BIGINT PRIMARY KEY, username TEXT DEFAULT '',
        first_name TEXT DEFAULT 'Игрок', balance INTEGER DEFAULT 1000,
        total_won INTEGER DEFAULT 0, total_lost INTEGER DEFAULT 0,
        games_played INTEGER DEFAULT 0, last_daily TIMESTAMP DEFAULT NULL,
        created_at TIMESTAMP DEFAULT NOW()
    )""")
    log.info("DB ready")

def ensure_player(tg_id, u, n):
    qexec("INSERT INTO players(tg_id,username,first_name)VALUES(%s,%s,%s)ON CONFLICT(tg_id)DO NOTHING",(tg_id,u,n))

def get_balance(tg_id):
    r = qone("SELECT balance FROM players WHERE tg_id=%s",(tg_id,)); return r[0] if r else 0

def update_balance(tg_id, delta, is_win):
    """Add or subtract from balance, track stats"""
    if is_win:
        qexec("""UPDATE players SET balance=balance+%s, total_won=total_won+%s,
            games_played=games_played+1 WHERE tg_id=%s""",(delta,delta,tg_id))
    else:
        qexec("""UPDATE players SET balance=GREATEST(0,balance-%s), total_lost=total_lost+%s,
            games_played=games_played+1 WHERE tg_id=%s""",(delta,delta,tg_id))
    return get_balance(tg_id)

def get_last_daily(tg_id):
    r = qone("SELECT last_daily FROM players WHERE tg_id=%s",(tg_id,)); return r[0] if r else None

def set_last_daily(tg_id):
    qexec("UPDATE players SET last_daily=NOW() WHERE tg_id=%s",(tg_id,))

def get_top():
    return qall("SELECT first_name,balance,games_played FROM players ORDER BY balance DESC LIMIT 10")

# ── ROOMS ──
ROOMS_LOCK = threading.Lock()
COUNTDOWN_SECS = 60
MAX_PLAYERS = {'poker':6,'durak':4}

# Stakes config: (label, buy_in, blind)
STAKES = {
    'small':  ('🟢 Малые',   200,  5),
    'medium': ('🟡 Средние', 500,  25),
    'big':    ('🔴 Большие', 2000, 100),
}

def _room(game, name, stake_key):
    label, buyin, blind = STAKES[stake_key]
    return {'game':game,'name':name,'stake':stake_key,'buyin':buyin,'blind':blind,
            'players':[],'host_id':None,
            'messages':[],'actions':[],'msg_seq':0,'act_seq':0,
            'countdown_start':None,'started':False}

ROOMS = {}
# Poker: 2 малых, 2 средних, 1 большой
ROOMS['poker_1'] = _room('poker','🟢 Покер · Малый 1','small')
ROOMS['poker_2'] = _room('poker','🟢 Покер · Малый 2','small')
ROOMS['poker_3'] = _room('poker','🟡 Покер · Средний 1','medium')
ROOMS['poker_4'] = _room('poker','🟡 Покер · Средний 2','medium')
ROOMS['poker_5'] = _room('poker','🔴 Покер · Большой','big')
# Durak: 2 малых, 2 средних, 1 большой
ROOMS['durak_1'] = _room('durak','🟢 Дурак · Малый 1','small')
ROOMS['durak_2'] = _room('durak','🟢 Дурак · Малый 2','small')
ROOMS['durak_3'] = _room('durak','🟡 Дурак · Средний 1','medium')
ROOMS['durak_4'] = _room('durak','🟡 Дурак · Средний 2','medium')
ROOMS['durak_5'] = _room('durak','🔴 Дурак · Большой','big')

def _cd(r):
    if not r['countdown_start']: return None
    if r['started']: return 0
    return max(0, COUNTDOWN_SECS - int(time.time() - r['countdown_start']))

def _push(r, msg):
    r['msg_seq'] += 1; r['messages'].append({'seq':r['msg_seq'],'data':msg})
    if len(r['messages']) > 200: r['messages'] = r['messages'][-200:]

def _cleanup(r):
    now = time.time()
    r['players'] = [p for p in r['players'] if now - p['last_ping'] < 35]
    if not r['players']:
        r.update({'host_id':None,'messages':[],'actions':[],'msg_seq':0,
                  'act_seq':0,'countdown_start':None,'started':False})
    else:
        if r['host_id'] not in [p['id'] for p in r['players']]:
            r['host_id'] = r['players'][0]['id']
        if len(r['players']) < 2: r['countdown_start'] = None

# ── FLASK ──
app = Flask(__name__)
CORS(app)

tg_app = None

@app.route("/health")
def health(): return "ok", 200

@app.route(f"/webhook/{BOT_TOKEN}", methods=["POST"])
async def webhook():
    if tg_app is None: return "not ready", 503
    data = request.get_json(force=True)
    update = Update.de_json(data, tg_app.bot)
    await tg_app.process_update(update)
    return "ok", 200

@app.route("/api/rooms")
def api_rooms():
    game = request.args.get("game","")
    with ROOMS_LOCK:
        out = []
        for rid,r in ROOMS.items():
            _cleanup(r)
            if game and r['game'] != game: continue
            out.append({'id':rid,'name':r['name'],'game':r['game'],
                'stake':r['stake'],'buyin':r['buyin'],'blind':r['blind'],
                'players':len(r['players']),'max':MAX_PLAYERS[r['game']],
                'started':r['started'],'countdown':_cd(r)})
    return jsonify(out)

@app.route("/api/rooms/join", methods=["POST","OPTIONS"])
def api_rooms_join():
    if request.method == "OPTIONS": return jsonify({}), 200
    d = request.get_json() or {}
    rid = d.get("room_id"); uid = d.get("uid"); name = d.get("name","Игрок")
    tg_id = d.get("tg_id")
    if not rid or not uid: return jsonify({"error":"missing"}), 400
    with ROOMS_LOCK:
        r = ROOMS.get(rid)
        if not r: return jsonify({"error":"no room"}), 404
        if r['started']: return jsonify({"error":"Игра уже идёт"}), 400
        if not any(p['id']==uid for p in r['players']) and len(r['players']) >= MAX_PLAYERS[r['game']]:
            return jsonify({"error":"Стол заполнен"}), 400
        # Check balance if tg_id provided
        if tg_id:
            bal = get_balance(int(tg_id))
            if bal < r['buyin']:
                return jsonify({"error":f"Недостаточно средств. Нужно ${r['buyin']}, у вас ${bal}"}), 400
        r['players'] = [p for p in r['players'] if p['id'] != uid]
        r['players'].append({'id':uid,'name':name,'tg_id':tg_id,'last_ping':time.time()})
        if not r['host_id']: r['host_id'] = uid
        if len(r['players']) >= 2 and not r['countdown_start']: r['countdown_start'] = time.time()
        _push(r, {'type':'pl','players':[p['name'] for p in r['players']]})
        return jsonify({'ok':True,'is_host':r['host_id']==uid,
            'players':[p['name'] for p in r['players']],
            'buyin':r['buyin'],'blind':r['blind'],'stake':r['stake'],
            'countdown':_cd(r),'msg_seq':r['msg_seq'],'act_seq':r['act_seq']})

@app.route("/api/rooms/leave", methods=["POST","OPTIONS"])
def api_rooms_leave():
    if request.method == "OPTIONS": return jsonify({}), 200
    d = request.get_json() or {}; rid = d.get("room_id"); uid = d.get("uid")
    with ROOMS_LOCK:
        r = ROOMS.get(rid)
        if r:
            r['players'] = [p for p in r['players'] if p['id'] != uid]
            if r['host_id'] == uid: r['host_id'] = r['players'][0]['id'] if r['players'] else None
            if len(r['players']) < 2: r['countdown_start'] = None
            if not r['players']:
                r.update({'messages':[],'actions':[],'msg_seq':0,'act_seq':0,'started':False,'host_id':None})
            else: _push(r, {'type':'pl','players':[p['name'] for p in r['players']]})
    return jsonify({"ok":True})

@app.route("/api/rooms/poll")
def api_rooms_poll():
    rid = request.args.get("room_id"); uid = request.args.get("uid")
    since_msg = int(request.args.get("since_msg",0)); since_act = int(request.args.get("since_act",0))
    is_host = request.args.get("is_host") == "1"
    with ROOMS_LOCK:
        r = ROOMS.get(rid)
        if not r: return jsonify({"error":"no room"}), 404
        for p in r['players']:
            if p['id'] == uid: p['last_ping'] = time.time()
        _cleanup(r); cd = _cd(r)
        if cd == 0 and not r['started'] and len(r['players']) >= 2: r['started'] = True
        res = {'players':[p['name'] for p in r['players']],'is_host':r['host_id']==uid,
               'countdown':cd,'started':r['started'],'host_id':r['host_id']}
        if is_host: res['actions']=[a for a in r['actions'] if a['seq']>since_act]; res['act_seq']=r['act_seq']
        else: res['messages']=[m for m in r['messages'] if m['seq']>since_msg]; res['msg_seq']=r['msg_seq']
    return jsonify(res)

@app.route("/api/rooms/push", methods=["POST","OPTIONS"])
def api_rooms_push():
    if request.method == "OPTIONS": return jsonify({}), 200
    d = request.get_json() or {}
    with ROOMS_LOCK:
        r = ROOMS.get(d.get("room_id"))
        if r: _push(r, d.get("msg"))
    return jsonify({"ok":True})

@app.route("/api/rooms/action", methods=["POST","OPTIONS"])
def api_rooms_action():
    if request.method == "OPTIONS": return jsonify({}), 200
    d = request.get_json() or {}
    with ROOMS_LOCK:
        r = ROOMS.get(d.get("room_id"))
        if r:
            r['act_seq'] += 1
            r['actions'].append({'seq':r['act_seq'],'from_id':d.get("uid"),'data':d.get("action")})
            if len(r['actions']) > 100: r['actions'] = r['actions'][-100:]
    return jsonify({"ok":True})

@app.route("/api/register", methods=["POST"])
def api_register():
    d = request.get_json() or {}; tg_id = d.get("tg_id")
    if not tg_id: return jsonify({"error":"no tg_id"}), 400
    ensure_player(tg_id, d.get("username",""), d.get("first_name","Игрок"))
    return jsonify({"balance":get_balance(tg_id),"name":d.get("first_name","Игрок")})

@app.route("/api/balance")
def api_balance():
    tg_id = request.args.get("tg_id", type=int)
    if not tg_id: return jsonify({"error":"no tg_id"}), 400
    return jsonify({"balance":get_balance(tg_id)})

@app.route("/api/update", methods=["POST"])
def api_update():
    """delta>0 = win (add), delta<0 = loss (subtract abs)"""
    d = request.get_json() or {}
    tg_id = d.get("tg_id"); delta = int(d.get("delta",0))
    if not tg_id: return jsonify({"error":"no tg_id"}), 400
    bal = update_balance(int(tg_id), abs(delta), delta > 0)
    return jsonify({"balance": bal})

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
        f"🎰 *Casino Night*\n\nПривет, {u.first_name}!\n💰 Баланс: *${bal}*\n\n"
        "🃏 /play — открыть казино\n💵 /balance — баланс\n🎁 /daily — бонус $500\n👑 /top — топ игроков\n\n"
        "_Пополнение баланса только через ежедневный бонус /daily_",
        parse_mode="Markdown",
        reply_markup=InlineKeyboardMarkup(kb) if kb else None)

async def cmd_play(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    u = update.effective_user
    ensure_player(u.id, u.username or "", u.first_name or "Игрок")
    if not GAME_URL: await update.message.reply_text("⚙️ GAME_URL не настроен"); return
    kb = [[InlineKeyboardButton("🎰 Casino Night", web_app=WebAppInfo(url=GAME_URL))]]
    await update.message.reply_text("Нажми кнопку:", reply_markup=InlineKeyboardMarkup(kb))

async def cmd_balance(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    u = update.effective_user
    ensure_player(u.id, u.username or "", u.first_name or "Игрок")
    r = qone("SELECT balance,total_won,total_lost,games_played FROM players WHERE tg_id=%s",(u.id,))
    if not r: await update.message.reply_text("Напиши /start"); return
    await update.message.reply_text(
        f"💰 *Баланс: ${r[0]}*\n🎮 Игр: {r[3]}\n📈 Выиграно: ${r[1]}\n📉 Проиграно: ${r[2]}\n\n"
        f"🎁 Следующий бонус: /daily",
        parse_mode="Markdown")

async def cmd_daily(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    u = update.effective_user
    ensure_player(u.id, u.username or "", u.first_name or "Игрок")
    last = get_last_daily(u.id)
    if last:
        diff = datetime.utcnow() - last.replace(tzinfo=None)
        if diff < timedelta(hours=24):
            remaining = timedelta(hours=24) - diff
            h = int(remaining.total_seconds() // 3600)
            m = int((remaining.total_seconds() % 3600) // 60)
            await update.message.reply_text(
                f"⏳ Следующий бонус через *{h}ч {m}м*", parse_mode="Markdown"); return
    # Give bonus
    qexec("UPDATE players SET balance=balance+500, last_daily=NOW() WHERE tg_id=%s",(u.id,))
    bal = get_balance(u.id)
    await update.message.reply_text(f"🎁 *+$500 бонус!*\n💰 Баланс: *${bal}*", parse_mode="Markdown")

async def cmd_top(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    rows = get_top() or []
    medals = ["🥇","🥈","🥉","4️⃣","5️⃣","6️⃣","7️⃣","8️⃣","9️⃣","🔟"]
    lines = ["👑 *Топ Casino Night*\n"] + [f"{medals[i]} {r[0]} — *${r[1]}* ({r[2]} игр)" for i,r in enumerate(rows)]
    await update.message.reply_text("\n".join(lines), parse_mode="Markdown")

# ── STARTUP ──
def setup():
    global tg_app
    import asyncio, requests as req

    if not BOT_TOKEN or not DATABASE_URL:
        log.error("Missing BOT_TOKEN or DATABASE_URL"); return

    init_db()

    tg_app = Application.builder().token(BOT_TOKEN).updater(None).build()
    for cmd,fn in [("start",cmd_start),("play",cmd_play),("balance",cmd_balance),
                   ("daily",cmd_daily),("top",cmd_top)]:
        tg_app.add_handler(CommandHandler(cmd,fn))

    async def init_tg():
        await tg_app.initialize()
        await tg_app.start()
        domain = RAILWAY_URL or "casino-night-production.up.railway.app"
        webhook_url = f"https://{domain}/webhook/{BOT_TOKEN}"
        await tg_app.bot.set_webhook(url=webhook_url, drop_pending_updates=True)
        log.info(f"Webhook set: {webhook_url}")

    loop = asyncio.new_event_loop()
    threading.Thread(target=loop.run_forever, daemon=True).start()
    asyncio.run_coroutine_threadsafe(init_tg(), loop)
    log.info("Bot setup done (webhook mode)")

setup()

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    log.info(f"Starting Flask on port {port}")
    app.run(host="0.0.0.0", port=port, threaded=True, use_reloader=False)
