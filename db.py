"""
VuliStudy — database layer
==========================
Project: VuliStudy
Author: ETHANTYAGI
ALL RIGHTS RESERVED

Postgres, not SQLite any more. An entirely new language.
"""

import os
import re
import threading

import psycopg2
import psycopg2.extras
import psycopg2.pool

# Im too broke for a DB path. (Still true — a Render disk costs money, which is
# exactly why the database lives in Supabase now instead of on the filesystem.)
#
# Supabase gives you two connection strings. Use the POOLER one (port 6543,
# "Transaction" mode) — a web app opens and closes connections constantly and
# the direct connection limit is low.
DATABASE_URL = os.environ.get('DATABASE_URL')

_pool = None
_pool_lock = threading.Lock()

# psycopg2's pool raises the moment it runs out of connections rather than
# waiting for one, which under load turns ordinary traffic into 500s. This
# semaphore makes a caller queue for a slot instead — the request takes slightly
# longer, but it succeeds.
_POOL_MAX = int(os.environ.get('DB_POOL_MAX', '8'))
_slots = threading.BoundedSemaphore(_POOL_MAX)
_SLOT_TIMEOUT = 15          # seconds to wait for a free connection


def _make_pool():
    if not DATABASE_URL:
        raise RuntimeError(
            'DATABASE_URL is not set. The app has no database to talk to. '
            'Copy the Supabase connection pooler URL into Render.'
        )
    kwargs = {
        'minconn': 1,
        'maxconn': _POOL_MAX,
        'dsn': DATABASE_URL,
        'connect_timeout': 10,
    }
    # Supabase requires TLS, so that is the default. A local Postgres used for
    # testing usually has no TLS at all, hence the override — and if the URL
    # already names an sslmode, that wins.
    if 'sslmode' not in DATABASE_URL:
        kwargs['sslmode'] = os.environ.get('DB_SSLMODE', 'require')
    return psycopg2.pool.ThreadedConnectionPool(**kwargs)


def _get_pool():
    global _pool
    if _pool is None:
        with _pool_lock:
            if _pool is None:
                _pool = _make_pool()
    return _pool


# ---------------------------------------------------------------------------
# SQL translation
#
# Every query in this project was written for SQLite. Rather than rewrite them
# all — and risk fumbling one — the differences are handled in one place.
# ---------------------------------------------------------------------------
_PLACEHOLDER_RE = re.compile(r"""
    (                       # group 1: anything we must NOT touch
      '(?:[^']|'')*'        #   single-quoted string literal
      | "(?:[^"]|"")*"      #   double-quoted identifier
    )
    | \?                    # group 0 fallback: a placeholder to convert
""", re.VERBOSE)


def _translate(sql):
    """SQLite `?` placeholders become Postgres `%s`, leaving literals alone."""
    def sub(m):
        if m.group(1) is not None:
            return m.group(1)       # a quoted literal — hands off
        return '%s'
    return _PLACEHOLDER_RE.sub(sub, sql)


class _Connection:
    """
    Thin wrapper so the rest of the app keeps calling conn.execute(...).

    sqlite3 lets you call .execute() straight on the connection and gives back
    a cursor. psycopg2 doesn't, so this restores that shape.
    """

    __slots__ = ('_raw', '_closed')

    def __init__(self, raw):
        self._raw = raw
        self._closed = False

    def execute(self, sql, params=None):
        cur = self._raw.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        # Passing None (rather than an empty tuple) tells psycopg2 to skip
        # parameter interpolation entirely, so a literal % in the SQL is safe.
        cur.execute(_translate(sql), params if params else None)
        return cur

    def commit(self):
        self._raw.commit()

    def rollback(self):
        self._raw.rollback()

    def close(self):
        if self._closed:
            return
        self._closed = True
        try:
            # Never hand a connection back mid-transaction; the next borrower
            # would inherit it.
            self._raw.rollback()
        except Exception:
            pass
        try:
            _get_pool().putconn(self._raw)
        except Exception:
            try:
                self._raw.close()
            except Exception:
                pass
        finally:
            # Always give the slot back, even if returning the connection failed,
            # or the pool would slowly starve itself.
            try:
                _slots.release()
            except ValueError:
                pass    # already released; never let this mask a real error

    # Let `with get_db() as conn:` work too, for anything written later.
    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        if exc_type:
            self.rollback()
        self.close()
        return False


