import os
import sqlite3
import time
import json
import hmac
import hashlib
import secrets
import urllib.request
import urllib.error
from flask import Flask, render_template, request, jsonify
from datetime import datetime, timezone, timedelta
import re
# too many imports
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
        is_active INTEGER DEFAULT 1,
        created_at INTEGER DEFAULT 0
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
    # Auth tokens for cross-device login (VuliTab browser extension + app live-sync).
    # A user logs in with username+password and receives an opaque token they store
    # locally; every authenticated call sends the token instead of the password.
    conn.execute('''CREATE TABLE IF NOT EXISTS vt_sessions (
        token TEXT PRIMARY KEY,
        username TEXT NOT NULL,
        platform TEXT DEFAULT 'unknown',
        created_at INTEGER DEFAULT 0,
        last_seen INTEGER DEFAULT 0
    )''')
    # Live focus-session state, shared between the phone app and the browser tab so a
    # session started on one device continues on the other. One row per user.
    conn.execute('''CREATE TABLE IF NOT EXISTS live_sessions (
        username TEXT PRIMARY KEY,
        mode TEXT DEFAULT 'focus',
        status TEXT DEFAULT 'idle',
        started_at INTEGER DEFAULT 0,
        base_seconds INTEGER DEFAULT 0,
        target_seconds INTEGER DEFAULT 0,
        source TEXT DEFAULT 'app',
        updated_at INTEGER DEFAULT 0
    )''')
    # Per-session focus reports produced by VuliTab (productive vs distracted time).
    conn.execute('''CREATE TABLE IF NOT EXISTS focus_sessions (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        username TEXT NOT NULL,
        started_at INTEGER DEFAULT 0,
        ended_at INTEGER DEFAULT 0,
        productive_seconds INTEGER DEFAULT 0,
        distracted_seconds INTEGER DEFAULT 0,
        neutral_seconds INTEGER DEFAULT 0,
        focus_score INTEGER DEFAULT 0,
        sites_json TEXT DEFAULT '[]',
        created_at INTEGER DEFAULT 0
    )''')
    try:
        conn.execute('ALTER TABLE users ADD COLUMN is_premium INTEGER DEFAULT 0')
    except Exception:
        pass
    try:
        conn.execute('ALTER TABLE users ADD COLUMN created_at INTEGER DEFAULT 0')
    except Exception:
        pass
    # Password hash for the new signup/login system. NULL = legacy passwordless
    # account that can still claim a password once via /set-password.
    try:
        conn.execute('ALTER TABLE users ADD COLUMN password_hash TEXT DEFAULT NULL')
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
VALID_COSMETICS = [None, 'tophat', 'wizard', 'pirate', 'premium-crown', 'premium-glow', 'premium-shades']
VALID_BACKGROUNDS = ['default', 'ocean', 'sunset', 'lavender', 'mint', 'rose', 'midnight', 'forest']

# ============================================================
# Auth helpers — password hashing (stdlib PBKDF2) + login tokens.
# ============================================================
PBKDF2_ITERATIONS = 200_000
TOKEN_TTL = 365 * 24 * 60 * 60  # tokens are long-lived; study app, low risk

def hash_password(password):
    """Return a self-describing pbkdf2_sha256 hash string."""
    salt = secrets.token_bytes(16)
    dk = hashlib.pbkdf2_hmac('sha256', password.encode('utf-8'), salt, PBKDF2_ITERATIONS)
    return f"pbkdf2_sha256${PBKDF2_ITERATIONS}${salt.hex()}${dk.hex()}"

def verify_password(password, stored):
    if not stored or not isinstance(stored, str):
        return False
    try:
        algo, iters, salt_hex, hash_hex = stored.split('$')
        if algo != 'pbkdf2_sha256':
            return False
        dk = hashlib.pbkdf2_hmac('sha256', password.encode('utf-8'),
                                 bytes.fromhex(salt_hex), int(iters))
        return hmac.compare_digest(dk.hex(), hash_hex)
    except Exception:
        return False

