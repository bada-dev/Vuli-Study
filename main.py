import os
import sqlite3
import time
import json
import urllib.request
import urllib.error
from flask import Flask, render_template, request, jsonify
from datetime import datetime, timezone, timedelta
import re

app = Flask(__name__)
ADMIN_PASSWORD = os.environ.get('ADMIN_PASSWORD')
SECOND_ADMIN_PASSWORD = os.environ.get('SECOND_ADMIN_PASSWORD')
GROQ_API_ONE = os.environ.get('GROQ_API_ONE')
GROQ_API_TWO = os.environ.get('GROQ_API_TWO')

def get_db():
    conn = sqlite3.connect('leaderboard.db')
    conn.row_factory = sqlite3.Row
    return conn

def init_db():
    conn = get_db()
    conn.execute('''CREATE TABLE IF NOT EXISTS users (
        username TEXT PRIMARY KEY,
        total_minutes INTEGER DEFAULT 0,
        streak INTEGER DEFAULT 0,
        reborns INTEGER DEFAULT 0,
        equipped_cosmetic TEXT DEFAULT NULL,
        active_background TEXT DEFAULT 'default',
        character_width INTEGER DEFAULT 140,
        happiness INTEGER DEFAULT 100,
        last_active INTEGER DEFAULT 0,
        is_active INTEGER DEFAULT 1
    )''')
    conn.execute('''CREATE TABLE IF NOT EXISTS feedback_cooldowns (
        ip TEXT PRIMARY KEY,
        last_submitted INTEGER DEFAULT 0
    )''')
    conn.execute('''CREATE TABLE IF NOT EXISTS sync_ratelimit (
        username TEXT PRIMARY KEY,
        last_sync INTEGER DEFAULT 0
    )''')
    conn.execute('''CREATE TABLE IF NOT EXISTS chats (
        id TEXT PRIMARY KEY,
        name TEXT NOT NULL,
        owner TEXT NOT NULL,
        created_at INTEGER DEFAULT 0
    )''')
    conn.execute('''CREATE TABLE IF NOT EXISTS chat_members (
        chat_id TEXT NOT NULL,
        username TEXT NOT NULL,
        PRIMARY KEY (chat_id, username)
    )''')
    conn.execute('''CREATE TABLE IF NOT EXISTS chat_messages (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        chat_id TEXT NOT NULL,
        username TEXT NOT NULL,
        message TEXT NOT NULL,
        timestamp INTEGER DEFAULT 0
    )''')
    conn.execute('''CREATE TABLE IF NOT EXISTS friend_requests (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        from_user TEXT NOT NULL,
        to_user TEXT NOT NULL,
        status TEXT DEFAULT 'pending',
        created_at INTEGER DEFAULT 0
    )''')
    conn.execute('''CREATE TABLE IF NOT EXISTS chat_timers (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        chat_id TEXT NOT NULL,
        creator TEXT NOT NULL,
        duration_seconds INTEGER NOT NULL,
        started_at INTEGER DEFAULT 0,
        completed INTEGER DEFAULT 0,
        reward_coins INTEGER DEFAULT 2
    )''')
    conn.execute('''CREATE TABLE IF NOT EXISTS chat_timer_presence (
        timer_id INTEGER NOT NULL,
        username TEXT NOT NULL,
        first_ping INTEGER DEFAULT 0,
        last_ping INTEGER DEFAULT 0,
        PRIMARY KEY (timer_id, username)
    )''')
    conn.execute('''CREATE TABLE IF NOT EXISTS premium_codes (
        code TEXT PRIMARY KEY,
        created_at INTEGER DEFAULT 0,
        redeemed INTEGER DEFAULT 0,
        redeemed_by TEXT DEFAULT NULL,
        redeemed_at INTEGER DEFAULT NULL
    )''')
    conn.execute('''CREATE TABLE IF NOT EXISTS weekly_study (
        username TEXT NOT NULL,
        week_start INTEGER NOT NULL,
        minutes INTEGER DEFAULT 0,
        PRIMARY KEY (username, week_start)
    )''')
    try:
        conn.execute('ALTER TABLE users ADD COLUMN is_premium INTEGER DEFAULT 0')
    except Exception:
        pass
    conn.commit()
    conn.close()

init_db()

FEEDBACK_COOLDOWN = 48 * 60 * 60
SYNC_COOLDOWN = 5
MAX_MINUTES = 50000
MAX_STREAK = 5000
MAX_REBORNS = 500
VALID_COSMETICS = [None, 'tophat', 'wizard', 'pirate']
VALID_BACKGROUNDS = ['default', 'ocean', 'sunset', 'lavender', 'mint', 'rose', 'midnight', 'forest']

@app.route('/sw.js')
def service_worker():
    from flask import send_from_directory
    return send_from_directory('static', 'sw.js', mimetype='application/javascript')

@app.route('/')
def home():
    return render_template('index.html')

