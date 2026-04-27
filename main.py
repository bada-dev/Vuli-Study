import os
import sqlite3
import time
from flask import Flask, render_template, request, jsonify

app = Flask(__name__)
ADMIN_PASSWORD = os.environ.get('ADMIN_PASSWORD')

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
    conn.commit()
    conn.close()

init_db()

FEEDBACK_COOLDOWN = 48 * 60 * 60
SYNC_COOLDOWN = 30
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
        total_minutes = min(int(data.get('totalMinutes', 0)), MAX_MINUTES)
        streak = min(int(data.get('streak', 0)), MAX_STREAK)
        reborns = min(int(data.get('reborns', 0)), MAX_REBORNS)
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
        conn.commit()
        return jsonify({'success': True})
    finally:
        conn.close()

@app.route('/leaderboard')
def leaderboard():
    conn = get_db()
    three_days_ago = int(time.time()) - (3 * 24 * 60 * 60)
    conn.execute('UPDATE users SET is_active=0 WHERE last_active < ? AND last_active > 0',
                 (three_days_ago,))
    conn.commit()
    users = conn.execute(
        '''SELECT username, total_minutes, streak, reborns,
           equipped_cosmetic, active_background, character_width, happiness,
           last_active, is_active
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
