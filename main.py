"""
VuliStudy — server
==================
Project: VuliStudy
Author: ETHANTYAGI
Version: 2.0.0
ALL RIGHTS RESERVED
"""

import hashlib
import json
import os
import secrets
import threading
import time
import urllib.error
import urllib.request
import uuid

from flask import Flask, g, jsonify, request

import economy
from db import (ensure_economy_row, get_db, init_db, list_tables,
                release_request_connection)
from security import (ADMIN_PASSWORD, SECOND_ADMIN_PASSWORD, admin_auth,
                      app_auth, audit, client_ip, hash_password, issue_admin_token,
                      issue_device_secret, issue_token, password_problem,
                      rate_limit, verify_password)

app = Flask(__name__)
app.config['MAX_CONTENT_LENGTH'] = 512 * 1024

API_ONE = os.environ.get('API_ONE')
API_TWO = os.environ.get('API_TWO')

# --- Discord + caps --------------------------------------------------------
# Every one of these is optional. Unset simply turns that feature off; nothing
# here can raise on a missing variable, because a missing webhook must never be
# able to stop someone signing up or sending a suggestion.
#
# These URLs are the reason the old feedback box had to go. It POSTed straight
# from the phone to Formspree with the form id hidden by string-splitting, which
# is a two-minute job to pull out of an APK and then spam forever. A webhook
# lives here and only here — the app never learns where its message goes, it
# only learns whether it was accepted.
SUGGEST_HOOK   = os.environ.get('SUGGEST_HOOK')      # pings you
NEW_JOIN_HOOK  = os.environ.get('NEW_JOIN_HOOK')     # silent
ALERT_HOOK     = os.environ.get('ALERT_HOOK')        # pings you
OWNER_ID       = (os.environ.get('OWNER_ID') or '').strip()
MAX_ACC        = int(os.environ.get('MAX_ACC') or 0)
LATEST_VERSION = (os.environ.get('LATEST_VERSION') or '').strip()

FEEDBACK_COOLDOWN = 48 * 60 * 60
BLOCKED_USERNAMES = {'admin', 'system', 'null', 'undefined', 'test', 'mod', 'owner'}

init_db()


# ===========================================================================
# Keeping Supabase awake
#
# The free tier suspends a project after about a week with no DATABASE
# activity, and a suspended project refuses every connection — which takes the
# whole app down while Render sits there looking perfectly healthy, still
# serving /healthz as if nothing were wrong.
#
# An uptime pinger cannot fix this on its own. It keeps RENDER awake, but
# /healthz and /classify-productivity never open a connection, so as far as
# Supabase is concerned nothing has happened for a week. The only thing that
# counts as activity is an actual query, so that is what this does.
#
# Deliberately not folded into /healthz: a health check that fails when the
# database blips would have Render marking the whole deploy unhealthy over
# something the process itself survived perfectly well.
#
# Each gunicorn worker starts one of these, so the real interval is one SELECT
# per worker. At six hours apart that is nothing, and the redundancy is welcome.
# ===========================================================================
KEEPALIVE_SECONDS = int(os.environ.get('KEEPALIVE_SECONDS') or 6 * 3600)


def _keepalive():
    while True:
        time.sleep(KEEPALIVE_SECONDS)
        try:
            conn = get_db()
            try:
                conn.execute('SELECT 1').fetchone()
                conn.commit()
            finally:
                conn.close()
        except Exception as exc:
            # This thread must never die. The ping that fails is immediately
            # before the one that matters most.
            app.logger.warning('supabase keepalive failed: %s', exc)


threading.Thread(target=_keepalive, daemon=True).start()


# ===========================================================================
# CORS — required now that the app is an APK
#
# When the site served the HTML itself, the app and the API were the same
# origin and none of this was needed. Inside the APK the page lives on
# https://localhost and the API is on Render, which makes every call
# cross-origin. Without these headers the WebView silently blocks the request
# before it is ever sent, and the app looks permanently offline.
#
# Allowing any origin is safe here because the API authenticates with headers
# (token + HMAC signature), not cookies — so a hostile page cannot ride along on
# a logged-in session the way it could with cookie auth. The browser console is
# a different matter, and it is deliberately NOT covered by this.
# ===========================================================================
CORS_PATH_PREFIXES = ('/api/', '/healthz', '/login', '/vt-', '/session-',
                      '/focus-', '/classify-productivity')

CORS_HEADERS = {
    'Access-Control-Allow-Origin': '*',
    'Access-Control-Allow-Methods': 'GET, POST, DELETE, OPTIONS',
    'Access-Control-Allow-Headers': (
        'Content-Type, X-Vuli-Token, X-Vuli-Device, X-Vuli-Timestamp, '
        'X-Vuli-Nonce, X-Vuli-Sign, X-Vuli-Admin'),
    'Access-Control-Max-Age': '86400',
}


# One database connection per request, released here. Without this the shared
# connection from get_db() would never go back to the pool.
app.teardown_request(release_request_connection)


@app.after_request
def apply_cors(response):
    if request.path.startswith(CORS_PATH_PREFIXES):
        for header, value in CORS_HEADERS.items():
            response.headers[header] = value
    return response


# ===========================================================================
# Helpers
# ===========================================================================
def body():
    return request.get_json(silent=True) or {}


def ok(**kwargs):
    return jsonify(dict(success=True, **kwargs))


def fail(error, status=400, **kwargs):
    return jsonify(dict(success=False, error=error, **kwargs)), status


def state_of(conn, username):
    return economy.snapshot(conn, username)


# ===========================================================================
# Discord
# ===========================================================================
def discord(hook, title, fields, colour=0x6BCF7F, ping=False):
    """Post one embed. Returns True only if Discord actually accepted it.

    Two attempts, because a single transient 5xx losing a suggestion is the one
    outcome that matters here. allowed_mentions is set explicitly so the join
    feed can never ping even if a username somehow contained a mention.
    """
    if not hook:
        return False
    mentions = {'users': [OWNER_ID]} if (ping and OWNER_ID) else {'parse': []}
    payload = json.dumps({
        'content': f'<@{OWNER_ID}>' if (ping and OWNER_ID) else '',
        'allowed_mentions': mentions,
        'embeds': [{'title': title[:250], 'color': colour,
                    'fields': [{'name': str(n)[:250],
                                'value': (str(v) if v not in (None, '') else '—')[:1024],
                                'inline': bool(i)} for n, v, i in fields]}],
    }).encode()
    # The User-Agent is not decoration. urllib defaults to "Python-urllib/3.x",
    # and Cloudflare — which sits in front of discord.com — blocks that outright.
    # The webhook works fine from a browser or curl and every post from here is
    # refused, which is exactly the "my webhook is correct, so your code is
    # wrong" symptom. It was.
    req = urllib.request.Request(hook, data=payload, headers={
        'Content-Type': 'application/json',
        'User-Agent': 'VuliStudy (https://studybuddy-r616.onrender.com, 1.2.0)',
    })
    for attempt in (1, 2):
        try:
            with urllib.request.urlopen(req, timeout=6) as r:
                if r.status < 300:
                    return True
                app.logger.warning('discord "%s" returned %s', title, r.status)
        except urllib.error.HTTPError as exc:
            # Read the body. Discord says precisely what it disliked, and
            # throwing that away is why this failed silently for weeks.
            try:
                detail = exc.read().decode('utf-8', 'replace')[:400]
            except Exception:
                detail = '(no body)'
            app.logger.warning('discord "%s" attempt %d: HTTP %s — %s',
                               title, attempt, exc.code, detail)
            if exc.code in (400, 401, 403, 404):
                return False          # malformed or wrong URL; retrying cannot help
            time.sleep(0.5)
        except Exception as exc:
            app.logger.warning('discord "%s" attempt %d failed: %s', title, attempt, exc)
            time.sleep(0.5)
    return False


def discord_async(hook, *args, **kwargs):
    """For pings nobody is waiting on. Joins and cap alerts must not add a
    single millisecond to the request that triggered them."""
    if hook:
        threading.Thread(target=discord, args=(hook,) + args,
                         kwargs=kwargs, daemon=True).start()


def purge_user(conn, username):
    """Remove every trace of one account. Used by both the user's own delete and
    the console's delete-anyone, so the two can never drift apart.

    This used to clear four tables. Everything else — their chat messages,
    friend requests, todos, study history, suggestions — was left orphaned under
    a username that no longer existed.

    Chats they OWNED are deliberately left standing: deleting one would take
    every other member's messages with it, and one person leaving should not
    destroy a group.
    """
    for table in ('economy', 'sessions', 'devices', 'processed_events',
                  'admin_tokens', 'chat_members', 'chat_messages',
                  'chat_timer_presence', 'weekly_study', 'daily_study',
                  'live_sessions', 'focus_sessions', 'todos', 'suggestions',
                  'inbox', 'app_errors'):
        try:
            conn.execute(f'DELETE FROM {table} WHERE username=?', (username,))
        except Exception:
            conn.rollback()      # a table that isn't there yet must not abort the rest
    try:
        conn.execute('DELETE FROM friend_requests WHERE from_user=? OR to_user=?',
                     (username, username))
    except Exception:
        conn.rollback()
    conn.execute('DELETE FROM users WHERE username=?', (username,))


def setting(conn, key, default=''):
    row = conn.execute('SELECT value FROM settings WHERE key=?', (key,)).fetchone()
    return row['value'] if row else default


def tz_of(conn, username):
    # Authentication already read this row, so in a normal request it costs
    # nothing. The query is only a fallback for callers outside app_auth.
    cached = getattr(g, 'tz_offset', None)
    if cached is not None:
        return cached
    row = conn.execute('SELECT tz_offset FROM users WHERE username=?', (username,)).fetchone()
    return row['tz_offset'] if row else 0


# ===========================================================================
# Health
# ===========================================================================
@app.route('/healthz')
def healthz():
    return jsonify({'ok': True, 'service': 'vulistudy', 'version': '2.0.0'})