@app.route('/check-password', methods=['POST'])
def check_password():
    data = request.get_json()
    if data.get('password') == ADMIN_PASSWORD:
        return jsonify({'correct': True})
    return jsonify({'correct': False})

@app.route('/set-username', methods=['POST'])
def set_username():
    data = request.get_json()
    username = data.get('username', '').strip()
    if not username or len(username) > 20 or len(username) < 2:
        return jsonify({'success': False, 'error': 'Invalid username'})
    blocked = ['admin', 'system', 'null', 'undefined', 'test', 'mod', 'owner']
    if username.lower() in blocked:
        return jsonify({'success': False, 'error': 'Username not allowed'})
    conn = get_db()
    try:
        existing = conn.execute('SELECT username FROM users WHERE username = ?', (username,)).fetchone()
        if existing:
            return jsonify({'success': False, 'error': 'Username taken'})
        conn.execute('INSERT INTO users (username, last_active, is_active) VALUES (?, ?, 1)',
                     (username, int(time.time())))
        conn.commit()
        return jsonify({'success': True})
    finally:
        conn.close()

@app.route('/sync-score', methods=['POST'])
def sync_score():
    data = request.get_json()
    username = data.get('username')
    if not username:
        return jsonify({'success': False})
    conn = get_db()
    try:
        user = conn.execute('SELECT username FROM users WHERE username = ?', (username,)).fetchone()
        if not user:
            return jsonify({'success': False, 'error': 'User not found'})
        # Rate limit
        rl = conn.execute('SELECT last_sync FROM sync_ratelimit WHERE username = ?', (username,)).fetchone()
        if rl and (int(time.time()) - rl['last_sync']) < SYNC_COOLDOWN:
            return jsonify({'success': False, 'error': 'Rate limited'})
        conn.execute('INSERT OR REPLACE INTO sync_ratelimit (username, last_sync) VALUES (?, ?)',
                     (username, int(time.time())))
        old_data = conn.execute('SELECT total_minutes FROM users WHERE username = ?', (username,)).fetchone()
        old_minutes = old_data['total_minutes'] if old_data else 0

        total_minutes = min(int(data.get('totalMinutes', 0)), MAX_MINUTES)
        streak = min(int(data.get('streak', 0)), MAX_STREAK)
        reborns = min(int(data.get('reborns', 0)), MAX_REBORNS)

        # Anti-tamper: minutes can never go down, and max gain per sync is 480 min (8 hrs)
        if total_minutes < old_minutes:
            total_minutes = old_minutes
        elif total_minutes - old_minutes > 480:
            total_minutes = old_minutes + 480
        equipped_cosmetic = data.get('equippedCosmetic', None)
        active_background = data.get('activeBackground', 'default')
        character_width = min(max(int(data.get('characterWidth', 140)), 140), 420)
        happiness = min(max(int(data.get('happiness', 100)), 0), 100)
        if equipped_cosmetic not in VALID_COSMETICS:
            equipped_cosmetic = None
        if active_background not in VALID_BACKGROUNDS:
            active_background = 'default'
        conn.execute('''UPDATE users SET
                        total_minutes=?, streak=?, reborns=?,
                        equipped_cosmetic=?, active_background=?,
                        character_width=?, happiness=?,
                        last_active=?, is_active=1
                        WHERE username=?''',
                     (total_minutes, streak, reborns, equipped_cosmetic,
                      active_background, character_width, happiness,
                      int(time.time()), username))
        gained = max(0, total_minutes - old_minutes)
        if gained > 0:
            now = datetime.now(timezone.utc)
            week_start_dt = now - timedelta(days=now.weekday())
            week_start_dt = week_start_dt.replace(hour=0, minute=0, second=0, microsecond=0)
            week_start_ts = int(week_start_dt.timestamp())
            conn.execute(
                '''INSERT INTO weekly_study (username, week_start, minutes)
                   VALUES (?, ?, ?)
                   ON CONFLICT(username, week_start)
                   DO UPDATE SET minutes = minutes + excluded.minutes''',
                (username, week_start_ts, gained)
            )
        conn.commit()
        conn.commit()
        return jsonify({'success': True, 'correctedMinutes': total_minutes})
    finally:
        conn.close()

