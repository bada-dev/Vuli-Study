"""
The page for editing everything

Project: VuliStudy
Author: ETHANTYAGI
ALL RIGHTS RESERVED
"""

import hmac
import html
import json
import os
import secrets
import time

from flask import (Response, abort, g, redirect, render_template_string, request,
                   session, url_for)

import economy
from db import ensure_economy_row, get_db, table_columns
from security import hash_password, password_problem

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
VIEWABLE = ['users', 'economy', 'premium_codes', 'inbox', 'chats', 'chat_members',
            'chat_messages', 'friend_requests', 'daily_study', 'weekly_study',
            'focus_sessions', 'live_sessions', 'admin_audit', 'processed_events']

# Tables worth searching by person. Anything here gets a search box that filters
# on this column; everything else just lists.
SEARCH_COLUMN = {
    'users': 'username', 'economy': 'username', 'inbox': 'username',
    'daily_study': 'username', 'weekly_study': 'username',
    'focus_sessions': 'username', 'live_sessions': 'username',
    'chat_members': 'username', 'chat_messages': 'username',
    'premium_codes': 'code', 'admin_audit': 'target',
}


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
 .card{background:#1d2129;border:1px solid #2c313a;border-radius:8px;padding:14px;
       margin-bottom:14px}
 .card h3{margin:0 0 4px;font-size:13px;color:#f0c674}
 .card p{margin:0 0 10px;color:#7c8694;font-size:12px}
 .row{display:flex;gap:8px;flex-wrap:wrap;align-items:center}
 .row input,.row select{width:auto;min-width:150px;flex:1}
 .grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(330px,1fr));gap:14px}
 textarea{background:#0f1216;color:#dfe4ea;border:1px solid #333a45;border-radius:4px;
          padding:6px;font:inherit;font-size:12px;width:100%;min-height:58px}
 .sg{border:1px solid #2c313a;border-radius:8px;padding:12px;margin-bottom:12px;
     background:#181b21}
 .sg .meta{color:#7c8694;font-size:11px;margin-bottom:6px}
 .sg .body{white-space:pre-wrap;margin-bottom:9px;font-size:13px}
 .tag{border-radius:4px;padding:1px 6px;font-size:11px;font-weight:700}
 .bug{background:#4a2222;color:#e08f8f} .idea{background:#22304a;color:#8ab4f8}
 .general{background:#1d3a28;color:#8fe0ab}
</style></head><body>
<header>
  <b>VuliStudy Console</b>
  <nav>
    <a href="{{ url_for('console_suggestions') }}"
       class="{{ 'on' if table == 'suggestions' else '' }}">suggestions</a>
    <a href="{{ url_for('console_errors') }}"
       class="{{ 'on' if table == 'errors' else '' }}">errors</a>
    <a href="{{ url_for('console_ops') }}"
       class="{{ 'on' if table == 'ops' else '' }}">ops</a>
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


def _esc(value):
    """Everything user-typed goes through here before it reaches the page.

    This stopped being optional the moment suggestions existed: their text is
    written by users and read here. Un-escaped, a suggestion containing a script
    tag would run with your console session — which is every admin power there
    is. The console is plain HTML by choice, so the escaping has to be explicit.
    """
    return html.escape('' if value is None else str(value), quote=True)


def _order_column(conn, table):
    """
    Newest-first ordering without SQLite's rowid, which Postgres doesn't have.
    Prefers a real id, then a timestamp, then falls back to the first column.
    """
    cols = table_columns(conn, table)
    for candidate in ('id', 'at', 'created_at', 'timestamp', 'processed_at',
                      'updated_at', 'last_seen', 'week_start', 'day'):
        if candidate in cols:
            return candidate
    return cols[0] if cols else 'username'


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

        q = (request.args.get('q') or '').strip()
        search_col = SEARCH_COLUMN.get(table)

        conn = get_db()
        try:
            # Postgres has no rowid, so order by whatever this table actually
            # has: an id, else a timestamp, else its primary key.
            order = _order_column(conn, table)
            # The column name comes from SEARCH_COLUMN, never from the request;
            # the term itself is always a bound parameter.
            if q and search_col:
                rows = conn.execute(
                    f'SELECT * FROM "{table}" WHERE "{search_col}" ILIKE ?'
                    f' ORDER BY "{order}" DESC LIMIT 300', (f'%{q}%',)).fetchall()
            else:
                rows = conn.execute(
                    f'SELECT * FROM "{table}" ORDER BY "{order}" DESC LIMIT 300').fetchall()
            cols = list(rows[0].keys()) if rows else table_columns(conn, table)
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

        if search_col:
            parts.append(
                f'<form class="inline" method="get" style="margin-bottom:12px">'
                f'<input name="q" value="{_esc(q)}" placeholder="search {search_col}…" '
                f'style="max-width:280px" autofocus><button>search</button>'
                + (f'<a href="?" style="color:#8ab4f8;font-size:12px">clear</a>' if q else '')
                + '</form>')

        if table == 'premium_codes':
            parts.append(
                '<div class="card"><h3>New / reset a premium code</h3>'
                '<p>An existing code is reset to unredeemed.</p>'
                f'<form class="row" method="post" action="{url_for("console_new_code")}">'
                '<input name="code" placeholder="CODE" maxlength="30" required>'
                '<button>create</button></form></div>')

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
                    cells.append(f'<td>{_esc(value)}</td>')

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
    # Suggestions — read them, and reply straight back into the app
    # -----------------------------------------------------------------------
    @app.route('/console/suggestions', methods=['GET'])
    def console_suggestions():
        q = (request.args.get('q') or '').strip()
        conn = get_db()
        try:
            sql = 'SELECT * FROM suggestions'
            args = ()
            if q:
                sql += ' WHERE username ILIKE ? OR message ILIKE ?'
                args = (f'%{q}%', f'%{q}%')
            rows = conn.execute(sql + ' ORDER BY id DESC LIMIT 200', args).fetchall()
            undelivered = conn.execute(
                'SELECT COUNT(*) c FROM suggestions WHERE delivered=0').fetchone()['c']
        finally:
            conn.close()

        parts = []
        for key, cls in (('msg', 'ok'), ('err', 'err')):
            if request.args.get(key):
                parts.append(f'<div class="msg {cls}">{_esc(request.args[key])}</div>')
        if undelivered:
            parts.append(f'<div class="msg err">{undelivered} not yet delivered to '
                         'Discord. They retry on the next submission — check '
                         'SUGGEST_HOOK is set correctly.</div>')

        parts.append(
            f'<form class="inline" method="get" style="margin-bottom:12px">'
            f'<input name="q" value="{_esc(q)}" placeholder="search user or text…" '
            f'style="max-width:280px"><button>search</button></form>')

        for r in rows:
            kind = r['kind'] if r['kind'] in ('bug', 'idea', 'general') else 'general'
            when = time.strftime('%Y-%m-%d %H:%M', time.gmtime(r['created_at']))
            replied = f'<div class="meta">You replied: {_esc(r["reply"])}</div>' if r['reply'] else ''
            parts.append(
                f'<div class="sg">'
                f'<div class="meta"><span class="tag {kind}">{kind}</span> '
                f'<b>{_esc(r["username"])}</b> · {when} UTC · v{_esc(r["version"])} '
                f'· Android {_esc(r["android"])} '
                f'· {"delivered" if r["delivered"] else "NOT delivered"}</div>'
                f'<div class="body">{_esc(r["message"])}</div>'
                f'<div class="meta">contact: {_esc(r["contact"]) or "none"}</div>'
                f'{replied}'
                f'<form method="post" action="{url_for("console_reply")}">'
                f'<input type="hidden" name="id" value="{r["id"]}">'
                f'<textarea name="body" placeholder="Reply to {_esc(r["username"])} — '
                f'shows in the app next time they open it"></textarea>'
                f'<div class="row" style="margin-top:6px"><button>send reply</button>'
                f'<a href="{url_for("console_table", table="users", q=r["username"])}" '
                f'style="color:#8ab4f8;font-size:12px">their account →</a></div>'
                f'</form></div>')

        if not rows:
            parts.append('<p class="hint">Nothing yet.</p>')
        return _render('suggestions', ''.join(parts))

    @app.route('/console/errors', methods=['GET'])
    def console_errors():
        """Crashes and errors from real phones, newest first.

        Grouped by message so one broken build reads as "this failed 340 times
        for 12 people" rather than 340 separate rows you have to scroll past.
        """
        conn = get_db()
        try:
            rows = conn.execute(
                '''SELECT message, source, MAX(stack) AS stack, MAX(fatal) AS fatal,
                          MAX(version) AS version, MAX(android) AS android,
                          COUNT(*) AS hits, COUNT(DISTINCT username) AS users,
                          MAX(created_at) AS last_at
                   FROM app_errors GROUP BY message, source
                   ORDER BY MAX(created_at) DESC LIMIT 200''').fetchall()
        finally:
            conn.close()

        parts = ['<form class="inline" method="post" '
                 f'action="{url_for("console_errors_clear")}" style="margin-bottom:12px">'
                 '<button>clear all</button></form>']
        for r in rows:
            when = time.strftime('%Y-%m-%d %H:%M', time.gmtime(r['last_at']))
            tag = 'err' if r['fatal'] else 'ok'
            parts.append(
                f'<div class="sg">'
                f'<div class="meta"><span class="tag {tag}">'
                f'{"CRASH" if r["fatal"] else "error"}</span> '
                f'{r["hits"]}× across {r["users"]} user(s) · last {when} UTC · '
                f'v{_esc(r["version"])} · Android {_esc(r["android"])}</div>'
                f'<div class="body">{_esc(r["message"])}</div>'
                f'<div class="meta">at {_esc(r["source"])}</div>'
                + (f'<pre style="white-space:pre-wrap;font-size:11px;opacity:.75">'
                   f'{_esc(r["stack"])}</pre>' if r['stack'] else '')
                + '</div>')
        if not rows:
            parts.append('<p class="hint">No errors reported. That is the good outcome.</p>')
        return _render('errors', ''.join(parts))

    @app.route('/console/errors/clear', methods=['POST'])
    def console_errors_clear():
        conn = get_db()
        try:
            conn.execute('DELETE FROM app_errors')
            conn.commit()
        finally:
            conn.close()
        return redirect(url_for('console_errors'))

    @app.route('/console/ops/reset-password', methods=['POST'])
    def console_reset_password():
        """Password reset, issued by you, by hand.

        There is no email on file for anyone, so this is the only reset route
        that can exist. The account facts are on the users page, so you can ask
        the person something only they would know before you do this.
        """
        username = (request.form.get('username') or '').strip()
        new_pw = request.form.get('password') or ''
        problem = password_problem(new_pw)
        if problem:
            return redirect(url_for('console_ops', err=problem))
        conn = get_db()
        try:
            row = conn.execute('SELECT 1 FROM users WHERE username=?',
                               (username,)).fetchone()
            if not row:
                return redirect(url_for('console_ops', err='No such user'))
            conn.execute('UPDATE users SET password_hash=? WHERE username=?',
                         (hash_password(new_pw), username))
            # Every existing session dies, so if someone else was in the
            # account, this is what removes them.
            conn.execute('DELETE FROM sessions WHERE username=?', (username,))
            conn.execute(
                'INSERT INTO admin_audit (actor, device_id, action, target, detail, ip, at)'
                ' VALUES (?,?,?,?,?,?,?)',
                (g.get('console_user', 'console'), 'console', 'reset_password',
                 username, 'password reset + all sessions revoked',
                 request.remote_addr or '', int(time.time())))
            conn.commit()
        finally:
            conn.close()
        return redirect(url_for('console_ops',
                                msg=f'Password reset for {username}. '
                                    'They must log in again on every device.'))

    @app.route('/console/reply', methods=['POST'])
    def console_reply():
        sid = (request.form.get('id') or '').strip()
        text = (request.form.get('body') or '').strip()[:1000]
        if not text:
            return redirect(url_for('console_suggestions', err='Write something first.'))
        conn = get_db()
        try:
            row = conn.execute('SELECT username FROM suggestions WHERE id=?',
                               (sid,)).fetchone()
            if not row:
                return redirect(url_for('console_suggestions', err='No such suggestion.'))
            # The reply token is minted here and travels with the message. It is
            # what lets them answer exactly once without a second cooldown.
            conn.execute('INSERT INTO inbox (username, body, created_at, reply_token)'
                         ' VALUES (?,?,?,?)',
                         (row['username'], text, int(time.time()), secrets.token_hex(16)))
            conn.execute('UPDATE suggestions SET reply=? WHERE id=?', (text, sid))
            conn.execute(
                'INSERT INTO admin_audit (actor, device_id, action, target, detail, ip, at)'
                ' VALUES (?,?,?,?,?,?,?)',
                ('web-console', 'browser', 'reply', row['username'], text[:200],
                 request.remote_addr, int(time.time())))
            conn.commit()
        finally:
            conn.close()
        return redirect(url_for('console_suggestions',
                                msg=f'Reply queued for {row["username"]}.'))

    # -----------------------------------------------------------------------
    # Ops — the switches that are not row edits
    # -----------------------------------------------------------------------
    @app.route('/console/ops', methods=['GET'])
    def console_ops():
        conn = get_db()
        try:
            row = conn.execute("SELECT value FROM settings WHERE key='lockdown'").fetchone()
            locked = bool(row and row['value'] == '1')
            total = conn.execute('SELECT COUNT(*) c FROM users').fetchone()['c']
        finally:
            conn.close()

        parts = []
        for key, cls in (('msg', 'ok'), ('err', 'err')):
            if request.args.get(key):
                parts.append(f'<div class="msg {cls}">{_esc(request.args[key])}</div>')

        cap = os.environ.get('MAX_ACC') or 'unset'
        parts.append(
            f'<div class="card"><h3>Signups</h3>'
            f'<p>{total} accounts. MAX_ACC is <b>{_esc(cap)}</b> (set in Render). '
            f'Lockdown is a manual override and beats the cap either way.</p>'
            f'<form class="row" method="post" action="{url_for("console_lockdown")}">'
            f'<input type="hidden" name="on" value="{0 if locked else 1}">'
            f'<button class="{"danger" if not locked else ""}">'
            f'{"Re-open signups" if locked else "Close signups now"}</button>'
            f'<span style="color:{"#e08f8f" if locked else "#8fe0ab"}">'
            f'currently {"CLOSED" if locked else "open"}</span></form></div>')

        parts.append('<div class="grid">')
        parts.append(
            f'<div class="card"><h3>Reset a password</h3>'
            f'<p>Nobody has an email on file, so this is the only reset there is. '
            f'Check their streak and join date on the users page and ask them '
            f'something only they would know first. Every session is revoked, so '
            f'anyone already in the account is kicked out.</p>'
            f'<form method="post" action="{url_for("console_reset_password")}">'
            f'<div class="row"><input name="username" placeholder="username" required>'
            f'<input name="password" placeholder="new password" required></div>'
            f'<div class="row" style="margin-top:8px"><button class="danger">'
            f'reset password</button></div></form></div>')
        parts.append(
            f'<div class="card"><h3>Create an account</h3>'
            f'<p>Makes a real account that can be signed into. Ignores the cap.</p>'
            f'<form method="post" action="{url_for("console_create_user")}">'
            f'<div class="row"><input name="username" placeholder="username" required>'
            f'<input name="password" placeholder="password" required></div>'
            f'<div class="row" style="margin-top:8px"><button>create</button></div>'
            f'</form></div>')
        parts.append(
            f'<div class="card"><h3>Download a user\'s data</h3>'
            f'<p>Every row we hold for them, as JSON.</p>'
            f'<form class="row" method="get" action="{url_for("console_export")}">'
            f'<input name="username" placeholder="username" required>'
            f'<button>download</button></form></div>')
        parts.append(
            f'<div class="card"><h3>Move progress between accounts</h3>'
            f'<p>Copies the economy row and headline stats from one account to '
            f'another. The source is left untouched.</p>'
            f'<form method="post" action="{url_for("console_move")}">'
            f'<div class="row"><input name="src" placeholder="from username" required>'
            f'<input name="dst" placeholder="to username" required></div>'
            f'<div class="row" style="margin-top:8px">'
            f'<button class="danger">copy</button></div></form></div>')
        parts.append(
            f'<div class="card"><h3>Premium code</h3>'
            f'<p>Creates it, or resets an existing one to unredeemed.</p>'
            f'<form class="row" method="post" action="{url_for("console_new_code")}">'
            f'<input name="code" placeholder="CODE" maxlength="30" required>'
            f'<button>create / reset</button></form></div>')
        parts.append('</div>')
        return _render('ops', ''.join(parts))

    def _audit(conn, action, target, detail):
        conn.execute(
            'INSERT INTO admin_audit (actor, device_id, action, target, detail, ip, at)'
            ' VALUES (?,?,?,?,?,?,?)',
            ('web-console', 'browser', action, target, detail,
             request.remote_addr, int(time.time())))

    @app.route('/console/ops/lockdown', methods=['POST'])
    def console_lockdown():
        on = '1' if request.form.get('on') == '1' else '0'
        conn = get_db()
        try:
            conn.execute("INSERT INTO settings (key,value) VALUES ('lockdown',?)"
                         " ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value", (on,))
            _audit(conn, 'lockdown', 'signups', on)
            conn.commit()
        finally:
            conn.close()
        return redirect(url_for('console_ops',
                                msg='Signups closed.' if on == '1' else 'Signups re-opened.'))

    @app.route('/console/ops/create-user', methods=['POST'])
    def console_create_user():
        username = (request.form.get('username') or '').strip()
        password = request.form.get('password') or ''
        if not (2 <= len(username) <= 20) or not username.replace('_', '').replace('-', '').isalnum():
            return redirect(url_for('console_ops', err='Username must be 2-20 letters, numbers, - or _.'))
        problem = password_problem(password)
        if problem:
            return redirect(url_for('console_ops', err=problem))
        conn = get_db()
        try:
            if conn.execute('SELECT 1 FROM users WHERE username=?', (username,)).fetchone():
                return redirect(url_for('console_ops', err='That username is taken.'))
            now = int(time.time())
            conn.execute('INSERT INTO users (username, password_hash, created_at,'
                         ' last_active, is_active) VALUES (?,?,?,?,1)',
                         (username, hash_password(password), now, now))
            ensure_economy_row(conn, username, now)
            _audit(conn, 'create-user', username, 'via console')
            conn.commit()
        finally:
            conn.close()
        return redirect(url_for('console_ops', msg=f'Created {username}.'))

    @app.route('/console/ops/export', methods=['GET'])
    def console_export():
        username = (request.args.get('username') or '').strip()
        if not username:
            return redirect(url_for('console_ops', err='Give a username.'))
        # Credentials are never exported, whatever the table happens to hold.
        secret_cols = {'password_hash', 'device_secret', 'token'}
        dump = {}
        conn = get_db()
        try:
            if not conn.execute('SELECT 1 FROM users WHERE username=?', (username,)).fetchone():
                return redirect(url_for('console_ops', err=f'No account called {username}.'))
            for t in VIEWABLE:
                if SEARCH_COLUMN.get(t) != 'username':
                    continue
                rows = conn.execute(f'SELECT * FROM "{t}" WHERE username=?',
                                    (username,)).fetchall()
                dump[t] = [{k: v for k, v in dict(r).items() if k not in secret_cols}
                           for r in rows]
            _audit(conn, 'export', username, 'downloaded')
            conn.commit()
        finally:
            conn.close()
        return Response(json.dumps(dump, indent=2, default=str),
                        mimetype='application/json',
                        headers={'Content-Disposition':
                                 f'attachment; filename="{username}.json"'})

    @app.route('/console/ops/move', methods=['POST'])
    def console_move():
        src = (request.form.get('src') or '').strip()
        dst = (request.form.get('dst') or '').strip()
        if not src or not dst or src == dst:
            return redirect(url_for('console_ops', err='Give two different usernames.'))
        # Only these move. Everything else — the password, the devices, the
        # sessions — stays exactly where it is.
        cols = ('coins', 'carrots', 'happiness', 'streak', 'has_book', 'revivals',
                'carrots_fed', 'reborns', 'longest_session')
        conn = get_db()
        try:
            row = conn.execute(
                f'SELECT {",".join(cols)} FROM economy WHERE username=?', (src,)).fetchone()
            if not row:
                return redirect(url_for('console_ops', err=f'{src} has no economy row.'))
            if not conn.execute('SELECT 1 FROM users WHERE username=?', (dst,)).fetchone():
                return redirect(url_for('console_ops', err=f'No account called {dst}.'))
            ensure_economy_row(conn, dst, int(time.time()))
            conn.execute(f'UPDATE economy SET {",".join(c + "=?" for c in cols)}'
                         ' WHERE username=?', tuple(row[c] for c in cols) + (dst,))
            _audit(conn, 'move-progress', dst, f'from {src}')
            conn.commit()
        finally:
            conn.close()
        return redirect(url_for('console_ops', msg=f'Copied {src} → {dst}.'))

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
            conn.execute('INSERT INTO premium_codes (code, created_at, redeemed) VALUES (?,?,0)'
                         ' ON CONFLICT (code) DO UPDATE SET created_at = EXCLUDED.created_at,'
                         ' redeemed = 0, redeemed_by = NULL, redeemed_at = NULL',
                         (code, int(time.time())))
            conn.commit()
        finally:
            conn.close()
        return redirect(url_for('console_table', table='premium_codes',
                                msg=f'Created code {code}.'))