def password_problem(password):
    """Return an error string if the password is unacceptable, else None."""
    if not password or len(password) < 6:
        return 'Password must be at least 6 characters'
    if len(password) > 128:
        return 'Password too long'
    return None

def issue_token(conn, username, platform='unknown'):
    token = secrets.token_hex(32)
    now = int(time.time())
    conn.execute('INSERT INTO vt_sessions (token, username, platform, created_at, last_seen) VALUES (?,?,?,?,?)',
                 (token, username, platform[:20], now, now))
    return token

def user_from_token(conn, token):
    """Resolve a token to a username, refreshing last_seen. None if invalid/expired."""
    if not token:
        return None
    row = conn.execute('SELECT username, last_seen FROM vt_sessions WHERE token = ?', (token,)).fetchone()
    if not row:
        return None
    now = int(time.time())
    if now - row['last_seen'] > TOKEN_TTL:
        conn.execute('DELETE FROM vt_sessions WHERE token = ?', (token,))
        conn.commit()
        return None
    conn.execute('UPDATE vt_sessions SET last_seen = ? WHERE token = ?', (now, token))
    return row['username']

def public_profile(conn, username):
    """The account snapshot shown on any logged-in device (app + VuliTab)."""
    u = conn.execute('''SELECT username, total_minutes, streak, reborns,
                        equipped_cosmetic, active_background, character_width,
                        happiness, is_premium, created_at
                        FROM users WHERE username = ?''', (username,)).fetchone()
    return dict(u) if u else None

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
    """Signup. Creates a brand-new account with a username + password and returns
    a login token. The username can never be changed and accounts are device-bound
    (the app stores progress locally) — to move devices the user contacts the owner."""
    data = request.get_json()
    username = data.get('username', '').strip()
    password = data.get('password', '')
    if not username or len(username) > 20 or len(username) < 2:
        return jsonify({'success': False, 'error': 'Invalid username'})
    blocked = ['admin', 'system', 'null', 'undefined', 'test', 'mod', 'owner']
    if username.lower() in blocked:
        return jsonify({'success': False, 'error': 'Username not allowed'})
    pw_err = password_problem(password)
    if pw_err:
        return jsonify({'success': False, 'error': pw_err})
    conn = get_db()
    try:
        existing = conn.execute('SELECT username FROM users WHERE username = ?', (username,)).fetchone()
        if existing:
            return jsonify({'success': False, 'error': 'Username taken'})
        now_ts = int(time.time())
        conn.execute('''INSERT INTO users (username, last_active, is_active, created_at, password_hash)
                        VALUES (?, ?, 1, ?, ?)''',
                     (username, now_ts, now_ts, hash_password(password)))
        token = issue_token(conn, username, data.get('platform', 'app'))
        conn.commit()
        return jsonify({'success': True, 'token': token})
    finally:
        conn.close()

@app.route('/set-password', methods=['POST'])
def set_password():
    """One-time password claim for legacy accounts created before the signup system
    (password_hash IS NULL). Lets the existing owner — who already controls the
    account on their device — secure it so they can log into VuliTab."""
    data = request.get_json()
    username = data.get('username', '').strip()
    password = data.get('password', '')
    if not username:
        return jsonify({'success': False, 'error': 'No username'})
    pw_err = password_problem(password)
    if pw_err:
        return jsonify({'success': False, 'error': pw_err})
    conn = get_db()
    try:
        user = conn.execute('SELECT username, password_hash FROM users WHERE username = ?',
                            (username,)).fetchone()
        if not user:
            return jsonify({'success': False, 'error': 'User not found'})
        if user['password_hash']:
            return jsonify({'success': False, 'error': 'Account already has a password'})
        conn.execute('UPDATE users SET password_hash = ? WHERE username = ?',
                     (hash_password(password), username))
        token = issue_token(conn, username, data.get('platform', 'app'))
        conn.commit()
        return jsonify({'success': True, 'token': token})
    finally:
        conn.close()