@app.route('/leaderboard')
def leaderboard():
    conn = get_db()
    three_days_ago = int(time.time()) - (3 * 24 * 60 * 60)
    conn.execute('UPDATE users SET is_active=0 WHERE last_active < ? AND last_active > 0',
                 (three_days_ago,))
    conn.commit()
    period = request.args.get('period', 'all')
    if period == 'weekly':
        now = datetime.now(timezone.utc)
        week_start_dt = now - timedelta(days=now.weekday())
        week_start_dt = week_start_dt.replace(hour=0, minute=0, second=0, microsecond=0)
        week_start_ts = int(week_start_dt.timestamp())
        users = conn.execute(
            '''SELECT u.username, u.total_minutes, u.streak, u.reborns,
               u.equipped_cosmetic, u.active_background, u.character_width, u.happiness,
               u.last_active, u.is_active, u.is_premium, COALESCE(w.minutes, 0) AS weekly_minutes
               FROM users u
               LEFT JOIN weekly_study w ON u.username = w.username AND w.week_start = ?
               WHERE u.is_active=1
               ORDER BY weekly_minutes DESC, u.total_minutes DESC LIMIT 20''',
            (week_start_ts,)
        ).fetchall()
    else:
        users = conn.execute(
            '''SELECT username, total_minutes, streak, reborns,
               equipped_cosmetic, active_background, character_width, happiness,
               last_active, is_active, is_premium
               FROM users WHERE is_active=1
               ORDER BY total_minutes DESC LIMIT 20'''
        ).fetchall()
    conn.close()
    return jsonify([dict(u) for u in users])

@app.route('/check-active', methods=['POST'])
def check_active():
    data = request.get_json()
    username = data.get('username')
    if not username:
        return jsonify({'active': False})
    conn = get_db()
    user = conn.execute('SELECT is_active FROM users WHERE username = ?', (username,)).fetchone()
    conn.close()
    if not user:
        return jsonify({'active': False, 'exists': False})
    return jsonify({'active': bool(user['is_active']), 'exists': True})

@app.route('/rejoin', methods=['POST'])
def rejoin():
    data = request.get_json()
    username = data.get('username')
    if not username:
        return jsonify({'success': False})
    conn = get_db()
    conn.execute('UPDATE users SET is_active=1, last_active=? WHERE username=?',
                 (int(time.time()), username))
    conn.commit()
    conn.close()
    return jsonify({'success': True})

@app.route('/feedback-cooldown', methods=['GET'])
def get_feedback_cooldown():
    ip = request.remote_addr
    conn = get_db()
    row = conn.execute('SELECT last_submitted FROM feedback_cooldowns WHERE ip = ?', (ip,)).fetchone()
    conn.close()
    if not row:
        return jsonify({'remaining': 0})
    elapsed = int(time.time()) - row['last_submitted']
    remaining = max(0, FEEDBACK_COOLDOWN - elapsed)
    return jsonify({'remaining': remaining * 1000})

@app.route('/set-feedback-cooldown', methods=['POST'])
def set_feedback_cooldown():
    ip = request.remote_addr
    conn = get_db()
    conn.execute('INSERT OR REPLACE INTO feedback_cooldowns (ip, last_submitted) VALUES (?, ?)',
                 (ip, int(time.time())))
    conn.commit()
    conn.close()
    return jsonify({'success': True})

@app.route('/delete-user', methods=['POST'])
def delete_user():
    data = request.get_json()
    username = data.get('username')
    if not username:
        return jsonify({'success': False})
    conn = get_db()
    conn.execute('DELETE FROM users WHERE username = ?', (username,))
    conn.execute('DELETE FROM sync_ratelimit WHERE username = ?', (username,))
    conn.commit()
    conn.close()
    return jsonify({'success': True})

@app.route('/create-chat', methods=['POST'])
def create_chat():
    data = request.get_json()
    username = data.get('username', '').strip()
    name = data.get('name', '').strip()
    if not username or not name or len(name) > 30:
        return jsonify({'success': False, 'error': 'Invalid fields'})
    conn = get_db()
    try:
        user = conn.execute('SELECT username FROM users WHERE username = ?', (username,)).fetchone()
        if not user:
            return jsonify({'success': False, 'error': 'User not found'})
        count = conn.execute('SELECT COUNT(*) FROM chat_members WHERE username = ?', (username,)).fetchone()[0]
        if count >= 7:
            return jsonify({'success': False, 'error': 'Max 7 chats allowed'})
        chat_id = f"{int(time.time())}-{username[:8]}-{__import__('uuid').uuid4().hex[:6]}"
        conn.execute('INSERT INTO chats (id, name, owner, created_at) VALUES (?, ?, ?, ?)',
                     (chat_id, name, username, int(time.time())))
        conn.execute('INSERT INTO chat_members (chat_id, username) VALUES (?, ?)', (chat_id, username))
        conn.commit()
        return jsonify({'success': True, 'chat_id': chat_id})
    finally:
        conn.close()

@app.route('/user-chats', methods=['POST'])
def user_chats():
    data = request.get_json()
    username = data.get('username', '').strip()
    if not username:
        return jsonify({'success': False, 'chats': []})
    conn = get_db()
    try:
        chats = conn.execute('''
            SELECT c.id, c.name, c.owner, c.created_at,
                   (SELECT COUNT(*) FROM chat_members WHERE chat_id = c.id) as member_count
            FROM chats c
            JOIN chat_members cm ON c.id = cm.chat_id
            WHERE cm.username = ?
            ORDER BY c.created_at DESC
        ''', (username,)).fetchall()
        return jsonify({'success': True, 'chats': [dict(c) for c in chats]})
    finally:
        conn.close()

