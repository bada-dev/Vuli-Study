"""
The page for editing everything

Project: VuliStudy
Author: ETHANTYAGI
ALL RIGHTS RESERVED
"""

import hmac
import json
import os
import secrets
import time

from flask import (Response, abort, g, redirect, render_template_string, request,
                   session, url_for)

import economy
from db import get_db

WEB_PASSWORD = os.environ.get('WEB_PASSWORD')
SECRET_KEY = os.environ.get('SECRET_KEY')

# Paths the browser gate must never intercept.
EXEMPT_PREFIXES = ('/api/', '/healthz', '/login', '/vt-', '/session-', '/focus-',
                   '/classify-productivity')

# ---------------------------------------------------------------------------
# What may be edited, and within what bounds.
#
# A column not listed here cannot be written through this console at all, no
# matter what is posted. Passwords, tokens and device secrets are absent by
# design — there is no screen anywhere that can set them.
# ---------------------------------------------------------------------------
EDITABLE = {
    'users': {
        'pk': 'username',
        'columns': {
            'is_active':   ('int', 0, 1),
            'is_premium':  ('int', 0, 1),
            'is_banned':   ('int', 0, 1),
            'total_minutes': ('int', 0, economy.MAX_TOTAL_MINUTES),
            'streak':      ('int', 0, economy.MAX_STREAK),
            'reborns':     ('int', 0, economy.MAX_REBORNS),
            'character_width': ('int', 140, 420),
            'equipped_cosmetic': ('choice', economy.VALID_COSMETICS),
            'active_background': ('choice', economy.VALID_BACKGROUNDS),
        },
    },
    'economy': {
        'pk': 'username',
        'columns': {
            'coins':           ('int', 0, 10_000_000),
            'carrots':         ('int', 0, 100_000),
            'happiness':       ('int', 0, 100),
            'streak':          ('int', 0, economy.MAX_STREAK),
            'streak_freeze':   ('int', 0, 1),
            'has_book':        ('int', 0, 1),
            'is_dead':         ('int', 0, 1),
            'revivals':        ('int', 0, 100_000),
            'carrots_fed':     ('int', 0, 1_000_000),
            'reborns':         ('int', 0, economy.MAX_REBORNS),
            'longest_session': ('int', 0, economy.MAX_SESSION_MINUTES),
        },
    },
    'premium_codes': {
        'pk': 'code',
        'columns': {'redeemed': ('int', 0, 1)},
    },
    'chats': {
        'pk': 'id',
        'columns': {'name': ('text', 30)},
    },
}

# Read-only views, for looking without touching.
VIEWABLE = ['users', 'economy', 'premium_codes', 'chats', 'chat_members',
            'chat_messages', 'friend_requests', 'daily_study', 'weekly_study',
            'focus_sessions', 'live_sessions', 'admin_audit', 'processed_events']


