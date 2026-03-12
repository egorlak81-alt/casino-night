import os, logging, time, threading
from datetime import datetime, timedelta

import psycopg2
from flask import Flask, request, jsonify
from flask_cors import CORS
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, WebAppInfo
from telegram.ext import Application, CommandHandler, MessageHandler, ContextTypes, filters

logging.basicConfig(level=logging.INFO)
log = logging.getLogger(__name__)

BOT_TOKEN    = os.environ.get("BOT_TOKEN",    "").strip()
DATABASE_URL = os.environ.get("DATABASE_URL", "").strip()
ADMIN_ID     = int(os.environ.get("ADMIN_ID", "0").strip() or "0")
GAME_URL     = os.environ.get("GAME_URL",     "").strip()
RAILWAY_URL  = os.environ.get("RAILWAY_PUBLIC_DOMAIN", "").strip()

# ════════════════════════════════
# ── DB ──
# ════════════════════════════════
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
        tg_id        BIGINT PRIMARY KEY,
        username     TEXT    DEFAULT \'\',
        nickname     TEXT    DEFAULT \'\',
        balance      INTEGER DEFAULT 1000,
        total_won    INTEGER DEFAULT 0,
        total_lost   INTEGER DEFAULT 0,
        games_played INTEGER DEFAULT 0,
        last_daily   TIMESTAMP DEFAULT NULL,
        created_at   TIMESTAMP DEFAULT NOW()
    )""")
    try: qexec("ALTER TABLE players ADD COLUMN IF NOT EXISTS nickname TEXT DEFAULT \'\'")
    except: pass
    # Таблица для хранения состояния диалога регистрации
    qexec("""CREATE TABLE IF NOT EXISTS reg_pending (
        tg_id      BIGINT PRIMARY KEY,
        step       TEXT DEFAULT \'nickname\',
        created_at TIMESTAMP DEFAULT NOW()
    )""")
    log.info("DB ready")

def player_exists(tg_id):
    r = qone("SELECT tg_id FROM players WHERE tg_id=%s AND nickname != \'\'", (tg_id,))
    return r is not None

def get_player(tg_id):
    return qone(
        "SELECT tg_id, nickname, balance, total_won, total_lost, games_played, last_daily FROM players WHERE tg_id=%s",
        (tg_id,)
    )

def create_player(tg_id, username, nickname):
    qexec(
        "INSERT INTO players(tg_id,username,nickname,balance) VALUES(%s,%s,%s,1000) "
        "ON CONFLICT(tg_id) DO UPDATE SET nickname=EXCLUDED.nickname, username=EXCLUDED.username",
        (tg_id, username, nickname)
    )

def get_balance(tg_id):
    r = qone("SELECT balance FROM players WHERE tg_id=%s", (tg_id,))
    return r[0] if r else 0

def apply_delta(tg_id, delta):
    if delta > 0:
        qexec("""UPDATE players SET balance=balance+%s, total_won=total_won+%s,
                 games_played=games_played+1 WHERE tg_id=%s""", (delta, delta, tg_id))
    elif delta < 0:
        amt = abs(delta)
        qexec("""UPDATE players SET balance=GREATEST(0,balance-%s), total_lost=total_lost+%s,
                 games_played=games_played+1 WHERE tg_id=%s""", (amt, amt, tg_id))
    new_bal = get_balance(tg_id)
    log.info(f"[BALANCE] tg_id={tg_id} delta={delta:+d} → {new_bal}")
    return new_bal

def get_top():
    return qall("""SELECT nickname, balance, games_played FROM players
                   WHERE nickname != \'\'  ORDER BY balance DESC LIMIT 10""")

def is_pending(tg_id):
    r = qone("SELECT tg_id FROM reg_pending WHERE tg_id=%s", (tg_id,))
    return r is not None

def set_pending(tg_id):
    qexec("INSERT INTO reg_pending(tg_id) VALUES(%s) ON CONFLICT DO NOTHING", (tg_id,))

