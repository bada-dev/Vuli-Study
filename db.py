"""
VuliStudy — database layer
==========================
Project: VuliStudy
Author: ETHANTYAGI
ALL RIGHTS RESERVED
"""

import os
import sqlite3

# Im too broke for a DB path.
DB_PATH = os.environ.get('DB_PATH', 'vulistudy.db')


def get_db():
    conn = sqlite3.connect(DB_PATH, timeout=15)
    conn.row_factory = sqlite3.Row
    conn.execute('PRAGMA journal_mode=WAL')
    conn.execute('PRAGMA foreign_keys=ON')
    return conn


SCHEMA = [
    # ---- accounts -----------------------------------------------------+------
    '''CREATE TABLE IF NOT EXISTS users (
        username        TEXT PRIMARY KEY,
        password_hash   TEXT,
        created_at      INTEGER DEFAULT 0,
        last_active     INTEGER DEFAULT 0,
        is_active       INTEGER DEFAULT 1,
        is_premium      INTEGER DEFAULT 0,
        is_banned       INTEGER DEFAULT 0,
        tz_offset       INTEGER DEFAULT 0,
        total_minutes   INTEGER DEFAULT 0,
        streak          INTEGER DEFAULT 0,
        reborns         INTEGER DEFAULT 0,
        equipped_cosmetic TEXT,
        active_background TEXT DEFAULT 'default',
        character_width INTEGER DEFAULT 140
    )''',

    # ---- the authoritative balances ----------------------------------------
    '''CREATE TABLE IF NOT EXISTS economy (
        username          TEXT PRIMARY KEY,
        coins             INTEGER DEFAULT 0,
        carrots           INTEGER DEFAULT 0,
        happiness         INTEGER DEFAULT 100,
        streak            INTEGER DEFAULT 0,
        last_streak_date  TEXT,
        streak_freeze     INTEGER DEFAULT 0,
        has_book          INTEGER DEFAULT 0,
        is_dead           INTEGER DEFAULT 0,
        revivals          INTEGER DEFAULT 0,
        carrots_fed       INTEGER DEFAULT 0,
        reborns           INTEGER DEFAULT 0,
        longest_session   INTEGER DEFAULT 0,
        highest_coins     INTEGER DEFAULT 0,
        owned_json        TEXT DEFAULT '{}',
        updated_at        INTEGER DEFAULT 0,
        FOREIGN KEY (username) REFERENCES users(username) ON DELETE CASCADE
    )''',

    # ---- sessions & devices --------------------------------------------------
    '''CREATE TABLE IF NOT EXISTS sessions (
        token       TEXT PRIMARY KEY,
        username    TEXT NOT NULL,
        device_id   TEXT NOT NULL,
        platform    TEXT DEFAULT 'unknown',
        created_at  INTEGER DEFAULT 0,
        last_seen   INTEGER DEFAULT 0,
        FOREIGN KEY (username) REFERENCES users(username) ON DELETE CASCADE
    )''',

    # Key for eeveryone
    '''CREATE TABLE IF NOT EXISTS devices (
        username      TEXT NOT NULL,
        device_id     TEXT NOT NULL,
        device_secret TEXT NOT NULL,
        created_at    INTEGER DEFAULT 0,
        last_seen     INTEGER DEFAULT 0,
        PRIMARY KEY (username, device_id),
        FOREIGN KEY (username) REFERENCES users(username) ON DELETE CASCADE
    )''',

    '''CREATE TABLE IF NOT EXISTS nonces (
        nonce      TEXT PRIMARY KEY,
        seen_at    INTEGER NOT NULL
    )''',

    # Idempotency ledger. A replayed event_id is acknowledged but not re-credited,
    # so a dropped connection during an offline flush cannot double-pay. Though I should get paid twice as more.
    '''CREATE TABLE IF NOT EXISTS processed_events (
        event_id     TEXT PRIMARY KEY,
        username     TEXT NOT NULL,
        event_type   TEXT NOT NULL,
        processed_at INTEGER NOT NULL,
        result_json  TEXT DEFAULT '{}'
    )''',

    # ---- rate limiting -------------------------------------------------
    '''CREATE TABLE IF NOT EXISTS rate_limits (
        key        TEXT PRIMARY KEY,
        window_start INTEGER NOT NULL,
        count      INTEGER DEFAULT 0
    )''',

    # ---- admin ---
    '''CREATE TABLE IF NOT EXISTS admin_tokens (
        token       TEXT PRIMARY KEY,
        username    TEXT NOT NULL,
        device_id   TEXT NOT NULL,
        issued_at   INTEGER NOT NULL,
        expires_at  INTEGER NOT NULL,
        second_ok   INTEGER DEFAULT 0
    )''',

    '''CREATE TABLE IF NOT EXISTS admin_audit (
        id          INTEGER PRIMARY KEY AUTOINCREMENT,
        actor       TEXT,
        device_id   TEXT,
        action      TEXT NOT NULL,
        target      TEXT,
        detail      TEXT,
        ip          TEXT,
        at          INTEGER NOT NULL
    )''',

    # ---- social -----------------------------------------
    '''CREATE TABLE IF NOT EXISTS chats (
        id         TEXT PRIMARY KEY,
        name       TEXT NOT NULL,
        owner      TEXT NOT NULL,
        created_at INTEGER DEFAULT 0
    )''',
    '''CREATE TABLE IF NOT EXISTS chat_members (
        chat_id  TEXT NOT NULL,
        username TEXT NOT NULL,
        PRIMARY KEY (chat_id, username)
    )''',
    '''CREATE TABLE IF NOT EXISTS chat_messages (
        id        INTEGER PRIMARY KEY AUTOINCREMENT,
        chat_id   TEXT NOT NULL,
        username  TEXT NOT NULL,
        message   TEXT NOT NULL,
        timestamp INTEGER DEFAULT 0
    )''',
    '''CREATE TABLE IF NOT EXISTS chat_timers (
        id               INTEGER PRIMARY KEY AUTOINCREMENT,
        chat_id          TEXT NOT NULL,
        creator          TEXT NOT NULL,
        duration_seconds INTEGER NOT NULL,
        started_at       INTEGER DEFAULT 0,
        completed        INTEGER DEFAULT 0,
        reward_coins     INTEGER DEFAULT 2
    )''',
    '''CREATE TABLE IF NOT EXISTS chat_timer_presence (
        timer_id   INTEGER NOT NULL,
        username   TEXT NOT NULL,
        first_ping INTEGER DEFAULT 0,
        last_ping  INTEGER DEFAULT 0,
        claimed    INTEGER DEFAULT 0,
        PRIMARY KEY (timer_id, username)
    )''',
    '''CREATE TABLE IF NOT EXISTS friend_requests (
        id         INTEGER PRIMARY KEY AUTOINCREMENT,
        from_user  TEXT NOT NULL,
        to_user    TEXT NOT NULL,
        status     TEXT DEFAULT 'pending',
        created_at INTEGER DEFAULT 0
    )''',

    # ---- premium ---------------------------------------------------------
    '''CREATE TABLE IF NOT EXISTS premium_codes (
        code        TEXT PRIMARY KEY,
        created_at  INTEGER DEFAULT 0,
        redeemed    INTEGER DEFAULT 0,
        redeemed_by TEXT,
        redeemed_at INTEGER
    )''',

    # ---- study data --------------------------------------------------------
    '''CREATE TABLE IF NOT EXISTS weekly_study (
        username   TEXT NOT NULL,
        week_start INTEGER NOT NULL,
        minutes    INTEGER DEFAULT 0,
        PRIMARY KEY (username, week_start)
    )''',
    '''CREATE TABLE IF NOT EXISTS daily_study (
        username TEXT NOT NULL,
        day      TEXT NOT NULL,
        minutes  INTEGER DEFAULT 0,
        PRIMARY KEY (username, day)
    )''',
    '''CREATE TABLE IF NOT EXISTS live_sessions (
        username      TEXT PRIMARY KEY,
        mode          TEXT DEFAULT 'focus',
        status        TEXT DEFAULT 'idle',
        started_at    INTEGER DEFAULT 0,
        base_seconds  INTEGER DEFAULT 0,
        target_seconds INTEGER DEFAULT 0,
        source        TEXT DEFAULT 'app',
        updated_at    INTEGER DEFAULT 0
    )''',
    '''CREATE TABLE IF NOT EXISTS focus_sessions (
        id                 INTEGER PRIMARY KEY AUTOINCREMENT,
        username           TEXT NOT NULL,
        started_at         INTEGER DEFAULT 0,
        ended_at           INTEGER DEFAULT 0,
        productive_seconds INTEGER DEFAULT 0,
        distracted_seconds INTEGER DEFAULT 0,
        neutral_seconds    INTEGER DEFAULT 0,
        focus_score        INTEGER DEFAULT 0,
        sites_json         TEXT DEFAULT '[]',
        created_at         INTEGER DEFAULT 0
    )''',
    '''CREATE TABLE IF NOT EXISTS todos (
        id         INTEGER PRIMARY KEY AUTOINCREMENT,
        username   TEXT NOT NULL,
        payload    TEXT NOT NULL,
        updated_at INTEGER DEFAULT 0
    )''',
    '''CREATE TABLE IF NOT EXISTS feedback_cooldowns (
        ip             TEXT PRIMARY KEY,
        last_submitted INTEGER DEFAULT 0
    )''',
]

