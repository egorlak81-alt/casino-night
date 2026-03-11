import os, logging, threading, time, asyncio
from datetime import datetime, timedelta
from urllib.parse import urlparse

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

# ── DB ──
def get_conn():
    return psycopg2.connect(DATABASE_URL, sslmode='require')

def qone(sql, params=()):
    c = get_conn()
    try:
        cur = c.cursor(); cur.execute(sql, params); return cur.fetchone()
    finally: c.close()

def qall(sql, params=()):
    c = get_conn()
    try:
        cur = c.cursor(); cur.execute(sql, params); return cur.fetchall()
    finally: c.close()

def qexec(sql, params=()):
    c = get_conn()
    try:
        cur = c.cursor(); cur.execute(sql, params); c.commit()
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

def ensure_player(tg_id, username, first_name):
    qexec("INSERT INTO players (tg_id,username,first_name) VALUES (%s,%s,%s) ON CONFLICT (tg_id) DO NOTHING",
          (tg_id, username, first_name))

def get_balance(tg_id):
    r = qone("SELECT balance FROM players WHERE tg_id=%s", (tg_id,)); return r[0] if r else 0

def add_balance(tg_id, delta):
    qexec("""UPDATE players SET balance=balance+%s,
        total_won=total_won+(CASE WHEN %s>0 THEN %s ELSE 0 END),
        total_lost=total_lost+(CASE WHEN %s<0 THEN ABS(%s) ELSE 0 END),
        games_played=games_played+1 WHERE tg_id=%s""",
        (delta,delta,delta,delta,delta,tg_id))
    return get_balance(tg_id)

def get_last_daily(tg_id):
    r = qone("SELECT last_daily FROM players WHERE tg_id=%s",(tg_id,)); return r[0] if r else None

def set_last_daily(tg_id):
    qexec("UPDATE players SET last_daily=NOW() WHERE tg_id=%s",(tg_id,))

def get_top():
    return qall("SELECT first_name,balance,games_played FROM players ORDER BY balance DESC LIMIT 10")

# ── ROOMS ──
ROOMS_LOCK = threading.Lock()
COUNTDOWN_SECS = 90
MAX_PLAYERS = {'poker':6,'durak':4}

def _room(game, name):
    return {'game':game,'name':name,'players':[],'host_id':None,
            'messages':[],'actions':[],'msg_seq':0,'act_seq':0,
            'countdown_start':None,'started':False}

ROOMS = {}
for i in range(1,6):
    ROOMS[f'poker_{i}'] = _room('poker', f'Покер · Стол {i}')
    ROOMS[f'durak_{i}'] = _room('durak', f'Дурак · Стол {i}')

def _cd(r):
    if not r['countdown_start']: return None
    if r['started']: return 0
    return max(0, COUNTDOWN_SECS - int(time.time() - r['countdown_start']))

def _push(r, msg):
    r['msg_seq'] += 1
    r['messages'].append({'seq':r['msg_seq'],'data':msg})
    if len(r['messages'])>200: r['messages']=r['messages'][-200:]

def _cleanup(r):
    now = time.time()
    r['players'] = [p for p in r['players'] if now-p['last_ping']<35]
    if not r['players']:
        r.update({'host_id':None,'messages':[],'actions':[],'msg_seq':0,
                  'act_seq':0,'countdown_start':None,'started':False})
    else:
        if r['host_id'] not in [p['id'] for p in r['players']]:
            r['host_id'] = r['players'][0]['id']
        if len(r['players'])<2: r['countdown_start']=None

# ── FLASK ──
app = Flask(__name__)
CORS(app)

@app.route("/health")
def health(): return "ok"

@app.route("/api/rooms")
def api_rooms():
    game = request.args.get("game","")
    with ROOMS_LOCK:
        out = []
        for rid,r in ROOMS.items():
            _cleanup(r)
            if game and r['game']!=game: continue
            out.append({'id':rid,'name':r['name'],'game':r['game'],
                'players':len(r['players']),'max':MAX_PLAYERS[r['game']],
                'started':r['started'],'countdown':_cd(r)})
    return jsonify(out)

