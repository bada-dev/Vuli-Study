"""
VuliStudy — authentication and request integrity
================================================
Project: VuliStudy
Author: ETHANTYAGI
ALL RIGHTS RESERVED

please dont hack ;-;
An entire folder dedicated to prevent getting carrots without studies!
"""

import hashlib
import hmac
import os
import secrets
import time
from functools import wraps

from flask import request, jsonify, g

from db import get_db

# ---------------------------------------------------------------------------
# Get render environemnt words
# ---------------------------------------------------------------------------
ADMIN_PASSWORD = os.environ.get('ADMIN_PASSWORD')
SECOND_ADMIN_PASSWORD = os.environ.get('SECOND_ADMIN_PASSWORD')
WEB_PASSWORD = os.environ.get('WEB_PASSWORD')

PBKDF2_ITERATIONS = 200_000
TOKEN_TTL = 365 * 24 * 60 * 60
# An hour, not 15 minutes. It is bound to one account on one device, needs the
# password to obtain, and every action it authorises is audited — so a short
# expiry bought very little and mostly meant the panel silently stopped
# working mid-use, which reads as a bug rather than as security.
ADMIN_TOKEN_TTL = 60 * 60
MAX_CLOCK_SKEW = 120          # seconds
NONCE_RETENTION = 600         # seconds; must exceed MAX_CLOCK_SKEW comfortably
NONCE_PRUNE_EVERY = 97        # prune on ~1 request in 97, not on every one
MAX_BODY_BYTES = 256 * 1024


# ---------------------------------------------------------------------------
# Lots of Passwords
# ---------------------------------------------------------------------------
def hash_password(password):
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
    if not password or len(password) < 6:
        return 'Password must be at least 6 characters'
    if len(password) > 128:
        return 'Password too long'
    return None


# ---------------------------------------------------------------------------
# Tokens and devices
# ---------------------------------------------------------------------------
def issue_token(conn, username, device_id, platform='app'):
    token = secrets.token_hex(32)
    now = int(time.time())
    conn.execute(
        'INSERT INTO sessions (token, username, device_id, platform, created_at, last_seen)'
        ' VALUES (?,?,?,?,?,?)',
        (token, username, device_id, str(platform)[:20], now, now))
    return token


def issue_device_secret(conn, username, device_id):
    """
    One secret per (account, device). Re-issued on each login so a lost phone's
    secret stops working as soon as the owner logs in again anywhere.
    """
    secret = secrets.token_hex(32)
    now = int(time.time())
    conn.execute(
        'INSERT INTO devices (username, device_id, device_secret, created_at, last_seen)'
        ' VALUES (?,?,?,?,?)'
        ' ON CONFLICT(username, device_id) DO UPDATE SET device_secret=excluded.device_secret,'
        ' last_seen=excluded.last_seen',
        (username, device_id, secret, now, now))
    return secret


def _signed_path():
    """
    The path the client signed, query string included, so nobody can rewrite
    ?period=weekly in flight. request.path drops the query, which once made
    every request carrying one fail as a forgery — that was the leaderboard,
    and only the leaderboard, which is why it never loaded.
    """
    path = request.path
    if request.query_string:
        path += '?' + request.query_string.decode('utf-8', 'ignore')
    return path