# ===========================================================================
# Auth
# ===========================================================================
@app.route('/api/v1/auth/signup', methods=['POST'])
def api_signup():
    data = body()
    username = (data.get('username') or '').strip()
    password = data.get('password') or ''
    device_id = (data.get('device_id') or '').strip()

    if not username or not (2 <= len(username) <= 20):
        return fail('Invalid username')
    if not username.replace('_', '').replace('-', '').isalnum():
        return fail('Username must be letters, numbers, - or _')
    if username.lower() in BLOCKED_USERNAMES:
        return fail('Username not allowed')
    if not device_id or len(device_id) > 64:
        return fail('Invalid device')
    problem = password_problem(password)
    if problem:
        return fail(problem)

    conn = get_db()
    try:
        if rate_limit(conn, f'signup:{client_ip()}', limit=5, window=3600):
            conn.commit()
            return fail('Too many signups from this connection. Try later.', 429)

        # Manual lockdown, flipped from the console. Beats the cap.
        if setting(conn, 'lockdown') == '1':
            conn.commit()
            return fail('Signups are closed right now. Check back soon.', 403)

        # MAX_ACC. Only new accounts are refused — everyone who already has one
        # carries on untouched. You get pinged once a day at most, not once per
        # rejected attempt.
        total = conn.execute('SELECT COUNT(*) AS c FROM users').fetchone()['c']
        if MAX_ACC and total >= MAX_ACC:
            quiet = rate_limit(conn, 'alert:maxacc', limit=1, window=86400)
            conn.commit()
            if not quiet:
                discord_async(ALERT_HOOK, 'Account cap reached',
                              [('Cap (MAX_ACC)', MAX_ACC, True), ('Accounts', total, True),
                               ('Effect', 'New signups are being refused.', False)],
                              colour=0xFF6B6B, ping=True)
            return fail('VuliStudy is currently full. Try again later.', 403)

        if conn.execute('SELECT 1 FROM users WHERE username=?', (username,)).fetchone():
            conn.commit()
            return fail('Username taken')

        now = int(time.time())
        conn.execute(
            'INSERT INTO users (username, password_hash, created_at, last_active, is_active)'
            ' VALUES (?,?,?,?,1)',
            (username, hash_password(password), now, now))
        ensure_economy_row(conn, username, now)
        token = issue_token(conn, username, device_id, data.get('platform', 'app'))
        secret = issue_device_secret(conn, username, device_id)
        conn.commit()
        discord_async(NEW_JOIN_HOOK, 'New account',
                      [('Username', username, True),
                       ('Accounts now', total + 1, True)],
                      colour=0x70A1FF, ping=False)
        return ok(token=token, device_secret=secret, state=state_of(conn, username))
    finally:
        conn.close()


@app.route('/api/v1/auth/login', methods=['POST'])
def api_login():
    data = body()
    username = (data.get('username') or '').strip()
    password = data.get('password') or ''
    device_id = (data.get('device_id') or '').strip()

    if not username or not password or not device_id:
        return fail('Missing credentials')

    conn = get_db()
    try:
        # Throttled per account AND per IP so neither can be brute-forced.
        if rate_limit(conn, f'login:{username}', limit=10, window=900) or \
           rate_limit(conn, f'loginip:{client_ip()}', limit=30, window=900):
            conn.commit()
            return fail('Too many attempts. Wait a few minutes.', 429)

        user = conn.execute('SELECT username, password_hash, is_banned FROM users WHERE username=?',
                            (username,)).fetchone()
        if not user or not verify_password(password, user['password_hash']):
            conn.commit()
            return fail('Incorrect username or password', 401)
        if user['is_banned']:
            conn.commit()
            return fail('This account has been disabled', 403)

        now = int(time.time())
        ensure_economy_row(conn, username, now)
        token = issue_token(conn, username, device_id, data.get('platform', 'app'))
        secret = issue_device_secret(conn, username, device_id)
        conn.execute('UPDATE users SET last_active=?, is_active=1 WHERE username=?', (now, username))
        conn.commit()
        return ok(token=token, device_secret=secret, state=state_of(conn, username))
    finally:
        conn.close()


@app.route('/api/v1/auth/claim-password', methods=['POST'])
def api_claim_password():
    """Legacy accounts with no password. Kept for continuity; no-op on a fresh DB."""
    data = body()
    username = (data.get('username') or '').strip()
    password = data.get('password') or ''
    device_id = (data.get('device_id') or '').strip()
    problem = password_problem(password)
    if problem:
        return fail(problem)
    if not device_id:
        return fail('Invalid device')

    conn = get_db()
    try:
        user = conn.execute('SELECT username, password_hash FROM users WHERE username=?',
                            (username,)).fetchone()
        if not user:
            return fail('User not found', 404)
        if user['password_hash']:
            return fail('Account already has a password')
        now = int(time.time())
        conn.execute('UPDATE users SET password_hash=? WHERE username=?',
                     (hash_password(password), username))
        ensure_economy_row(conn, username, now)
        token = issue_token(conn, username, device_id, 'app')
        secret = issue_device_secret(conn, username, device_id)
        conn.commit()
        return ok(token=token, device_secret=secret, state=state_of(conn, username))
    finally:
        conn.close()


@app.route('/api/v1/auth/logout', methods=['POST'])
@app_auth
def api_logout():
    conn = get_db()
    try:
        conn.execute('DELETE FROM sessions WHERE token=?',
                     (request.headers.get('X-Vuli-Token', ''),))
        conn.commit()
        return ok()
    finally:
        conn.close()


@app.route('/api/v1/me', methods=['GET'])
@app_auth
def api_me():
    conn = get_db()
    try:
        # The inbox rides this poll rather than getting a loop of its own — the
        # app already asks for /me every 20 seconds while it is open.
        return ok(state=state_of(conn, g.username), inbox=inbox_for(conn, g.username))
    finally:
        conn.close()


@app.route('/api/v1/me', methods=['DELETE'])
@app_auth
def api_delete_me():
    """
    Was /delete-user, which took a username and no authentication at all — anyone
    could delete anyone. It now only ever deletes the caller's own account.
    """
    conn = get_db()
    try:
        purge_user(conn, g.username)
        conn.commit()
        return ok()
    finally:
        conn.close()


@app.route('/api/v1/me/active', methods=['POST'])
@app_auth
def api_me_active():
    conn = get_db()
    try:
        if body().get('rejoin'):
            conn.execute('UPDATE users SET is_active=1, last_active=? WHERE username=?',
                         (g.now, g.username))
            conn.commit()
        row = conn.execute('SELECT is_active FROM users WHERE username=?', (g.username,)).fetchone()
        return ok(active=bool(row['is_active']), exists=True)
    finally:
        conn.close()


@app.route('/api/v1/me/timezone', methods=['POST'])
@app_auth
def api_set_tz():
    """The phone reports its UTC offset, never its date. See economy.local_date."""
    try:
        offset = max(-720, min(840, int(body().get('offset_minutes', 0))))
    except (TypeError, ValueError):
        return fail('Invalid offset')
    conn = get_db()
    try:
        conn.execute('UPDATE users SET tz_offset=? WHERE username=?', (offset, g.username))
        conn.commit()
        return ok(offset_minutes=offset)
    finally:
        conn.close()


# ===========================================================================
# Profile pictures
#
# Stored as a data URL on the user row. The phone crops to a square and shrinks
# to 128px before sending, so a picture is a handful of kilobytes — small enough
# that a separate image host would be more moving parts than it is worth.
#
# Nothing here trusts the client. The declared MIME type is ignored in favour of
# the actual magic bytes, because "image/png" on the front of a zip file is the
# oldest trick there is.
# ===========================================================================
AVATAR_MAX_BYTES = 60 * 1024      # generous for a 128px square
AVATAR_TYPES = {
    'png':  (b'\x89PNG\r\n\x1a\n', 'image/png'),
    'jpeg': (b'\xff\xd8\xff',      'image/jpeg'),
    'webp': (b'RIFF',              'image/webp'),
}


def _sniff_image(raw):
    """Return a MIME type based on the bytes themselves, or None."""
    for kind, (magic, mime) in AVATAR_TYPES.items():
        if raw.startswith(magic):
            if kind == 'webp' and raw[8:12] != b'WEBP':
                continue
            return mime
    return None


@app.route('/api/v1/me/avatar', methods=['POST'])
@app_auth
def api_set_avatar():
    import base64

    data_url = (body().get('image') or '').strip()
    if not data_url:
        return fail('No image supplied')
    if not data_url.startswith('data:'):
        return fail('Image must be a data URL')

    try:
        header, encoded = data_url.split(',', 1)
    except ValueError:
        return fail('Malformed image')
    if ';base64' not in header:
        return fail('Image must be base64')

    try:
        raw = base64.b64decode(encoded, validate=True)
    except Exception:
        return fail('Image is not valid base64')

    if len(raw) > AVATAR_MAX_BYTES:
        return fail('Picture is too large — it should be shrunk before sending')
    if len(raw) < 64:
        return fail('That is not an image')

    mime = _sniff_image(raw)
    if not mime:
        return fail('Only PNG, JPEG and WebP pictures are allowed')

    # Re-encode from the bytes we verified, so whatever the client claimed in
    # the header is discarded entirely.
    clean = 'data:' + mime + ';base64,' + base64.b64encode(raw).decode('ascii')

    conn = get_db()
    try:
        if rate_limit(conn, f'avatar:{g.username}', limit=10, window=3600):
            conn.commit()
            return fail('Too many picture changes. Try again later.', 429)
        conn.execute('UPDATE users SET avatar=?, avatar_updated=? WHERE username=?',
                     (clean, g.now, g.username))
        conn.commit()
        return ok(avatar=clean)
    finally:
        conn.close()


@app.route('/api/v1/me/avatar', methods=['DELETE'])
@app_auth
def api_clear_avatar():
    conn = get_db()
    try:
        conn.execute('UPDATE users SET avatar=NULL, avatar_updated=? WHERE username=?',
                     (g.now, g.username))
        conn.commit()
        return ok()
    finally:
        conn.close()