# ---------------------------------------------------------------------------
# Templates — one shared shell, kept intentionally plain.
# ---------------------------------------------------------------------------
SHELL = """
<!doctype html><html><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>VuliStudy Console</title>
<style>
 *{box-sizing:border-box}
 body{margin:0;font:14px/1.5 ui-monospace,SFMono-Regular,Menlo,Consolas,monospace;
      background:#14171c;color:#dfe4ea}
 header{background:#1d2129;padding:12px 18px;border-bottom:1px solid #2c313a;
        display:flex;gap:16px;align-items:center;flex-wrap:wrap}
 header b{color:#f0c674;font-size:15px}
 nav a{color:#8ab4f8;text-decoration:none;margin-right:12px;font-size:12px}
 nav a:hover{text-decoration:underline}
 nav a.on{color:#f0c674;font-weight:700}
 main{padding:18px;max-width:100%;overflow-x:auto}
 table{border-collapse:collapse;width:100%;font-size:12px;white-space:nowrap}
 th,td{border:1px solid #2c313a;padding:5px 8px;text-align:left}
 th{background:#1d2129;color:#9aa4b2;position:sticky;top:0}
 tr:nth-child(even) td{background:#181b21}
 input,select{background:#0f1216;color:#dfe4ea;border:1px solid #333a45;
              border-radius:4px;padding:3px 6px;font:inherit;font-size:12px;width:100%}
 button{background:#2f7d4f;color:#fff;border:0;border-radius:5px;
        padding:6px 13px;font:inherit;font-weight:700;cursor:pointer}
 button.danger{background:#a33}
 .msg{padding:9px 12px;border-radius:6px;margin-bottom:14px;font-size:13px}
 .ok{background:#1d3a28;color:#8fe0ab;border:1px solid #2f7d4f}
 .err{background:#3a1d1d;color:#e08f8f;border:1px solid #a33}
 .hint{color:#7c8694;font-size:12px;margin-bottom:12px}
 form.inline{display:flex;gap:6px;align-items:center}
 .wrap{overflow-x:auto;border:1px solid #2c313a;border-radius:6px}
</style></head><body>
<header>
  <b>VuliStudy Console</b>
  <nav>
    {% for t in viewable %}
      <a href="{{ url_for('console_table', table=t) }}"
         class="{{ 'on' if t == table else '' }}">{{ t }}</a>
    {% endfor %}
  </nav>
  <form method="post" action="{{ url_for('console_logout') }}" style="margin-left:auto">
    <button class="danger">Log out</button>
  </form>
</header>
<main>{{ content|safe }}</main>
</body></html>
"""

LOGIN_PAGE = """
<!doctype html><html><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>VuliStudy</title>
<style>
 body{margin:0;height:100vh;display:flex;align-items:center;justify-content:center;
      background:#14171c;color:#dfe4ea;font:14px ui-monospace,Menlo,Consolas,monospace}
 form{background:#1d2129;padding:30px;border-radius:10px;border:1px solid #2c313a;
      width:300px;text-align:center}
 h1{margin:0 0 6px;font-size:17px;color:#f0c674}
 p{margin:0 0 18px;color:#7c8694;font-size:12px}
 input{width:100%;padding:10px;margin-bottom:12px;background:#0f1216;color:#dfe4ea;
       border:1px solid #333a45;border-radius:6px;font:inherit}
 button{width:100%;padding:10px;background:#2f7d4f;color:#fff;border:0;
        border-radius:6px;font:inherit;font-weight:700;cursor:pointer}
 .err{color:#e08f8f;font-size:12px;margin-bottom:10px}
</style></head><body>
<form method="post">
  <h1>VuliStudy</h1>
  <p>Restricted console</p>
  {% if error %}<div class="err">{{ error }}</div>{% endif %}
  <input type="password" name="password" placeholder="Password" autofocus autocomplete="current-password">
  <button>Enter</button>
</form>
</body></html>
"""


def _looks_like_browser():
    """
    A person navigating, as opposed to a client calling an API.

    Browsers ask for HTML and submit forms; API clients send JSON. Getting this
    wrong is only a cosmetic issue (a redirect instead of a 404), so the test is
    kept simple and biased towards treating unknowns as non-browser.
    """
    if request.is_json or request.mimetype == 'application/json':
        return False
    if request.headers.get('X-Requested-With') == 'XMLHttpRequest':
        return False
    if request.headers.get('X-Vuli-Token'):
        return False
    accept = request.headers.get('Accept', '')
    return 'text/html' in accept or accept in ('', '*/*')


def _coerce(spec, raw):
    """Validate a submitted value against its column spec. Raises ValueError too annoying."""
    kind = spec[0]
    if kind == 'int':
        _, lo, hi = spec
        value = int(raw)
        if not (lo <= value <= hi):
            raise ValueError(f'must be between {lo} and {hi}')
        return value
    if kind == 'choice':
        allowed = spec[1]
        value = raw if raw != '' else None
        if value not in allowed:
            raise ValueError('not an allowed value')
        return value
    if kind == 'text':
        maxlen = spec[1]
        value = str(raw)
        if len(value) > maxlen:
            raise ValueError(f'longer than {maxlen} characters')
        return value
    raise ValueError('unsupported column')