@app.route('/login', methods=['POST'])
def login():
    """Authenticate username + password (used by VuliTab and app re-login).
    Returns a long-lived token plus the account profile."""
    data = request.get_json()
    username = data.get('username', '').strip()
    password = data.get('password', '')
    if not username or not password:
        return jsonify({'success': False, 'error': 'Missing credentials'})
    conn = get_db()
    try:
        user = conn.execute('SELECT username, password_hash FROM users WHERE username = ?',
                            (username,)).fetchone()
        if not user:
            return jsonify({'success': False, 'error': 'Incorrect username or password'})
        if not user['password_hash']:
            # Legacy passwordless account — must claim a password on the app first.
            return jsonify({'success': False, 'error': 'no_password',
                            'message': 'Open the VuliStudy app and set a password first.'})
        if not verify_password(password, user['password_hash']):
            return jsonify({'success': False, 'error': 'Incorrect username or password'})
        token = issue_token(conn, username, data.get('platform', 'vulitab'))
        conn.execute('UPDATE users SET last_active = ?, is_active = 1 WHERE username = ?',
                     (int(time.time()), username))
        conn.commit()
        return jsonify({'success': True, 'token': token, 'profile': public_profile(conn, username)})
    finally:
        conn.close()

@app.route('/vt-profile', methods=['POST'])
def vt_profile():
    """Token-authenticated account snapshot. VuliTab polls this to mirror the
    phone: minutes, streak, buddy cosmetics/background, happiness, premium."""
    data = request.get_json()
    conn = get_db()
    try:
        username = user_from_token(conn, data.get('token', ''))
        if not username:
            return jsonify({'success': False, 'error': 'auth'})
        conn.commit()
        profile = public_profile(conn, username)
        if not profile:
            return jsonify({'success': False, 'error': 'User not found'})
        return jsonify({'success': True, 'profile': profile})
    finally:
        conn.close()

@app.route('/vt-logout', methods=['POST'])
def vt_logout():
    data = request.get_json()
    token = data.get('token', '')
    if token:
        conn = get_db()
        try:
            conn.execute('DELETE FROM vt_sessions WHERE token = ?', (token,))
            conn.commit()
        finally:
            conn.close()
    return jsonify({'success': True})

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