def get_db():
    # Wait for a free slot rather than failing instantly when busy.
    if not _slots.acquire(timeout=_SLOT_TIMEOUT):
        raise RuntimeError('Database is busy — no free connection after '
                           f'{_SLOT_TIMEOUT}s')
    try:
        raw = _get_pool().getconn()
    except Exception:
        _slots.release()
        raise

    # A connection can go stale (Supabase closes idle ones, deploys drop them).
    # Prove it works before handing it out; replace it if not.
    try:
        cur = raw.cursor()
        cur.execute('SELECT 1')
        cur.close()
        raw.rollback()
    except Exception:
        try:
            _get_pool().putconn(raw, close=True)
        except Exception:
            pass
        try:
            raw = _get_pool().getconn()
        except Exception:
            _slots.release()
            raise

    return _Connection(raw)


# ---------------------------------------------------------------------------
# Schema
#
# Written natively for Postgres rather than translated. INTEGER columns are kept
# where SQLite used them (including the 0/1 flags) so every `bool(row['x'])` and
# comparison in the app behaves exactly as before.
# ---------------------------------------------------------------------------
SCHEMA = [
    # ---- accounts -----------------------------------------------------------
    '''CREATE TABLE IF NOT EXISTS users (
        username        TEXT PRIMARY KEY,
        password_hash   TEXT,
        created_at      BIGINT DEFAULT 0,
        last_active     BIGINT DEFAULT 0,
        is_active       INTEGER DEFAULT 1,
        is_premium      INTEGER DEFAULT 0,
        is_banned       INTEGER DEFAULT 0,
        tz_offset       INTEGER DEFAULT 0,
        total_minutes   INTEGER DEFAULT 0,
        streak          INTEGER DEFAULT 0,
        reborns         INTEGER DEFAULT 0,
        equipped_cosmetic TEXT,
        active_background TEXT DEFAULT 'default',
        character_width INTEGER DEFAULT 140,
        avatar          TEXT,
        avatar_updated  BIGINT DEFAULT 0
    )''',

    # ---- the authoritative balances ----------------------------------------
    '''CREATE TABLE IF NOT EXISTS economy (
        username          TEXT PRIMARY KEY REFERENCES users(username) ON DELETE CASCADE,
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
        updated_at        BIGINT DEFAULT 0
    )''',

    # ---- sessions & devices -------------------------------------------------
    '''CREATE TABLE IF NOT EXISTS sessions (
        token       TEXT PRIMARY KEY,
        username    TEXT NOT NULL REFERENCES users(username) ON DELETE CASCADE,
        device_id   TEXT NOT NULL,
        platform    TEXT DEFAULT 'unknown',
        created_at  BIGINT DEFAULT 0,
        last_seen   BIGINT DEFAULT 0
    )''',

    # Key for eeveryone
    '''CREATE TABLE IF NOT EXISTS devices (
        username      TEXT NOT NULL REFERENCES users(username) ON DELETE CASCADE,
        device_id     TEXT NOT NULL,
        device_secret TEXT NOT NULL,
        created_at    BIGINT DEFAULT 0,
        last_seen     BIGINT DEFAULT 0,
        PRIMARY KEY (username, device_id)
    )''',

    '''CREATE TABLE IF NOT EXISTS nonces (
        nonce      TEXT PRIMARY KEY,
        seen_at    BIGINT NOT NULL
    )''',

    # Idempotency ledger. A replayed event_id is acknowledged but not re-credited,
    # so a dropped connection during an offline flush cannot double-pay. Though I should get paid twice as more.
    '''CREATE TABLE IF NOT EXISTS processed_events (
        event_id     TEXT PRIMARY KEY,
        username     TEXT NOT NULL,
        event_type   TEXT NOT NULL,
        processed_at BIGINT NOT NULL,
        result_json  TEXT DEFAULT '{}'
    )''',

    # ---- rate limiting -------------------------------------------------
    '''CREATE TABLE IF NOT EXISTS rate_limits (
        key          TEXT PRIMARY KEY,
        window_start BIGINT NOT NULL,
        count        INTEGER DEFAULT 0
    )''',

    # ---- admin ---
    '''CREATE TABLE IF NOT EXISTS admin_tokens (
        token       TEXT PRIMARY KEY,
        username    TEXT NOT NULL,
        device_id   TEXT NOT NULL,
        issued_at   BIGINT NOT NULL,
        expires_at  BIGINT NOT NULL,
        second_ok   INTEGER DEFAULT 0
    )''',

    '''CREATE TABLE IF NOT EXISTS admin_audit (
        id          BIGSERIAL PRIMARY KEY,
        actor       TEXT,
        device_id   TEXT,
        action      TEXT NOT NULL,
        target      TEXT,
        detail      TEXT,
        ip          TEXT,
        at          BIGINT NOT NULL
    )''',

    # ---- social -----------------------------------------
    '''CREATE TABLE IF NOT EXISTS chats (
        id         TEXT PRIMARY KEY,
        name       TEXT NOT NULL,
        owner      TEXT NOT NULL,
        created_at BIGINT DEFAULT 0
    )''',
    '''CREATE TABLE IF NOT EXISTS chat_members (
        chat_id  TEXT NOT NULL,
        username TEXT NOT NULL,
        PRIMARY KEY (chat_id, username)
    )''',
    '''CREATE TABLE IF NOT EXISTS chat_messages (
        id        BIGSERIAL PRIMARY KEY,
        chat_id   TEXT NOT NULL,
        username  TEXT NOT NULL,
        message   TEXT NOT NULL,
        timestamp BIGINT DEFAULT 0
    )''',
    '''CREATE TABLE IF NOT EXISTS chat_timers (
        id               BIGSERIAL PRIMARY KEY,
        chat_id          TEXT NOT NULL,
        creator          TEXT NOT NULL,
        duration_seconds INTEGER NOT NULL,
        started_at       BIGINT DEFAULT 0,
        completed        INTEGER DEFAULT 0,
        reward_coins     INTEGER DEFAULT 2
    )''',
    '''CREATE TABLE IF NOT EXISTS chat_timer_presence (
        timer_id   BIGINT NOT NULL,
        username   TEXT NOT NULL,
        first_ping BIGINT DEFAULT 0,
        last_ping  BIGINT DEFAULT 0,
        claimed    INTEGER DEFAULT 0,
        PRIMARY KEY (timer_id, username)
    )''',
    '''CREATE TABLE IF NOT EXISTS friend_requests (
        id         BIGSERIAL PRIMARY KEY,
        from_user  TEXT NOT NULL,
        to_user    TEXT NOT NULL,
        status     TEXT DEFAULT 'pending',
        created_at BIGINT DEFAULT 0
    )''',

    # ---- premium ---------------------------------------------------------
    '''CREATE TABLE IF NOT EXISTS premium_codes (
        code        TEXT PRIMARY KEY,
        created_at  BIGINT DEFAULT 0,
        redeemed    INTEGER DEFAULT 0,
        redeemed_by TEXT,
        redeemed_at BIGINT
    )''',

    # ---- study data --------------------------------------------------------
    '''CREATE TABLE IF NOT EXISTS weekly_study (
        username   TEXT NOT NULL,
        week_start BIGINT NOT NULL,
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
        username       TEXT PRIMARY KEY,
        mode           TEXT DEFAULT 'focus',
        status         TEXT DEFAULT 'idle',
        started_at     BIGINT DEFAULT 0,
        base_seconds   INTEGER DEFAULT 0,
        target_seconds INTEGER DEFAULT 0,
        source         TEXT DEFAULT 'app',
        updated_at     BIGINT DEFAULT 0
    )''',
    '''CREATE TABLE IF NOT EXISTS focus_sessions (
        id                 BIGSERIAL PRIMARY KEY,
        username           TEXT NOT NULL,
        started_at         BIGINT DEFAULT 0,
        ended_at           BIGINT DEFAULT 0,
        productive_seconds INTEGER DEFAULT 0,
        distracted_seconds INTEGER DEFAULT 0,
        neutral_seconds    INTEGER DEFAULT 0,
        focus_score        INTEGER DEFAULT 0,
        sites_json         TEXT DEFAULT '[]',
        created_at         BIGINT DEFAULT 0
    )''',
    '''CREATE TABLE IF NOT EXISTS todos (
        id         BIGSERIAL PRIMARY KEY,
        username   TEXT NOT NULL,
        payload    TEXT NOT NULL,
        updated_at BIGINT DEFAULT 0
    )''',
    '''CREATE TABLE IF NOT EXISTS feedback_cooldowns (
        ip             TEXT PRIMARY KEY,
        last_submitted BIGINT DEFAULT 0
    )''',
]