def register(app):
    app.secret_key = SECRET_KEY or secrets.token_hex(32)
    app.config.update(
        SESSION_COOKIE_HTTPONLY=True,
        SESSION_COOKIE_SAMESITE='Lax',
        SESSION_COOKIE_SECURE=True,
        PERMANENT_SESSION_LIFETIME=8 * 3600,
    )

    # -----------------------------------------------------------------------
    # The G A T E.
    # -----------------------------------------------------------------------
    @app.before_request
    def _gate():
        path = request.path
        if any(path.startswith(p) for p in EXEMPT_PREFIXES):
            return None
        if path == '/console/login' or path.startswith('/static/'):
            return None

        if not WEB_PASSWORD or not SECRET_KEY:
            # Fail closed. An unconfigured server must not be an open one.
            return Response(
                'HOW do you forget to set the variables g',
                status=503, mimetype='text/plain')

        if not _looks_like_browser():
            return Response(json.dumps({'success': False, 'error': 'not_found'}),
                            status=404, mimetype='application/json')

        if not session.get('web_ok'):
            return redirect(url_for('console_login', next=path))
        return None

    # -----------------------------------------------------------------------
    # login
    # -----------------------------------------------------------------------
    @app.route('/console/login', methods=['GET', 'POST'])
    def console_login():
        if not WEB_PASSWORD or not SECRET_KEY:
            return Response('This server is not configured.', status=503,
                            mimetype='text/plain')

        error = None
        if request.method == 'POST':
            conn = get_db()
            try:
                from security import rate_limit, client_ip
                if rate_limit(conn, f'weblogin:{client_ip()}', limit=8, window=900):
                    conn.commit()
                    error = 'Too many attempts. Wait 15 minutes.'
                elif hmac.compare_digest(request.form.get('password', ''), WEB_PASSWORD):
                    session.permanent = True
                    session['web_ok'] = True
                    session['since'] = int(time.time())
                    conn.commit()
                    nxt = request.args.get('next', '')
                    return redirect(nxt if nxt.startswith('/') else url_for('console_home'))
                else:
                    conn.commit()
                    error = 'Incorrect password.'
            finally:
                conn.close()

        return render_template_string(LOGIN_PAGE, error=error)

    @app.route('/console/logout', methods=['POST'])
    def console_logout():
        session.clear()
        return redirect(url_for('console_login'))

    # -----------------------------------------------------------------------
    # Long ables
    # -----------------------------------------------------------------------
    def _render(table, content):
        return render_template_string(SHELL, viewable=VIEWABLE, table=table,
                                      content=content)

    @app.route('/')
    def console_home():
        return redirect(url_for('console_table', table='users'))

    @app.route('/console/<table>', methods=['GET'])
    def console_table(table):
        if table not in VIEWABLE:
            abort(404)

        conn = get_db()
        try:
            rows = conn.execute(
                f'SELECT * FROM "{table}" ORDER BY rowid DESC LIMIT 300').fetchall()
            cols = rows[0].keys() if rows else [
                r[1] for r in conn.execute(f'PRAGMA table_info("{table}")').fetchall()]
        finally:
            conn.close()

        spec = EDITABLE.get(table)
        editable_cols = spec['columns'] if spec else {}
        pk = spec['pk'] if spec else None

        parts = []
        msg = request.args.get('msg')
        err = request.args.get('err')
        if msg:
            parts.append(f'<div class="msg ok">{msg}</div>')
        if err:
            parts.append(f'<div class="msg err">{err}</div>')

        if spec:
            parts.append(
                f'<div class="hint">Editable columns in <b>{table}</b>: '
                f'{", ".join(editable_cols)}. Everything else is read-only. '
                'Changes are written straight to the live database.</div>')
        else:
            parts.append(f'<div class="hint"><b>{table}</b> is read-only.</div>')

        # Hide anything that could leak a credential, even though none of these
        # columns are editable.
        hidden = {'password_hash', 'device_secret', 'token', 'result_json', 'sites_json'}
        shown = [c for c in cols if c not in hidden]

        head = ''.join(f'<th>{c}</th>' for c in shown)
        if spec:
            head += '<th>save</th>'

        body_rows = []
        for row in rows:
            d = dict(row)
            cells = []
            key_value = d.get(pk) if pk else None
            for c in shown:
                if spec and c in editable_cols:
                    col_spec = editable_cols[c]
                    if col_spec[0] == 'choice':
                        opts = ''.join(
                            f'<option value="{"" if o is None else o}"'
                            f'{" selected" if d.get(c) == o else ""}>'
                            f'{"(none)" if o is None else o}</option>'
                            for o in col_spec[1])
                        cells.append(f'<td><select name="{c}" form="f_{key_value}">{opts}</select></td>')
                    else:
                        cells.append(
                            f'<td><input name="{c}" form="f_{key_value}" '
                            f'value="{"" if d.get(c) is None else d.get(c)}"></td>')
                else:
                    value = '' if d.get(c) is None else str(d.get(c))
                    if len(value) > 90:
                        value = value[:90] + '…'
                    cells.append(f'<td>{value}</td>')

            if spec:
                cells.append(
                    f'<td><form id="f_{key_value}" class="inline" method="post" '
                    f'action="{url_for("console_save", table=table)}">'
                    f'<input type="hidden" name="__pk" value="{key_value}">'
                    f'<button>save</button></form></td>')
            body_rows.append('<tr>' + ''.join(cells) + '</tr>')

        parts.append(
            f'<div class="wrap"><table><thead><tr>{head}</tr></thead>'
            f'<tbody>{"".join(body_rows)}</tbody></table></div>')

        if not rows:
            parts.append('<p class="hint">No rows yet.</p>')

        return _render(table, ''.join(parts))

    @app.route('/console/<table>/save', methods=['POST'])
    def console_save(table):
        spec = EDITABLE.get(table)
        if not spec:
            abort(404)

        pk_value = request.form.get('__pk')
        if not pk_value:
            return redirect(url_for('console_table', table=table, err='Missing row key.'))

        updates, problems = {}, []
        for col, col_spec in spec['columns'].items():
            if col not in request.form:
                continue
            try:
                updates[col] = _coerce(col_spec, request.form[col])
            except (ValueError, TypeError) as exc:
                problems.append(f'{col}: {exc}')

        if problems:
            return redirect(url_for('console_table', table=table,
                                    err='Rejected — ' + '; '.join(problems)))
        if not updates:
            return redirect(url_for('console_table', table=table, err='Nothing to change.'))

        # Column names come only from the whitelist above, never from the request.
        assignments = ', '.join(f'"{c}" = ?' for c in updates)
        params = list(updates.values()) + [pk_value]

        conn = get_db()
        try:
            conn.execute(
                f'UPDATE "{table}" SET {assignments} WHERE "{spec["pk"]}" = ?', params)
            conn.execute(
                'INSERT INTO admin_audit (actor, device_id, action, target, detail, ip, at)'
                ' VALUES (?,?,?,?,?,?,?)',
                ('web-console', 'browser', f'edit:{table}', pk_value,
                 json.dumps(updates), request.remote_addr, int(time.time())))
            conn.commit()
        finally:
            conn.close()

        return redirect(url_for('console_table', table=table,
                                msg=f'Updated {pk_value}: ' +
                                    ', '.join(f'{k}={v}' for k, v in updates.items())))

    # -----------------------------------------------------------------------
    # small operational extras
    # -----------------------------------------------------------------------
    @app.route('/console/premium/new', methods=['POST'])
    def console_new_code():
        code = (request.form.get('code') or '').strip()
        if not code or len(code) > 30:
            return redirect(url_for('console_table', table='premium_codes',
                                    err='Code must be 1-30 characters.'))
        conn = get_db()
        try:
            conn.execute('INSERT OR REPLACE INTO premium_codes (code, created_at, redeemed)'
                         ' VALUES (?,?,0)', (code, int(time.time())))
            conn.commit()
        finally:
            conn.close()
        return redirect(url_for('console_table', table='premium_codes',
                                msg=f'Created code {code}.'))