def clear_pending(tg_id):
    qexec("DELETE FROM reg_pending WHERE tg_id=%s", (tg_id,))

def get_last_daily(tg_id):
    r = qone("SELECT last_daily FROM players WHERE tg_id=%s", (tg_id,))
    return r[0] if r else None

# ════════════════════════════════
# ── ROOMS ──
# ════════════════════════════════
ROOMS_LOCK = threading.Lock()
COUNTDOWN_SECS = 60
MAX_PLAYERS = {'poker': 6, 'durak': 4}

STAKES = {
    'small':  ('🟢 Малые',   200,  5),
    'medium': ('🟡 Средние', 500,  25),
    'big':    ('🔴 Большие', 2000, 100),
}

def _room(game, name, stake_key):
    label, buyin, blind = STAKES[stake_key]
    return {'game': game, 'name': name, 'stake': stake_key, 'buyin': buyin, 'blind': blind,
            'players': [], 'host_id': None,
            'messages': [], 'actions': [], 'msg_seq': 0, 'act_seq': 0,
            'countdown_start': None, 'started': False}

ROOMS = {}
ROOMS['poker_1'] = _room('poker', '🟢 Покер · Малый 1',   'small')
ROOMS['poker_2'] = _room('poker', '🟢 Покер · Малый 2',   'small')
ROOMS['poker_3'] = _room('poker', '🟡 Покер · Средний 1', 'medium')
ROOMS['poker_4'] = _room('poker', '🟡 Покер · Средний 2', 'medium')
ROOMS['poker_5'] = _room('poker', '🔴 Покер · Большой',   'big')
ROOMS['durak_1'] = _room('durak', '🟢 Дурак · Малый 1',   'small')
ROOMS['durak_2'] = _room('durak', '🟢 Дурак · Малый 2',   'small')
ROOMS['durak_3'] = _room('durak', '🟡 Дурак · Средний 1', 'medium')
ROOMS['durak_4'] = _room('durak', '🟡 Дурак · Средний 2', 'medium')
ROOMS['durak_5'] = _room('durak', '🔴 Дурак · Большой',   'big')

def _cd(r):
    if not r['countdown_start']: return None
    if r['started']: return 0
    return max(0, COUNTDOWN_SECS - int(time.time() - r['countdown_start']))

def _push(r, msg):
    r['msg_seq'] += 1
    r['messages'].append({'seq': r['msg_seq'], 'data': msg})
    if len(r['messages']) > 200: r['messages'] = r['messages'][-200:]

def _cleanup(r):
    now = time.time()
    r['players'] = [p for p in r['players'] if now - p['last_ping'] < 35]
    if not r['players']:
        r.update({'host_id': None, 'messages': [], 'actions': [], 'msg_seq': 0,
                  'act_seq': 0, 'countdown_start': None, 'started': False})
    else:
        if r['host_id'] not in [p['id'] for p in r['players']]:
            r['host_id'] = r['players'][0]['id']
        if len(r['players']) < 2: r['countdown_start'] = None

# ════════════════════════════════
# ── FLASK ──
# ════════════════════════════════
app = Flask(__name__)
CORS(app, resources={r"/*": {"origins": "*"}})

@app.after_request
def add_cors(resp):
    resp.headers["Access-Control-Allow-Origin"]  = "*"
    resp.headers["Access-Control-Allow-Headers"] = "Content-Type"
    resp.headers["Access-Control-Allow-Methods"] = "GET, POST, OPTIONS"
    return resp

tg_app = None
tg_loop = None

@app.route("/health")
def health(): return "ok", 200

@app.route(f"/webhook/{BOT_TOKEN}", methods=["POST"])
def webhook():
    if tg_app is None or tg_loop is None: return "not ready", 503
    data = request.get_json(force=True)
    if not data: return "bad request", 400
    import asyncio
    async def _process():
        update = Update.de_json(data, tg_app.bot)
        await tg_app.process_update(update)
    future = asyncio.run_coroutine_threadsafe(_process(), tg_loop)
    try: future.result(timeout=25)
    except Exception as e: log.error(f"Webhook error: {e}")
    return "ok", 200

