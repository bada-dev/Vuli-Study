"""
Economy engine
Project: VuliStudy
Author: BadaDev
ALL RIGHTS RESERVED
"""

import json
import time
from datetime import datetime, timedelta, timezone

# ---------------------------------------------------------------------------
# copied rules
# ---------------------------------------------------------------------------
MIN_SESSION_MINUTES = 5        # below this a session earns nothing
COINS_PER_5_MIN = 2
BOOK_MULTIPLIER = 1.5
HAPPINESS_ON_COMPLETE = 20
HAPPINESS_PER_CARROT = 15
HAPPINESS_DECAY = 15
CARROTS_PER_REVIVAL = 3

# stop capping rules
MAX_SESSION_MINUTES = 480      # realistically no one is studying over 8 hours
MAX_MINUTES_PER_HOUR = 480     # rolling cap across all events
MAX_TOTAL_MINUTES = 50_000
MAX_STREAK = 5_000
MAX_REBORNS = 500

VALID_COSMETICS = [None, 'tophat', 'wizard', 'pirate',
                   'premium-crown', 'premium-glow', 'premium-shades']
# Granted by the subscription rather than bought, so they never appear in
# owned['cosmetics'] and have to be permitted separately — see apply_equip.
PREMIUM_COSMETICS = ('premium-crown', 'premium-glow', 'premium-shades')
VALID_BACKGROUNDS = ['default', 'ocean', 'sunset', 'lavender',
                     'mint', 'rose', 'midnight', 'forest']
VALID_MODES = ('focus', 'stopwatch', 'long')

# ---------------------------------------------------------------------------
# Just shop
# ---------------------------------------------------------------------------
SHOP = {
    'carrot':         {'price': 10,  'kind': 'consumable', 'grants': {'carrots': 1}},
    'carrot_bundle':  {'price': 25,  'kind': 'consumable', 'grants': {'carrots': 3}},
    'streak_freezer': {'price': 60,  'kind': 'flag',       'flag': 'streak_freeze'},
    'study_book':     {'price': 25,  'kind': 'flag',       'flag': 'has_book'},

    'tophat':         {'price': 150, 'kind': 'cosmetic'},
    'wizard':         {'price': 200, 'kind': 'cosmetic'},
    'pirate':         {'price': 180, 'kind': 'cosmetic'},

    'wave':           {'price': 30,  'kind': 'emote'},
    'love':           {'price': 30,  'kind': 'emote'},
    'star':           {'price': 30,  'kind': 'emote'},
    'crazy':          {'price': 70,  'kind': 'emote'},

    'ocean':          {'price': 100, 'kind': 'background'},
    'sunset':         {'price': 150, 'kind': 'background'},
    'lavender':       {'price': 150, 'kind': 'background'},
    'mint':           {'price': 200, 'kind': 'background'},
    'rose':           {'price': 200, 'kind': 'background'},
    'midnight':       {'price': 300, 'kind': 'background'},
    'forest':         {'price': 250, 'kind': 'background'},
}

# Spend-money-to-lose-money-to-make-more-money-extremely-important-rule
PREMIUM_ITEM_DISCOUNT = 0.20
PREMIUM_BG_DISCOUNT = 0.20


class EconomyError(Exception):
    """A rule refused the request. The message is safe to show the user."""


# ---------------------------------------------------------------------------
# HHELPER
# --------------------------------------------------------------------------
def _clamp(v, lo, hi):
    return max(lo, min(hi, v))


def local_date(tz_offset_minutes, ts=None):
    """
    The user's own calendar date, derived server-side.

    The phone tells us its UTC offset, not its date. That matters: streaks are
    per-day, so letting the client name the date would let it claim a new streak
    day whenever it liked. An offset is a much smaller thing to trust, and it's
    range-checked by the caller.
    """
    ts = ts if ts is not None else time.time()
    offset = _clamp(int(tz_offset_minutes or 0), -720, 840)
    dt = datetime.fromtimestamp(ts, tz=timezone.utc) + timedelta(minutes=offset)
    return dt.strftime('%Y-%m-%d')