@app.route('/api/v1/users/profile', methods=['POST'])
@app_auth
def api_user_profile():
    """
    The full profile shown when you tap someone. Deliberately separate from the
    leaderboard: testers wanted pictures on the person you tapped, not smeared
    across every row of the table.
    """
    target = (body().get('username') or '').strip()
    if not target:
        return fail('No user')
    conn = get_db()
    try:
        row = conn.execute(
            '''SELECT u.username, u.total_minutes, u.streak, u.reborns, u.is_premium,
                      u.equipped_cosmetic, u.active_background, u.created_at, u.avatar,
                      COALESCE(e.happiness, 100) AS happiness,
                      COALESCE(e.longest_session, 0) AS longest_session
               FROM users u LEFT JOIN economy e ON e.username = u.username
               WHERE u.username = ? AND u.is_banned = 0''', (target,)).fetchone()
        if not row:
            return fail('User not found', 404)

        profile = dict(row)
        # Are we friends? Changes what the app offers to do next.
        fr = conn.execute(
            '''SELECT status FROM friend_requests
               WHERE (from_user=? AND to_user=?) OR (from_user=? AND to_user=?)''',
            (g.username, target, target, g.username)).fetchone()
        profile['friendship'] = fr['status'] if fr else None
        profile['is_self'] = (target == g.username)
        return ok(profile=profile)
    finally:
        conn.close()


@app.route('/api/v1/users/avatars', methods=['POST'])
@app_auth
def api_bulk_avatars():
    """
    Pictures for a list of usernames in one call — chat needs a face beside every
    message and fetching them one at a time would be absurd.
    """
    names = body().get('usernames') or []
    if not isinstance(names, list) or not names:
        return ok(avatars={})
    names = [str(n)[:20] for n in names[:60]]

    conn = get_db()
    try:
        placeholders = ','.join(['?'] * len(names))
        rows = conn.execute(
            f'SELECT username, avatar FROM users WHERE username IN ({placeholders})',
            tuple(names)).fetchall()
        return ok(avatars={r['username']: r['avatar'] for r in rows if r['avatar']})
    finally:
        conn.close()


# ===========================================================================
# Events — the only way the economy changes through normal play
# ===========================================================================
EVENT_HANDLERS = {
    'session_completed', 'carrot_fed', 'shop_purchase', 'equip',
    'happiness_decay', 'character_width', 'legacy_sync',
}


def _undo_event(conn, used_savepoint):
    """
    Discard one failed event without losing the ones that already succeeded.

    With a savepoint we rewind to it. Without one (single-event batch) the whole
    transaction goes, which is the same thing — and either way the connection
    comes back out of Postgres's aborted state so the rest of the request, in
    particular reading the user's state back, still works.
    """
    try:
        conn.execute('ROLLBACK TO SAVEPOINT ev' if used_savepoint else 'ROLLBACK')
    except Exception:
        try:
            conn.rollback()
        except Exception:
            pass


@app.route('/api/v1/events', methods=['POST'])
@app_auth
def api_events():
    """
    Accepts a batch of things the user did and returns the authoritative state.

    Idempotent: an event_id that has already been processed is acknowledged but
    not re-applied, so replaying a queue after a dropped connection is safe.

    A rejected event does not poison the batch — it is reported and skipped, so
    one bad entry can't block a legitimate offline backlog.
    """
    events = body().get('events') or []
    if not isinstance(events, list):
        return fail('Invalid events')
    if len(events) > 200:
        return fail('Too many events in one batch')

    accepted, rejected, results = [], [], {}
    conn = get_db()
    try:
        tz = tz_of(conn, g.username)
        ensure_economy_row(conn, g.username, g.now)

        for ev in events:
            if not isinstance(ev, dict):
                continue
            event_id = str(ev.get('event_id') or '')[:64]
            etype = ev.get('type')
            if not event_id or etype not in EVENT_HANDLERS:
                rejected.append({'event_id': event_id, 'error': 'unknown_event'})
                continue

            # Already processed — acknowledge so the client stops resending it.
            seen = conn.execute('SELECT result_json FROM processed_events WHERE event_id=?',
                                (event_id,)).fetchone()
            if seen:
                accepted.append(event_id)
                continue

            # ---------------------------------------------------------------
            # Each event gets its own savepoint when there is more than one.
            #
            # Postgres is unforgiving here in a way SQLite never was: the moment
            # ANY statement inside a transaction fails, every later statement
            # fails too until a rollback. So a single bad event used to poison
            # the whole batch — including the final read of the user's state —
            # and the request died with a 500 having saved nothing. That is what
            # made purchases look like they refunded themselves.
            #
            # A savepoint scopes the damage to the one event. Single-event
            # batches skip it: there is nothing to protect, and it saves two
            # round trips on much the most common request in the app.
            # ---------------------------------------------------------------
            use_savepoint = len(events) > 1
            if use_savepoint:
                conn.execute('SAVEPOINT ev')

            try:
                if etype == 'session_completed':
                    res = economy.apply_session_completed(conn, g.username, tz, ev, g.now)
                elif etype == 'carrot_fed':
                    res = economy.apply_carrot_fed(conn, g.username, g.now)
                elif etype == 'shop_purchase':
                    res = economy.apply_purchase(conn, g.username, ev.get('item_id'), g.now)
                elif etype == 'equip':
                    res = economy.apply_equip(conn, g.username, ev.get('slot'),
                                              ev.get('item_id'), g.now)
                elif etype == 'happiness_decay':
                    res = economy.apply_happiness_decay(conn, g.username, g.now)
                elif etype == 'character_width':
                    res = economy.apply_character_width(conn, g.username, ev.get('width'), g.now)
                else:
                    # legacy_sync: an old-style client told us its totals. We record
                    # that it happened and deliberately award nothing.
                    res = {'ignored': True, 'reason': 'totals_are_not_accepted'}

                conn.execute(
                    'INSERT INTO processed_events (event_id, username, event_type,'
                    ' processed_at, result_json) VALUES (?,?,?,?,?)',
                    (event_id, g.username, etype, g.now, json.dumps(res)))

                if use_savepoint:
                    conn.execute('RELEASE SAVEPOINT ev')
                accepted.append(event_id)
                results[event_id] = res

            except economy.EconomyError as exc:
                _undo_event(conn, use_savepoint)
                rejected.append({'event_id': event_id, 'error': str(exc)})
            except Exception:
                app.logger.exception('event %s (%s) failed', event_id, etype)
                _undo_event(conn, use_savepoint)
                rejected.append({'event_id': event_id, 'error': 'internal_error'})

        conn.commit()
        return ok(accepted=accepted, rejected=rejected, results=results,
                  state=state_of(conn, g.username))
    finally:
        conn.close()


@app.route('/api/v1/shop', methods=['GET'])
@app_auth
def api_shop():
    conn = get_db()
    try:
        u = conn.execute('SELECT is_premium FROM users WHERE username=?', (g.username,)).fetchone()
        return ok(catalogue=economy.shop_catalogue(bool(u['is_premium'])))
    finally:
        conn.close()


# ===========================================================================
# Leaderboard
# ===========================================================================
@app.route('/api/v1/leaderboard', methods=['GET'])
@app_auth
def api_leaderboard():
    conn = get_db()
    try:
        three_days = g.now - (3 * 24 * 60 * 60)
        conn.execute('UPDATE users SET is_active=0 WHERE last_active < ? AND last_active > 0',
                     (three_days,))
        conn.commit()

        period = request.args.get('period')

        # Friends board. Everyone here is someone you both agreed to, so this is
        # the only view where the ranking is against people you actually know —
        # and where a new account can realistically be near the top.
        if period == 'friends':
            rows = conn.execute(
                '''SELECT u.username, u.total_minutes, u.streak, u.reborns,
                          u.equipped_cosmetic, u.active_background, u.character_width,
                          u.is_premium, COALESCE(e.happiness,100) AS happiness
                   FROM users u
                   LEFT JOIN economy e ON e.username=u.username
                   WHERE u.is_banned=0 AND (u.username=? OR u.username IN (
                       SELECT CASE WHEN from_user=? THEN to_user ELSE from_user END
                       FROM friend_requests
                       WHERE status='accepted' AND (from_user=? OR to_user=?)))
                   ORDER BY u.total_minutes DESC LIMIT 50''',
                (g.username, g.username, g.username, g.username)).fetchall()
            ranked = [dict(r) for r in rows]
            mine = next((i for i, r in enumerate(ranked)
                         if r['username'] == g.username), None)
            return jsonify({'top': ranked,
                            'me': None if mine is None else {
                                'username': g.username, 'rank': mine + 1,
                                'minutes': ranked[mine]['total_minutes'] or 0,
                                'total': len(ranked)}})

        if period == 'weekly':
            rows = conn.execute(
                '''SELECT u.username, u.total_minutes, u.streak, u.reborns,
                          u.equipped_cosmetic, u.active_background, u.character_width,
                          u.is_premium, COALESCE(e.happiness,100) AS happiness,
                          COALESCE(w.minutes,0) AS weekly_minutes
                   FROM users u
                   LEFT JOIN weekly_study w ON u.username=w.username AND w.week_start=?
                   LEFT JOIN economy e ON e.username=u.username
                   WHERE u.is_active=1 AND u.is_banned=0
                   ORDER BY weekly_minutes DESC, u.total_minutes DESC LIMIT 20''',
                (economy.week_start_ts(g.now),)).fetchall()
        else:
            rows = conn.execute(
                '''SELECT u.username, u.total_minutes, u.streak, u.reborns,
                          u.equipped_cosmetic, u.active_background, u.character_width,
                          u.is_premium, COALESCE(e.happiness,100) AS happiness
                   FROM users u
                   LEFT JOIN economy e ON e.username=u.username
                   WHERE u.is_active=1 AND u.is_banned=0
                   ORDER BY u.total_minutes DESC LIMIT 20''').fetchall()

        # The caller's OWN position, whatever it is. Without this the app can
        # only show you the top 20 — so anyone outside it opened the leaderboard
        # and found nothing about themselves at all, which is why it read as
        # "this isn't for me". Two cheap counts, no window function needed.
        return jsonify({'top': [dict(r) for r in rows],
                        'me': my_rank(conn, request.args.get('period'))})
    finally:
        conn.close()