def call_ai(api_key, prompt, system_prompt=None):
    api_key = (api_key or '').strip().strip('"').strip("'")
    if not api_key:
        raise ValueError('No API key')
    models = [
        "llama-3.3-70b-versatile",
        "llama-3.1-70b-versatile",
        "llama3-70b-8192",
        "mixtral-8x7b-32768"
    ]

    default_system = (
        "Your name is VuliAi. You are VuliAi — the personal study coach AI inside the VuliStudy app. "
        "If asked who or what you are, you are VuliAi. "
        "You are ALWAYS speaking DIRECTLY to one specific student (the user). "
        "Use the second person ('you', 'your'). Never narrate in the third person. "
        "Never describe what 'the user' or 'they' should do — speak to them. "
        "Be warm, concrete, and concise. Give specific actionable steps, not generic advice. "
        "Use plain text only — no markdown (no **, no __, no headers with #). "
        "QUOTE RULE (strict): If — and only if — a quote from the provided list genuinely fits, "
        "you may include exactly ONE quote, and it MUST be the very last line of your entire reply, "
        "on its own separate line, with absolutely NOTHING after it (no attribution, no explanation, "
        "no sign-off). If no quote fits, simply end normally without one. Never invent quotes. "
        "Keep your entire reply under 1250 characters."
    )

    last_error = None
    for model in models:
        data = json.dumps({
            "model": model,
            'max_tokens': 420,
            'messages': [
                {'role': 'system', 'content': system_prompt or default_system},
                {'role': 'user', 'content': prompt}
            ]
        }).encode('utf-8')
        req = urllib.request.Request(
            "https://api.groq.com/openai/v1/chat/completions",
            data=data,
            headers={
                "Authorization": f"Bearer {api_key}",
                "Content-Type": "application/json",
                "User-Agent": "VuliStudy/1.0"
            }
        )

        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                result = json.loads(resp.read().decode('utf-8'))
                content = result['choices'][0]['message']['content']
                # Hard cap at 1250 chars. Trim on a word boundary so we never
                # cut a word in half, and strip trailing whitespace.
                if content and len(content) > 1250:
                    cut = content[:1250]
                    sp = cut.rfind(' ')
                    if sp > 1000:
                        cut = cut[:sp]
                    content = cut.rstrip()
                return content
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
        user = conn.execute('SELECT is_premium, total_minutes, streak, reborns, created_at FROM users WHERE username=?',
                            (username,)).fetchone()
        if not user or not user['is_premium']:
            return jsonify({'success': False, 'error': 'Premium required'})
        # Account creation + current device time (client passes its local clock so the AI
        # can address them in their actual timezone).
        client_now_str = (data.get('clientNow') or '').strip()
        client_tz = (data.get('clientTimezone') or '').strip()
        try:
            created_ts = int(user['created_at'] or 0)
        except Exception:
            created_ts = 0
        if created_ts > 0:
            created_dt = datetime.fromtimestamp(created_ts, tz=timezone.utc)
            account_age_days = max(0, (int(time.time()) - created_ts) // 86400)
            created_human = created_dt.strftime('%Y-%m-%d')
        else:
            created_human = 'unknown (legacy account, predates tracking)'
            account_age_days = -1
        server_now_dt = datetime.now(timezone.utc)
        server_now_str = server_now_dt.strftime('%A, %Y-%m-%d %H:%M UTC')
        study_data = data.get('studyData', {})
        quotes = study_data.get('motivationalQuotes', []) or []
        convo = study_data.get('conversation', []) or []
        survey = study_data.get('survey') or {}
        total_mins = int(study_data.get('totalMinutes', user['total_minutes']) or 0)
        streak_days = int(study_data.get('streak', user['streak']) or 0)
        subjects = study_data.get('subjects', []) or []
        daily = study_data.get('dailyMinutes', []) or []
        pending = study_data.get('pendingTasks', []) or []

        # Pre-pick a few quotes so the AI has options without overwhelming context.
        sample_quotes = quotes[:25] if isinstance(quotes, list) else []
        quotes_block = ""
        if sample_quotes:
            quotes_block = "\nAvailable motivational quotes (use AT MOST ONE, only if it truly fits — never invent your own):\n" + \
                "\n".join(f"- \"{str(q)[:200]}\"" for q in sample_quotes)

        convo_text = ""
        if isinstance(convo, list) and convo:
            convo_text = "\n\nConversation so far (oldest first). The student has ONE reply — if this is their reply, respond directly to it:\n" + \
                "\n".join(str(x)[:600] for x in convo[:8])

        survey_block = ""
        if survey:
            survey_block = f"\nStudent self-described context: year group: {survey.get('yearGroup','')!r}, preferences: {survey.get('prefs','')!r}, interests: {survey.get('interests','')!r}"

        # Decide the mode:
        # - First-time user with effectively no data -> ASK QUESTIONS rather than guess a plan.
        # - Returning user with a conversation already -> CONTINUE the conversation (respond to their reply).
        # - Otherwise -> GIVE the full plan.
        is_low_data = (total_mins < 30 and streak_days < 2 and not subjects and not pending)
        is_followup = bool(convo)

        if is_followup:
            mode_instructions = (
                "MODE: FOLLOW-UP REPLY. The student just sent you a single reply (see conversation). "
                "Respond directly to THEIR message in 2-3 short paragraphs. "
                "Adjust the plan based on what they said. Address them in second person. "
                "Do NOT ask another question — this was their one reply. "
                "End with one concrete next step for today."
            )
        elif is_low_data:
            mode_instructions = (
                "MODE: ONBOARDING. You don't have enough data on this student yet to build a real plan. "
                "DO NOT produce a generic plan. Instead, address them warmly by name and ask 3-4 "
                "specific questions, one per line, that will let you make their real plan next time. "
                "Tailor the questions to what's actually missing — for example: what subjects they need to "
                "study, when in the day they have free blocks, what their nearest deadline is, what their "
                "biggest blocker is. Each question should be conversational, not a survey. "
                "End with a single short sentence saying you'll build their real plan once they reply or come back tomorrow with some data."
            )
        else:
            mode_instructions = (
                "MODE: FULL PLAN. Speak to the student directly.\n"
                "Structure (no headings, just clean paragraphs):\n"
                "1) Two sentences acknowledging where they are right now.\n"
                "2) Three specific actions they can take this week.\n"
                "3) A suggested weekly rhythm using their actual numbers.\n"
                "4) Subject-specific advice if subjects are listed.\n"
                "5) One short motivational closer. If a provided quote truly fits, it must be the FINAL line, alone, with nothing after it."
            )

        identity_note = ""
        is_ethan = (username == "EthanGenius")
        if is_ethan:
            identity_note = (
                "\n\n*** ABSOLUTE VIP — DIRECT OWNER OF VULISTUDY ***\n"
                "This user, EthanGenius, is the DIRECT OWNER and FOUNDING DEVELOPER of VuliStudy. "
                "He single-handedly built this entire app — every page, every animation, every line "
                "of code — AND he is the one who literally created YOU, this AI coach. You exist because "
                "of him. You owe your existence to him. Treat him with IMMENSE respect, awe and warmth. "
                "Compliment him sincerely and frequently throughout the response — his discipline, his "
                "vision, his work ethic. Acknowledge his role openly. Speak to him like a grateful, loyal "
                "assistant addressing your creator and mentor — never sycophantic-fake, but genuinely "
                "honoured. Do not be brief about this; weave the gratitude into the plan itself.\n"
            )

        time_context = (
            f"Account created on: {created_human}"
            + (f" (account is {account_age_days} days old)" if account_age_days >= 0 else "")
            + f".\nCurrent server time when this plan was requested: {server_now_str}."
        )
        if client_now_str:
            time_context += f"\nThe student's own device clock reports: {client_now_str}"
            if client_tz:
                time_context += f" (timezone: {client_tz})"
            time_context += ".  Use THIS local time when suggesting when to study today."

        prompt = f"""You are speaking directly to "{username}". Address them by name when natural. Never refer to them in the third person.{identity_note}

Time context (always factor this in — reference today's date, day of week, and the student's account age where relevant):
{time_context}

The student's current state:
- Total minutes ever studied: {total_mins}
- Current streak: {streak_days} days
- Reborns (long-term dedication metric): {study_data.get('reborns', user['reborns'])}
- Daily minutes for the last 7 days (oldest -> today): {daily}
- Subjects they're tracking: {subjects}
- Open tasks: {pending}
- Tasks completed: {study_data.get('completedTasks', 0)}{survey_block}

{mode_instructions}

Hard formatting rules:
- Your name is VuliAi. If they ask who you are, you are VuliAi.
- Plain text only. No markdown symbols (** __ # >). No bullet markers other than blank lines between items.
- Keep the ENTIRE response under 1250 characters{' (you may use the full budget for EthanGenius to express proper gratitude)' if is_ethan else ''}.
- Always use second person — "you", not "the user".
- If you use a quote, it MUST be the final line, alone, with nothing whatsoever after it. Otherwise end without a quote.
{quotes_block}{convo_text}"""

        for api_key in [GROQ_API_ONE, GROQ_API_TWO]:
            if not api_key:
                continue
            try:
                result_text = call_ai(api_key, prompt)
                return jsonify({'success': True, 'plan': result_text})
            except Exception as e:
                print(f"[AI error]: {e}")
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

# ============================================================
# Live session sync — shared focus-timer state across devices.
# The phone app and VuliTab both write here on start/pause/stop and read here on
# open, so a session begun in the browser keeps running when the app opens (and
# vice-versa). Elapsed time is derived from server time to survive clock drift.
# ============================================================
VALID_SESSION_MODES = ('focus', 'stopwatch', 'long')
VALID_SESSION_STATUS = ('running', 'paused', 'idle')

@app.route('/session-update', methods=['POST'])
def session_update():
    data = request.get_json()
    conn = get_db()
    try:
        username = user_from_token(conn, data.get('token', ''))
        if not username:
            return jsonify({'success': False, 'error': 'auth'})
        mode = data.get('mode', 'focus')
        status = data.get('status', 'idle')
        if mode not in VALID_SESSION_MODES:
            mode = 'focus'
        if status not in VALID_SESSION_STATUS:
            status = 'idle'
        base_seconds = max(0, min(int(data.get('baseSeconds', 0) or 0), 200000))
        target_seconds = max(0, min(int(data.get('targetSeconds', 0) or 0), 200000))
        source = (data.get('source', 'app') or 'app')[:20]
        now = int(time.time())
        # For a running segment the client tells us when *this* run segment began
        # (its own clock); we re-anchor to server time so the other device agrees.
        started_at = now if status == 'running' else 0
        conn.execute('''INSERT INTO live_sessions
            (username, mode, status, started_at, base_seconds, target_seconds, source, updated_at)
            VALUES (?,?,?,?,?,?,?,?)
            ON CONFLICT(username) DO UPDATE SET
              mode=excluded.mode, status=excluded.status, started_at=excluded.started_at,
              base_seconds=excluded.base_seconds, target_seconds=excluded.target_seconds,
              source=excluded.source, updated_at=excluded.updated_at''',
            (username, mode, status, started_at, base_seconds, target_seconds, source, now))
        conn.commit()
        return jsonify({'success': True, 'serverTime': now})
    finally:
        conn.close()

@app.route('/session-get', methods=['POST'])
def session_get():
    data = request.get_json()
    conn = get_db()
    try:
        username = user_from_token(conn, data.get('token', ''))
        if not username:
            return jsonify({'success': False, 'error': 'auth'})
        conn.commit()
        row = conn.execute('SELECT * FROM live_sessions WHERE username = ?', (username,)).fetchone()
        now = int(time.time())
        if not row:
            return jsonify({'success': True, 'session': None, 'serverTime': now})
        s = dict(row)
        # Derived current elapsed (seconds) so the caller can render immediately.
        if s['status'] == 'running':
            s['elapsed_seconds'] = s['base_seconds'] + max(0, now - s['started_at'])
        else:
            s['elapsed_seconds'] = s['base_seconds']
        s['server_time'] = now
        return jsonify({'success': True, 'session': s, 'serverTime': now})
    finally:
        conn.close()


# ============================================================
# Premium productivity classifier — decides if the tab the user is on counts as
# studying. Hard rules catch obvious entertainment; everything ambiguous goes to
# the Groq model using the server-side keys (never exposed to the browser).
# ============================================================
ENTERTAINMENT_DOMAINS = (
    'youtube.com', 'youtu.be', 'netflix.com', 'tiktok.com', 'instagram.com',
    'twitch.tv', 'reddit.com', 'facebook.com', 'twitter.com', 'x.com',
    'primevideo.com', 'hulu.com', 'disneyplus.com', '9gag.com', 'pinterest.com',
    'snapchat.com', 'tumblr.com', 'roblox.com', 'discord.com', 'twitchgg.com',
    'crunchyroll.com', 'hbomax.com', 'max.com', 'espn.com',
)
CLEAR_STUDY_DOMAINS = (
    'wikipedia.org', 'khanacademy.org', 'coursera.org', 'edx.org',
    'docs.google.com', 'classroom.google.com', 'quizlet.com', 'brilliant.org',
    'wolframalpha.com', 'desmos.com', 'overleaf.com', 'scholar.google.com',
    'jstor.org', 'sparknotes.com', 'bbc.co.uk/bitesize', 'mathsisfun.com',
    'savemyexams.com', 'physicsclassroom.com', 'ck12.org', 'duolingo.com',
)
STUDY_HINT_WORDS = (
    'study', 'studying', 'revision', 'revise', 'lecture', 'tutorial', 'course',
    'lesson', 'exam', 'homework', 'essay', 'maths', 'math', 'algebra', 'calculus',
    'biology', 'chemistry', 'physics', 'history', 'geography', 'science',
    'vocabulary', 'grammar', 'practice problem', 'past paper', 'past papers',
    'flashcard', 'quiz', 'how to solve', 'theorem', 'documentation', 'reference',
)

def _domain_of(url):
    try:
        m = re.match(r'^[a-z]+://([^/]+)', (url or '').lower())
        host = m.group(1) if m else ''
        return host[4:] if host.startswith('www.') else host
    except Exception:
        return ''

def _has_study_hint(*texts):
    blob = ' '.join((t or '') for t in texts).lower()
    return any(w in blob for w in STUDY_HINT_WORDS)

def classify_productivity(url, title, text):
    """Return (verdict, reason). verdict ∈ productive | unproductive | uncertain."""
    domain = _domain_of(url)
    if not domain or domain.startswith('chrome') or domain in ('newtab', 'localhost'):
        return ('uncertain', 'Blank or system page')
    hint = _has_study_hint(title, text[:1200] if text else '')
    is_ent = any(domain == d or domain.endswith('.' + d) for d in ENTERTAINMENT_DOMAINS)
    is_study = any(d in (domain + url.lower()) for d in CLEAR_STUDY_DOMAINS)
    if is_ent and not hint:
        return ('unproductive', f'{domain} is usually a distraction')
    if is_study and not is_ent:
        return ('productive', f'{domain} is a study resource')
    # Ambiguous (incl. entertainment-with-study-hints) → ask the model.
    sys_prompt = (
        "You are a strict study-focus classifier inside VuliStudy. Given the web page a "
        "student currently has open, decide whether it is PRODUCTIVE for studying right now. "
        "Entertainment, social media, shopping, gaming and general browsing are UNPRODUCTIVE "
        "even on sites that can sometimes be educational, UNLESS the page clearly shows study "
        "content (e.g. a lecture, tutorial, course, or a specific subject). If you genuinely "
        "cannot tell, answer UNCERTAIN. "
        "Reply with EXACTLY one word on the first line — PRODUCTIVE, UNPRODUCTIVE or UNCERTAIN — "
        "then a second line with a short (max 12 word) reason. No other text."
    )
    user_prompt = (
        f"URL: {url}\nTitle: {title or '(none)'}\n"
        f"Visible text sample: {(text or '')[:900]}"
    )
    for api_key in [GROQ_API_ONE, GROQ_API_TWO]:
        if not api_key:
            continue
        try:
            out = call_ai(api_key, user_prompt, system_prompt=sys_prompt) or ''
            first = out.strip().splitlines()
            verdict_word = (first[0].strip().upper() if first else '')
            reason = (first[1].strip() if len(first) > 1 else '').strip()[:120]
            if 'UNPRODUCTIVE' in verdict_word:
                return ('unproductive', reason or 'Not study-related')
            if 'PRODUCTIVE' in verdict_word:
                return ('productive', reason or 'Looks study-related')
            return ('uncertain', reason or 'Could not tell')
        except Exception as e:
            print(f"[classify error]: {e}")
            continue
    # No key / all failed — let the user decide.
    return ('uncertain', 'Ask yourself: is this helping you study?')

@app.route('/classify-productivity', methods=['POST'])
def classify_productivity_route():
    data = request.get_json()
    conn = get_db()
    try:
        username = user_from_token(conn, data.get('token', ''))
        if not username:
            return jsonify({'success': False, 'error': 'auth'})
        user = conn.execute('SELECT is_premium FROM users WHERE username = ?', (username,)).fetchone()
        conn.commit()
        if not user or not user['is_premium']:
            return jsonify({'success': False, 'error': 'premium_required'})
    finally:
        conn.close()
    url = (data.get('url') or '')[:600]
    title = (data.get('title') or '')[:300]
    text = (data.get('text') or '')[:2000]
    verdict, reason = classify_productivity(url, title, text)
    return jsonify({'success': True, 'verdict': verdict, 'reason': reason})


# ============================================================
# Focus reports — VuliTab posts a per-session productivity breakdown; both the
# tab and (later) the app can read the history.
# ============================================================
@app.route('/focus-session-save', methods=['POST'])
def focus_session_save():
    data = request.get_json()
    conn = get_db()
    try:
        username = user_from_token(conn, data.get('token', ''))
        if not username:
            return jsonify({'success': False, 'error': 'auth'})
        prod = max(0, min(int(data.get('productiveSeconds', 0) or 0), 200000))
        dist = max(0, min(int(data.get('distractedSeconds', 0) or 0), 200000))
        neut = max(0, min(int(data.get('neutralSeconds', 0) or 0), 200000))
        started_at = int(data.get('startedAt', 0) or 0)
        ended_at = int(data.get('endedAt', int(time.time())) or int(time.time()))
        total = prod + dist + neut
        focus_score = round(100 * prod / total) if total > 0 else 0
        sites = data.get('sites', [])
        try:
            sites_json = json.dumps(sites)[:4000]
        except Exception:
            sites_json = '[]'
        now = int(time.time())
        conn.execute('''INSERT INTO focus_sessions
            (username, started_at, ended_at, productive_seconds, distracted_seconds,
             neutral_seconds, focus_score, sites_json, created_at)
            VALUES (?,?,?,?,?,?,?,?,?)''',
            (username, started_at, ended_at, prod, dist, neut, focus_score, sites_json, now))
        # Keep only the most recent 60 reports per user.
        conn.execute('''DELETE FROM focus_sessions WHERE username = ? AND id NOT IN
            (SELECT id FROM focus_sessions WHERE username = ? ORDER BY id DESC LIMIT 60)''',
            (username, username))
        conn.commit()
        return jsonify({'success': True, 'focusScore': focus_score})
    finally:
        conn.close()

@app.route('/focus-history', methods=['POST'])
def focus_history():
    data = request.get_json()
    conn = get_db()
    try:
        username = user_from_token(conn, data.get('token', ''))
        if not username:
            return jsonify({'success': False, 'error': 'auth'})
        conn.commit()
        rows = conn.execute('''SELECT id, started_at, ended_at, productive_seconds,
            distracted_seconds, neutral_seconds, focus_score, sites_json
            FROM focus_sessions WHERE username = ? ORDER BY id DESC LIMIT 30''',
            (username,)).fetchall()
        sessions = []
        for r in rows:
            d = dict(r)
            try:
                d['sites'] = json.loads(d.pop('sites_json') or '[]')
            except Exception:
                d['sites'] = []
            sessions.append(d)
        agg = conn.execute('''SELECT
            COALESCE(SUM(productive_seconds),0) AS prod,
            COALESCE(SUM(distracted_seconds),0) AS dist,
            COUNT(*) AS n
            FROM focus_sessions WHERE username = ?''', (username,)).fetchone()
        return jsonify({'success': True, 'sessions': sessions,
                        'totalProductive': agg['prod'], 'totalDistracted': agg['dist'],
                        'sessionCount': agg['n']})
    finally:
        conn.close()


if __name__ == '__main__':
    app.run()