@app.route('/add-chat-member', methods=['POST'])
def add_chat_member():
    data = request.get_json()
    requester = data.get('requester', '').strip()
    chat_id = data.get('chat_id', '').strip()
    new_member = data.get('username', '').strip()
    if not all([requester, chat_id, new_member]):
        return jsonify({'success': False, 'error': 'Missing fields'})
    conn = get_db()
    try:
        chat = conn.execute('SELECT owner FROM chats WHERE id = ?', (chat_id,)).fetchone()
        if not chat:
            return jsonify({'success': False, 'error': 'Chat not found'})
        if chat['owner'] != requester:
            return jsonify({'success': False, 'error': 'Only the owner can add members'})
        user = conn.execute('SELECT username FROM users WHERE username = ?', (new_member,)).fetchone()
        if not user:
            return jsonify({'success': False, 'error': 'User not found — username is case-sensitive'})
        count = conn.execute('SELECT COUNT(*) FROM chat_members WHERE chat_id = ?', (chat_id,)).fetchone()[0]
        if count >= 30:
            return jsonify({'success': False, 'error': 'Chat is full (30 members max)'})
        existing = conn.execute('SELECT 1 FROM chat_members WHERE chat_id = ? AND username = ?',
                                (chat_id, new_member)).fetchone()
        if existing:
            return jsonify({'success': False, 'error': 'Already a member'})
        member_chats = conn.execute('SELECT COUNT(*) FROM chat_members WHERE username = ?',
                                    (new_member,)).fetchone()[0]
        if member_chats >= 7:
            return jsonify({'success': False, 'error': f'{new_member} is already in 7 chats'})
        conn.execute('INSERT INTO chat_members (chat_id, username) VALUES (?, ?)', (chat_id, new_member))
        conn.commit()
        return jsonify({'success': True})
    finally:
        conn.close()

@app.route('/chat-messages', methods=['POST'])
def chat_messages():
    data = request.get_json()
    username = data.get('username', '').strip()
    chat_id = data.get('chat_id', '').strip()
    since = int(data.get('since', 0))
    if not username or not chat_id:
        return jsonify({'success': False, 'messages': []})
    conn = get_db()
    try:
        member = conn.execute('SELECT 1 FROM chat_members WHERE chat_id = ? AND username = ?',
                              (chat_id, username)).fetchone()
        if not member:
            return jsonify({'success': False, 'error': 'Not a member'})
        messages = conn.execute('''
            SELECT id, username, message, timestamp FROM chat_messages
            WHERE chat_id = ? AND id > ?
            ORDER BY id ASC LIMIT 80
        ''', (chat_id, since)).fetchall()
        return jsonify({'success': True, 'messages': [dict(m) for m in messages]})
    finally:
        conn.close()

@app.route('/send-message', methods=['POST'])
def send_message():
    data = request.get_json()
    username = data.get('username', '').strip()
    chat_id = data.get('chat_id', '').strip()
    message = data.get('message', '').strip()
    if not username or not chat_id or not message or len(message) > 500:
        return jsonify({'success': False, 'error': 'Invalid data'})
    conn = get_db()
    try:
        member = conn.execute('SELECT 1 FROM chat_members WHERE chat_id = ? AND username = ?',
                              (chat_id, username)).fetchone()
        if not member:
            return jsonify({'success': False, 'error': 'Not a member'})
        conn.execute('INSERT INTO chat_messages (chat_id, username, message, timestamp) VALUES (?, ?, ?, ?)',
                     (chat_id, username, message, int(time.time())))
        conn.commit()
        return jsonify({'success': True})
    finally:
        conn.close()

@app.route('/chat-members', methods=['POST'])
def get_chat_members():
    data = request.get_json()
    username = data.get('username', '').strip()
    chat_id = data.get('chat_id', '').strip()
    if not username or not chat_id:
        return jsonify({'success': False})
    conn = get_db()
    try:
        member_check = conn.execute('SELECT 1 FROM chat_members WHERE chat_id = ? AND username = ?',
                                    (chat_id, username)).fetchone()
        if not member_check:
            return jsonify({'success': False, 'error': 'Not a member'})
        members = conn.execute('SELECT username FROM chat_members WHERE chat_id = ? ORDER BY username ASC',
                               (chat_id,)).fetchall()
        chat = conn.execute('SELECT owner FROM chats WHERE id = ?', (chat_id,)).fetchone()
        return jsonify({'success': True, 'members': [m['username'] for m in members], 'owner': chat['owner']})
    finally:
        conn.close()