def my_rank(conn, period):
    """{rank, minutes, total} for g.username, or None if they aren't ranked."""
    if period == 'weekly':
        ws = economy.week_start_ts(g.now)
        row = conn.execute(
            'SELECT COALESCE(minutes,0) AS m FROM weekly_study WHERE username=? AND week_start=?',
            (g.username, ws)).fetchone()
        mine = row['m'] if row else 0
        ahead = conn.execute(
            '''SELECT COUNT(*) AS c FROM users u
               LEFT JOIN weekly_study w ON u.username=w.username AND w.week_start=?
               WHERE u.is_active=1 AND u.is_banned=0 AND COALESCE(w.minutes,0) > ?''',
            (ws, mine)).fetchone()['c']
    else:
        row = conn.execute('SELECT total_minutes AS m, is_active, is_banned FROM users'
                           ' WHERE username=?', (g.username,)).fetchone()
        if not row:
            return None
        mine = row['m'] or 0
        ahead = conn.execute(
            'SELECT COUNT(*) AS c FROM users WHERE is_active=1 AND is_banned=0'
            ' AND total_minutes > ?', (mine,)).fetchone()['c']

    total = conn.execute('SELECT COUNT(*) AS c FROM users WHERE is_active=1'
                         ' AND is_banned=0').fetchone()['c']
    return {'username': g.username, 'rank': ahead + 1, 'minutes': mine, 'total': total}


@app.route('/api/v1/users/stats', methods=['POST'])
@app_auth
def api_user_stats():
    target = (body().get('target') or body().get('username') or '').strip()
    if not target:
        return fail('No user')
    conn = get_db()
    try:
        row = conn.execute(
            '''SELECT u.username, u.total_minutes, u.streak, u.reborns, u.is_premium,
                      u.equipped_cosmetic, u.active_background, u.created_at,
                      COALESCE(e.happiness,100) AS happiness
               FROM users u LEFT JOIN economy e ON e.username=u.username
               WHERE u.username=? AND u.is_banned=0''', (target,)).fetchone()
        if not row:
            return fail('User not found', 404)
        return ok(stats=dict(row))
    finally:
        conn.close()


# ===========================================================================
# Chats — the actor is always g.username, never a body field
# ===========================================================================
@app.route('/api/v1/chats/create', methods=['POST'])
@app_auth
def api_chat_create():
    name = (body().get('name') or '').strip()
    if not name or len(name) > 30:
        return fail('Invalid chat name')
    conn = get_db()
    try:
        count = conn.execute('SELECT COUNT(*) c FROM chat_members WHERE username=?',
                             (g.username,)).fetchone()['c']
        if count >= 7:
            return fail('Max 7 chats allowed')
        chat_id = f"{g.now}-{g.username[:8]}-{uuid.uuid4().hex[:6]}"
        conn.execute('INSERT INTO chats (id,name,owner,created_at) VALUES (?,?,?,?)',
                     (chat_id, name, g.username, g.now))
        conn.execute('INSERT INTO chat_members (chat_id,username) VALUES (?,?)',
                     (chat_id, g.username))
        conn.commit()
        return ok(chat_id=chat_id)
    finally:
        conn.close()


@app.route('/api/v1/chats/list', methods=['POST'])
@app_auth
def api_chat_list():
    conn = get_db()
    try:
        rows = conn.execute(
            '''SELECT c.id, c.name, c.owner, c.created_at,
                      (SELECT COUNT(*) FROM chat_members WHERE chat_id=c.id) AS member_count
               FROM chats c JOIN chat_members m ON c.id=m.chat_id
               WHERE m.username=? ORDER BY c.created_at DESC''', (g.username,)).fetchall()
        return ok(chats=[dict(r) for r in rows])
    finally:
        conn.close()


def _is_member(conn, chat_id, username):
    return conn.execute('SELECT 1 FROM chat_members WHERE chat_id=? AND username=?',
                        (chat_id, username)).fetchone() is not None


@app.route('/api/v1/chats/add-member', methods=['POST'])
@app_auth
def api_chat_add_member():
    data = body()
    chat_id = (data.get('chat_id') or '').strip()
    new_member = (data.get('member') or data.get('new_member') or '').strip()
    if not chat_id or not new_member:
        return fail('Missing fields')
    conn = get_db()
    try:
        chat = conn.execute('SELECT owner FROM chats WHERE id=?', (chat_id,)).fetchone()
        if not chat:
            return fail('Chat not found', 404)
        if chat['owner'] != g.username:
            return fail('Only the owner can add members', 403)
        if not conn.execute('SELECT 1 FROM users WHERE username=?', (new_member,)).fetchone():
            return fail('User not found — username is case-sensitive')
        if conn.execute('SELECT COUNT(*) c FROM chat_members WHERE chat_id=?',
                        (chat_id,)).fetchone()['c'] >= 30:
            return fail('Chat is full (30 members max)')
        if _is_member(conn, chat_id, new_member):
            return fail('Already a member')
        if conn.execute('SELECT COUNT(*) c FROM chat_members WHERE username=?',
                        (new_member,)).fetchone()['c'] >= 7:
            return fail(f'{new_member} is already in 7 chats')
        conn.execute('INSERT INTO chat_members (chat_id,username) VALUES (?,?)',
                     (chat_id, new_member))
        conn.commit()
        return ok()
    finally:
        conn.close()


@app.route('/api/v1/chats/messages', methods=['POST'])
@app_auth
def api_chat_messages():
    data = body()
    chat_id = (data.get('chat_id') or '').strip()
    try:
        since = int(data.get('since', 0))
    except (TypeError, ValueError):
        since = 0
    conn = get_db()
    try:
        if not _is_member(conn, chat_id, g.username):
            return fail('Not a member', 403)
        rows = conn.execute(
            'SELECT id, username, message, timestamp FROM chat_messages'
            ' WHERE chat_id=? AND id>? ORDER BY id ASC LIMIT 80', (chat_id, since)).fetchall()
        return ok(messages=[dict(r) for r in rows])
    finally:
        conn.close()


@app.route('/api/v1/chats/send', methods=['POST'])
@app_auth
def api_chat_send():
    data = body()
    chat_id = (data.get('chat_id') or '').strip()
    message = (data.get('message') or '').strip()
    if not message or len(message) > 500:
        return fail('Invalid message')
    conn = get_db()
    try:
        if not _is_member(conn, chat_id, g.username):
            return fail('Not a member', 403)
        if rate_limit(conn, f'msg:{g.username}', limit=30, window=60):
            conn.commit()
            return fail('Slow down a moment.', 429)
        conn.execute('INSERT INTO chat_messages (chat_id,username,message,timestamp)'
                     ' VALUES (?,?,?,?)', (chat_id, g.username, message, g.now))
        conn.commit()
        return ok()
    finally:
        conn.close()


@app.route('/api/v1/chats/members', methods=['POST'])
@app_auth
def api_chat_members():
    chat_id = (body().get('chat_id') or '').strip()
    conn = get_db()
    try:
        if not _is_member(conn, chat_id, g.username):
            return fail('Not a member', 403)
        members = conn.execute('SELECT username FROM chat_members WHERE chat_id=?'
                               ' ORDER BY username ASC', (chat_id,)).fetchall()
        chat = conn.execute('SELECT owner FROM chats WHERE id=?', (chat_id,)).fetchone()
        return ok(members=[m['username'] for m in members], owner=chat['owner'])
    finally:
        conn.close()


@app.route('/api/v1/chats/leave', methods=['POST'])
@app_auth
def api_chat_leave():
    chat_id = (body().get('chat_id') or '').strip()
    conn = get_db()
    try:
        chat = conn.execute('SELECT owner FROM chats WHERE id=?', (chat_id,)).fetchone()
        if not chat:
            return fail('Chat not found', 404)
        conn.execute('DELETE FROM chat_members WHERE chat_id=? AND username=?',
                     (chat_id, g.username))
        remaining = conn.execute('SELECT COUNT(*) c FROM chat_members WHERE chat_id=?',
                                 (chat_id,)).fetchone()['c']
        if remaining == 0 or chat['owner'] == g.username:
            conn.execute('DELETE FROM chats WHERE id=?', (chat_id,))
            conn.execute('DELETE FROM chat_members WHERE chat_id=?', (chat_id,))
            conn.execute('DELETE FROM chat_messages WHERE chat_id=?', (chat_id,))
        conn.commit()
        return ok()
    finally:
        conn.close()


# ---- shared chat timers ---------------------------------------------------
@app.route('/api/v1/chats/timer/start', methods=['POST'])
@app_auth
def api_timer_start():
    data = body()
    chat_id = (data.get('chat_id') or '').strip()
    try:
        duration = int(data.get('duration_seconds', 0))
    except (TypeError, ValueError):
        return fail('Invalid duration')
    if not (60 <= duration <= 4 * 3600):
        return fail('Duration must be 1 minute to 4 hours')
    conn = get_db()
    try:
        if not _is_member(conn, chat_id, g.username):
            return fail('Not a member', 403)
        active = conn.execute(
            'SELECT id FROM chat_timers WHERE chat_id=? AND completed=0 AND started_at+duration_seconds>?',
            (chat_id, g.now)).fetchone()
        if active:
            return fail('A timer is already running in this chat')
        cur = conn.execute(
            'INSERT INTO chat_timers (chat_id,creator,duration_seconds,started_at,completed,reward_coins)'
            ' VALUES (?,?,?,?,0,2) RETURNING id', (chat_id, g.username, duration, g.now))
        new_id = cur.fetchone()['id']
        conn.commit()
        return ok(timer_id=new_id)
    finally:
        conn.close()


@app.route('/api/v1/chats/timer/get', methods=['POST'])
@app_auth
def api_timer_get():
    chat_id = (body().get('chat_id') or '').strip()
    conn = get_db()
    try:
        if not _is_member(conn, chat_id, g.username):
            return fail('Not a member', 403)
        row = conn.execute(
            'SELECT * FROM chat_timers WHERE chat_id=? ORDER BY id DESC LIMIT 1',
            (chat_id,)).fetchone()
        if not row:
            return ok(timer=None)
        t = dict(row)
        t['remaining'] = max(0, t['started_at'] + t['duration_seconds'] - g.now)
        return ok(timer=t)
    finally:
        conn.close()