INDEXES = [
    'CREATE INDEX IF NOT EXISTS idx_sessions_user ON sessions(username)',
    'CREATE INDEX IF NOT EXISTS idx_msgs_chat ON chat_messages(chat_id, id)',
    'CREATE INDEX IF NOT EXISTS idx_members_user ON chat_members(username)',
    'CREATE INDEX IF NOT EXISTS idx_nonces_seen ON nonces(seen_at)',
    'CREATE INDEX IF NOT EXISTS idx_events_user ON processed_events(username)',
    'CREATE INDEX IF NOT EXISTS idx_events_type_time ON processed_events(username, event_type, processed_at)',
    'CREATE INDEX IF NOT EXISTS idx_friends_to ON friend_requests(to_user, status)',
    'CREATE INDEX IF NOT EXISTS idx_audit_at ON admin_audit(at)',
    'CREATE INDEX IF NOT EXISTS idx_users_active ON users(is_active, total_minutes)',
]

# Columns added after the first Postgres deploy. Adding a column to a live table
# must never take the app down, so each one is attempted and ignored if present.
MIGRATIONS = [
    "ALTER TABLE users ADD COLUMN IF NOT EXISTS avatar TEXT",
    "ALTER TABLE users ADD COLUMN IF NOT EXISTS avatar_updated BIGINT DEFAULT 0",
]