@app.route("/api/rooms/join", methods=["POST","OPTIONS"])
def api_rooms_join():
    if request.method=="OPTIONS": return jsonify({}),200
    d=request.get_json() or {}
    rid=d.get("room_id"); uid=d.get("uid"); name=d.get("name","Игрок")
    if not rid or not uid: return jsonify({"error":"missing"}),400
    with ROOMS_LOCK:
        r=ROOMS.get(rid)
        if not r: return jsonify({"error":"no room"}),404
        if r['started']: return jsonify({"error":"Игра уже идёт"}),400
        if not any(p['id']==uid for p in r['players']) and len(r['players'])>=MAX_PLAYERS[r['game']]:
            return jsonify({"error":"Стол заполнен"}),400
        r['players']=[p for p in r['players'] if p['id']!=uid]
        r['players'].append({'id':uid,'name':name,'last_ping':time.time()})
        if not r['host_id']: r['host_id']=uid
        if len(r['players'])>=2 and not r['countdown_start']: r['countdown_start']=time.time()
        _push(r,{'type':'pl','players':[p['name'] for p in r['players']]})
        return jsonify({'ok':True,'is_host':r['host_id']==uid,
            'players':[p['name'] for p in r['players']],
            'countdown':_cd(r),'msg_seq':r['msg_seq'],'act_seq':r['act_seq']})

@app.route("/api/rooms/leave", methods=["POST","OPTIONS"])
def api_rooms_leave():
    if request.method=="OPTIONS": return jsonify({}),200
    d=request.get_json() or {}; rid=d.get("room_id"); uid=d.get("uid")
    with ROOMS_LOCK:
        r=ROOMS.get(rid)
        if r:
            r['players']=[p for p in r['players'] if p['id']!=uid]
            if r['host_id']==uid: r['host_id']=r['players'][0]['id'] if r['players'] else None
            if len(r['players'])<2: r['countdown_start']=None
            if not r['players']:
                r.update({'messages':[],'actions':[],'msg_seq':0,'act_seq':0,'started':False,'host_id':None})
            else:
                _push(r,{'type':'pl','players':[p['name'] for p in r['players']]})
    return jsonify({"ok":True})

@app.route("/api/rooms/poll")
def api_rooms_poll():
    rid=request.args.get("room_id"); uid=request.args.get("uid")
    since_msg=int(request.args.get("since_msg",0)); since_act=int(request.args.get("since_act",0))
    is_host=request.args.get("is_host")=="1"
    with ROOMS_LOCK:
        r=ROOMS.get(rid)
        if not r: return jsonify({"error":"no room"}),404
        for p in r['players']:
            if p['id']==uid: p['last_ping']=time.time()
        _cleanup(r)
        cd=_cd(r)
        if cd==0 and not r['started'] and len(r['players'])>=2: r['started']=True
        res={'players':[p['name'] for p in r['players']],'is_host':r['host_id']==uid,
             'countdown':cd,'started':r['started'],'host_id':r['host_id']}
        if is_host:
            res['actions']=[a for a in r['actions'] if a['seq']>since_act]; res['act_seq']=r['act_seq']
        else:
            res['messages']=[m for m in r['messages'] if m['seq']>since_msg]; res['msg_seq']=r['msg_seq']
    return jsonify(res)

@app.route("/api/rooms/push", methods=["POST","OPTIONS"])
def api_rooms_push():
    if request.method=="OPTIONS": return jsonify({}),200
    d=request.get_json() or {}
    with ROOMS_LOCK:
        r=ROOMS.get(d.get("room_id"))
        if r: _push(r,d.get("msg"))
    return jsonify({"ok":True})

@app.route("/api/rooms/action", methods=["POST","OPTIONS"])
def api_rooms_action():
    if request.method=="OPTIONS": return jsonify({}),200
    d=request.get_json() or {}
    with ROOMS_LOCK:
        r=ROOMS.get(d.get("room_id"))
        if r:
            r['act_seq']+=1
            r['actions'].append({'seq':r['act_seq'],'from_id':d.get("uid"),'data':d.get("action")})
            if len(r['actions'])>100: r['actions']=r['actions'][-100:]
    return jsonify({"ok":True})

@app.route("/api/register", methods=["POST"])
def api_register():
    d=request.get_json() or {}; tg_id=d.get("tg_id")
    if not tg_id: return jsonify({"error":"no tg_id"}),400
    ensure_player(tg_id,d.get("username",""),d.get("first_name","Игрок"))
    return jsonify({"balance":get_balance(tg_id),"name":d.get("first_name","Игрок")})

@app.route("/api/balance")
def api_balance():
    tg_id=request.args.get("tg_id",type=int)
    if not tg_id: return jsonify({"error":"no tg_id"}),400
    return jsonify({"balance":get_balance(tg_id)})

@app.route("/api/update", methods=["POST"])
def api_update():
    d=request.get_json() or {}; tg_id=d.get("tg_id"); delta=d.get("delta",0)
    if not tg_id: return jsonify({"error":"no tg_id"}),400
    return jsonify({"balance":add_balance(tg_id,delta)})

@app.route("/api/top")
def api_top():
    rows=get_top() or []
    return jsonify([{"name":r[0],"balance":r[1],"games":r[2]} for r in rows])