def app_auth(f):
    """
    Every authenticated app endpoint wears this.

    On success it sets g.username / g.device_id. Handlers must use those and
    never a username taken from the request body — that was the original hole,
    where any client could act as any account simply by naming it.

    PERFORMANCE. This used to issue eight separate statements: look up the
    session, look up the user, look up the device secret, insert the nonce,
    prune old nonces, touch two last_seen columns, commit. Against a hosted
    Postgres that was roughly two seconds of pure network round-trips on every
    single call, and it was why the app felt broken rather than merely slow.

    It is now two: one read that joins all three tables, and one write that
    does the nonce and both timestamps in a single statement. Nothing about
    what is *checked* has been relaxed — the same facts are verified, they are
    simply fetched together instead of one at a time.
    """
    @wraps(f)
    def wrapper(*args, **kwargs):
        now = int(time.time())
        token = request.headers.get('X-Vuli-Token', '')
        device_id = request.headers.get('X-Vuli-Device', '')
        ts = request.headers.get('X-Vuli-Timestamp', '')
        nonce = request.headers.get('X-Vuli-Nonce', '')
        sig = request.headers.get('X-Vuli-Sign', '')

        if not token or not device_id:
            return jsonify({'success': False, 'error': 'auth_required'}), 401
        if not (ts and nonce and sig):
            return jsonify({'success': False, 'error': 'signature_missing'}), 401

        # Cheap checks first — no reason to touch the database to reject a
        # request whose own timestamp is nonsense.
        try:
            ts_int = int(ts)
        except ValueError:
            return jsonify({'success': False, 'error': 'bad_timestamp'}), 401
        if abs(now - ts_int) > MAX_CLOCK_SKEW:
            return jsonify({'success': False, 'error': 'stale_request'}), 401

        body_bytes = request.get_data(cache=True)
        if len(body_bytes) > MAX_BODY_BYTES:
            return jsonify({'success': False, 'error': 'body_too_large'}), 413

        conn = get_db()

        # ---- round trip 1: everything the check needs, in one go -----------
        row = conn.execute(
            '''SELECT s.username        AS username,
                      s.device_id       AS session_device,
                      s.last_seen       AS last_seen,
                      u.is_banned       AS is_banned,
                      u.tz_offset       AS tz_offset,
                      d.device_secret   AS device_secret
               FROM sessions s
               JOIN users u   ON u.username = s.username
               LEFT JOIN devices d
                      ON d.username = s.username AND d.device_id = ?
               WHERE s.token = ?''',
            (device_id, token)).fetchone()

        if not row:
            return jsonify({'success': False, 'error': 'invalid_token'}), 401
        if now - row['last_seen'] > TOKEN_TTL:
            conn.execute('DELETE FROM sessions WHERE token=?', (token,))
            conn.commit()
            return jsonify({'success': False, 'error': 'token_expired'}), 401
        if row['session_device'] != device_id:
            return jsonify({'success': False, 'error': 'device_mismatch'}), 401
        if row['is_banned']:
            return jsonify({'success': False, 'error': 'account_disabled'}), 403
        if not row['device_secret']:
            return jsonify({'success': False, 'error': 'unknown_device'}), 401

        username = row['username']

        # ---- signature: pure CPU, no database ------------------------------
        body_hash = hashlib.sha256(body_bytes or b'').hexdigest()
        canonical = '\n'.join([request.method, _signed_path(), ts, nonce, body_hash])
        expected = hmac.new(bytes.fromhex(row['device_secret']),
                            canonical.encode('utf-8'), hashlib.sha256).hexdigest()
        if not hmac.compare_digest(expected, sig):
            return jsonify({'success': False, 'error': 'bad_signature'}), 401

        # ---- round trip 2: burn the nonce and touch both last_seen ---------
        # A CTE lets one statement do all three writes. The INSERT is the
        # gatekeeper: if this nonce has been seen the primary key rejects it,
        # which is exactly the replay we want to catch.
        try:
            conn.execute(
                '''WITH burn AS (
                       INSERT INTO nonces (nonce, seen_at) VALUES (?, ?)
                       RETURNING nonce
                   ), touch_session AS (
                       UPDATE sessions SET last_seen = ? WHERE token = ?
                   )
                   UPDATE devices SET last_seen = ?
                    WHERE username = ? AND device_id = ?''',
                (nonce, now, now, token, now, username, device_id))
        except Exception:
            conn.rollback()
            return jsonify({'success': False, 'error': 'replayed_request'}), 401

        # Housekeeping, occasionally. Pruning on every request added a round
        # trip to pay for a table that only ever holds a few minutes of rows.
        if not (now % NONCE_PRUNE_EVERY):
            try:
                conn.execute('DELETE FROM nonces WHERE seen_at < ?',
                             (now - NONCE_RETENTION,))
            except Exception:
                pass

        conn.commit()

        g.username = username
        g.device_id = device_id
        g.now = now
        g.tz_offset = row['tz_offset'] or 0    # already here; saves a lookup

        return f(*args, **kwargs)
    return wrapper