@app.route('/api/v1/chats/timer/ping', methods=['POST'])
@app_auth
def api_timer_ping():
    try:
        timer_id = int(body().get('timer_id', 0))
    except (TypeError, ValueError):
        return fail('Invalid timer')
    conn = get_db()
    try:
        row = conn.execute('SELECT chat_id FROM chat_timers WHERE id=?', (timer_id,)).fetchone()
        if not row or not _is_member(conn, row['chat_id'], g.username):
            return fail('Not a member', 403)
        conn.execute(
            'INSERT INTO chat_timer_presence (timer_id,username,first_ping,last_ping)'
            ' VALUES (?,?,?,?) ON CONFLICT(timer_id,username) DO UPDATE SET last_ping=excluded.last_ping',
            (timer_id, g.username, g.now, g.now))
        conn.commit()
        return ok()
    finally:
        conn.close()


@app.route('/api/v1/chats/timer/claim', methods=['POST'])
@app_auth
def api_timer_claim():
    """
    The one place a coin is awarded outside a study session. The server checks the
    timer actually finished and that this user was present for it, and marks the
    claim so it cannot be collected twice.
    """
    try:
        timer_id = int(body().get('timer_id', 0))
    except (TypeError, ValueError):
        return fail('Invalid timer')
    conn = get_db()
    try:
        t = conn.execute('SELECT * FROM chat_timers WHERE id=?', (timer_id,)).fetchone()
        if not t:
            return fail('Timer not found', 404)
        if g.now < t['started_at'] + t['duration_seconds']:
            return fail('Timer has not finished yet')

        p = conn.execute('SELECT * FROM chat_timer_presence WHERE timer_id=? AND username=?',
                         (timer_id, g.username)).fetchone()
        if not p:
            return fail('You were not present for this timer', 403)
        if p['claimed']:
            return fail('Already claimed')

        # Must have been present for most of it, not just joined at the end.
        present_for = p['last_ping'] - p['first_ping']
        if present_for < t['duration_seconds'] * 0.8:
            return fail('You were not present for enough of the session')

        reward = int(t['reward_coins'] or 2)
        e = conn.execute('SELECT coins, highest_coins FROM economy WHERE username=?',
                         (g.username,)).fetchone()
        new_coins = e['coins'] + reward
        conn.execute('UPDATE economy SET coins=?, highest_coins=?, updated_at=? WHERE username=?',
                     (new_coins, max(e['highest_coins'], new_coins), g.now, g.username))
        conn.execute('UPDATE chat_timer_presence SET claimed=1 WHERE timer_id=? AND username=?',
                     (timer_id, g.username))
        conn.commit()
        return ok(reward_coins=reward, state=state_of(conn, g.username))
    finally:
        conn.close()


# ===========================================================================
# Friends
# ===========================================================================
@app.route('/api/v1/friends/request', methods=['POST'])
@app_auth
def api_friend_request():
    to_user = (body().get('to_user') or body().get('target') or '').strip()
    if not to_user or to_user == g.username:
        return fail('Invalid user')
    conn = get_db()
    try:
        if not conn.execute('SELECT 1 FROM users WHERE username=?', (to_user,)).fetchone():
            return fail('User not found')
        existing = conn.execute(
            '''SELECT id, status FROM friend_requests
               WHERE (from_user=? AND to_user=?) OR (from_user=? AND to_user=?)''',
            (g.username, to_user, to_user, g.username)).fetchone()
        if existing:
            return fail('Already friends' if existing['status'] == 'accepted'
                        else 'Request already pending')
        if rate_limit(conn, f'friendreq:{g.username}', limit=20, window=3600):
            conn.commit()
            return fail('Too many friend requests. Try later.', 429)
        conn.execute('INSERT INTO friend_requests (from_user,to_user,status,created_at)'
                     " VALUES (?,?,'pending',?)", (g.username, to_user, g.now))
        conn.commit()
        return ok()
    finally:
        conn.close()


@app.route('/api/v1/friends/list', methods=['POST'])
@app_auth
def api_friends_list():
    conn = get_db()
    try:
        rows = conn.execute(
            '''SELECT CASE WHEN from_user=? THEN to_user ELSE from_user END AS friend
               FROM friend_requests
               WHERE status='accepted' AND (from_user=? OR to_user=?)''',
            (g.username, g.username, g.username)).fetchall()
        names = [r['friend'] for r in rows]
        friends = []
        for name in names:
            u = conn.execute(
                '''SELECT u.username, u.total_minutes, u.streak, u.is_premium,
                          u.equipped_cosmetic, u.active_background,
                          COALESCE(e.happiness,100) AS happiness
                   FROM users u LEFT JOIN economy e ON e.username=u.username
                   WHERE u.username=?''', (name,)).fetchone()
            if u:
                friends.append(dict(u))
        return ok(friends=friends)
    finally:
        conn.close()


@app.route('/api/v1/friends/requests', methods=['POST'])
@app_auth
def api_friend_requests():
    conn = get_db()
    try:
        rows = conn.execute(
            "SELECT id, from_user, created_at FROM friend_requests"
            " WHERE to_user=? AND status='pending' ORDER BY created_at DESC",
            (g.username,)).fetchall()
        return ok(requests=[dict(r) for r in rows])
    finally:
        conn.close()


@app.route('/api/v1/friends/respond', methods=['POST'])
@app_auth
def api_friend_respond():
    data = body()
    try:
        req_id = int(data.get('request_id', 0))
    except (TypeError, ValueError):
        return fail('Invalid request')
    accept = bool(data.get('accept'))
    conn = get_db()
    try:
        row = conn.execute('SELECT * FROM friend_requests WHERE id=?', (req_id,)).fetchone()
        # Only the recipient may answer — checked against the token, not the body.
        if not row or row['to_user'] != g.username:
            return fail('Request not found', 404)
        if row['status'] != 'pending':
            return fail('Already answered')
        if accept:
            conn.execute("UPDATE friend_requests SET status='accepted' WHERE id=?", (req_id,))
        else:
            conn.execute('DELETE FROM friend_requests WHERE id=?', (req_id,))
        conn.commit()
        return ok()
    finally:
        conn.close()


@app.route('/api/v1/friends/remove', methods=['POST'])
@app_auth
def api_friend_remove():
    friend = (body().get('friend') or body().get('target') or '').strip()
    conn = get_db()
    try:
        conn.execute(
            '''DELETE FROM friend_requests
               WHERE status='accepted' AND ((from_user=? AND to_user=?) OR (from_user=? AND to_user=?))''',
            (g.username, friend, friend, g.username))
        conn.commit()
        return ok()
    finally:
        conn.close()


# ===========================================================================
# Premium
# ===========================================================================
@app.route('/api/v1/premium/redeem', methods=['POST'])
@app_auth
def api_premium_redeem():
    code = (body().get('code') or '').strip()
    if not code:
        return fail('Missing code')
    conn = get_db()
    try:
        if rate_limit(conn, f'redeem:{g.username}', limit=10, window=3600):
            conn.commit()
            return fail('Too many attempts. Try later.', 429)
        u = conn.execute('SELECT is_premium FROM users WHERE username=?', (g.username,)).fetchone()
        if u['is_premium']:
            conn.commit()
            return fail('Already premium')
        row = conn.execute('SELECT code FROM premium_codes WHERE code=? AND redeemed=0',
                           (code,)).fetchone()
        if not row:
            conn.commit()
            return fail('Invalid or already used code')
        conn.execute('UPDATE premium_codes SET redeemed=1, redeemed_by=?, redeemed_at=? WHERE code=?',
                     (g.username, g.now, code))
        conn.execute('UPDATE users SET is_premium=1 WHERE username=?', (g.username,))
        conn.commit()
        return ok(state=state_of(conn, g.username))
    finally:
        conn.close()


# ===========================================================================
# Live session sync (shared with the VuliTab extension)
# ===========================================================================
VALID_SESSION_MODES = ('focus', 'stopwatch', 'long')
VALID_SESSION_STATUS = ('running', 'paused', 'idle')


@app.route('/api/v1/session', methods=['POST'])
@app_auth
def api_session_update():
    data = body()
    mode = data.get('mode', 'focus')
    status = data.get('status', 'idle')
    if mode not in VALID_SESSION_MODES:
        mode = 'focus'
    if status not in VALID_SESSION_STATUS:
        status = 'idle'
    try:
        base = max(0, min(int(data.get('baseSeconds', 0)), 86400))
        target = max(0, min(int(data.get('targetSeconds', 0)), 86400))
    except (TypeError, ValueError):
        base, target = 0, 0
    conn = get_db()
    try:
        conn.execute(
            '''INSERT INTO live_sessions (username,mode,status,started_at,base_seconds,
                 target_seconds,source,updated_at) VALUES (?,?,?,?,?,?,?,?)
               ON CONFLICT(username) DO UPDATE SET mode=excluded.mode, status=excluded.status,
                 started_at=excluded.started_at, base_seconds=excluded.base_seconds,
                 target_seconds=excluded.target_seconds, source=excluded.source,
                 updated_at=excluded.updated_at''',
            (g.username, mode, status, g.now, base, target,
             str(data.get('source', 'app'))[:20], g.now))
        conn.commit()
        return ok()
    finally:
        conn.close()


@app.route('/api/v1/session', methods=['GET'])
@app_auth
def api_session_get():
    conn = get_db()
    try:
        row = conn.execute('SELECT * FROM live_sessions WHERE username=?', (g.username,)).fetchone()
        if not row:
            return ok(session=None)
        s = dict(row)
        # Elapsed is derived from server time so a fiddled device clock changes nothing.
        if s['status'] == 'running':
            s['elapsed'] = s['base_seconds'] + max(0, g.now - s['started_at'])
        else:
            s['elapsed'] = s['base_seconds']
        return ok(session=s)
    finally:
        conn.close()


@app.route('/api/v1/focus/save', methods=['POST'])
@app_auth
def api_focus_save():
    d = body()
    conn = get_db()
    try:
        conn.execute(
            '''INSERT INTO focus_sessions (username,started_at,ended_at,productive_seconds,
                 distracted_seconds,neutral_seconds,focus_score,sites_json,created_at)
               VALUES (?,?,?,?,?,?,?,?,?)''',
            (g.username, int(d.get('started_at', 0)), int(d.get('ended_at', 0)),
             int(d.get('productive_seconds', 0)), int(d.get('distracted_seconds', 0)),
             int(d.get('neutral_seconds', 0)), int(d.get('focus_score', 0)),
             json.dumps(d.get('sites', []))[:8000], g.now))
        conn.commit()
        return ok()
    finally:
        conn.close()