def _shift_date(date_str, days):
    d = datetime.strptime(date_str, '%Y-%m-%d') + timedelta(days=days)
    return d.strftime('%Y-%m-%d')


def week_start_ts(ts=None):
    now = datetime.fromtimestamp(ts or time.time(), tz=timezone.utc)
    start = (now - timedelta(days=now.weekday())).replace(
        hour=0, minute=0, second=0, microsecond=0)
    return int(start.timestamp())


def load_owned(row):
    try:
        owned = json.loads(row['owned_json'] or '{}')
    except Exception:
        owned = {}
    owned.setdefault('cosmetics', [])
    owned.setdefault('backgrounds', ['default'])
    owned.setdefault('emotes', [])
    return owned


def price_for(item_id, is_premium):
    item = SHOP.get(item_id)
    if not item:
        raise EconomyError('That item does not exist.')
    discount = 0.0
    if is_premium:
        discount = PREMIUM_BG_DISCOUNT if item['kind'] == 'background' else PREMIUM_ITEM_DISCOUNT
    return int(item['price'] * (1 - discount))


def shop_catalogue(is_premium):
    """What the client is allowed to render. No logic, just prices."""
    return {
        item_id: {
            'price': price_for(item_id, is_premium),
            'base_price': meta['price'],
            'kind': meta['kind'],
        }
        for item_id, meta in SHOP.items()
    }



# the client is told after every change.
# One joined read rather than two round trips — this runs on the end of almost
# every request, so halving it halves a lot.
SNAPSHOT_SQL = '''
    SELECT e.coins, e.carrots, e.happiness, e.streak, e.last_streak_date,
           e.streak_freeze, e.has_book, e.is_dead, e.revivals, e.carrots_fed,
           e.reborns, e.longest_session, e.highest_coins, e.owned_json,
           u.equipped_cosmetic, u.active_background, u.character_width,
           u.total_minutes, u.is_premium, u.avatar
      FROM economy e
      JOIN users u ON u.username = e.username
     WHERE e.username = ?'''


def snapshot(conn, username):
    row = conn.execute(SNAPSHOT_SQL, (username,)).fetchone()
    if not row:
        return None
    e = u = row
    owned = load_owned(row)
    return {
        'username': username,
        'coins': e['coins'],
        'carrots': e['carrots'],
        'happiness': e['happiness'],
        'streak': e['streak'],
        'lastStreakDate': e['last_streak_date'],
        'streakFreezeActive': bool(e['streak_freeze']),
        'hasBook': bool(e['has_book']),
        'isDead': bool(e['is_dead']),
        'revivals': e['revivals'],
        'carrotsFed': e['carrots_fed'],
        'reborns': e['reborns'],
        'longestSession': e['longest_session'],
        'highestCoins': e['highest_coins'],
        'ownedCosmetics': owned['cosmetics'],
        'ownedBackgrounds': owned['backgrounds'],
        'ownedEmotes': owned['emotes'],
        'equippedCosmetic': u['equipped_cosmetic'],
        'activeBackground': u['active_background'],
        'characterWidth': u['character_width'],
        'totalMinutes': u['total_minutes'],
        'isPremium': bool(u['is_premium']),
        # So the app can draw your own face without a second round trip.
        'avatar': u['avatar'] if 'avatar' in u.keys() else None,
    }


def _save(conn, username, e, now_ts):
    conn.execute(
        '''UPDATE economy SET coins=?, carrots=?, happiness=?, streak=?,
           last_streak_date=?, streak_freeze=?, has_book=?, is_dead=?, revivals=?,
           carrots_fed=?, reborns=?, longest_session=?, highest_coins=?,
           owned_json=?, updated_at=? WHERE username=?''',
        (e['coins'], e['carrots'], e['happiness'], e['streak'],
         e['last_streak_date'], e['streak_freeze'], e['has_book'], e['is_dead'],
         e['revivals'], e['carrots_fed'], e['reborns'], e['longest_session'],
         e['highest_coins'], json.dumps(e['owned']), now_ts, username)
    )