# ---------------------------------------------------------------------------
# Admin
# ---------------------------------------------------------------------------
def issue_admin_token(conn, username, device_id, now):
    token = secrets.token_hex(32)
    conn.execute('DELETE FROM admin_tokens WHERE expires_at < ?', (now,))
    conn.execute(
        'INSERT INTO admin_tokens (token, username, device_id, issued_at, expires_at, second_ok)'
        ' VALUES (?,?,?,?,?,0)',
        (token, username, device_id, now, now + ADMIN_TOKEN_TTL))
    return token


def admin_auth(require_second=False):
    """
    Server-side admin enforcement.

    This is the fix for "you can open the admin panel by editing the page". The
    panel being visible is now irrelevant — the button behind it calls an endpoint
    wearing this decorator, and without a live admin token issued by the server to
    this exact account and device, it returns 403.
    """
    def decorator(f):
        @wraps(f)
        @app_auth
        def wrapper(*args, **kwargs):
            now = int(time.time())
            admin_token = request.headers.get('X-Vuli-Admin', '')
            if not admin_token:
                return jsonify({'success': False, 'error': 'admin_required'}), 403

            conn = get_db()
            try:
                row = conn.execute('SELECT * FROM admin_tokens WHERE token=?',
                                   (admin_token,)).fetchone()
                if not row:
                    return jsonify({'success': False, 'error': 'admin_required'}), 403
                if row['expires_at'] < now:
                    conn.execute('DELETE FROM admin_tokens WHERE token=?', (admin_token,))
                    conn.commit()
                    return jsonify({'success': False, 'error': 'admin_expired'}), 403
                # Bound to whoever unlocked it, on the device they unlocked it from.
                if row['username'] != g.username or row['device_id'] != g.device_id:
                    return jsonify({'success': False, 'error': 'admin_bound_elsewhere'}), 403
                if require_second and not row['second_ok']:
                    return jsonify({'success': False, 'error': 'second_password_required'}), 403

                g.admin_token = admin_token
            finally:
                conn.close()

            return f(*args, **kwargs)
        return wrapper
    return decorator


def audit(conn, action, target=None, detail=None):
    conn.execute(
        'INSERT INTO admin_audit (actor, device_id, action, target, detail, ip, at)'
        ' VALUES (?,?,?,?,?,?,?)',
        (getattr(g, 'username', None), getattr(g, 'device_id', None), action,
         target, detail, request.headers.get('X-Forwarded-For', request.remote_addr),
         int(time.time())))


# ---------------------------------------------------------------------------
# Generic rate limiting
# ---------------------------------------------------------------------------
def rate_limit(conn, key, limit, window):
    """Fixed-window counter. Returns True if the caller is over the limit."""
    now = int(time.time())
    row = conn.execute('SELECT window_start, count FROM rate_limits WHERE key=?',
                       (key,)).fetchone()
    if not row or now - row['window_start'] >= window:
        conn.execute(
            'INSERT INTO rate_limits (key, window_start, count) VALUES (?,?,1)'
            ' ON CONFLICT(key) DO UPDATE SET window_start=excluded.window_start, count=1',
            (key, now))
        return False
    if row['count'] >= limit:
        return True
    conn.execute('UPDATE rate_limits SET count = count + 1 WHERE key=?', (key,))
    return False


def client_ip():
    """The address our own proxy observed — NOT the one the caller claims.

    X-Forwarded-For is "client, proxy1, proxy2...", and the left-hand entries
    are written by whoever sent the request. Reading the leftmost value meant a
    caller could put anything there and get a brand new identity for every
    request, which quietly defeated every per-IP limit in the app: signup
    (5/hour), login (30/15min) and VuliTab login. Sending
    `X-Forwarded-For: <random>` on each attempt made them all unlimited.

    Render appends the address it actually saw, so the RIGHTMOST entry is the
    one written by infrastructure we control. A caller can prepend as many fake
    hops as they like and it changes nothing.
    """
    fwd = request.headers.get('X-Forwarded-For', '')
    if fwd:
        hops = [h.strip() for h in fwd.split(',') if h.strip()]
        if hops:
            return hops[-1]
    return request.remote_addr or 'unknown'