@app.route('/api/v1/focus/history', methods=['POST'])
@app_auth
def api_focus_history():
    conn = get_db()
    try:
        rows = conn.execute(
            'SELECT * FROM focus_sessions WHERE username=? ORDER BY id DESC LIMIT 30',
            (g.username,)).fetchall()
        return ok(sessions=[dict(r) for r in rows])
    finally:
        conn.close()


# ===========================================================================
# Suggestions
#
# Reaching this function at all already required a request signed with the
# device secret, carrying a UTC timestamp inside MAX_CLOCK_SKEW and a nonce
# that has never been used. So there is no anonymous path in, no way to replay
# a captured submission, and no timezone in which the timestamp check drifts —
# int(time.time()) is UTC everywhere on earth.
#
# The cooldown is per ACCOUNT and lives here. The old one was enforced by the
# phone deciding whether to show the button, which is not enforcement.
# ===========================================================================
KINDS = ('general', 'bug', 'idea')
KIND_COLOUR = {'bug': 0xFF6B6B, 'idea': 0x70A1FF, 'general': 0x6BCF7F}


def flush_suggestions(conn):
    """Send anything not yet on Discord, oldest first. Stops at the first
    failure so a webhook outage can't turn one submit into a minute of retries;
    whatever is left keeps delivered=0 and goes out with the next one."""
    rows = conn.execute('SELECT * FROM suggestions WHERE delivered=0'
                        ' ORDER BY id LIMIT 5').fetchall()
    sent_any = False
    for r in rows:
        if LATEST_VERSION:
            tick = '✅ latest' if r['version'] == LATEST_VERSION else '❌ outdated'
        else:
            tick = '—  (set LATEST_VERSION)'
        if not discord(SUGGEST_HOOK, f"{r['kind'].upper()} · {r['username']}",
                       [('Message', r['message'], False),
                        ('Version', f"{r['version']}  {tick}", True),
                        ('Android', r['android'], True),
                        ('Contact', r['contact'] or 'none — reply from the console', False),
                        ('Stats', f"Console → search `{r['username']}`", False)],
                       colour=KIND_COLOUR.get(r['kind'], 0x6BCF7F), ping=True):
            break
        conn.execute('UPDATE suggestions SET delivered=1 WHERE id=?', (r['id'],))
        sent_any = True
    conn.commit()
    return sent_any


@app.route('/api/v1/feedback', methods=['POST'])
@app_auth
def api_feedback():
    d = body()
    kind = (d.get('type') or 'general').lower()
    msg = (d.get('message') or '').strip()
    if kind not in KINDS:
        kind = 'general'
    if not 5 <= len(msg) <= 2000:
        return fail('Write between 5 and 2000 characters.')

    conn = get_db()
    try:
        if rate_limit(conn, f'feedback:{g.username}', limit=1, window=FEEDBACK_COOLDOWN):
            conn.commit()
            return fail('You can send one suggestion every 48 hours.', 429)

        # Saved before anything is sent. If Discord is down this row survives and
        # is retried; it is also what the console reads and replies to.
        conn.execute(
            'INSERT INTO suggestions (username,kind,message,contact,version,android,created_at)'
            ' VALUES (?,?,?,?,?,?,?)',
            (g.username, kind, msg, (d.get('contact') or '').strip()[:120],
             (d.get('version') or '?').strip()[:40],
             (d.get('android') or '?').strip()[:40], g.now))
        conn.commit()
        return ok(delivered=flush_suggestions(conn))
    finally:
        conn.close()


# ===========================================================================
# Inbox — your console replies, on their way to the phone
# ===========================================================================
def inbox_for(conn, username):
    rows = conn.execute('SELECT id, body, reply_token FROM inbox'
                        ' WHERE username=? AND seen_at=0 ORDER BY id LIMIT 5',
                        (username,)).fetchall()
    return [{'id': r['id'], 'body': r['body'], 'token': r['reply_token'] or ''}
            for r in rows]


@app.route('/api/v1/me/inbox', methods=['POST'])
@app_auth
def api_inbox_ack():
    """Marks a message read, and carries the one reply they are allowed.

    The reply is gated by a single-use token minted with the message and burned
    here — so there is no second cooldown to maintain, and a reply cannot be
    sent twice or by anyone the message was not addressed to.
    """
    d = body()
    conn = get_db()
    try:
        row = conn.execute('SELECT * FROM inbox WHERE id=? AND username=?',
                           (int(d.get('id') or 0), g.username)).fetchone()
        if not row:
            return fail('No such message')
        conn.execute('UPDATE inbox SET seen_at=? WHERE id=?', (g.now, row['id']))

        reply = (d.get('reply') or '').strip()[:1000]
        if reply and not row['replied'] and row['reply_token'] \
                and secrets.compare_digest(str(d.get('token') or ''), row['reply_token']):
            conn.execute('UPDATE inbox SET replied=1, reply_token=NULL WHERE id=?',
                         (row['id'],))
            discord_async(SUGGEST_HOOK, f'Reply · {g.username}',
                          [('They said', reply, False)], colour=0xFFD700, ping=True)
        conn.commit()
        return ok()
    finally:
        conn.close()


# ===========================================================================
# Crash / error reports
#
# Fatal errors ping you; everything else waits quietly in the console. That
# split is deliberate — a single broken build can throw the same non-fatal
# warning thousands of times, and a Discord channel full of that is a channel
# you stop reading.
# ===========================================================================
@app.route('/api/v1/errors', methods=['POST'])
@app_auth
def api_errors():
    items = body().get('errors') or []
    if not isinstance(items, list) or not items:
        return ok(stored=0)

    conn = get_db()
    try:
        # Generous, because a genuinely broken build produces a burst — but not
        # unlimited, because this endpoint writes rows.
        if rate_limit(conn, f'errors:{g.username}', limit=40, window=3600):
            conn.commit()
            return ok(stored=0, throttled=True)

        stored, worst = 0, None
        for it in items[:20]:
            if not isinstance(it, dict):
                continue
            msg = (str(it.get('message') or '')).strip()[:500]
            if not msg:
                continue
            fatal = 1 if it.get('fatal') else 0
            conn.execute(
                'INSERT INTO app_errors (username,message,source,stack,fatal,version,android,created_at)'
                ' VALUES (?,?,?,?,?,?,?,?)',
                (g.username, msg, str(it.get('source') or '')[:300],
                 str(it.get('stack') or '')[:2000], fatal,
                 str(it.get('version') or '?')[:40],
                 str(it.get('android') or '?')[:40], g.now))
            stored += 1
            if fatal and worst is None:
                worst = (msg, str(it.get('source') or ''))
        conn.commit()

        # One ping per distinct crash per hour, not one per report.
        if worst:
            key = 'crash:' + hashlib.sha256(worst[0].encode()).hexdigest()[:16]
            if not rate_limit(conn, key, limit=1, window=3600):
                conn.commit()
                discord_async(ALERT_HOOK, 'Crash on a real device',
                              [('Error', worst[0], False), ('Where', worst[1], False),
                               ('User', g.username, True),
                               ('Console', 'Errors tab', True)],
                              colour=0xFF6B6B, ping=True)
            conn.commit()
        return ok(stored=stored)
    finally:
        conn.close()


@app.route('/api/v1/feedback/cooldown', methods=['GET', 'POST'])
@app_auth
def api_feedback_cooldown():
    conn = get_db()
    try:
        if request.method == 'POST':
            conn.execute('INSERT INTO feedback_cooldowns (ip,last_submitted) VALUES (?,?)'
                         ' ON CONFLICT (ip) DO UPDATE SET last_submitted = EXCLUDED.last_submitted',
                         (client_ip(), g.now))
            conn.commit()
            return ok()
        row = conn.execute('SELECT last_submitted FROM feedback_cooldowns WHERE ip=?',
                           (client_ip(),)).fetchone()
        remaining = 0 if not row else max(0, FEEDBACK_COOLDOWN - (g.now - row['last_submitted']))
        return jsonify({'remaining': remaining * 1000})
    finally:
        conn.close()


# ===========================================================================
# AI study plan — API keys never leave this process
#
# OpenRouter, not Groq. Groq's free tier kept retiring models underneath us —
# a dead model id doesn't fail fast, it burns a whole HTTP round trip before
# the fallback list moves on, so a stale list makes every request slow AND
# broken. OpenRouter is one OpenAI-compatible endpoint that routes across many
# providers and fails over internally, which is what the list below was trying
# to do by hand.
#
# Ordered cheapest-capable first. At 420 output tokens a plan costs a fraction
# of a penny, and /api/v1/ai/plan is premium-only and capped at 3 a day per
# user, so the whole feature is pennies a month — cheap enough that paying is
# worth it purely to stop free-tier deprecations breaking the app again. The
# :free entry is a last resort, not the plan.
#
# Verified live on OpenRouter 2026-08-28. Re-check with:
#   curl -s https://openrouter.ai/api/v1/models | grep -o '"id":"[^"]*"'
# ===========================================================================
OPENROUTER_URL = 'https://openrouter.ai/api/v1/chat/completions'