# ── PLAYER API ──

@app.route("/api/me")
def api_me():
    tg_id = request.args.get("tg_id", type=int)
    if not tg_id: return jsonify({"error": "no tg_id"}), 400
    row = get_player(tg_id)
    if not row or not row[1]:
        return jsonify({"exists": False}), 200
    return jsonify({"exists": True, "tg_id": row[0], "nickname": row[1],
                    "balance": row[2], "total_won": row[3],
                    "total_lost": row[4], "games_played": row[5]})

@app.route("/api/balance")
def api_balance():
    tg_id = request.args.get("tg_id", type=int)
    if not tg_id: return jsonify({"error": "no tg_id"}), 400
    row = get_player(tg_id)
    if not row: return jsonify({"error": "not found"}), 404
    return jsonify({"balance": row[2], "nickname": row[1]})

@app.route("/api/update", methods=["POST", "OPTIONS"])
def api_update():
    if request.method == "OPTIONS": return jsonify({}), 200
    d = request.get_json() or {}
    tg_id = d.get("tg_id"); delta = int(d.get("delta", 0))
    if not tg_id: return jsonify({"error": "no tg_id"}), 400
    if not player_exists(int(tg_id)):
        return jsonify({"error": "player not found"}), 404
    bal = apply_delta(int(tg_id), delta)
    return jsonify({"balance": bal, "delta": delta})

@app.route("/api/top")
def api_top():
    rows = get_top() or []
    return jsonify([{"nickname": r[0], "balance": r[1], "games": r[2]} for r in rows])

@app.route("/api/botinfo")
def api_botinfo():
    """Возвращает username бота для ссылки в приложении"""
    return jsonify({"username": BOT_USERNAME})

@app.route("/api/debug")
def api_debug():
    rows = qall("SELECT tg_id,nickname,balance,games_played FROM players ORDER BY created_at DESC LIMIT 30") or []
    return jsonify([{"tg_id": r[0], "nick": r[1], "bal": r[2], "games": r[3]} for r in rows])

# ── ROOMS API ──

@app.route("/api/rooms")
def api_rooms():
    game = request.args.get("game", "")
    with ROOMS_LOCK:
        out = []
        for rid, r in ROOMS.items():
            _cleanup(r)
            if game and r['game'] != game: continue
            out.append({'id': rid, 'name': r['name'], 'game': r['game'],
                        'stake': r['stake'], 'buyin': r['buyin'], 'blind': r['blind'],
                        'players': len(r['players']), 'max': MAX_PLAYERS[r['game']],
                        'started': r['started'], 'countdown': _cd(r)})
    return jsonify(out)

@app.route("/api/rooms/join", methods=["POST", "OPTIONS"])
def api_rooms_join():
    if request.method == "OPTIONS": return jsonify({}), 200
    d = request.get_json() or {}
    rid = d.get("room_id"); uid = d.get("uid"); name = d.get("name", "Игрок")
    tg_id = d.get("tg_id")
    if not rid or not uid: return jsonify({"error": "missing"}), 400
    with ROOMS_LOCK:
        r = ROOMS.get(rid)
        if not r: return jsonify({"error": "no room"}), 404
        if r['started']: return jsonify({"error": "Игра уже идёт"}), 400
        if not any(p['id'] == uid for p in r['players']) and len(r['players']) >= MAX_PLAYERS[r['game']]:
            return jsonify({"error": "Стол заполнен"}), 400
        if tg_id:
            bal = get_balance(int(tg_id))
            if bal < r['buyin']:
                return jsonify({"error": f"Недостаточно средств. Нужно ${r['buyin']}, у вас ${bal}"}), 400
        r['players'] = [p for p in r['players'] if p['id'] != uid]
        r['players'].append({'id': uid, 'name': name, 'tg_id': tg_id, 'last_ping': time.time()})
        if not r['host_id']: r['host_id'] = uid
        if len(r['players']) >= 2 and not r['countdown_start']: r['countdown_start'] = time.time()
        _push(r, {'type': 'pl', 'players': [p['name'] for p in r['players']]})
        return jsonify({'ok': True, 'is_host': r['host_id'] == uid,
                        'players': [p['name'] for p in r['players']],
                        'buyin': r['buyin'], 'blind': r['blind'], 'stake': r['stake'],
                        'countdown': _cd(r), 'msg_seq': r['msg_seq'], 'act_seq': r['act_seq']})