# ── BOT HANDLERS ──
async def cmd_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    u=update.effective_user; ensure_player(u.id,u.username or "",u.first_name or "Игрок")
    bal=get_balance(u.id)
    kb=[[InlineKeyboardButton("🎰 Играть",web_app=WebAppInfo(url=GAME_URL))]] if GAME_URL else []
    await update.message.reply_text(
        f"🎰 *Casino Night — {u.first_name}!*\n\n💰 Баланс: *${bal}*\n\n"
        "🃏 /play — открыть казино\n💵 /balance — баланс\n🎁 /daily — бонус $500\n👑 /top — топ",
        parse_mode="Markdown",reply_markup=InlineKeyboardMarkup(kb) if kb else None)

async def cmd_play(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    u=update.effective_user; ensure_player(u.id,u.username or "",u.first_name or "Игрок")
    if not GAME_URL: await update.message.reply_text("⚙️ GAME_URL не настроен"); return
    kb=[[InlineKeyboardButton("🎰 Casino Night",web_app=WebAppInfo(url=GAME_URL))]]
    await update.message.reply_text("Нажми:",reply_markup=InlineKeyboardMarkup(kb))

async def cmd_balance(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    u=update.effective_user; ensure_player(u.id,u.username or "",u.first_name or "Игрок")
    r=qone("SELECT balance,total_won,total_lost,games_played FROM players WHERE tg_id=%s",(u.id,))
    if not r: await update.message.reply_text("Напиши /start"); return
    await update.message.reply_text(
        f"💰 *Баланс: ${r[0]}*\n🎮 Игр: {r[3]}\n📈 Выиграно: ${r[1]}\n📉 Проиграно: ${r[2]}",
        parse_mode="Markdown")

async def cmd_daily(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    u=update.effective_user; ensure_player(u.id,u.username or "",u.first_name or "Игрок")
    last=get_last_daily(u.id)
    if last:
        diff=datetime.utcnow()-last.replace(tzinfo=None)
        if diff<timedelta(hours=24):
            h=int((timedelta(hours=24)-diff).total_seconds()/3600)
            await update.message.reply_text(f"⏳ Следующий бонус через *{h} ч.*",parse_mode="Markdown"); return
    add_balance(u.id,500); set_last_daily(u.id)
    await update.message.reply_text(f"🎁 *+$500!*\n💰 Баланс: *${get_balance(u.id)}*",parse_mode="Markdown")

async def cmd_top(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    rows=get_top() or []
    medals=["🥇","🥈","🥉","4️⃣","5️⃣","6️⃣","7️⃣","8️⃣","9️⃣","🔟"]
    lines=["👑 *Топ Casino Night*\n"]+[f"{medals[i]} {r[0]} — *${r[1]}*" for i,r in enumerate(rows)]
    await update.message.reply_text("\n".join(lines),parse_mode="Markdown")

async def cmd_topup(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id!=ADMIN_ID: return
    args=ctx.args
    if len(args)<2: await update.message.reply_text("/topup <tg_id> <сумма>"); return
    await update.message.reply_text(f"✅ Баланс: ${add_balance(int(args[0]),int(args[1]))}")

# ── BOT THREAD ──
def _bot_thread():
    async def run():
        import requests as req
        try:
            req.get(f"https://api.telegram.org/bot{BOT_TOKEN}/deleteWebhook?drop_pending_updates=true",timeout=10)
            log.info("Webhook cleared")
        except: pass

        bot_app = Application.builder().token(BOT_TOKEN).build()
        async def err_h(u,c):
            e=str(c.error)
            if 'Conflict' in e: log.warning("Conflict")
            else: log.error(f"Bot error: {e}")
        bot_app.add_error_handler(err_h)
        for cmd,fn in [("start",cmd_start),("play",cmd_play),("balance",cmd_balance),
                       ("daily",cmd_daily),("top",cmd_top),("topup",cmd_topup)]:
            bot_app.add_handler(CommandHandler(cmd,fn))
        await bot_app.initialize()
        await bot_app.start()
        await bot_app.updater.start_polling(allowed_updates=Update.ALL_TYPES, drop_pending_updates=True)
        log.info("Bot polling started")
        await asyncio.sleep(float('inf'))
    asyncio.run(run())

# Start bot + DB on module load (for gunicorn)
if DATABASE_URL:
    try:
        init_db()
        log.info("DB initialized")
    except Exception as e:
        log.error(f"DB init failed: {e}")

if BOT_TOKEN:
    t = threading.Thread(target=_bot_thread, daemon=True)
    t.start()
    log.info("Bot thread started")

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port)