@app.route('/admin-get-user', methods=['POST'])
def admin_get_user():
    data = request.get_json()
    if data.get('password') != ADMIN_PASSWORD:
        return jsonify({'success': False, 'error': 'Unauthorized'})
    username = data.get('username', '').strip()
    if not username:
        return jsonify({'success': False, 'error': 'No username'})
    conn = get_db()
    try:
        user = conn.execute('SELECT * FROM users WHERE username = ?', (username,)).fetchone()
        if not user:
            return jsonify({'success': False, 'error': 'User not found on server'})
        return jsonify({'success': True, 'user': dict(user)})
    finally:
        conn.close()


@app.route('/admin-export-db', methods=['POST'])
def admin_export_db():
    data = request.get_json()
    if data.get('password') != ADMIN_PASSWORD:
        return jsonify({'success': False, 'error': 'Unauthorized'})
    conn = get_db()
    try:
        tables = [r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'").fetchall()]
        dump = {}
        for t in tables:
            rows = conn.execute(f'SELECT * FROM {t}').fetchall()
            dump[t] = [dict(r) for r in rows]
        return jsonify({'success': True, 'dump': dump, 'exported_at': int(time.time())})
    finally:
        conn.close()

@app.route('/admin-import-db', methods=['POST'])
def admin_import_db():
    data = request.get_json()
    if data.get('password') != ADMIN_PASSWORD:
        return jsonify({'success': False, 'error': 'Unauthorized'})
    dump = data.get('dump')
    if not isinstance(dump, dict):
        return jsonify({'success': False, 'error': 'Invalid dump'})
    conn = get_db()
    try:
        conn.execute('PRAGMA foreign_keys=OFF')
        for table, rows in dump.items():
            if not isinstance(rows, list):
                continue
            conn.execute(f'DELETE FROM {table}')
            for row in rows:
                if not isinstance(row, dict) or not row:
                    continue
                cols = list(row.keys())
                vals = [row[c] for c in cols]
                q = ','.join('?' for _ in cols)
                conn.execute(f"INSERT INTO {table} ({','.join(cols)}) VALUES ({q})", vals)
        conn.commit()
        return jsonify({'success': True})
    except Exception as e:
        conn.rollback()
        return jsonify({'success': False, 'error': str(e)})
    finally:
        conn.close()

def call_ai(api_key, prompt):
    api_key = (api_key or '').strip().strip('"').strip("'")
    if not api_key:
        raise ValueError('No API key')
    models = [
        "llama-3.3-70b-versatile",
        "llama-3.1-70b-versatile",
        "llama3-70b-8192",
        "mixtral-8x7b-32768"
    ]

    last_error = None
    for model in models:
        data = json.dumps({
            "model": model,
            'max_tokens': 1000,
            'messages': [
                {'role': 'system', 'content': 'You are Study Buddy AI. You are speaking directly to the student. Give concrete study plans, not just descriptions. Keep tone natural and concise. Ask one helpful follow-up question at the end to improve the plan. Use quotes sparingly and only when they improve clarity.'},
                {'role': 'user', 'content': prompt}
            ]
        }).encode('utf-8')
        req = urllib.request.Request(
            "https://api.groq.com/openai/v1/chat/completions",
            data=data,
            headers={
                "Authorization": f"Bearer {api_key}",
                "Content-Type": "application/json",
                "User-Agent": "StudyBuddy/1.0"
            }
        )

        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                result = json.loads(resp.read().decode('utf-8'))
                return result['choices'][0]['message']['content']
        except urllib.error.HTTPError as e:
            body = e.read().decode('utf-8', errors='ignore')
            last_error = RuntimeError(f'Groq HTTP {e.code} [{model}]: {body[:240]}')
            continue

    if last_error:
        raise last_error
    raise RuntimeError('Groq request failed for all models')



@app.route('/second-admin-verify', methods=['POST'])
def second_admin_verify():
    data = request.get_json()
    if data.get('password') == SECOND_ADMIN_PASSWORD and SECOND_ADMIN_PASSWORD:
        return jsonify({'success': True})
    return jsonify({'success': False})

@app.route('/set-premium-code', methods=['POST'])
def set_premium_code():
    data = request.get_json()
    if data.get('password') != SECOND_ADMIN_PASSWORD or not SECOND_ADMIN_PASSWORD:
        return jsonify({'success': False, 'error': 'Unauthorized'})
    code = data.get('code', '').strip()
    if not code or len(code) > 30:
        return jsonify({'success': False, 'error': 'Invalid code'})
    conn = get_db()
    try:
        conn.execute('INSERT OR REPLACE INTO premium_codes (code, created_at, redeemed) VALUES (?, ?, 0)',
                     (code, int(time.time())))
        conn.commit()
        return jsonify({'success': True})
    finally:
        conn.close()

@app.route('/get-premium-code-status', methods=['POST'])
def get_premium_code_status():
    data = request.get_json()
    if data.get('password') != SECOND_ADMIN_PASSWORD or not SECOND_ADMIN_PASSWORD:
        return jsonify({'success': False, 'error': 'Unauthorized'})
    conn = get_db()
    try:
        codes = conn.execute('SELECT * FROM premium_codes ORDER BY created_at DESC LIMIT 1').fetchone()
        if not codes:
            return jsonify({'success': True, 'code': None})
        return jsonify({'success': True, 'code': dict(codes)})
    finally:
        conn.close()

@app.route('/redeem-premium', methods=['POST'])
def redeem_premium():
    data = request.get_json()
    username = data.get('username', '').strip()
    code = data.get('code', '').strip()
    if not username or not code:
        return jsonify({'success': False, 'error': 'Missing fields'})
    conn = get_db()
    try:
        user = conn.execute('SELECT username, is_premium FROM users WHERE username = ?', (username,)).fetchone()
        if not user:
            return jsonify({'success': False, 'error': 'User not found'})
        if user['is_premium']:
            return jsonify({'success': False, 'error': 'Already premium'})
        row = conn.execute('SELECT * FROM premium_codes WHERE code = ? AND redeemed = 0', (code,)).fetchone()
        if not row:
            return jsonify({'success': False, 'error': 'Invalid or already used code'})
        conn.execute('UPDATE premium_codes SET redeemed=1, redeemed_by=?, redeemed_at=? WHERE code=?',
                     (username, int(time.time()), code))
        conn.execute('UPDATE users SET is_premium=1 WHERE username=?', (username,))
        conn.commit()
        return jsonify({'success': True})
    finally:
        conn.close()

@app.route('/send-friend-request', methods=['POST'])
def send_friend_request():
    data = request.get_json()
    from_user = data.get('from_user', '').strip()
    to_user = data.get('to_user', '').strip()
    if not from_user or not to_user or from_user == to_user:
        return jsonify({'success': False, 'error': 'Invalid users'})
    conn = get_db()
    try:
        target = conn.execute('SELECT username FROM users WHERE username = ?', (to_user,)).fetchone()
        if not target:
            return jsonify({'success': False, 'error': 'User not found — username is case-sensitive'})
        existing = conn.execute(
            '''SELECT 1 FROM friend_requests WHERE ((from_user=? AND to_user=?) OR (from_user=? AND to_user=?))
               AND status != 'declined' ''',
            (from_user, to_user, to_user, from_user)).fetchone()
        if existing:
            return jsonify({'success': False, 'error': 'Request already exists or already friends'})
        count = conn.execute(
            '''SELECT COUNT(*) FROM friend_requests WHERE (from_user=? OR to_user=?) AND status='accepted' ''',
            (from_user, from_user)).fetchone()[0]
        if count >= 20:
            return jsonify({'success': False, 'error': 'Max 20 friends reached'})
        conn.execute('INSERT INTO friend_requests (from_user, to_user, created_at) VALUES (?,?,?)',
                     (from_user, to_user, int(time.time())))
        conn.commit()
        return jsonify({'success': True})
    finally:
        conn.close()

@app.route('/get-friends', methods=['POST'])
def get_friends():
    data = request.get_json()
    username = data.get('username', '').strip()
    if not username:
        return jsonify({'success': False, 'friends': []})
    conn = get_db()
    try:
        rows = conn.execute(
            '''SELECT fr.to_user as friend_username, u.total_minutes, u.streak, u.happiness, u.equipped_cosmetic, u.is_premium, fr.created_at
               FROM friend_requests fr
               JOIN users u ON u.username = fr.to_user
               WHERE fr.from_user=? AND fr.status='accepted'
               UNION ALL
               SELECT fr.from_user as friend_username, u.total_minutes, u.streak, u.happiness, u.equipped_cosmetic, u.is_premium, fr.created_at
               FROM friend_requests fr
               JOIN users u ON u.username = fr.from_user
               WHERE fr.to_user=? AND fr.status='accepted'
               ORDER BY created_at DESC''',
            (username, username)).fetchall()
        return jsonify({'success': True, 'friends': [dict(r) for r in rows]})
    finally:
        conn.close()

@app.route('/get-friend-requests', methods=['POST'])
def get_friend_requests():
    data = request.get_json()
    username = data.get('username', '').strip()
    if not username:
        return jsonify({'success': False, 'requests': []})
    conn = get_db()
    try:
        rows = conn.execute(
            "SELECT id, from_user, created_at FROM friend_requests WHERE to_user=? AND status='pending'",
            (username,)).fetchall()
        return jsonify({'success': True, 'requests': [dict(r) for r in rows]})
    finally:
        conn.close()

@app.route('/respond-friend-request', methods=['POST'])
def respond_friend_request():
    data = request.get_json()
    req_id = data.get('request_id')
    username = data.get('username', '').strip()
    status = data.get('status', '')
    if not req_id or status not in ('accepted', 'declined'):
        return jsonify({'success': False})
    conn = get_db()
    try:
        row = conn.execute('SELECT * FROM friend_requests WHERE id=? AND to_user=?', (req_id, username)).fetchone()
        if not row:
            return jsonify({'success': False, 'error': 'Request not found'})
        if status == 'accepted':
            count = conn.execute(
                "SELECT COUNT(*) FROM friend_requests WHERE (from_user=? OR to_user=?) AND status='accepted'",
                (username, username)).fetchone()[0]
            if count >= 20:
                return jsonify({'success': False, 'error': 'Max 20 friends reached'})
        conn.execute('UPDATE friend_requests SET status=? WHERE id=?', (status, req_id))
        conn.commit()
        return jsonify({'success': True})
    finally:
        conn.close()

@app.route('/remove-friend', methods=['POST'])
def remove_friend():
    data = request.get_json()
    username = data.get('username', '').strip()
    friend = data.get('friend_username', '').strip()
    if not username or not friend:
        return jsonify({'success': False})
    conn = get_db()
    try:
        conn.execute(
            "DELETE FROM friend_requests WHERE ((from_user=? AND to_user=?) OR (from_user=? AND to_user=?)) AND status='accepted'",
            (username, friend, friend, username))
        conn.commit()
        return jsonify({'success': True})
    finally:
        conn.close()

@app.route('/get-user-stats', methods=['POST'])
def get_user_stats():
    data = request.get_json()
    requester = data.get('requester', '').strip()
    target = data.get('target', '').strip()
    if not requester or not target:
        return jsonify({'success': False})
    conn = get_db()
    try:
        are_friends = conn.execute(
            "SELECT 1 FROM friend_requests WHERE ((from_user=? AND to_user=?) OR (from_user=? AND to_user=?)) AND status='accepted'",
            (requester, target, target, requester)).fetchone()
        if not are_friends:
            return jsonify({'success': False, 'error': 'Not friends'})
        user = conn.execute(
            'SELECT username, total_minutes, streak, reborns, happiness, equipped_cosmetic, active_background, is_premium FROM users WHERE username=?',
            (target,)).fetchone()
        if not user:
            return jsonify({'success': False, 'error': 'User not found'})
        return jsonify({'success': True, 'user': dict(user)})
    finally:
        conn.close()

@app.route('/start-chat-timer', methods=['POST'])
def start_chat_timer():
    data = request.get_json()
    chat_id = data.get('chat_id', '').strip()
    creator = data.get('creator', '').strip()
    duration = int(data.get('duration_seconds', 0))
    reward = min(max(int(data.get('reward_coins', 2)), 1), 20)
    if not chat_id or not creator or duration < 60 or duration > 7200:
        return jsonify({'success': False, 'error': 'Invalid fields'})
    conn = get_db()
    try:
        member = conn.execute('SELECT 1 FROM chat_members WHERE chat_id=? AND username=?', (chat_id, creator)).fetchone()
        if not member:
            return jsonify({'success': False, 'error': 'Not a member'})
        active = conn.execute('SELECT id FROM chat_timers WHERE chat_id=? AND completed=0', (chat_id,)).fetchone()
        if active:
            return jsonify({'success': False, 'error': 'Timer already active'})
        cur = conn.execute(
            'INSERT INTO chat_timers (chat_id, creator, duration_seconds, started_at, reward_coins) VALUES (?,?,?,?,?)',
            (chat_id, creator, duration, int(time.time()), reward))
        conn.commit()
        return jsonify({'success': True, 'timer_id': cur.lastrowid})
    finally:
        conn.close()

@app.route('/get-chat-timer', methods=['POST'])
def get_chat_timer():
    data = request.get_json()
    chat_id = data.get('chat_id', '').strip()
    username = data.get('username', '').strip()
    if not chat_id or not username:
        return jsonify({'success': False, 'timer': None})
    conn = get_db()
    try:
        timer = conn.execute('SELECT * FROM chat_timers WHERE chat_id=? AND completed=0 ORDER BY started_at DESC LIMIT 1',
                             (chat_id,)).fetchone()
        if not timer:
            return jsonify({'success': True, 'timer': None})
        t = dict(timer)
        now = int(time.time())
        end_time = t['started_at'] + t['duration_seconds']
        if now >= end_time and t['completed'] == 0:
            conn.execute('UPDATE chat_timers SET completed=1 WHERE id=?', (t['id'],))
            conn.commit()
            t['completed'] = 1
        t['server_time'] = now
        return jsonify({'success': True, 'timer': t})
    finally:
        conn.close()

@app.route('/ping-chat-presence', methods=['POST'])
def ping_chat_presence():
    data = request.get_json()
    timer_id = data.get('timer_id')
    username = data.get('username', '').strip()
    if not timer_id or not username:
        return jsonify({'success': False})
    now = int(time.time())
    conn = get_db()
    try:
        existing = conn.execute('SELECT first_ping FROM chat_timer_presence WHERE timer_id=? AND username=?',
                                (timer_id, username)).fetchone()
        if existing:
            conn.execute('UPDATE chat_timer_presence SET last_ping=? WHERE timer_id=? AND username=?',
                         (now, timer_id, username))
        else:
            conn.execute('INSERT INTO chat_timer_presence (timer_id, username, first_ping, last_ping) VALUES (?,?,?,?)',
                         (timer_id, username, now, now))
        conn.commit()
        return jsonify({'success': True})
    finally:
        conn.close()

@app.route('/claim-chat-timer-reward', methods=['POST'])
def claim_chat_timer_reward():
    data = request.get_json()
    timer_id = data.get('timer_id')
    username = data.get('username', '').strip()
    if not timer_id or not username:
        return jsonify({'eligible': False})
    conn = get_db()
    try:
        timer = conn.execute('SELECT * FROM chat_timers WHERE id=?', (timer_id,)).fetchone()
        if not timer:
            return jsonify({'eligible': False})
        end_time = timer['started_at'] + timer['duration_seconds']
        presence = conn.execute('SELECT first_ping, last_ping FROM chat_timer_presence WHERE timer_id=? AND username=?',
                                (timer_id, username)).fetchone()
        if not presence:
            return jsonify({'eligible': False, 'reason': 'No presence recorded'})
        joined_in_time = presence['first_ping'] <= timer['started_at'] + 300
        was_present_at_end = presence['last_ping'] >= end_time - 300
        if joined_in_time and was_present_at_end:
            return jsonify({'eligible': True, 'reward_coins': timer['reward_coins']})
        return jsonify({'eligible': False, 'reason': 'Not present long enough'})
    finally:
        conn.close()

@app.route('/generate-study-plan', methods=['POST'])
def generate_study_plan():
    data = request.get_json()
    username = data.get('username', '').strip()
    if not username:
        return jsonify({'success': False, 'error': 'No username'})
    conn = get_db()
    try:
        user = conn.execute('SELECT is_premium, total_minutes, streak, reborns FROM users WHERE username=?',
                            (username,)).fetchone()
        if not user or not user['is_premium']:
            return jsonify({'success': False, 'error': 'Premium required'})
        study_data = data.get('studyData', {})
        quotes = study_data.get('motivationalQuotes', [])
        convo = study_data.get('conversation', [])
        convo_text = ""
        if isinstance(convo, list) and convo:
            convo_text = "\\nConversation context (latest first):\\n" + "\\n".join([str(x)[:500] for x in convo[:6]])
        prompt = f"""You are an expert study advisor analyzing a student named "{username}".

Study Statistics:
- Total minutes studied (all time): {study_data.get('totalMinutes', user['total_minutes'])}
- Current streak: {study_data.get('streak', user['streak'])} days
- Reborn count (dedication level): {study_data.get('reborns', user['reborns'])}
- Daily minutes (last 7 days): {study_data.get('dailyMinutes', [])}
- Subjects studied: {study_data.get('subjects', [])}
- Pending tasks: {study_data.get('pendingTasks', [])}
- Completed tasks: {study_data.get('completedTasks', 0)}

Create a personalized, motivating study plan:
1. Quick assessment of their current habits (2-3 sentences)
2. Top 3 specific, actionable improvements
3. Recommended weekly schedule
4. Subject-specific tips if subjects are listed
5. One motivational insight

Mandatory output formatting rules:
- Use plain text only (no markdown symbols like ** or __).

Keep it under 350 words. Be encouraging and specific.
{convo_text}"""

        for api_key in [GROQ_API_ONE, GROQ_API_TWO]:
            if not api_key:
                continue
            try:
                result_text = call_ai(api_key, prompt)
                return jsonify({'success': True, 'plan': result_text})
            except Exception as e:
                print(f"[ExtremelyNiceErrorMessage!!] Here's the error message. GoodLuck!!!!!!: {e}")
                continue
        return jsonify({'success': False, 'error': 'contact_owner'})
    finally:
        conn.close()

@app.route('/leave-chat', methods=['POST'])
def leave_chat():
    data = request.get_json()
    username = data.get('username', '').strip()
    chat_id = data.get('chat_id', '').strip()
    if not username or not chat_id:
        return jsonify({'success': False})
    conn = get_db()
    try:
        chat = conn.execute('SELECT owner FROM chats WHERE id = ?', (chat_id,)).fetchone()
        if not chat:
            return jsonify({'success': False})
        conn.execute('DELETE FROM chat_members WHERE chat_id = ? AND username = ?', (chat_id, username))
        remaining = conn.execute('SELECT COUNT(*) FROM chat_members WHERE chat_id = ?',
                                 (chat_id,)).fetchone()[0]
        if remaining == 0 or chat['owner'] == username:
            conn.execute('DELETE FROM chats WHERE id = ?', (chat_id,))
            conn.execute('DELETE FROM chat_members WHERE chat_id = ?', (chat_id,))
            conn.execute('DELETE FROM chat_messages WHERE chat_id = ?', (chat_id,))
        conn.commit()
        return jsonify({'success': True})
    finally:
        conn.close()

if __name__ == '__main__':
    app.run()