def init_db():
    conn = get_db()
    try:
        for stmt in SCHEMA:
            conn.execute(stmt)
        for stmt in INDEXES:
            conn.execute(stmt)
        for stmt in MIGRATIONS:
            try:
                conn.execute(stmt)
            except Exception:
                conn.rollback()
        conn.commit()
    finally:
        conn.close()


def ensure_economy_row(conn, username, now_ts):
    """Every account has exactly one economy row. Created lazily, never by a client."""
    row = conn.execute('SELECT username FROM economy WHERE username = ?', (username,)).fetchone()
    if not row:
        conn.execute(
            'INSERT INTO economy (username, updated_at) VALUES (?, ?)'
            ' ON CONFLICT (username) DO NOTHING',
            (username, now_ts)
        )


def table_columns(conn, table):
    """Column names for a table — replaces SQLite's PRAGMA table_info."""
    rows = conn.execute(
        'SELECT column_name FROM information_schema.columns'
        ' WHERE table_schema = ? AND table_name = ?'
        ' ORDER BY ordinal_position',
        ('public', table)
    ).fetchall()
    return [r['column_name'] for r in rows]


def list_tables(conn):
    """User tables — replaces SQLite's sqlite_master query."""
    rows = conn.execute(
        "SELECT table_name FROM information_schema.tables"
        " WHERE table_schema = ? AND table_type = 'BASE TABLE'"
        " ORDER BY table_name",
        ('public',)
    ).fetchall()
    return [r['table_name'] for r in rows]