INDEXES = [
    'CREATE INDEX IF NOT EXISTS idx_sessions_user ON sessions(username)',
    'CREATE INDEX IF NOT EXISTS idx_msgs_chat ON chat_messages(chat_id, id)',
    'CREATE INDEX IF NOT EXISTS idx_members_user ON chat_members(username)',
    'CREATE INDEX IF NOT EXISTS idx_nonces_seen ON nonces(seen_at)',
    'CREATE INDEX IF NOT EXISTS idx_events_user ON processed_events(username)',
    'CREATE INDEX IF NOT EXISTS idx_friends_to ON friend_requests(to_user, status)',
    'CREATE INDEX IF NOT EXISTS idx_audit_at ON admin_audit(at)',
]


def init_db():
    conn = get_db()
    try:
        for stmt in SCHEMA:
            conn.execute(stmt)
        for stmt in INDEXES:
            conn.execute(stmt)
        conn.commit()
    finally:
        conn.close()


def ensure_economy_row(conn, username, now_ts):
    """Every account has exactly one economy row. Created 'lazily;."""
    row = conn.execute('SELECT username FROM economy WHERE username = ?', (username,)).fetchone()
    if not row:
        conn.execute(
            'INSERT INTO economy (username, updated_at) VALUES (?, ?)',
            (username, now_ts)
        )