@app.route("/api/rooms/leave", methods=["POST", "OPTIONS"])
def api_rooms_leave():
    if request.method == "OPTIONS": return jsonify({}), 200
    d = request.get_json() or {}; rid = d.get("room_id"); uid = d.get("uid")
    with ROOMS_LOCK:
        r = ROOMS.get(rid)
        if r:
            r['players'] = [p for p in r['players'] if p['id'] != uid]
            if r['host_id'] == uid:
                r['host_id'] = r['players'][0]['id'] if r['players'] else None
            if len(r['players']) < 2: r['countdown_start'] = None
            if not r['players']:
                r.update({'messages': [], 'actions': [], 'msg_seq': 0, 'act_seq': 0,
                           'started': False, 'host_id': None})
            else: _push(r, {'type': 'pl', 'players': [p['name'] for p in r['players']]})
    return jsonify({"ok": True})

@app.route("/api/rooms/poll")
def api_rooms_poll():
    rid = request.args.get("room_id"); uid = request.args.get("uid")
    since_msg = int(request.args.get("since_msg", 0)); since_act = int(request.args.get("since_act", 0))
    is_host = request.args.get("is_host") == "1"
    with ROOMS_LOCK:
        r = ROOMS.get(rid)
        if not r: return jsonify({"error": "no room"}), 404
        for p in r['players']:
            if p['id'] == uid: p['last_ping'] = time.time()
        _cleanup(r); cd = _cd(r)
        if cd == 0 and not r['started'] and len(r['players']) >= 2: r['started'] = True
        res = {'players': [p['name'] for p in r['players']], 'is_host': r['host_id'] == uid,
               'countdown': cd, 'started': r['started'], 'host_id': r['host_id']}
        if is_host: res['actions'] = [a for a in r['actions'] if a['seq'] > since_act]; res['act_seq'] = r['act_seq']
        else: res['messages'] = [m for m in r['messages'] if m['seq'] > since_msg]; res['msg_seq'] = r['msg_seq']
    return jsonify(res)

@app.route("/api/rooms/push", methods=["POST", "OPTIONS"])
def api_rooms_push():
    if request.method == "OPTIONS": return jsonify({}), 200
    d = request.get_json() or {}
    with ROOMS_LOCK:
        r = ROOMS.get(d.get("room_id"))
        if r: _push(r, d.get("msg"))
    return jsonify({"ok": True})

@app.route("/api/rooms/action", methods=["POST", "OPTIONS"])
def api_rooms_action():
    if request.method == "OPTIONS": return jsonify({}), 200
    d = request.get_json() or {}
    with ROOMS_LOCK:
        r = ROOMS.get(d.get("room_id"))
        if r:
            r['act_seq'] += 1
            r['actions'].append({'seq': r['act_seq'], 'from_id': d.get("uid"), 'data': d.get("action")})
            if len(r['actions']) > 100: r['actions'] = r['actions'][-100:]
    return jsonify({"ok": True})

# ════════════════════════════════
# ── BOT HANDLERS ──
# ════════════════════════════════

BOT_USERNAME = ""  # заполним при старте

def play_kb(tg_id=None):
    if not GAME_URL: return None
    url = GAME_URL
    if tg_id:
        sep = "&" if "?" in url else "?"
        url = f"{url}{sep}tg_id={tg_id}"
    return InlineKeyboardMarkup([[InlineKeyboardButton("🎰 Играть", web_app=WebAppInfo(url=url))]])