def call_ai(api_key, prompt, system_prompt=None):
    api_key = (api_key or '').strip().strip('"').strip("'")
    if not api_key:
        raise ValueError('No API key')

    models = ["google/gemini-2.5-flash-lite",
              "deepseek/deepseek-v3.2",
              "anthropic/claude-haiku-4.5",
              "z-ai/glm-5.2:free"]

    default_system = (
        "Your name is VuliAi. You are VuliAi — the personal study coach AI inside the "
        "VuliStudy app. If asked who or what you are, you are VuliAi. You are ALWAYS "
        "speaking DIRECTLY to one specific student (the user). Use the second person "
        "('you', 'your'). Never narrate in the third person. Be warm, concrete, and "
        "concise. Give specific actionable steps, not generic advice. Use plain text "
        "only — no markdown. QUOTE RULE (strict): If — and only if — a quote from the "
        "provided list genuinely fits, you may include exactly ONE quote, and it MUST "
        "be the very last line of your entire reply, on its own separate line, with "
        "absolutely NOTHING after it. If no quote fits, end normally without one. "
        "Never invent quotes. Keep your entire reply under 1250 characters."
    )

    last_error = None
    for model in models:
        payload = json.dumps({
            "model": model,
            "max_tokens": 420,
            "messages": [
                {"role": "system", "content": system_prompt or default_system},
                {"role": "user", "content": prompt},
            ],
        }).encode('utf-8')
        req = urllib.request.Request(
            OPENROUTER_URL,
            data=payload,
            headers={"Authorization": f"Bearer {api_key}",
                     "Content-Type": "application/json",
                     # OpenRouter uses these two for attribution. Neither is
                     # required, and neither identifies a user.
                     "HTTP-Referer": "https://studybuddy-r616.onrender.com",
                     "X-Title": "VuliStudy",
                     "User-Agent": "VuliStudy/2.0"})
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                result = json.loads(resp.read().decode('utf-8'))
                # OpenRouter can answer 200 and still have failed — an upstream
                # refusal comes back as an error object, and a filtered reply
                # comes back as an empty string. Both must fall through to the
                # next model rather than being returned as the study plan.
                if result.get('error'):
                    last_error = RuntimeError(
                        f"OpenRouter [{model}]: {str(result['error'])[:240]}")
                    continue
                choices = result.get('choices') or []
                content = (choices[0].get('message', {}).get('content') or '').strip() if choices else ''
                if not content:
                    last_error = RuntimeError(f'Empty reply from {model}')
                    continue
                if len(content) > 1250:
                    cut = content[:1250]
                    sp = cut.rfind(' ')
                    if sp > 1000:
                        cut = cut[:sp]
                    content = cut.rstrip()
                return content
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode('utf-8', errors='ignore')
            last_error = RuntimeError(f'OpenRouter HTTP {exc.code} [{model}]: {detail[:240]}')
            continue
        except Exception as exc:
            last_error = exc
            continue

    raise last_error or RuntimeError('OpenRouter request failed for all models')