def _load(conn, username):
    # Pulls is_premium along for the ride so purchase pricing doesn't need a
    # second query for it.
    row = conn.execute(
        '''SELECT e.*, u.is_premium, u.total_minutes
             FROM economy e JOIN users u ON u.username = e.username
            WHERE e.username = ?''', (username,)).fetchone()
    if not row:
        raise EconomyError('No economy row for this account.')
    e = dict(row)
    e['owned'] = load_owned(row)
    return e


# THE Rate limit
def _minutes_earned_recently(conn, username, now_ts, window=3600):
    row = conn.execute(
        # NB: no jsonb `?` operator here. `?` is the placeholder marker the db
        # layer rewrites to `%s`, so using Postgres's own `?` would be mangled.
        # `->>` yields NULL when the key is absent and SUM skips NULLs anyway.
        '''SELECT COALESCE(SUM((result_json::jsonb ->> 'minutes')::int), 0) AS m
           FROM processed_events
           WHERE username=? AND event_type='session_completed' AND processed_at > ?''',
        (username, now_ts - window)
    ).fetchone()
    return int(row['m'] or 0)


# ---------------------------------------------------------------------------
# Event handlers
#
# Each returns (result_dict). They assume the caller has already checked
# idempotency and opened a transaction.
# --------------------------------------------------------------------------
def apply_session_completed(conn, username, tz_offset, payload, now_ts):
    """
    The core earner. Ported from completeSession()/finalizeStop() in the client.

    Guards the original never had:
      - wall-clock consistency: claimed minutes must fit the reported window
      - a rolling hourly cap, so a flood of queued offline sessions can't mint coins
    """
    try:
        minutes = int(payload.get('minutes', 0))
    except (TypeError, ValueError):
        raise EconomyError('Invalid session length.')

    mode = payload.get('mode', 'focus')
    if mode not in VALID_MODES:
        mode = 'focus'

    if minutes <= 0:
        return {'coins_awarded': 0, 'minutes': 0, 'reason': 'empty'}
    if minutes > MAX_SESSION_MINUTES:
        raise EconomyError('Session too long to be genuine.')

    # A session that claims 60 minutes must have actually spanned like 60 minutes.
    started = payload.get('started_at')
    ended = payload.get('ended_at')
    if started and ended:
        try:
            span = int(ended) - int(started)
            if span < (minutes * 60) - 90:
                raise EconomyError('Session length does not match its duration.')
        except (TypeError, ValueError):
            pass

    # Need to save money.
    already = _minutes_earned_recently(conn, username, now_ts)
    if already + minutes > MAX_MINUTES_PER_HOUR:
        minutes = max(0, MAX_MINUTES_PER_HOUR - already)
        if minutes == 0:
            raise EconomyError('Hourly study limit reached.')

    e = _load(conn, username)
    u = e                      # _load already joined users

    coins = 0
    book_used = False
    if minutes >= MIN_SESSION_MINUTES:
        coins = (minutes // 5) * COINS_PER_5_MIN
        if e['has_book'] and coins > 0:
            coins = int(coins * BOOK_MULTIPLIER)
            e['has_book'] = 0
            book_used = True

    e['coins'] += coins
    e['highest_coins'] = max(e['highest_coins'], e['coins'])
    if minutes > e['longest_session']:
        e['longest_session'] = minutes

    # lazy breaks give no credit and take no penalty since same as the original.
    if mode != 'long' and minutes >= MIN_SESSION_MINUTES:
        e['happiness'] = _clamp(e['happiness'] + HAPPINESS_ON_COMPLETE, 0, 100)
        if e['happiness'] > 0:
            e['is_dead'] = 0
        _apply_streak(e, local_date(tz_offset, now_ts))

    _save(conn, username, e, now_ts)

    # SSTUUDY TOTALS
    if minutes > 0 and mode != 'long':
        day = local_date(tz_offset, now_ts)
        conn.execute(
            '''INSERT INTO daily_study (username, day, minutes) VALUES (?,?,?)
               ON CONFLICT(username, day) DO UPDATE
                 SET minutes = daily_study.minutes + excluded.minutes''',
            (username, day, minutes))
        conn.execute(
            '''INSERT INTO weekly_study (username, week_start, minutes) VALUES (?,?,?)
               ON CONFLICT(username, week_start) DO UPDATE
                 SET minutes = weekly_study.minutes + excluded.minutes''',
            (username, week_start_ts(now_ts), minutes))
        new_total = min(int(u['total_minutes'] or 0) + minutes, MAX_TOTAL_MINUTES)
        conn.execute(
            'UPDATE users SET total_minutes=?, streak=?, last_active=?, is_active=1 WHERE username=?',
            (new_total, e['streak'], now_ts, username))

    return {'coins_awarded': coins, 'minutes': minutes, 'book_used': book_used}


def _apply_streak(e, today):
    """Ported from updateStreak(), but keyed on the server's stored date."""
    last = e['last_streak_date']
    if last == today:
        return
    if last is None or last == _shift_date(today, -1):
        e['streak'] = min(e['streak'] + 1, MAX_STREAK)
    elif e['streak_freeze'] and last == _shift_date(today, -2):
        e['streak'] = min(e['streak'] + 1, MAX_STREAK)
        e['streak_freeze'] = 0
    else:
        e['streak'] = 1
    e['last_streak_date'] = today


def apply_carrot_fed(conn, username, now_ts):
    e = _load(conn, username)
    if e['carrots'] <= 0:
        raise EconomyError('No carrots left.')

    e['carrots'] -= 1
    e['carrots_fed'] += 1
    revived = False

    if e['is_dead']:
        # Bring back them from the dead with 3 measly carrots
        if e['carrots_fed'] % CARROTS_PER_REVIVAL == 0 and \
           (e['carrots_fed'] // CARROTS_PER_REVIVAL) > e['revivals']:
            e['revivals'] += 1
            e['is_dead'] = 0
            e['happiness'] = 50
            revived = True
    else:
        e['happiness'] = _clamp(e['happiness'] + HAPPINESS_PER_CARROT, 0, 100)

    _save(conn, username, e, now_ts)
    return {'revived': revived, 'carrots_left': e['carrots']}


def apply_purchase(conn, username, item_id, now_ts):
    """
    The client sends only an item id. Price, affordability and the grant are all
    decided here — which is what stops a repacked APK from buying anything free.
    """
    if item_id not in SHOP:
        raise EconomyError('That item does not exist.')

    e = _load(conn, username)
    item = SHOP[item_id]
    cost = price_for(item_id, bool(e['is_premium']))

    if e['coins'] < cost:
        raise EconomyError('Not enough coins.')

    kind = item['kind']
    owned = e['owned']

    if kind == 'cosmetic' and item_id in owned['cosmetics']:
        raise EconomyError('Already owned.')
    if kind == 'background' and item_id in owned['backgrounds']:
        raise EconomyError('Already owned.')
    if kind == 'emote' and item_id in owned['emotes']:
        raise EconomyError('Already owned.')
    if kind == 'flag' and e[item['flag']]:
        raise EconomyError('You already have that.')

    e['coins'] -= cost

    if kind == 'consumable':
        for field, amount in item['grants'].items():
            e[field] += amount
    elif kind == 'flag':
        e[item['flag']] = 1
    elif kind == 'cosmetic':
        owned['cosmetics'].append(item_id)
    elif kind == 'background':
        owned['backgrounds'].append(item_id)
    elif kind == 'emote':
        owned['emotes'].append(item_id)

    _save(conn, username, e, now_ts)
    return {'item': item_id, 'spent': cost, 'coins_left': e['coins']}


def apply_equip(conn, username, slot, item_id, now_ts):
    """Cosmetics can only be equipped if they were actually bought."""
    e = _load(conn, username)
    owned = e['owned']

    if slot == 'cosmetic':
        # Checked before ownership so an unknown id says so, rather than being
        # reported as something you failed to buy.
        if item_id not in VALID_COSMETICS:
            raise EconomyError('Unknown cosmetic.')
        if item_id is not None and item_id not in owned['cosmetics']:
            # The premium three come with the subscription instead of being
            # bought, so they are never in owned['cosmetics'] — but that
            # exemption used to apply to EVERYONE, which let any free account
            # equip the crown, glow and shades simply by naming them. The
            # subscription is the thing being checked here, not the purchase.
            if not (item_id in PREMIUM_COSMETICS and e['is_premium']):
                raise EconomyError('You do not own that.')
        conn.execute('UPDATE users SET equipped_cosmetic=? WHERE username=?', (item_id, username))

    elif slot == 'background':
        if item_id not in owned['backgrounds']:
            raise EconomyError('You do not own that background.')
        if item_id not in VALID_BACKGROUNDS:
            raise EconomyError('Unknown background.')
        conn.execute('UPDATE users SET active_background=? WHERE username=?', (item_id, username))

    else:
        raise EconomyError('Unknown slot.')

    return {'slot': slot, 'item': item_id}


def apply_happiness_decay(conn, username, now_ts):
    """Called on read, not on a timer — the buddy gets sad while you're away."""
    e = _load(conn, username)
    if e['is_dead']:
        return {'happiness': e['happiness']}
    e['happiness'] = _clamp(e['happiness'] - HAPPINESS_DECAY, 0, 100)
    if e['happiness'] == 0:
        e['is_dead'] = 1
    _save(conn, username, e, now_ts)
    return {'happiness': e['happiness'], 'died': bool(e['is_dead'])}


def apply_character_width(conn, username, width, now_ts):
    width = _clamp(int(width or 140), 140, 420)
    conn.execute('UPDATE users SET character_width=? WHERE username=?', (width, username))
    return {'characterWidth': width}


# ---------------------------------------------------------------------------
# Admin grants — the one legitimate way a balance changes without being earned.
# Always audited by the caller.
# ------------------------------------------------------------------
ADMIN_FIELDS = {
    'coins': (0, 10_000_000),
    'carrots': (0, 100_000),
    'happiness': (0, 100),
    'streak': (0, MAX_STREAK),
    'reborns': (0, MAX_REBORNS),
    'revivals': (0, 100_000),
    'carrots_fed': (0, 1_000_000),
    'longest_session': (0, MAX_SESSION_MINUTES),
}


def admin_set(conn, username, changes, now_ts):
    e = _load(conn, username)
    applied = {}
    for field, value in changes.items():
        if field not in ADMIN_FIELDS:
            continue
        lo, hi = ADMIN_FIELDS[field]
        try:
            e[field] = _clamp(int(value), lo, hi)
            applied[field] = e[field]
        except (TypeError, ValueError):
            continue
    if 'happiness' in applied:
        e['is_dead'] = 1 if e['happiness'] == 0 else 0
    e['highest_coins'] = max(e['highest_coins'], e['coins'])
    _save(conn, username, e, now_ts)
    if 'streak' in applied:
        conn.execute('UPDATE users SET streak=? WHERE username=?', (e['streak'], username))
    return applied


def admin_kill(conn, username, now_ts):
    e = _load(conn, username)
    e['happiness'] = 0
    e['is_dead'] = 1
    _save(conn, username, e, now_ts)
    return {'isDead': True}


def admin_revive(conn, username, now_ts):
    e = _load(conn, username)
    e['happiness'] = 100
    e['is_dead'] = 0
    _save(conn, username, e, now_ts)
    return {'isDead': False}


def admin_grant_item(conn, username, item_id, now_ts):
    e = _load(conn, username)
    item = SHOP.get(item_id)
    if not item:
        raise EconomyError('Unknown item.')
    kind = item['kind']
    if kind == 'flag':
        e[item['flag']] = 1
    elif kind == 'consumable':
        for field, amount in item['grants'].items():
            e[field] += amount
    elif kind == 'cosmetic' and item_id not in e['owned']['cosmetics']:
        e['owned']['cosmetics'].append(item_id)
    elif kind == 'background' and item_id not in e['owned']['backgrounds']:
        e['owned']['backgrounds'].append(item_id)
    elif kind == 'emote' and item_id not in e['owned']['emotes']:
        e['owned']['emotes'].append(item_id)
    _save(conn, username, e, now_ts)
    return {'granted': item_id}