async def cmd_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    u = update.effective_user
    await handle_any(update, ctx, from_start=True)

async def cmd_play(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    u = update.effective_user
    if not player_exists(u.id):
        await update.message.reply_text(
            "❌ Ты ещё не зарегистрирован!\n\nОтправь мне любое сообщение чтобы выбрать ник.")
        return
    kb = play_kb(u.id)
    await update.message.reply_text("🎰 Открывай!", reply_markup=kb)

async def cmd_balance(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    u = update.effective_user
    if not player_exists(u.id):
        await update.message.reply_text("❌ Сначала зарегистрируйся — просто напиши мне любое сообщение!")
        return
    row = get_player(u.id)
    _, nick, bal, won, lost, games, _ = row
    await update.message.reply_text(
        f"💰 *{nick}* · Баланс: *${bal}*\n\n"
        f"🎮 Игр: {games}  📈 Выиграно: ${won}  📉 Проиграно: ${lost}\n\n"
        f"🎁 Ежедневный бонус: /daily",
        parse_mode="Markdown")

async def cmd_daily(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    u = update.effective_user
    if not player_exists(u.id):
        await update.message.reply_text("❌ Сначала зарегистрируйся — напиши мне любое сообщение!")
        return
    row = get_player(u.id); nick = row[1]
    last = get_last_daily(u.id)
    if last:
        diff = datetime.utcnow() - last.replace(tzinfo=None)
        if diff < timedelta(hours=24):
            rem = timedelta(hours=24) - diff
            h = int(rem.total_seconds()//3600); m = int((rem.total_seconds()%3600)//60)
            await update.message.reply_text(
                f"⏳ *{nick}*, следующий бонус через *{h}ч {m}м*", parse_mode="Markdown"); return
    qexec("UPDATE players SET balance=balance+500, last_daily=NOW() WHERE tg_id=%s", (u.id,))
    bal = get_balance(u.id)
    log.info(f"[DAILY] tg_id={u.id} nick={nick} → {bal}")
    await update.message.reply_text(f"🎁 *{nick}*, +$500!\n💰 Баланс: *${bal}*", parse_mode="Markdown")

async def cmd_top(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    rows = get_top() or []
    medals = ["🥇","🥈","🥉","4️⃣","5️⃣","6️⃣","7️⃣","8️⃣","9️⃣","🔟"]
    if not rows:
        await update.message.reply_text("Пока никто не сыграл!"); return
    lines = ["👑 *Топ Casino Night*\n"] + [f"{medals[i]} *{r[0]}* — ${r[1]} ({r[2]} игр)" for i,r in enumerate(rows)]
    await update.message.reply_text("\n".join(lines), parse_mode="Markdown")

async def handle_any(update: Update, ctx: ContextTypes.DEFAULT_TYPE, from_start=False):
    """Обрабатывает любое сообщение — регистрация или команды"""
    u = update.effective_user
    text = (update.message.text or "").strip()

    # Уже зарегистрирован
    if player_exists(u.id):
        clear_pending(u.id)
        row = get_player(u.id)
        nick, bal = row[1], row[2]
        kb = play_kb()
        if from_start:
            await update.message.reply_text(
                f"🎰 *Casino Night*\n\n"
                f"С возвращением, *{nick}*! 🎉\n"
                f"💰 Баланс: *${bal}*\n\n"
                f"🃏 /play — открыть казино\n"
                f"💵 /balance — баланс\n"
                f"🎁 /daily — бонус $500 (раз в 24ч)\n"
                f"👑 /top — топ игроков",
                parse_mode="Markdown", reply_markup=kb)
        return

    # Ожидает ввода ника
    if is_pending(u.id):
        nick = text
        # Валидация
        if len(nick) < 2 or len(nick) > 16:
            await update.message.reply_text(
                "❌ Ник должен быть от *2 до 16 символов*.\nПопробуй ещё раз:",
                parse_mode="Markdown"); return
        if not nick.replace("_","").replace("-","").replace(".","").isalnum():
            await update.message.reply_text(
                "❌ Только буквы, цифры, _ - .\nПопробуй ещё раз:",
                parse_mode="Markdown"); return
        # Проверить уникальность
        existing = qone("SELECT tg_id FROM players WHERE LOWER(nickname)=LOWER(%s)", (nick,))
        if existing:
            await update.message.reply_text(
                f"❌ Ник *{nick}* уже занят. Попробуй другой:",
                parse_mode="Markdown"); return
        # Создать игрока
        create_player(u.id, u.username or "", nick)
        clear_pending(u.id)
        bal = get_balance(u.id)
        log.info(f"[REG] tg_id={u.id} nick={nick} balance={bal}")
        kb = play_kb(u.id)
        await update.message.reply_text(
            f"✅ *Добро пожаловать в Casino Night, {nick}!*\n\n"
            f"💰 Стартовый баланс: *$1000*\n\n"
            f"🃏 /play — открыть казино\n"
            f"💵 /balance — твой баланс\n"
            f"🎁 /daily — бонус $500 каждые 24ч\n"
            f"👑 /top — таблица лидеров\n\n"
            f"_Ник нельзя изменить, выбирай с умом!_",
            parse_mode="Markdown", reply_markup=kb)
        return

    # Новый пользователь — начать регистрацию
    set_pending(u.id)
    await update.message.reply_text(
        f"🎰 *Casino Night*\n\n"
        f"Привет, {u.first_name}! Это онлайн-казино для Telegram.\n\n"
        f"Для начала выбери себе *ник* — он будет виден другим игрокам "
        f"и его *нельзя изменить*.\n\n"
        f"✏️ Введи желаемый ник (2–16 символов, только буквы/цифры):",
        parse_mode="Markdown")

# ════════════════════════════════
# ── STARTUP ──
# ════════════════════════════════
def setup():
    global tg_app, tg_loop, BOT_USERNAME
    import asyncio

    if not BOT_TOKEN or not DATABASE_URL:
        log.error("Missing BOT_TOKEN or DATABASE_URL"); return

    init_db()

    tg_app = Application.builder().token(BOT_TOKEN).updater(None).build()

    # Команды
    tg_app.add_handler(CommandHandler("start",   cmd_start))
    tg_app.add_handler(CommandHandler("play",    cmd_play))
    tg_app.add_handler(CommandHandler("balance", cmd_balance))
    tg_app.add_handler(CommandHandler("daily",   cmd_daily))
    tg_app.add_handler(CommandHandler("top",     cmd_top))
    # Любое текстовое сообщение (не команда) → регистрация
    tg_app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_any))

    async def init_tg():
        global BOT_USERNAME
        await tg_app.initialize()
        await tg_app.start()
        me = await tg_app.bot.get_me()
        BOT_USERNAME = me.username or ""
        domain = RAILWAY_URL or "casino-night-production.up.railway.app"
        wh = f"https://{domain}/webhook/{BOT_TOKEN}"
        await tg_app.bot.set_webhook(url=wh, drop_pending_updates=True)
        # Установить команды в меню бота
        from telegram import BotCommand
        await tg_app.bot.set_my_commands([
            BotCommand("start",   "🎰 Главное меню"),
            BotCommand("play",    "🃏 Открыть казино"),
            BotCommand("balance", "💰 Мой баланс"),
            BotCommand("daily",   "🎁 Бонус $500"),
            BotCommand("top",     "👑 Топ игроков"),
        ])
        log.info(f"Bot @{BOT_USERNAME} ready. Webhook: {wh}")

    loop = asyncio.new_event_loop()
    tg_loop = loop
    threading.Thread(target=loop.run_forever, daemon=True).start()
    asyncio.run_coroutine_threadsafe(init_tg(), loop)
    log.info("Setup done")

setup()

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    log.info(f"Flask on port {port}")
    app.run(host="0.0.0.0", port=port, threaded=True, use_reloader=False)