@app.route('/api/v1/ai/plan', methods=['POST'])
@app_auth
def api_ai_plan():
    data = body()
    conn = get_db()
    try:
        u = conn.execute(
            'SELECT is_premium, total_minutes, streak, reborns, created_at, tz_offset'
            ' FROM users WHERE username=?', (g.username,)).fetchone()
        if not u or not u['is_premium']:
            return fail('Premium required', 403)

        # AI calls cost money — one plan per day, enforced here rather than by the client.
        if rate_limit(conn, f'aiplan:{g.username}', limit=3, window=86400):
            conn.commit()
            return fail('You have already generated your plans for today.', 429)
        conn.commit()

        study = data.get('studyData') or {}
        quotes = (study.get('motivationalQuotes') or [])[:25]
        convo = (study.get('conversation') or [])[:8]
        survey = study.get('survey') or {}

        daily = conn.execute(
            'SELECT day, minutes FROM daily_study WHERE username=? ORDER BY day DESC LIMIT 7',
            (g.username,)).fetchall()
        daily_minutes = [r['minutes'] for r in reversed(daily)]

        total_mins = int(u['total_minutes'] or 0)
        streak_days = int(u['streak'] or 0)
        subjects = study.get('subjects') or []
        pending = study.get('pendingTasks') or []

        from datetime import datetime, timezone as _tz
        created_ts = int(u['created_at'] or 0)
        if created_ts:
            created_human = datetime.fromtimestamp(created_ts, tz=_tz.utc).strftime('%Y-%m-%d')
            age_days = max(0, (g.now - created_ts) // 86400)
        else:
            created_human, age_days = 'unknown', -1

        server_now = datetime.now(_tz.utc).strftime('%A, %Y-%m-%d %H:%M UTC')
        local_today = economy.local_date(u['tz_offset'], g.now)

        is_low_data = (total_mins < 30 and streak_days < 2 and not subjects and not pending)
        is_followup = bool(convo)

        if is_followup:
            mode_instructions = (
                "MODE: FOLLOW-UP REPLY. The student just sent you a single reply. Respond "
                "directly to THEIR message in 2-3 short paragraphs. Adjust the plan based on "
                "what they said. Do NOT ask another question. End with one concrete next step.")
        elif is_low_data:
            mode_instructions = (
                "MODE: ONBOARDING. You don't have enough data yet to build a real plan. DO NOT "
                "produce a generic plan. Address them warmly by name and ask 3-4 specific "
                "questions, one per line, that will let you make their real plan next time. "
                "End with a short sentence saying you'll build their real plan once they reply.")
        else:
            mode_instructions = (
                "MODE: FULL PLAN. Speak to the student directly.\n"
                "1) Two sentences acknowledging where they are right now.\n"
                "2) Three specific actions for this week.\n"
                "3) A weekly rhythm using their actual numbers.\n"
                "4) Subject-specific advice if subjects are listed.\n"
                "5) One short motivational closer.")

        quotes_block = ""
        if quotes:
            quotes_block = "\nAvailable motivational quotes (use AT MOST ONE, only if it truly fits):\n" + \
                "\n".join(f'- "{str(q)[:200]}"' for q in quotes)

        convo_text = ""
        if convo:
            convo_text = "\n\nConversation so far (oldest first):\n" + \
                "\n".join(str(x)[:600] for x in convo)

        survey_block = ""
        if survey:
            survey_block = (f"\nStudent context: year group: {survey.get('yearGroup','')!r}, "
                            f"preferences: {survey.get('prefs','')!r}, "
                            f"interests: {survey.get('interests','')!r}")

        prompt = f"""You are speaking directly to "{g.username}". Address them by name when natural.

Time context:
Account created on: {created_human}{f' (account is {age_days} days old)' if age_days >= 0 else ''}.
Current server time: {server_now}. The student's local date is {local_today}.

The student's current state:
- Total minutes ever studied: {total_mins}
- Current streak: {streak_days} days
- Reborns: {u['reborns']}
- Daily minutes for the last 7 days (oldest -> today): {daily_minutes}
- Subjects they're tracking: {subjects}
- Open tasks: {pending}{survey_block}

{mode_instructions}

Hard formatting rules:
- Your name is VuliAi.
- Plain text only. No markdown symbols.
- Keep the ENTIRE response under 1250 characters.
- Always use second person.
- If you use a quote, it MUST be the final line, alone.
{quotes_block}{convo_text}"""

        for key in (API_ONE, API_TWO):
            if not key:
                continue
            try:
                return ok(plan=call_ai(key, prompt))
            except Exception as exc:
                app.logger.warning('AI provider failed: %s', exc)
                continue
        return fail('contact_owner', 503)
    finally:
        conn.close()


# ===========================================================================
# Admin API
#
# The old design asked the server "is this password correct?" and let the client
# act on the answer. Anyone who could open a debugger could skip that. Now the
# server issues a token and re-checks it on every action — revealing the panel
# achieves nothing on its own.
# ===========================================================================
@app.route('/api/v1/admin/unlock', methods=['POST'])
@app_auth
def api_admin_unlock():
    if not ADMIN_PASSWORD:
        return fail('Admin is not configured on this server.', 503)

    conn = get_db()
    try:
        if rate_limit(conn, f'adminunlock:{g.username}', limit=5, window=900):
            conn.commit()
            audit(conn, 'admin_unlock_ratelimited')
            conn.commit()
            return fail('Too many attempts. Wait 15 minutes.', 429)

        import hmac as _hmac
        if not _hmac.compare_digest(str(body().get('password', '')), ADMIN_PASSWORD):
            audit(conn, 'admin_unlock_failed')
            conn.commit()
            return fail('Incorrect code', 403)

        token = issue_admin_token(conn, g.username, g.device_id, g.now)
        audit(conn, 'admin_unlock_success')
        conn.commit()
        return ok(admin_token=token, expires_in=15 * 60)
    finally:
        conn.close()


@app.route('/api/v1/admin/user', methods=['POST'])
@admin_auth()
def api_admin_user():
    target = (body().get('username') or g.username).strip()
    conn = get_db()
    try:
        u = conn.execute('SELECT * FROM users WHERE username=?', (target,)).fetchone()
        if not u:
            return fail('User not found on server', 404)
        state = state_of(conn, target)
        audit(conn, 'admin_view_user', target)
        conn.commit()
        return ok(user=dict(u), state=state)
    finally:
        conn.close()


@app.route('/api/v1/admin/grant', methods=['POST'])
@admin_auth()
def api_admin_grant():
    data = body()
    target = (data.get('username') or g.username).strip()
    changes = data.get('changes') or {}
    conn = get_db()
    try:
        if not conn.execute('SELECT 1 FROM users WHERE username=?', (target,)).fetchone():
            return fail('User not found', 404)
        ensure_economy_row(conn, target, g.now)
        applied = economy.admin_set(conn, target, changes, g.now)
        audit(conn, 'admin_grant', target, json.dumps(applied))
        conn.commit()
        return ok(applied=applied, state=state_of(conn, target))
    except economy.EconomyError as exc:
        return fail(str(exc))
    finally:
        conn.close()


@app.route('/api/v1/admin/item', methods=['POST'])
@admin_auth()
def api_admin_item():
    data = body()
    target = (data.get('username') or g.username).strip()
    item_id = data.get('item_id')
    conn = get_db()
    try:
        ensure_economy_row(conn, target, g.now)
        res = economy.admin_grant_item(conn, target, item_id, g.now)
        audit(conn, 'admin_grant_item', target, str(item_id))
        conn.commit()
        return ok(result=res, state=state_of(conn, target))
    except economy.EconomyError as exc:
        return fail(str(exc))
    finally:
        conn.close()


@app.route('/api/v1/admin/kill', methods=['POST'])
@admin_auth()
def api_admin_kill():
    target = (body().get('username') or g.username).strip()
    conn = get_db()
    try:
        ensure_economy_row(conn, target, g.now)
        economy.admin_kill(conn, target, g.now)
        audit(conn, 'admin_kill', target)
        conn.commit()
        return ok(state=state_of(conn, target))
    except economy.EconomyError as exc:
        return fail(str(exc))
    finally:
        conn.close()


@app.route('/api/v1/admin/revive', methods=['POST'])
@admin_auth()
def api_admin_revive():
    target = (body().get('username') or g.username).strip()
    conn = get_db()
    try:
        ensure_economy_row(conn, target, g.now)
        economy.admin_revive(conn, target, g.now)
        audit(conn, 'admin_revive', target)
        conn.commit()
        return ok(state=state_of(conn, target))
    except economy.EconomyError as exc:
        return fail(str(exc))
    finally:
        conn.close()


@app.route('/api/v1/admin/export', methods=['POST'])
@admin_auth()
def api_admin_export():
    """
    Read-only dump. The matching import endpoint is gone: the old one interpolated
    client-supplied table and column names straight into SQL. Restores now happen
    from the web console, against a fixed schema.
    """
    conn = get_db()
    try:
        tables = list_tables(conn)
        # Signing secrets are never exported.
        tables = [t for t in tables if t not in ('devices', 'sessions', 'admin_tokens', 'nonces')]
        dump = {t: [dict(r) for r in conn.execute(f'SELECT * FROM "{t}"').fetchall()]
                for t in tables}
        audit(conn, 'admin_export')
        conn.commit()
        return ok(dump=dump, exported_at=g.now)
    finally:
        conn.close()


@app.route('/api/v1/admin/premium/unlock', methods=['POST'])
@admin_auth()
def api_admin_premium_unlock():
    if not SECOND_ADMIN_PASSWORD:
        return fail('Second admin password is not configured.', 503)
    conn = get_db()
    try:
        if rate_limit(conn, f'admin2:{g.username}', limit=5, window=900):
            conn.commit()
            return fail('Too many attempts.', 429)
        import hmac as _hmac
        if not _hmac.compare_digest(str(body().get('password', '')), SECOND_ADMIN_PASSWORD):
            audit(conn, 'admin_second_failed')
            conn.commit()
            return fail('Incorrect password', 403)
        conn.execute('UPDATE admin_tokens SET second_ok=1 WHERE token=?', (g.admin_token,))
        audit(conn, 'admin_second_success')
        conn.commit()
        return ok()
    finally:
        conn.close()


@app.route('/api/v1/admin/premium/set', methods=['POST'])
@admin_auth(require_second=True)
def api_admin_premium_set():
    code = (body().get('code') or '').strip()
    if not code or len(code) > 30:
        return fail('Invalid code')
    conn = get_db()
    try:
        conn.execute('INSERT INTO premium_codes (code, created_at, redeemed) VALUES (?,?,0)'
                     ' ON CONFLICT (code) DO UPDATE SET created_at = EXCLUDED.created_at,'
                     ' redeemed = 0, redeemed_by = NULL, redeemed_at = NULL',
                     (code, g.now))
        audit(conn, 'admin_premium_set', None, code)
        conn.commit()
        return ok()
    finally:
        conn.close()


@app.route('/api/v1/admin/premium/status', methods=['POST'])
@admin_auth(require_second=True)
def api_admin_premium_status():
    conn = get_db()
    try:
        row = conn.execute('SELECT * FROM premium_codes ORDER BY created_at DESC LIMIT 1').fetchone()
        return ok(code=dict(row) if row else None)
    finally:
        conn.close()


# ===========================================================================
# VuliTab compatibility
#
# The browser extension is already deployed and calls the old paths. These shims
# keep it working. They are token-authenticated but not signature-checked, since
# the extension has no device secret — so they are deliberately limited to reads
# and the live-session sync, and cannot touch the economy.
# ===========================================================================
def _vt_user(conn, token):
    if not token:
        return None
    row = conn.execute('SELECT username, last_seen FROM sessions WHERE token=?',
                       (token,)).fetchone()
    if not row:
        return None
    conn.execute('UPDATE sessions SET last_seen=? WHERE token=?', (int(time.time()), token))
    return row['username']


@app.route('/login', methods=['POST'])
def vt_login():
    data = body()
    username = (data.get('username') or '').strip()
    password = data.get('password') or ''
    if not username or not password:
        return jsonify({'success': False, 'error': 'Missing credentials'})
    conn = get_db()
    try:
        if rate_limit(conn, f'vtlogin:{client_ip()}', limit=20, window=900):
            conn.commit()
            return jsonify({'success': False, 'error': 'Too many attempts'})
        user = conn.execute('SELECT username, password_hash, is_banned FROM users WHERE username=?',
                            (username,)).fetchone()
        if not user or not verify_password(password, user['password_hash']):
            conn.commit()
            return jsonify({'success': False, 'error': 'Incorrect username or password'})
        if user['is_banned']:
            conn.commit()
            return jsonify({'success': False, 'error': 'Account disabled'})
        token = issue_token(conn, username, 'vulitab-extension', 'vulitab')
        conn.commit()
        return jsonify({'success': True, 'token': token,
                        'profile': economy.snapshot(conn, username)})
    finally:
        conn.close()


@app.route('/vt-profile', methods=['POST'])
def vt_profile():
    conn = get_db()
    try:
        username = _vt_user(conn, body().get('token', ''))
        if not username:
            return jsonify({'success': False, 'error': 'auth'})
        conn.commit()
        return jsonify({'success': True, 'profile': economy.snapshot(conn, username)})
    finally:
        conn.close()


@app.route('/vt-logout', methods=['POST'])
def vt_logout():
    token = body().get('token', '')
    if token:
        conn = get_db()
        try:
            conn.execute('DELETE FROM sessions WHERE token=?', (token,))
            conn.commit()
        finally:
            conn.close()
    return jsonify({'success': True})


@app.route('/session-update', methods=['POST'])
def vt_session_update():
    data = body()
    conn = get_db()
    try:
        username = _vt_user(conn, data.get('token', ''))
        if not username:
            return jsonify({'success': False, 'error': 'auth'})
        mode = data.get('mode', 'focus')
        status = data.get('status', 'idle')
        if mode not in VALID_SESSION_MODES:
            mode = 'focus'
        if status not in VALID_SESSION_STATUS:
            status = 'idle'
        now = int(time.time())
        try:
            base = max(0, min(int(data.get('baseSeconds', 0)), 86400))
            target = max(0, min(int(data.get('targetSeconds', 0)), 86400))
        except (TypeError, ValueError):
            base, target = 0, 0
        conn.execute(
            '''INSERT INTO live_sessions (username,mode,status,started_at,base_seconds,
                 target_seconds,source,updated_at) VALUES (?,?,?,?,?,?,?,?)
               ON CONFLICT(username) DO UPDATE SET mode=excluded.mode, status=excluded.status,
                 started_at=excluded.started_at, base_seconds=excluded.base_seconds,
                 target_seconds=excluded.target_seconds, source=excluded.source,
                 updated_at=excluded.updated_at''',
            (username, mode, status, now, base, target,
             str(data.get('source', 'vulitab'))[:20], now))
        conn.commit()
        return jsonify({'success': True})
    finally:
        conn.close()


@app.route('/session-get', methods=['POST'])
def vt_session_get():
    conn = get_db()
    try:
        username = _vt_user(conn, body().get('token', ''))
        if not username:
            return jsonify({'success': False, 'error': 'auth'})
        row = conn.execute('SELECT * FROM live_sessions WHERE username=?', (username,)).fetchone()
        conn.commit()
        if not row:
            return jsonify({'success': True, 'session': None})
        s = dict(row)
        now = int(time.time())
        s['elapsed'] = s['base_seconds'] + (max(0, now - s['started_at'])
                                            if s['status'] == 'running' else 0)
        return jsonify({'success': True, 'session': s})
    finally:
        conn.close()


@app.route('/focus-session-save', methods=['POST'])
def vt_focus_save():
    d = body()
    conn = get_db()
    try:
        username = _vt_user(conn, d.get('token', ''))
        if not username:
            return jsonify({'success': False, 'error': 'auth'})
        conn.execute(
            '''INSERT INTO focus_sessions (username,started_at,ended_at,productive_seconds,
                 distracted_seconds,neutral_seconds,focus_score,sites_json,created_at)
               VALUES (?,?,?,?,?,?,?,?,?)''',
            (username, int(d.get('started_at', 0)), int(d.get('ended_at', 0)),
             int(d.get('productive_seconds', 0)), int(d.get('distracted_seconds', 0)),
             int(d.get('neutral_seconds', 0)), int(d.get('focus_score', 0)),
             json.dumps(d.get('sites', []))[:8000], int(time.time())))
        conn.commit()
        return jsonify({'success': True})
    finally:
        conn.close()


@app.route('/focus-history', methods=['POST'])
def vt_focus_history():
    conn = get_db()
    try:
        username = _vt_user(conn, body().get('token', ''))
        if not username:
            return jsonify({'success': False, 'error': 'auth'})
        rows = conn.execute(
            'SELECT * FROM focus_sessions WHERE username=? ORDER BY id DESC LIMIT 30',
            (username,)).fetchall()
        conn.commit()
        return jsonify({'success': True, 'sessions': [dict(r) for r in rows]})
    finally:
        conn.close()


# ---- productivity classifier (pure function, no account data) --------------
STUDY_HINTS = ('khan', 'quizlet', 'wikipedia', 'scholar', 'docs.google', 'notion',
               'anki', 'coursera', 'edx', 'bbc.co.uk/bitesize', 'savemyexams',
               'physicsandmathstutor', 'desmos', 'wolframalpha', 'overleaf')
DISTRACT_HINTS = ('youtube', 'tiktok', 'instagram', 'reddit', 'twitter', 'x.com',
                  'facebook', 'snapchat', 'twitch', 'netflix', 'discord', 'roblox')


@app.route('/classify-productivity', methods=['POST'])
def classify_productivity_route():
    d = body()
    url = (d.get('url') or '').lower()
    title = (d.get('title') or '').lower()
    blob = f'{url} {title}'
    if any(h in blob for h in STUDY_HINTS):
        return jsonify({'category': 'productive'})
    if any(h in blob for h in DISTRACT_HINTS):
        return jsonify({'category': 'distracted'})
    return jsonify({'category': 'neutral'})


# ===========================================================================
# The human-facing site
# ===========================================================================
import adminsite  # noqa: E402  (registers routes + the WEB_PASSWORD gate)

adminsite.register(app)


if __name__ == '__main__':
    app.run(host='0.0.0.0', port=int(os.environ.get('PORT', 5000)))
