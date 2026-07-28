"""
VuliStudy — authentication and request integrity
================================================
Project: VuliStudy
Author: ETHANTYAGI
ALL RIGHTS RESERVED

please dont hack ;-;
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
ADMIN_TOKEN_TTL = 15 * 60
MAX_CLOCK_SKEW = 120          # seconds
NONCE_RETENTION = 600         # seconds; must exceed MAX_CLOCK_SKEW comfortably
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


def _prune_nonces(conn, now):
    conn.execute('DELETE FROM nonces WHERE seen_at < ?', (now - NONCE_RETENTION,))


def _verify_signature(conn, username, device_id, body_bytes, now):
    """Returns None if valid, else an error string."""
    ts = request.headers.get('X-Vuli-Timestamp', '')
    nonce = request.headers.get('X-Vuli-Nonce', '')
    sig = request.headers.get('X-Vuli-Sign', '')

    if not (ts and nonce and sig):
        return 'signature_missing'

    try:
        ts_int = int(ts)
    except ValueError:
        return 'bad_timestamp'

    if abs(now - ts_int) > MAX_CLOCK_SKEW:
        return 'stale_request'

    row = conn.execute(
        'SELECT device_secret FROM devices WHERE username=? AND device_id=?',
        (username, device_id)).fetchone()
    if not row:
        return 'unknown_device'

    body_hash = hashlib.sha256(body_bytes or b'').hexdigest()
    canonical = '\n'.join([request.method, request.path, ts, nonce, body_hash])
    expected = hmac.new(bytes.fromhex(row['device_secret']),
                        canonical.encode('utf-8'), hashlib.sha256).hexdigest()

    if not hmac.compare_digest(expected, sig):
        return 'bad_signature'

    # Single use. Insert fails if this nonce has been seen — that's a replay.
    try:
        conn.execute('INSERT INTO nonces (nonce, seen_at) VALUES (?,?)', (nonce, now))
    except Exception:
        return 'replayed_request'

    _prune_nonces(conn, now)
    return None


def app_auth(f):
    """
    Every authenticated app endpoint wears this.

    On success it sets g.username / g.device_id. Handlers must use those and never
    a username taken from the request body — that was the original hole, where any
    client could act as any account simply by naming it.
    """
    @wraps(f)
    def wrapper(*args, **kwargs):
        now = int(time.time())
        token = request.headers.get('X-Vuli-Token', '')
        device_id = request.headers.get('X-Vuli-Device', '')

        if not token or not device_id:
            return jsonify({'success': False, 'error': 'auth_required'}), 401

        body_bytes = request.get_data(cache=True)
        if len(body_bytes) > MAX_BODY_BYTES:
            return jsonify({'success': False, 'error': 'body_too_large'}), 413

        conn = get_db()
        try:
            row = conn.execute(
                'SELECT username, last_seen, device_id FROM sessions WHERE token=?',
                (token,)).fetchone()
            if not row:
                return jsonify({'success': False, 'error': 'invalid_token'}), 401
            if now - row['last_seen'] > TOKEN_TTL:
                conn.execute('DELETE FROM sessions WHERE token=?', (token,))
                conn.commit()
                return jsonify({'success': False, 'error': 'token_expired'}), 401
            if row['device_id'] != device_id:
                return jsonify({'success': False, 'error': 'device_mismatch'}), 401

            username = row['username']

            banned = conn.execute('SELECT is_banned FROM users WHERE username=?',
                                  (username,)).fetchone()
            if banned and banned['is_banned']:
                return jsonify({'success': False, 'error': 'account_disabled'}), 403

            problem = _verify_signature(conn, username, device_id, body_bytes, now)
            if problem:
                return jsonify({'success': False, 'error': problem}), 401

            conn.execute('UPDATE sessions SET last_seen=? WHERE token=?', (now, token))
            conn.execute('UPDATE devices SET last_seen=? WHERE username=? AND device_id=?',
                         (now, username, device_id))
            conn.commit()

            g.username = username
            g.device_id = device_id
            g.now = now
        finally:
            conn.close()

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
    fwd = request.headers.get('X-Forwarded-For', '')
    return fwd.split(',')[0].strip() if fwd else (request.remote_addr or 'unknown')
