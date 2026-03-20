import os
import json
import sqlite3
import time
import random
import math
import ast
import hashlib
from datetime import datetime, date, timedelta
from fractions import Fraction
from flask import Flask, render_template, request, jsonify, send_from_directory
from groq import Groq
from apscheduler.schedulers.background import BackgroundScheduler

app = Flask(__name__)

GROQ_API_KEY  = os.environ.get('GROQ_API_KEY')
DISCORD_WEBHOOK = os.environ.get('DISCORD_WEBHOOK')
DISCORD_BOT_TOKEN          = os.environ.get('DISCORD_BOT_TOKEN')
DISCORD_CONSOLE_CHANNEL_ID = os.environ.get('DISCORD_CONSOLE_CHANNEL_ID')
client = Groq(api_key=GROQ_API_KEY)

# ─────────────────────────────────────────────────────────────────────────────
# DATABASE
# ─────────────────────────────────────────────────────────────────────────────

def get_db():
    conn = sqlite3.connect('questions.db')
    conn.row_factory = sqlite3.Row
    return conn

def init_db():
    conn = get_db()
    conn.execute('''CREATE TABLE IF NOT EXISTS questions (
        id              INTEGER PRIMARY KEY AUTOINCREMENT,
        date            TEXT UNIQUE,
        question        TEXT,
        question_hash   TEXT,
        option_a        TEXT,
        option_b        TEXT,
        option_c        TEXT,
        option_d        TEXT,
        option_e        TEXT,
        answer          TEXT,
        explanation     TEXT,
        level           TEXT,
        source          TEXT DEFAULT "template"
    )''')
    try:
        conn.execute('ALTER TABLE questions ADD COLUMN question_hash TEXT')
    except Exception:
        pass
    try:
        conn.execute('CREATE INDEX IF NOT EXISTS idx_question_hash ON questions(question_hash)')
    except Exception:
        pass
    try:
        conn.execute('''CREATE TABLE IF NOT EXISTS pending_rewards (
            mc_username  TEXT PRIMARY KEY,
            streak       INTEGER,
            item         TEXT,
            amount       INTEGER,
            label        TEXT,
            earned_date  TEXT
        )''')
    except Exception:
        pass
    conn.execute('''CREATE TABLE IF NOT EXISTS submissions (
        id              INTEGER PRIMARY KEY AUTOINCREMENT,
        date            TEXT,
        mc_username     TEXT,
        discord_username TEXT,
        answer          TEXT,
        is_correct      INTEGER,
        submitted_at    INTEGER
    )''')
    conn.execute('''CREATE TABLE IF NOT EXISTS player_streaks (
        mc_username     TEXT PRIMARY KEY,
        current_streak  INTEGER DEFAULT 0,
        last_correct    TEXT DEFAULT NULL
    )''')
    conn.execute('''CREATE TABLE IF NOT EXISTS feedback_cooldowns (
        ip              TEXT PRIMARY KEY,
        last_submitted  INTEGER DEFAULT 0
    )''')
    
    conn.commit()
    conn.close()

init_db()

FEEDBACK_COOLDOWN = 48 * 60 * 60

RCON_HOST     = os.environ.get('RCON_HOST')
RCON_PORT     = int(os.environ.get('RCON_PORT', 25575))
RCON_PASSWORD = os.environ.get('RCON_PASSWORD')

STREAK_REWARDS = [
    {"day": 1, "item": "diamond",        "amount": 1,  "label": "1 Diamond"},
    {"day": 2, "item": "cooked_porkchop","amount": 16, "label": "16 Cooked Porkchops"},
    {"day": 3, "item": "diamond",        "amount": 5,  "label": "5 Diamonds"},
    {"day": 4, "item": "golden_apple",   "amount": 1,  "label": "1 Golden Apple"},
    {"day": 5, "item": "iron_block",     "amount": 1,  "label": "1 Iron Block"},
    {"day": 6, "item": "diamond",        "amount": 10, "label": "10 Diamonds"},
    {"day": 7, "item": "ancient_debris", "amount": 1,  "label": "1 Ancient Debris"},
]

def q_hash(question_text):
    normalised = question_text.lower().strip()
    return hashlib.md5(normalised.encode()).hexdigest()


def hash_exists(h):
    conn = get_db()
    row = conn.execute(
        'SELECT id FROM questions WHERE question_hash = ?', (h,)
    ).fetchone()
    conn.close()
    return row is not None


# ─────────────────────────────────────────────────────────────────────────────
# OPTION GENERATION HELPERS
# ─────────────────────────────────────────────────────────────────────────────

def make_numeric_options(correct, positive_only=True):
    cv = int(correct) if isinstance(correct, float) and correct == int(correct) else correct
    mag = max(abs(cv), 1)

    if mag <= 12:
        pool = [-6,-5,-4,-3,-2,-1,1,2,3,4,5,6,7,8,-7,-8]
    elif mag <= 60:
        pool = [-20,-15,-12,-10,-8,-6,-5,-4,-3,-2,2,3,4,5,6,8,10,12,15,20]
    elif mag <= 300:
        pool = [-60,-50,-40,-30,-20,-15,-10,-5,5,10,15,20,30,40,50,60,80,100]
    elif mag <= 1000:
        pool = [-200,-150,-100,-80,-50,-30,30,50,80,100,150,200,300,-300]
    else:
        pool = [-500,-300,-200,-100,100,200,300,500,800,-800]

    random.shuffle(pool)
    wrongs = set()
    for d in pool:
        if len(wrongs) >= 4:
            break
        w = cv + d
        if w != cv and (not positive_only or w > 0):
            wrongs.add(w)

    step = max(mag // 4, 1)
    attempts = 1
    while len(wrongs) < 4:
        for sign in (1, -1):
            w = cv + sign * step * attempts
            if w != cv and (not positive_only or w > 0):
                wrongs.add(w)
            if len(wrongs) >= 4:
                break
        attempts += 1

    all_opts = [cv] + list(wrongs)[:4]
    random.shuffle(all_opts)
    answer_letter = 'ABCDE'[all_opts.index(cv)]
    return [str(x) for x in all_opts], answer_letter


def make_pi_options(correct_base):
    cb = int(correct_base)
    if cb <= 6:
        pool = [1,2,3,4,5,6,8,9,10,12]
    elif cb <= 30:
        pool = [cb-8, cb-6, cb-4, cb-2, cb+2, cb+4, cb+6, cb+8, cb+10, cb+12]
    else:
        pool = [cb-20, cb-12, cb-8, cb-4, cb+4, cb+8, cb+12, cb+20, cb+24]

    pool = list({abs(x) for x in pool if x > 0 and x != cb})
    random.shuffle(pool)
    all_opts = [cb] + pool[:4]
    while len(all_opts) < 5:
        all_opts.append(all_opts[-1] + 4)
    random.shuffle(all_opts)
    letter = 'ABCDE'[all_opts.index(cb)]
    return [f"{x}π" for x in all_opts], letter


def make_fraction_options(num, den):
    from math import gcd
    g = gcd(abs(num), abs(den))
    correct_str = f"{num // g}/{den // g}"
    seen = {correct_str}
    wrongs = []
    for delta in range(1, den + 5):
        if len(wrongs) >= 4:
            break
        for wn in (num + delta, num - delta):
            if len(wrongs) >= 4:
                break
            if wn > 0:
                wg = gcd(wn, den)
                ws = f"{wn // wg}/{den // wg}"
                if ws not in seen:
                    seen.add(ws)
                    wrongs.append(ws)
    while len(wrongs) < 4:
        wrongs.append(f"{len(wrongs)+2}/{den+1}")
    all_opts = [correct_str] + wrongs[:4]
    random.shuffle(all_opts)
    letter = 'ABCDE'[all_opts.index(correct_str)]
    return all_opts, letter


# ─────────────────────────────────────────────────────────────────────────────
# PYTHAGOREAN TRIPLES
# ─────────────────────────────────────────────────────────────────────────────

PYTHAGOREAN_TRIPLES = [
    (3,4,5),(5,12,13),(8,15,17),(7,24,25),
    (9,40,41),(20,21,29),(9,12,15),(12,16,20),
    (15,20,25),(6,8,10),(10,24,26),(18,24,30),
]


# ─────────────────────────────────────────────────────────────────────────────
# QUESTION TEMPLATES
# ─────────────────────────────────────────────────────────────────────────────

def tpl_rectangle_area():
    a, b, _ = random.choice(PYTHAGOREAN_TRIPLES)
    s = random.randint(1, 5)
    w, h = a * s, b * s
    ans = w * h
    assert w * h == ans
    opts, let = make_numeric_options(ans)
    return {
        "question": f"A rectangle has length {w} cm and width {h} cm. What is its area?",
        "option_a": opts[0]+" cm²","option_b": opts[1]+" cm²",
        "option_c": opts[2]+" cm²","option_d": opts[3]+" cm²","option_e": opts[4]+" cm²",
        "answer": let,
        "explanation": f"Area = length × width = {w} × {h} = {ans} cm².",
        "level": "Junior (JMC)"
    }


def tpl_triangle_area_bh():
    b = random.choice([4,6,8,10,12,14,16,18,20])
    h = random.randint(3, 15)
    ans = b * h // 2
    assert b * h % 2 == 0 and b * h // 2 == ans
    opts, let = make_numeric_options(ans)
    return {
        "question": f"A triangle has base {b} cm and perpendicular height {h} cm. What is its area?",
        "option_a": opts[0]+" cm²","option_b": opts[1]+" cm²",
        "option_c": opts[2]+" cm²","option_d": opts[3]+" cm²","option_e": opts[4]+" cm²",
        "answer": let,
        "explanation": f"Area = ½ × base × height = ½ × {b} × {h} = {ans} cm².",
        "level": "Junior (JMC)"
    }


def tpl_trapezoid_area():
    h = random.randint(2, 10)
    a = random.randint(3, 14)
    b = random.randint(3, 14)
    while b == a:
        b = random.randint(3, 14)
    if (a + b) % 2 != 0:
        b += 1
    ans = (a + b) // 2 * h
    assert (a + b) % 2 == 0 and (a + b) // 2 * h == ans
    opts, let = make_numeric_options(ans)
    return {
        "question": (f"A trapezoid has parallel sides of length {a} cm and {b} cm, "
                     f"and a perpendicular height of {h} cm. What is its area?"),
        "option_a": opts[0]+" cm²","option_b": opts[1]+" cm²",
        "option_c": opts[2]+" cm²","option_d": opts[3]+" cm²","option_e": opts[4]+" cm²",
        "answer": let,
        "explanation": (f"Area of a trapezoid = ½(a+b)×h = ½×({a}+{b})×{h} "
                        f"= ½×{a+b}×{h} = {ans} cm²."),
        "level": "Intermediate (IMC)"
    }


def tpl_circle_area():
    r = random.randint(2, 12)
    base = r * r
    opts, let = make_pi_options(base)
    return {
        "question": f"A circle has radius {r} cm. What is its area? (Give your answer in terms of π.)",
        "option_a": opts[0],"option_b": opts[1],"option_c": opts[2],
        "option_d": opts[3],"option_e": opts[4],
        "answer": let,
        "explanation": f"Area = πr² = π × {r}² = {base}π cm².",
        "level": "Junior (JMC)"
    }


def tpl_circle_circumference():
    r = random.randint(2, 15)
    base = 2 * r
    opts, let = make_pi_options(base)
    return {
        "question": f"A circle has radius {r} cm. What is its circumference? (Give your answer in terms of π.)",
        "option_a": opts[0],"option_b": opts[1],"option_c": opts[2],
        "option_d": opts[3],"option_e": opts[4],
        "answer": let,
        "explanation": f"Circumference = 2πr = 2 × π × {r} = {base}π cm.",
        "level": "Junior (JMC)"
    }


def tpl_cube_surface_area():
    s = random.randint(2, 10)
    ans = 6 * s * s
    assert 6 * s**2 == ans
    opts, let = make_numeric_options(ans)
    return {
        "question": f"A cube has side length {s} cm. What is its total surface area?",
        "option_a": opts[0]+" cm²","option_b": opts[1]+" cm²",
        "option_c": opts[2]+" cm²","option_d": opts[3]+" cm²","option_e": opts[4]+" cm²",
        "answer": let,
        "explanation": f"Surface area = 6s² = 6 × {s}² = 6 × {s**2} = {ans} cm².",
        "level": "Junior (JMC)"
    }


def tpl_cube_volume():
    s = random.randint(2, 10)
    ans = s ** 3
    assert s**3 == ans
    opts, let = make_numeric_options(ans)
    return {
        "question": f"A cube has side length {s} cm. What is its volume?",
        "option_a": opts[0]+" cm³","option_b": opts[1]+" cm³",
        "option_c": opts[2]+" cm³","option_d": opts[3]+" cm³","option_e": opts[4]+" cm³",
        "answer": let,
        "explanation": f"Volume = s³ = {s}³ = {ans} cm³.",
        "level": "Junior (JMC)"
    }


def tpl_cylinder_volume():
    r = random.randint(2, 7)
    h = random.randint(3, 12)
    base = r * r * h
    assert r**2 * h == base
    opts, let = make_pi_options(base)
    return {
        "question": (f"A cylinder has radius {r} cm and height {h} cm. "
                     f"What is its volume? (Give your answer in terms of π.)"),
        "option_a": opts[0],"option_b": opts[1],"option_c": opts[2],
        "option_d": opts[3],"option_e": opts[4],
        "answer": let,
        "explanation": f"Volume = πr²h = π × {r}² × {h} = {base}π cm³.",
        "level": "Intermediate (IMC)"
    }


def tpl_sphere_surface_area():
    r = random.randint(2, 8)
    base = 4 * r * r
    opts, let = make_pi_options(base)
    return {
        "question": (f"A sphere has radius {r} cm. What is its surface area? "
                     f"(Give your answer in terms of π.)"),
        "option_a": opts[0],"option_b": opts[1],"option_c": opts[2],
        "option_d": opts[3],"option_e": opts[4],
        "answer": let,
        "explanation": f"Surface area = 4πr² = 4 × π × {r}² = 4 × {r**2}π = {base}π cm².",
        "level": "Intermediate (IMC)"
    }


def tpl_right_triangle_hypotenuse():
    a, b, c = random.choice(PYTHAGOREAN_TRIPLES)
    s = random.randint(1, 3)
    sa, sb, sc = a*s, b*s, c*s
    assert sa**2 + sb**2 == sc**2
    opts, let = make_numeric_options(sc)
    return {
        "question": f"A right-angled triangle has legs {sa} cm and {sb} cm. What is the hypotenuse?",
        "option_a": opts[0]+" cm","option_b": opts[1]+" cm",
        "option_c": opts[2]+" cm","option_d": opts[3]+" cm","option_e": opts[4]+" cm",
        "answer": let,
        "explanation": (f"Hypotenuse = √({sa}² + {sb}²) = √({sa**2} + {sb**2}) "
                        f"= √{sc**2} = {sc} cm."),
        "level": "Junior (JMC)"
    }


def tpl_right_triangle_leg():
    a, b, c = random.choice(PYTHAGOREAN_TRIPLES)
    s = random.randint(1, 3)
    sa, sb, sc = a*s, b*s, c*s
    assert sa**2 + sb**2 == sc**2
    opts, let = make_numeric_options(sb)
    return {
        "question": (f"A right-angled triangle has hypotenuse {sc} cm "
                     f"and one leg {sa} cm. What is the length of the other leg?"),
        "option_a": opts[0]+" cm","option_b": opts[1]+" cm",
        "option_c": opts[2]+" cm","option_d": opts[3]+" cm","option_e": opts[4]+" cm",
        "answer": let,
        "explanation": (f"Leg = √({sc}² − {sa}²) = √({sc**2} − {sa**2}) "
                        f"= √{sb**2} = {sb} cm."),
        "level": "Junior (JMC)"
    }


def tpl_interior_angle_polygon():
    n = random.choice([5, 6, 8, 9, 10, 12])
    total = (n - 2) * 180
    assert total % n == 0
    angle = total // n
    opts, let = make_numeric_options(angle)
    return {
        "question": f"What is the size of each interior angle of a regular {n}-sided polygon?",
        "option_a": opts[0]+"°","option_b": opts[1]+"°",
        "option_c": opts[2]+"°","option_d": opts[3]+"°","option_e": opts[4]+"°",
        "answer": let,
        "explanation": (f"Sum of interior angles = (n−2)×180° = {n-2}×180° = {total}°. "
                        f"Each angle = {total}° ÷ {n} = {angle}°."),
        "level": "Intermediate (IMC)"
    }


def tpl_exterior_angle_polygon():
    ext = random.choice([30, 36, 40, 45, 60, 72])
    n = 360 // ext
    assert 360 % ext == 0 and 360 // ext == n
    opts, let = make_numeric_options(n)
    return {
        "question": (f"Each exterior angle of a regular polygon measures {ext}°. "
                     f"How many sides does the polygon have?"),
        "option_a": opts[0],"option_b": opts[1],"option_c": opts[2],
        "option_d": opts[3],"option_e": opts[4],
        "answer": let,
        "explanation": f"Number of sides = 360° ÷ {ext}° = {n}.",
        "level": "Intermediate (IMC)"
    }


def tpl_triangle_missing_angle():
    a = random.randint(30, 80)
    b = random.randint(30, 80)
    c = 180 - a - b
    assert c > 0 and a + b + c == 180
    opts, let = make_numeric_options(c)
    return {
        "question": f"Two angles of a triangle are {a}° and {b}°. What is the third angle?",
        "option_a": opts[0]+"°","option_b": opts[1]+"°",
        "option_c": opts[2]+"°","option_d": opts[3]+"°","option_e": opts[4]+"°",
        "answer": let,
        "explanation": f"Angles sum to 180°: third angle = 180° − {a}° − {b}° = {c}°.",
        "level": "Junior (JMC)"
    }


def tpl_quadrilateral_missing_angle():
    a = random.randint(60, 110)
    b = random.randint(60, 110)
    c = random.randint(60, 110)
    d = 360 - a - b - c
    assert d > 0 and d < 360 and a + b + c + d == 360
    opts, let = make_numeric_options(d)
    return {
        "question": (f"Three angles of a quadrilateral are {a}°, {b}°, and {c}°. "
                     f"What is the fourth angle?"),
        "option_a": opts[0]+"°","option_b": opts[1]+"°",
        "option_c": opts[2]+"°","option_d": opts[3]+"°","option_e": opts[4]+"°",
        "answer": let,
        "explanation": (f"Angles sum to 360°: fourth angle = 360° − {a}° − {b}° − {c}° = {d}°."),
        "level": "Junior (JMC)"
    }


def tpl_distance_two_points():
    a, b, c = random.choice(PYTHAGOREAN_TRIPLES)
    s = random.randint(1, 3)
    sa, sb, sc = a*s, b*s, c*s
    x1, y1 = random.randint(0, 6), random.randint(0, 6)
    x2, y2 = x1 + sa, y1 + sb
    assert (x2-x1)**2 + (y2-y1)**2 == sc**2
    opts, let = make_numeric_options(sc)
    return {
        "question": (f"What is the distance between the points ({x1}, {y1}) and ({x2}, {y2})?"),
        "option_a": opts[0]+" units","option_b": opts[1]+" units",
        "option_c": opts[2]+" units","option_d": opts[3]+" units","option_e": opts[4]+" units",
        "answer": let,
        "explanation": (f"Distance = √((x₂−x₁)²+(y₂−y₁)²) = √({x2-x1}²+{y2-y1}²) "
                        f"= √({sa**2}+{sb**2}) = √{sc**2} = {sc} units."),
        "level": "Intermediate (IMC)"
    }


def tpl_gradient_two_points():
    m = random.choice([-4, -3, -2, -1, 1, 2, 3, 4])
    x1 = random.randint(0, 5)
    y1 = random.randint(0, 5)
    dx = random.randint(1, 4)
    x2 = x1 + dx
    y2 = y1 + m * dx
    assert (y2 - y1) == m * (x2 - x1)
    opts, let = make_numeric_options(m, positive_only=False)
    return {
        "question": f"What is the gradient of the line passing through ({x1}, {y1}) and ({x2}, {y2})?",
        "option_a": opts[0],"option_b": opts[1],"option_c": opts[2],
        "option_d": opts[3],"option_e": opts[4],
        "answer": let,
        "explanation": (f"Gradient = (y₂−y₁)/(x₂−x₁) = ({y2}−{y1})/({x2}−{x1}) "
                        f"= {y2-y1}/{x2-x1} = {m}."),
        "level": "Intermediate (IMC)"
    }


def tpl_square_from_perimeter():
    side = random.randint(3, 16)
    perim = 4 * side
    ans = side * side
    assert 4 * side == perim and side**2 == ans
    opts, let = make_numeric_options(ans)
    return {
        "question": f"A square has perimeter {perim} cm. What is its area?",
        "option_a": opts[0]+" cm²","option_b": opts[1]+" cm²",
        "option_c": opts[2]+" cm²","option_d": opts[3]+" cm²","option_e": opts[4]+" cm²",
        "answer": let,
        "explanation": (f"Side = perimeter ÷ 4 = {perim} ÷ 4 = {side} cm. "
                        f"Area = {side}² = {ans} cm²."),
        "level": "Junior (JMC)"
    }


def tpl_last_digit_power():
    base = random.choice([2, 3, 7, 8])
    cycles = {2:[2,4,8,6], 3:[3,9,7,1], 7:[7,9,3,1], 8:[8,4,2,6]}
    cycle = cycles[base]
    period = 4
    exp = random.choice([10,20,30,40,50,60,100,200])
    idx = (exp % period) - 1
    if idx < 0:
        idx = period - 1
    ans = cycle[idx]
    assert (base ** exp) % 10 == ans
    opts, let = make_numeric_options(ans)
    return {
        "question": f"What is the last digit of {base}^{exp}?",
        "option_a": opts[0],"option_b": opts[1],"option_c": opts[2],
        "option_d": opts[3],"option_e": opts[4],
        "answer": let,
        "explanation": (f"The last digits of powers of {base} cycle: "
                        f"{', '.join(str(x) for x in cycle)}, repeating every {period}. "
                        f"{exp} mod {period} = {exp%period if exp%period else period}, "
                        f"so last digit = {ans}."),
        "level": "Intermediate (IMC)"
    }


def tpl_sum_first_n():
    n = random.choice([20, 25, 30, 40, 50, 60, 75, 80, 100])
    ans = n * (n + 1) // 2
    assert n * (n + 1) % 2 == 0 and n * (n + 1) // 2 == ans
    opts, let = make_numeric_options(ans)
    return {
        "question": f"What is the value of 1 + 2 + 3 + ... + {n}?",
        "option_a": opts[0],"option_b": opts[1],"option_c": opts[2],
        "option_d": opts[3],"option_e": opts[4],
        "answer": let,
        "explanation": f"Sum = n(n+1)/2 = {n}×{n+1}/2 = {n*(n+1)}/2 = {ans}.",
        "level": "Junior (JMC)"
    }


def tpl_sum_squares():
    n = random.randint(5, 13)
    ans = n * (n + 1) * (2 * n + 1) // 6
    assert n*(n+1)*(2*n+1) % 6 == 0 and n*(n+1)*(2*n+1)//6 == ans
    opts, let = make_numeric_options(ans)
    return {
        "question": f"What is the value of 1² + 2² + 3² + ... + {n}²?",
        "option_a": opts[0],"option_b": opts[1],"option_c": opts[2],
        "option_d": opts[3],"option_e": opts[4],
        "answer": let,
        "explanation": (f"Sum of squares = n(n+1)(2n+1)/6 "
                        f"= {n}×{n+1}×{2*n+1}/6 = {n*(n+1)*(2*n+1)}/6 = {ans}."),
        "level": "Intermediate (IMC)"
    }


def tpl_sum_arithmetic_sequence():
    a = random.randint(1, 10)
    d = random.randint(1, 5)
    n = random.randint(10, 25)
    last = a + (n - 1) * d
    ans = n * (a + last) // 2
    assert n * (a + last) % 2 == 0 and n * (a + last) // 2 == ans
    terms_preview = f"{a}, {a+d}, {a+2*d}, ..."
    opts, let = make_numeric_options(ans)
    return {
        "question": (f"What is the sum of the arithmetic sequence "
                     f"{terms_preview}, {last}? (There are {n} terms.)"),
        "option_a": opts[0],"option_b": opts[1],"option_c": opts[2],
        "option_d": opts[3],"option_e": opts[4],
        "answer": let,
        "explanation": (f"Sum = n/2 × (first + last) = {n}/2 × ({a} + {last}) "
                        f"= {n}/2 × {a+last} = {ans}."),
        "level": "Intermediate (IMC)"
    }


def tpl_nth_term():
    a = random.randint(1, 10)
    d = random.randint(1, 6)
    n = random.randint(10, 40)
    ans = a + (n - 1) * d
    assert a + (n-1)*d == ans
    opts, let = make_numeric_options(ans)
    return {
        "question": f"The nth term of a sequence is {a} + (n−1)×{d}. What is the {n}th term?",
        "option_a": opts[0],"option_b": opts[1],"option_c": opts[2],
        "option_d": opts[3],"option_e": opts[4],
        "answer": let,
        "explanation": (f"Substitute n = {n}: {a} + ({n}−1)×{d} = {a} + {n-1}×{d} "
                        f"= {a} + {(n-1)*d} = {ans}."),
        "level": "Junior (JMC)"
    }


def tpl_geometric_nth_term():
    a = random.randint(1, 4)
    r = random.randint(2, 4)
    n = random.randint(3, 7)
    ans = a * r**(n-1)
    assert a * r**(n-1) == ans
    terms = [a * r**i for i in range(min(4, n))]
    terms_str = ", ".join(str(t) for t in terms) + ", ..."
    opts, let = make_numeric_options(ans)
    return {
        "question": (f"The geometric sequence {terms_str} has first term {a} "
                     f"and common ratio {r}. What is the {n}th term?"),
        "option_a": opts[0],"option_b": opts[1],"option_c": opts[2],
        "option_d": opts[3],"option_e": opts[4],
        "answer": let,
        "explanation": f"nth term = a×rⁿ⁻¹ = {a}×{r}^{n-1} = {a}×{r**(n-1)} = {ans}.",
        "level": "Intermediate (IMC)"
    }


def tpl_power_of_two_sum():
    n = random.randint(4, 10)
    ans = 2**(n + 1) - 1
    assert sum(2**i for i in range(n + 1)) == ans
    opts, let = make_numeric_options(ans)
    return {
        "question": f"What is the value of 2⁰ + 2¹ + 2² + ... + 2^{n}?",
        "option_a": opts[0],"option_b": opts[1],"option_c": opts[2],
        "option_d": opts[3],"option_e": opts[4],
        "answer": let,
        "explanation": (f"Geometric series with a=1, r=2: sum = 2^(n+1)−1 "
                        f"= 2^{n+1}−1 = {2**(n+1)}−1 = {ans}."),
        "level": "Intermediate (IMC)"
    }


def tpl_divisibility_count():
    d = random.choice([3, 4, 6, 7, 8, 9, 11, 13])
    n = random.choice([50, 100, 150, 200, 250, 500])
    ans = n // d
    assert n // d == ans
    opts, let = make_numeric_options(ans)
    return {
        "question": f"How many positive integers from 1 to {n} are divisible by {d}?",
        "option_a": opts[0],"option_b": opts[1],"option_c": opts[2],
        "option_d": opts[3],"option_e": opts[4],
        "answer": let,
        "explanation": f"Count = ⌊{n}÷{d}⌋ = {ans}.",
        "level": "Junior (JMC)"
    }


def tpl_hcf():
    coprime_pairs = [(2,3),(2,5),(2,7),(3,4),(3,5),(3,7),(4,5),(4,7),(5,6),(5,7),(3,8),(4,9)]
    p, q = random.choice(coprime_pairs)
    h = random.randint(2, 12)
    a, b = h * p, h * q
    assert math.gcd(a, b) == h
    opts, let = make_numeric_options(h)
    return {
        "question": f"What is the highest common factor (HCF) of {a} and {b}?",
        "option_a": opts[0],"option_b": opts[1],"option_c": opts[2],
        "option_d": opts[3],"option_e": opts[4],
        "answer": let,
        "explanation": (f"{a} = {h}×{p},  {b} = {h}×{q}. "
                        f"Since {p} and {q} share no common factor, HCF = {h}."),
        "level": "Intermediate (IMC)"
    }


def tpl_lcm():
    pairs = [(3,4),(3,5),(4,5),(4,6),(5,6),(5,8),(6,8),(4,9),(6,9),(8,9),
             (5,12),(6,10),(9,12),(10,15),(8,12)]
    a, b = random.choice(pairs)
    ans = a * b // math.gcd(a, b)
    assert a * b // math.gcd(a, b) == ans
    opts, let = make_numeric_options(ans)
    return {
        "question": f"What is the lowest common multiple (LCM) of {a} and {b}?",
        "option_a": opts[0],"option_b": opts[1],"option_c": opts[2],
        "option_d": opts[3],"option_e": opts[4],
        "answer": let,
        "explanation": (f"LCM = (a×b) ÷ HCF({a},{b}) = ({a}×{b}) ÷ {math.gcd(a,b)} "
                        f"= {a*b} ÷ {math.gcd(a,b)} = {ans}."),
        "level": "Intermediate (IMC)"
    }


def tpl_count_factors():
    data = [
        (12, "2² × 3",       6),  (18, "2 × 3²",       6),
        (20, "2² × 5",       6),  (24, "2³ × 3",        8),
        (28, "2² × 7",       6),  (30, "2 × 3 × 5",     8),
        (36, "2² × 3²",      9),  (40, "2³ × 5",        8),
        (48, "2⁴ × 3",      10),  (50, "2 × 5²",        6),
        (60, "2² × 3 × 5",  12),  (72, "2³ × 3²",      12),
        (80, "2⁴ × 5",      10),  (84, "2² × 3 × 7",   12),
        (90, "2 × 3² × 5",  12), (100, "2² × 5²",       9),
        (120,"2³ × 3 × 5",  16), (144, "2⁴ × 3²",      15),
    ]
    n, factstr, ans = random.choice(data)
    actual = len([i for i in range(1, n+1) if n % i == 0])
    assert actual == ans
    opts, let = make_numeric_options(ans)
    return {
        "question": f"How many positive factors does {n} have?",
        "option_a": opts[0],"option_b": opts[1],"option_c": opts[2],
        "option_d": opts[3],"option_e": opts[4],
        "answer": let,
        "explanation": (f"{n} = {factstr}. "
                        f"Number of factors = product of (exponent+1) = {ans}."),
        "level": "Intermediate (IMC)"
    }


def tpl_mod_remainder():
    d = random.choice([7, 8, 9, 11, 12, 13])
    q = random.randint(5, 20)
    n = d * q + random.randint(1, d - 1)
    ans = n % d
    assert n % d == ans and 0 < ans < d
    opts, let = make_numeric_options(ans)
    return {
        "question": f"What is the remainder when {n} is divided by {d}?",
        "option_a": opts[0],"option_b": opts[1],"option_c": opts[2],
        "option_d": opts[3],"option_e": opts[4],
        "answer": let,
        "explanation": f"{n} = {n//d} × {d} + {ans}. Remainder = {ans}.",
        "level": "Junior (JMC)"
    }


def tpl_factorial_value():
    n = random.randint(3, 7)
    ans = math.factorial(n)
    assert math.factorial(n) == ans
    steps = " × ".join(str(i) for i in range(n, 0, -1))
    opts, let = make_numeric_options(ans)
    return {
        "question": f"What is the value of {n}! ({n} factorial)?",
        "option_a": opts[0],"option_b": opts[1],"option_c": opts[2],
        "option_d": opts[3],"option_e": opts[4],
        "answer": let,
        "explanation": f"{n}! = {steps} = {ans}.",
        "level": "Intermediate (IMC)"
    }


def tpl_factorial_sum():
    n = random.randint(3, 6)
    ans = sum(math.factorial(i) for i in range(1, n+1))
    assert sum(math.factorial(i) for i in range(1, n+1)) == ans
    terms = " + ".join(f"{math.factorial(i)}" for i in range(1, n+1))
    opts, let = make_numeric_options(ans)
    return {
        "question": f"What is 1! + 2! + 3! + ... + {n}!?",
        "option_a": opts[0],"option_b": opts[1],"option_c": opts[2],
        "option_d": opts[3],"option_e": opts[4],
        "answer": let,
        "explanation": f"= {terms} = {ans}.",
        "level": "Intermediate (IMC)"
    }


def tpl_consecutive_odd_sum():
    n = random.randint(4, 15)
    ans = n * n
    assert sum(2*k-1 for k in range(1, n+1)) == ans
    last_odd = 2*n - 1
    opts, let = make_numeric_options(ans)
    return {
        "question": f"What is the sum of the first {n} positive odd numbers? (1 + 3 + 5 + ... + {last_odd})",
        "option_a": opts[0],"option_b": opts[1],"option_c": opts[2],
        "option_d": opts[3],"option_e": opts[4],
        "answer": let,
        "explanation": (f"The sum of the first n odd numbers = n². "
                        f"So the answer is {n}² = {ans}."),
        "level": "Intermediate (IMC)"
    }


def tpl_linear_equation():
    a = random.randint(2, 8)
    x = random.randint(2, 15)
    b = random.randint(1, 20)
    c = a * x + b
    assert (c - b) % a == 0 and (c - b) // a == x
    opts, let = make_numeric_options(x)
    return {
        "question": f"If {a}x + {b} = {c}, what is the value of x?",
        "option_a": opts[0],"option_b": opts[1],"option_c": opts[2],
        "option_d": opts[3],"option_e": opts[4],
        "answer": let,
        "explanation": f"{a}x = {c} − {b} = {c-b}.  x = {c-b} ÷ {a} = {x}.",
        "level": "Junior (JMC)"
    }


def tpl_substitute_quadratic():
    a = random.randint(1, 4)
    b = random.randint(1, 6)
    x = random.randint(2, 8)
    ans = a * x * x + b * x
    assert a*x**2 + b*x == ans
    opts, let = make_numeric_options(ans)
    return {
        "question": f"If f(x) = {a}x² + {b}x, what is f({x})?",
        "option_a": opts[0],"option_b": opts[1],"option_c": opts[2],
        "option_d": opts[3],"option_e": opts[4],
        "answer": let,
        "explanation": (f"f({x}) = {a}×{x}² + {b}×{x} "
                        f"= {a}×{x**2} + {b*x} = {a*x**2} + {b*x} = {ans}."),
        "level": "Intermediate (IMC)"
    }


def tpl_difference_of_squares():
    a = random.randint(10, 60)
    b = random.randint(2, 12)
    ans = a*a - b*b
    assert (a+b)*(a-b) == ans
    opts, let = make_numeric_options(ans)
    return {
        "question": f"What is the value of {a}² − {b}²?",
        "option_a": opts[0],"option_b": opts[1],"option_c": opts[2],
        "option_d": opts[3],"option_e": opts[4],
        "answer": let,
        "explanation": (f"Difference of squares: ({a}+{b})({a}−{b}) "
                        f"= {a+b} × {a-b} = {ans}."),
        "level": "Intermediate (IMC)"
    }


def tpl_expression_evaluation():
    a = random.randint(2, 6)
    b = random.randint(2, 7)
    c = random.randint(1, 10)
    ans = a*a*b + c
    assert a**2*b + c == ans
    opts, let = make_numeric_options(ans)
    return {
        "question": f"If a = {a}, b = {b}, c = {c}, what is a²b + c?",
        "option_a": opts[0],"option_b": opts[1],"option_c": opts[2],
        "option_d": opts[3],"option_e": opts[4],
        "answer": let,
        "explanation": f"a²b + c = {a}²×{b} + {c} = {a**2}×{b} + {c} = {a**2*b} + {c} = {ans}.",
        "level": "Junior (JMC)"
    }


def tpl_expand_single_bracket():
    a = random.randint(2, 9)
    b = random.randint(2, 12)
    c = random.randint(2, 12)
    ans = a * (b + c)
    assert a*b + a*c == ans
    opts, let = make_numeric_options(ans)
    return {
        "question": f"Expand and evaluate: {a}({b} + {c})",
        "option_a": opts[0],"option_b": opts[1],"option_c": opts[2],
        "option_d": opts[3],"option_e": opts[4],
        "answer": let,
        "explanation": f"{a}({b} + {c}) = {a}×{b} + {a}×{c} = {a*b} + {a*c} = {ans}.",
        "level": "Junior (JMC)"
    }


def tpl_simultaneous_equations():
    x = random.randint(2, 8)
    y = random.randint(1, 6)
    a1 = random.randint(1, 4)
    b1 = random.randint(1, 4)
    c1 = a1*x + b1*y
    a2 = random.randint(1, 4)
    b2 = random.randint(1, 4)
    for _ in range(20):
        if a1*b2 != a2*b1:
            break
        a2 = random.randint(1, 4)
        b2 = random.randint(1, 4)
    assert a1*b2 != a2*b1, "parallel lines"
    c2 = a2*x + b2*y
    assert a1*x + b1*y == c1 and a2*x + b2*y == c2
    ans = x + y
    opts, let = make_numeric_options(ans)
    return {
        "question": (f"Solve the simultaneous equations:\n"
                     f"{a1}x + {b1}y = {c1}\n"
                     f"{a2}x + {b2}y = {c2}\n"
                     f"What is x + y?"),
        "option_a": opts[0],"option_b": opts[1],"option_c": opts[2],
        "option_d": opts[3],"option_e": opts[4],
        "answer": let,
        "explanation": (f"The solution is x = {x}, y = {y}. "
                        f"(Check: {a1}×{x}+{b1}×{y}={c1} ✓ and {a2}×{x}+{b2}×{y}={c2} ✓.) "
                        f"x + y = {x} + {y} = {ans}."),
        "level": "Intermediate (IMC)"
    }


def tpl_index_law_multiply():
    base = random.choice([2, 3, 5])
    p = random.randint(2, 6)
    q = random.randint(2, 6)
    ans = p + q
    assert base**p * base**q == base**(p+q)
    opts, let = make_numeric_options(ans)
    return {
        "question": f"What is n if {base}^{p} × {base}^{q} = {base}^n?",
        "option_a": opts[0],"option_b": opts[1],"option_c": opts[2],
        "option_d": opts[3],"option_e": opts[4],
        "answer": let,
        "explanation": (f"When multiplying powers of the same base, add exponents: "
                        f"{p} + {q} = {ans}."),
        "level": "Intermediate (IMC)"
    }


def tpl_index_divide():
    base = random.choice([2, 3, 5])
    p = random.randint(4, 9)
    q = random.randint(1, p - 2)
    ans = p - q
    assert base**p // base**q == base**(p-q)
    opts, let = make_numeric_options(ans)
    return {
        "question": f"What is n if {base}^{p} ÷ {base}^{q} = {base}^n?",
        "option_a": opts[0],"option_b": opts[1],"option_c": opts[2],
        "option_d": opts[3],"option_e": opts[4],
        "answer": let,
        "explanation": (f"When dividing powers of the same base, subtract exponents: "
                        f"{p} − {q} = {ans}."),
        "level": "Intermediate (IMC)"
    }


def tpl_solve_power():
    base = random.choice([2, 3, 4, 5])
    exp = random.randint(2, 6)
    ans = base ** exp
    assert base**exp == ans
    steps = " × ".join([str(base)] * exp)
    opts, let = make_numeric_options(ans)
    return {
        "question": f"What is the value of {base}^{exp}?",
        "option_a": opts[0],"option_b": opts[1],"option_c": opts[2],
        "option_d": opts[3],"option_e": opts[4],
        "answer": let,
        "explanation": f"{base}^{exp} = {steps} = {ans}.",
        "level": "Junior (JMC)"
    }


def tpl_powers_arithmetic():
    a = random.randint(2, 4)
    b = random.randint(2, 5)
    c = random.randint(2, 7)
    ans = a**b * c
    assert a**b * c == ans
    opts, let = make_numeric_options(ans)
    return {
        "question": f"What is the value of {a}^{b} × {c}?",
        "option_a": opts[0],"option_b": opts[1],"option_c": opts[2],
        "option_d": opts[3],"option_e": opts[4],
        "answer": let,
        "explanation": f"{a}^{b} = {a**b}. Then {a**b} × {c} = {ans}.",
        "level": "Junior (JMC)"
    }


def tpl_percentage_of():
    p = random.choice([10,15,20,25,30,40,50,60,75,80])
    n = random.choice([20,40,60,80,100,120,160,200,240,300,400])
    assert p*n % 100 == 0
    ans = p * n // 100
    opts, let = make_numeric_options(ans)
    return {
        "question": f"What is {p}% of {n}?",
        "option_a": opts[0],"option_b": opts[1],"option_c": opts[2],
        "option_d": opts[3],"option_e": opts[4],
        "answer": let,
        "explanation": f"{p}% of {n} = ({p}/100) × {n} = {ans}.",
        "level": "Junior (JMC)"
    }


def tpl_reverse_percentage():
    p = random.choice([10,20,25,30,40,50])
    orig = random.choice([40,60,80,100,120,160,200,240])
    assert orig*(100-p) % 100 == 0
    sale = orig * (100-p) // 100
    assert sale * 100 // (100-p) == orig
    opts, let = make_numeric_options(orig)
    return {
        "question": f"After a {p}% discount, an item costs £{sale}. What was the original price?",
        "option_a": "£"+opts[0],"option_b": "£"+opts[1],
        "option_c": "£"+opts[2],"option_d": "£"+opts[3],"option_e": "£"+opts[4],
        "answer": let,
        "explanation": (f"Sale price = original × (1 − {p}/100). "
                        f"Original = £{sale} ÷ {(100-p)/100} = £{orig}."),
        "level": "Intermediate (IMC)"
    }


def tpl_percentage_increase():
    orig = random.choice([60,80,100,120,160,200,240])
    p = random.choice([10,20,25,30,40,50])
    assert orig*(100+p) % 100 == 0
    ans = orig*(100+p)//100
    opts, let = make_numeric_options(ans)
    return {
        "question": f"A price of £{orig} increases by {p}%. What is the new price?",
        "option_a": "£"+opts[0],"option_b": "£"+opts[1],
        "option_c": "£"+opts[2],"option_d": "£"+opts[3],"option_e": "£"+opts[4],
        "answer": let,
        "explanation": f"£{orig} × (1 + {p}/100) = £{orig} × {(100+p)/100} = £{ans}.",
        "level": "Junior (JMC)"
    }


def tpl_ratio_sharing():
    a = random.randint(1, 5)
    b = random.randint(1, 5)
    while a == b:
        b = random.randint(1, 5)
    total_parts = a + b
    total = total_parts * random.randint(4, 20)
    larger = max(a, b)
    ans = larger * total // total_parts
    assert larger * total % total_parts == 0 and larger * total // total_parts == ans
    opts, let = make_numeric_options(ans)
    return {
        "question": f"£{total} is shared in the ratio {a}:{b}. What is the larger share?",
        "option_a": "£"+opts[0],"option_b": "£"+opts[1],
        "option_c": "£"+opts[2],"option_d": "£"+opts[3],"option_e": "£"+opts[4],
        "answer": let,
        "explanation": (f"Each part = £{total} ÷ {total_parts} = £{total//total_parts}. "
                        f"Larger share = {larger} × £{total//total_parts} = £{ans}."),
        "level": "Junior (JMC)"
    }


def tpl_speed_distance_time():
    s = random.randint(20, 90)
    t = random.randint(2, 5)
    ans = s * t
    assert s*t == ans
    opts, let = make_numeric_options(ans)
    return {
        "question": f"A car travels at {s} km/h for {t} hours. How far does it travel?",
        "option_a": opts[0]+" km","option_b": opts[1]+" km",
        "option_c": opts[2]+" km","option_d": opts[3]+" km","option_e": opts[4]+" km",
        "answer": let,
        "explanation": f"Distance = speed × time = {s} × {t} = {ans} km.",
        "level": "Junior (JMC)"
    }


def tpl_speed_time_from_distance():
    s = random.choice([30,40,50,60])
    t = random.randint(2, 5)
    d = s * t
    assert d // s == t and d % s == 0
    opts, let = make_numeric_options(t)
    return {
        "question": f"A car travels {d} km at {s} km/h. How many hours does the journey take?",
        "option_a": opts[0]+" h","option_b": opts[1]+" h",
        "option_c": opts[2]+" h","option_d": opts[3]+" h","option_e": opts[4]+" h",
        "answer": let,
        "explanation": f"Time = distance ÷ speed = {d} ÷ {s} = {t} hours.",
        "level": "Junior (JMC)"
    }


def tpl_profit_percentage():
    pct = random.choice([10,20,25,30,40,50])
    cost = random.choice([40,60,80,100,200,400])
    assert cost*pct % 100 == 0
    profit = cost*pct//100
    selling = cost + profit
    ans = pct
    assert (selling - cost)*100 // cost == ans
    opts, let = make_numeric_options(ans)
    return {
        "question": (f"An item is bought for £{cost} and sold for £{selling}. "
                     f"What is the percentage profit?"),
        "option_a": opts[0]+"%","option_b": opts[1]+"%",
        "option_c": opts[2]+"%","option_d": opts[3]+"%","option_e": opts[4]+"%",
        "answer": let,
        "explanation": (f"Profit = £{selling} − £{cost} = £{profit}. "
                        f"Percentage profit = ({profit}/{cost}) × 100 = {ans}%."),
        "level": "Intermediate (IMC)"
    }


def tpl_compound_interest():
    combos = [
        (100, 10, 1, 110),  (200, 10, 1, 220),  (500, 10, 1, 550),
        (1000,10, 1, 1100), (100, 20, 1, 120),  (200, 20, 1, 240),
        (100, 10, 2, 121),  (200, 10, 2, 242),  (1000,10, 2, 1210),
        (100, 20, 2, 144),  (500, 20, 2, 720),  (400, 25, 2, 625),
        (100, 50, 2, 225),  (800, 25, 2, 1250),
    ]
    P, r, n, ans = random.choice(combos)
    computed = P
    for _ in range(n):
        computed = computed * (100 + r) // 100
    assert computed == ans
    yr = "year" if n == 1 else "years"
    opts, let = make_numeric_options(ans)
    return {
        "question": (f"£{P} is invested at {r}% compound interest per year. "
                     f"What is the total amount after {n} {yr}?"),
        "option_a": "£"+opts[0],"option_b": "£"+opts[1],
        "option_c": "£"+opts[2],"option_d": "£"+opts[3],"option_e": "£"+opts[4],
        "answer": let,
        "explanation": (f"Amount = P × (1 + r/100)^n = £{P} × (1.{r:02d})^{n} = £{ans}."),
        "level": "Intermediate (IMC)"
    }


def tpl_fraction_add():
    denom_pairs = [(2,3),(2,5),(3,4),(3,5),(4,5),(2,7),(3,7),(4,7),(5,6),(3,8)]
    b, d = random.choice(denom_pairs)
    a = random.randint(1, b-1)
    c = random.randint(1, d-1)
    result = Fraction(a, b) + Fraction(c, d)
    num, den = result.numerator, result.denominator
    assert Fraction(num, den) == Fraction(a,b) + Fraction(c,d)
    opts, let = make_fraction_options(num, den)
    return {
        "question": f"What is {a}/{b} + {c}/{d}? (Give your answer as a fraction in its simplest form.)",
        "option_a": opts[0],"option_b": opts[1],"option_c": opts[2],
        "option_d": opts[3],"option_e": opts[4],
        "answer": let,
        "explanation": (f"{a}/{b} + {c}/{d} = {a*d}/({b*d}) + {b*c}/({b*d}) "
                        f"= {a*d+b*c}/{b*d} = {num}/{den}."),
        "level": "Intermediate (IMC)"
    }


def tpl_fraction_multiply():
    combos = [
        (2,3,3,4),(3,4,4,5),(2,5,5,6),(3,5,5,9),(2,7,7,8),
        (3,4,8,9),(4,5,5,8),(2,3,3,8),(5,6,6,7),(3,8,4,9),
    ]
    a,b,c,d = random.choice(combos)
    result = Fraction(a,b) * Fraction(c,d)
    num, den = result.numerator, result.denominator
    assert Fraction(num,den) == Fraction(a,b)*Fraction(c,d)
    opts, let = make_fraction_options(num, den)
    return {
        "question": f"What is {a}/{b} × {c}/{d}? (Give your answer as a fraction in its simplest form.)",
        "option_a": opts[0],"option_b": opts[1],"option_c": opts[2],
        "option_d": opts[3],"option_e": opts[4],
        "answer": let,
        "explanation": (f"{a}/{b} × {c}/{d} = {a*c}/{b*d} = {num}/{den} "
                        f"(dividing by their HCF)."),
        "level": "Intermediate (IMC)"
    }


def tpl_fraction_divide():
    combos = [
        (2,3,4,5),(3,4,6,7),(2,5,4,5),(3,5,9,10),(1,2,3,4),
        (4,5,8,9),(2,7,6,7),(5,6,5,9),(3,8,9,16),(2,3,8,9),
    ]
    a,b,c,d = random.choice(combos)
    result = Fraction(a,b) / Fraction(c,d)
    num, den = result.numerator, result.denominator
    assert Fraction(num,den) == Fraction(a,b)/Fraction(c,d)
    opts, let = make_fraction_options(num, den)
    return {
        "question": f"What is {a}/{b} ÷ {c}/{d}? (Give your answer as a fraction in its simplest form.)",
        "option_a": opts[0],"option_b": opts[1],"option_c": opts[2],
        "option_d": opts[3],"option_e": opts[4],
        "answer": let,
        "explanation": (f"{a}/{b} ÷ {c}/{d} = {a}/{b} × {d}/{c} "
                        f"= {a*d}/{b*c} = {num}/{den}."),
        "level": "Intermediate (IMC)"
    }


def tpl_simple_probability():
    r = random.randint(2, 7)
    b = random.randint(2, 7)
    g = random.randint(2, 7)
    total = r + b + g
    opts, let = make_fraction_options(r, total)
    return {
        "question": (f"A bag contains {r} red, {b} blue, and {g} green balls. "
                     f"A ball is chosen at random. What is the probability it is red?"),
        "option_a": opts[0],"option_b": opts[1],"option_c": opts[2],
        "option_d": opts[3],"option_e": opts[4],
        "answer": let,
        "explanation": f"P(red) = {r}/{total} = {Fraction(r, total)}.",
        "level": "Junior (JMC)"
    }


def tpl_probability_complement():
    r = random.randint(2, 8)
    total = random.randint(r+3, 20)
    num = total - r
    den = total
    opts, let = make_fraction_options(num, den)
    return {
        "question": (f"A bag has {r} red balls and {total-r} blue balls. "
                     f"What is the probability of NOT picking a red ball?"),
        "option_a": opts[0],"option_b": opts[1],"option_c": opts[2],
        "option_d": opts[3],"option_e": opts[4],
        "answer": let,
        "explanation": (f"P(not red) = 1 − P(red) = 1 − {r}/{total} "
                        f"= {total-r}/{total} = {Fraction(num, den)}."),
        "level": "Junior (JMC)"
    }


def tpl_permutations():
    n = random.randint(3, 6)
    ans = math.factorial(n)
    assert math.factorial(n) == ans
    steps = " × ".join(str(i) for i in range(n, 0, -1))
    opts, let = make_numeric_options(ans)
    return {
        "question": f"In how many different orders can {n} people be arranged in a line?",
        "option_a": opts[0],"option_b": opts[1],"option_c": opts[2],
        "option_d": opts[3],"option_e": opts[4],
        "answer": let,
        "explanation": f"Number of arrangements = {n}! = {steps} = {ans}.",
        "level": "Intermediate (IMC)"
    }


def tpl_combinations():
    n = random.randint(5, 10)
    r = random.randint(2, 4)
    ans = math.comb(n, r)
    assert math.comb(n, r) == ans
    opts, let = make_numeric_options(ans)
    return {
        "question": f"How many ways can {r} items be chosen from {n} distinct items (order doesn't matter)?",
        "option_a": opts[0],"option_b": opts[1],"option_c": opts[2],
        "option_d": opts[3],"option_e": opts[4],
        "answer": let,
        "explanation": f"C({n},{r}) = {n}! / ({r}! × {n-r}!) = {ans}.",
        "level": "Intermediate (IMC)"
    }


def tpl_mean():
    n = 5
    target = random.randint(8, 18)
    vals = [random.randint(4, 20) for _ in range(n-1)]
    last = target * n - sum(vals)
    if last <= 0 or last > 30:
        vals = [10, 12, 14, 16]
        target = 14
        last = target*n - sum(vals)
    vals = sorted(vals + [last])
    assert sum(vals) == target * n
    opts, let = make_numeric_options(target)
    return {
        "question": f"What is the mean of {', '.join(str(v) for v in vals)}?",
        "option_a": opts[0],"option_b": opts[1],"option_c": opts[2],
        "option_d": opts[3],"option_e": opts[4],
        "answer": let,
        "explanation": f"Mean = {sum(vals)} ÷ {n} = {target}.",
        "level": "Junior (JMC)"
    }


def tpl_find_missing_for_mean():
    n = 5
    target = random.randint(10, 18)
    known = sorted([random.randint(5, 22) for _ in range(n-1)])
    missing = target * n - sum(known)
    if missing <= 0 or missing > 35:
        known = [10, 12, 14, 16]
        target = 15
        missing = target * n - sum(known)
    assert sum(known) + missing == target * n
    opts, let = make_numeric_options(missing)
    return {
        "question": (f"The mean of five numbers is {target}. "
                     f"Four of them are {', '.join(str(k) for k in known)}. "
                     f"What is the fifth?"),
        "option_a": opts[0],"option_b": opts[1],"option_c": opts[2],
        "option_d": opts[3],"option_e": opts[4],
        "answer": let,
        "explanation": (f"Total = {target}×{n} = {target*n}. "
                        f"Sum of known = {sum(known)}. "
                        f"Fifth = {target*n} − {sum(known)} = {missing}."),
        "level": "Junior (JMC)"
    }


def tpl_median():
    n = random.choice([5, 7, 9])
    vals = sorted([random.randint(1, 25) for _ in range(n)])
    ans = vals[n // 2]
    assert sorted(vals)[n//2] == ans
    shuffled = vals[:]
    random.shuffle(shuffled)
    opts, let = make_numeric_options(ans)
    return {
        "question": f"What is the median of the data set: {', '.join(str(v) for v in shuffled)}?",
        "option_a": opts[0],"option_b": opts[1],"option_c": opts[2],
        "option_d": opts[3],"option_e": opts[4],
        "answer": let,
        "explanation": (f"Ordered: {', '.join(str(v) for v in vals)}. "
                        f"The middle ({n//2+1}{['st','nd','rd','th'][min(n//2,3)]} of {n}) value is {ans}."),
        "level": "Junior (JMC)"
    }


def tpl_range_data():
    vals = [random.randint(1, 30) for _ in range(random.randint(5, 8))]
    ans = max(vals) - min(vals)
    assert max(vals) - min(vals) == ans
    opts, let = make_numeric_options(ans)
    return {
        "question": f"What is the range of the data set: {', '.join(str(v) for v in vals)}?",
        "option_a": opts[0],"option_b": opts[1],"option_c": opts[2],
        "option_d": opts[3],"option_e": opts[4],
        "answer": let,
        "explanation": f"Range = max − min = {max(vals)} − {min(vals)} = {ans}.",
        "level": "Junior (JMC)"
    }


def tpl_square_root():
    n = random.choice([4,9,16,25,36,49,64,81,100,121,144,169,196,225,256,289,324,361,400])
    ans = int(math.isqrt(n))
    assert ans * ans == n
    opts, let = make_numeric_options(ans)
    return {
        "question": f"What is √{n}?",
        "option_a": opts[0],"option_b": opts[1],"option_c": opts[2],
        "option_d": opts[3],"option_e": opts[4],
        "answer": let,
        "explanation": f"√{n} = {ans} because {ans}² = {n}.",
        "level": "Junior (JMC)"
    }


INTERMEDIATE_SENIOR_TEMPLATES = [
    tpl_trapezoid_area, tpl_cylinder_volume, tpl_sphere_surface_area,
    tpl_interior_angle_polygon, tpl_exterior_angle_polygon,
    tpl_distance_two_points, tpl_gradient_two_points,
    tpl_last_digit_power, tpl_sum_squares, tpl_sum_arithmetic_sequence,
    tpl_geometric_nth_term, tpl_power_of_two_sum, tpl_hcf, tpl_lcm,
    tpl_count_factors, tpl_factorial_value, tpl_factorial_sum,
    tpl_consecutive_odd_sum, tpl_substitute_quadratic, tpl_difference_of_squares,
    tpl_simultaneous_equations, tpl_index_law_multiply, tpl_index_divide,
    tpl_reverse_percentage, tpl_profit_percentage, tpl_compound_interest,
    tpl_fraction_add, tpl_fraction_multiply, tpl_fraction_divide,
    tpl_permutations, tpl_combinations,
]

ALL_TEMPLATES = [
    # Geometry — kept harder ones, removed pure formula plugs
    tpl_triangle_area_bh, tpl_trapezoid_area,
    tpl_cube_volume, tpl_cylinder_volume, tpl_sphere_surface_area,
    tpl_right_triangle_hypotenuse, tpl_right_triangle_leg,
    tpl_interior_angle_polygon, tpl_exterior_angle_polygon,
    tpl_distance_two_points, tpl_gradient_two_points,
    # Number theory
    tpl_last_digit_power, tpl_sum_first_n, tpl_sum_squares,
    tpl_sum_arithmetic_sequence, tpl_nth_term, tpl_geometric_nth_term,
    tpl_power_of_two_sum, tpl_divisibility_count, tpl_hcf, tpl_lcm,
    tpl_count_factors, tpl_mod_remainder, tpl_factorial_value,
    tpl_factorial_sum, tpl_consecutive_odd_sum,
    # Algebra — removed trivial ones
    tpl_substitute_quadratic, tpl_difference_of_squares,
    tpl_simultaneous_equations, tpl_index_law_multiply, tpl_index_divide,
    # Percentage / ratio
    tpl_reverse_percentage, tpl_percentage_increase,
    tpl_ratio_sharing, tpl_speed_distance_time, tpl_speed_time_from_distance,
    tpl_profit_percentage, tpl_compound_interest,
    # Fractions — kept harder ones
    tpl_fraction_multiply, tpl_fraction_divide,
    # Probability / combinatorics
    tpl_probability_complement, tpl_permutations, tpl_combinations,
    # Statistics — kept slightly harder ones
    tpl_find_missing_for_mean, tpl_median, tpl_range_data,
]


def generate_from_template(exclude_hashes=None):
    if exclude_hashes is None:
        exclude_hashes = set()
    order = (INTERMEDIATE_SENIOR_TEMPLATES * 3 + ALL_TEMPLATES)[:]
    random.shuffle(order)
    for fn in order:
        for _attempt in range(5):
            try:
                result = fn()
                if not result:
                    continue
                h = q_hash(result['question'])
                if h in exclude_hashes or hash_exists(h):
                    continue
                result['question_hash'] = h
                return result
            except AssertionError:
                continue
            except Exception as e:
                print(f"⚠️ Template {fn.__name__}: {e}")
                continue
    return None


# ─────────────────────────────────────────────────────────────────────────────
# AI SYSTEM
# ─────────────────────────────────────────────────────────────────────────────

UKMT_PROMPT = """You are an expert UKMT question writer with deep knowledge of all past UKMT papers from 1988 to present.

Generate ONE original UKMT-style multiple choice question at level: {level}

WHAT MAKES A REAL UKMT QUESTION:
- It has a CLEVER TRICK or elegant insight that makes it easy once you see it
- A student who just blindly calculates will likely get it wrong or take too long
- The best students solve it in under 60 seconds using pattern recognition or a shortcut
- Topics: number patterns, clever algebra, geometry with a twist, clever counting, modular arithmetic, sequences with a trick

GOOD UKMT QUESTION TYPES:
- "What is 1×2 + 2×3 + 3×4 + ... + 99×100?" (telescoping or formula insight)
- "How many integers from 1-1000 have digit sum equal to 5?" (counting with structure)
- "A square is inscribed in a right triangle. What fraction of the triangle is the square?" (geometry insight)
- "What is the last digit of 7^2025?" (cyclicity)
- "The sum of 5 consecutive odd numbers is 235. What is the largest?" (algebraic shortcut)
- "How many ways can you write 12 as an ordered sum of positive integers?" (combinatorics insight)
- "What is 2025 × 2025 - 2024 × 2026?" (difference of squares trick)

BAD UKMT QUESTION TYPES (DO NOT USE):
- "What is 2/5 × 5/6?" (just arithmetic, no insight)
- "What is 15% of 240?" (just arithmetic)
- "Solve 3x + 7 = 25" (just algebra, no insight)
- "What is the area of a rectangle with length 6 and width 8?" (formula plugging)
- Any question that just asks to apply a formula directly

ABSOLUTE REQUIREMENTS:
1. Exactly 5 options A–E, exactly one correct.
2. The question MUST require insight, not just calculation.
3. Triple-check your arithmetic.
4. Distractors must be the answers students get from common MISTAKES or wrong approaches.
5. You MUST provide a verify_expr: a Python expression that evaluates to True when correct.
6. VARY the answer letter — do not always use A.

Good verify_expr examples:
- "2025**2 - 2024*2026 == 1"
- "sum(i*(i+1) for i in range(1,100)) == 328350"
- "(7**2025) % 10 == 7"

Respond ONLY in this exact JSON (no markdown, no preamble):
{{
  "question": "full question text here",
  "option_a": "...",
  "option_b": "...",
  "option_c": "...",
  "option_d": "...",
  "option_e": "...",
  "answer": "B",
  "explanation": "explain the TRICK or INSIGHT that makes this easy, not just the calculation",
  "verify_expr": "python expression → True",
  "level": "{level}"
}}"""


def safe_eval_verify(expr):
    if not expr or not isinstance(expr, str):
        return None
    try:
        tree = ast.parse(expr, mode='eval')
        SAFE_NODES = {
            ast.Expression, ast.BoolOp, ast.BinOp, ast.UnaryOp,
            ast.Compare, ast.Constant, ast.Add, ast.Sub, ast.Mult,
            ast.Div, ast.Pow, ast.Mod, ast.FloorDiv, ast.BitAnd,
            ast.Eq, ast.NotEq, ast.Lt, ast.LtE, ast.Gt, ast.GtE,
            ast.And, ast.Or, ast.Not, ast.USub, ast.UAdd,
            ast.Call, ast.Name, ast.Load, ast.Num, ast.Attribute,
        }
        for node in ast.walk(tree):
            if type(node) not in SAFE_NODES:
                print(f"⚠️ Unsafe AST node: {type(node).__name__} — skipping verify")
                return None
        safe_env = {
            "__builtins__": {},
            "math": math, "abs": abs, "round": round,
            "int": int, "float": float,
            "sum": sum, "range": range, "min": min, "max": max,
            "sqrt": math.sqrt, "factorial": math.factorial,
            "comb": math.comb, "gcd": math.gcd, "pow": pow,
        }
        result = eval(compile(tree, '<verify>', 'eval'), safe_env)
        return bool(result)
    except Exception as e:
        print(f"⚠️ verify_expr eval error: {e}")
        return None


def ai_second_opinion(data):
    try:
        prompt = (
            f"Solve this maths question carefully. Show full working, then give the correct answer letter.\n\n"
            f"Question: {data['question']}\n"
            f"A) {data['option_a']}\n"
            f"B) {data['option_b']}\n"
            f"C) {data['option_c']}\n"
            f"D) {data['option_d']}\n"
            f"E) {data['option_e']}\n\n"
            f"The STATED answer is {data['answer']}.\n\n"
            f"Respond ONLY in JSON (no markdown):\n"
            f'{{ "working": "step by step", "my_answer": "A", '
            f'"agrees": true }}\n'
            f"Set agrees to true if your answer matches the stated answer, false if not."
        )
        resp = client.chat.completions.create(
            model="llama-3.3-70b-versatile",
            messages=[{"role": "user", "content": prompt}],
            temperature=0,
            max_tokens=600
        )
        raw = resp.choices[0].message.content.strip()
        if '{' in raw:
            raw = raw[raw.index('{'):raw.rindex('}')+1]
        result = json.loads(raw)

        if result.get('agrees'):
            return True, data

        their_ans = result.get('my_answer', '').strip().upper()
        if their_ans in 'ABCDE' and len(their_ans) == 1 and their_ans != data['answer']:
            print(f"⚠️ Second opinion differs: stated={data['answer']}, checker={their_ans}. Auto-correcting.")
            data = dict(data)
            data['answer']      = their_ans
            data['explanation'] = result.get('working', data['explanation'])
            return False, data

        return True, data

    except Exception as e:
        print(f"⚠️ Second opinion exception: {e}")
        return True, data


def generate_ai_question(level, exclude_hashes=None):
    if exclude_hashes is None:
        exclude_hashes = set()

    for attempt in range(5):
        try:
            resp = client.chat.completions.create(
                model="llama-3.3-70b-versatile",
                messages=[{"role": "user", "content": UKMT_PROMPT.format(level=level)}],
                temperature=0.75,
                max_tokens=1400
            )
            raw = resp.choices[0].message.content.strip()
            if '{' in raw:
                raw = raw[raw.index('{'):raw.rindex('}')+1]
            data = json.loads(raw)

            required = ['question','option_a','option_b','option_c','option_d',
                        'option_e','answer','explanation','verify_expr']
            if not all(data.get(k) for k in required):
                print(f"⚠️ Attempt {attempt+1}: missing fields")
                continue
            if data['answer'] not in 'ABCDE' or len(data['answer']) != 1:
                print(f"⚠️ Attempt {attempt+1}: bad answer letter")
                continue

            v1 = safe_eval_verify(data.get('verify_expr', ''))
            if v1 is False:
                print(f"⚠️ Attempt {attempt+1}: verify_expr=False → sending to second opinion")

            agreed, data = ai_second_opinion(data)

            if not agreed:
                v2 = safe_eval_verify(data.get('verify_expr', ''))
                if v2 is False:
                    print(f"⚠️ Attempt {attempt+1}: still wrong after correction. Retrying.")
                    continue
            elif v1 is False:
                print(f"⚠️ Attempt {attempt+1}: verify False + second agrees but math wrong. Retrying.")
                continue

            h = q_hash(data['question'])
            if h in exclude_hashes or hash_exists(h):
                print(f"ℹ️ Attempt {attempt+1}: duplicate question, retrying.")
                continue

            data['question_hash'] = h
            print(f"✅ AI question passed all checks (attempt {attempt+1})")
            return data

        except json.JSONDecodeError as e:
            print(f"⚠️ Attempt {attempt+1}: JSON parse error: {e}")
        except Exception as e:
            print(f"❌ Attempt {attempt+1} exception: {e}")

    print("❌ AI: all 5 attempts failed — will use template fallback")
    return None


def pick_level():
    r = random.random()
    if r < 0.70:
        return "Intermediate (IMC)"
    elif r < 0.90:
        return "Senior (SMC)"
    else:
        return "Junior (JMC)"


# ─────────────────────────────────────────────────────────────────────────────
# MAIN GENERATION LOGIC
# ─────────────────────────────────────────────────────────────────────────────

def generate_question():
    today = date.today().isoformat()
    conn = get_db()
    existing = conn.execute(
        'SELECT id FROM questions WHERE date = ?', (today,)
    ).fetchone()
    conn.close()
    if existing:
        return

    use_ai = random.random() < 0.50
    level  = pick_level()
    source = 'ai' if use_ai else 'template'
    data   = None

    if use_ai:
        data = generate_ai_question(level)

    if data is None:
        source = 'template' if not use_ai else 'template_fallback'
        data = generate_from_template()

    if data is None:
        print("❌ CRITICAL: both AI and template failed. No question today.")
        return

    conn = get_db()
    conn.execute(
        '''INSERT OR IGNORE INTO questions
           (date, question, question_hash, option_a, option_b, option_c,
            option_d, option_e, answer, explanation, level, source)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)''',
        (today, data['question'], data.get('question_hash', q_hash(data['question'])),
         data['option_a'], data['option_b'], data['option_c'],
         data['option_d'], data['option_e'],
         data['answer'], data['explanation'],
         data.get('level', level), source)
    )
    conn.commit()
    conn.close()
    print(f"✅ [{source}] Question saved for {today} — level: {data.get('level', level)}")


# ─────────────────────────────────────────────────────────────────────────────
# DISCORD / RCON HELPERS
# ─────────────────────────────────────────────────────────────────────────────

def run_rcon(command):
    """Only used for 'list' to check online status."""
    if not RCON_HOST or not RCON_PASSWORD:
        return None
    try:
        from mcrcon import MCRcon
        with MCRcon(RCON_HOST, RCON_PASSWORD, port=RCON_PORT) as mcr:
            return mcr.command(command)
    except Exception as e:
        print(f"❌ RCON error: {e}")
        return None


def send_console_command(command):
    """Send a plain command to DiscordSRV console channel → executed on server."""
    if not DISCORD_BOT_TOKEN or not DISCORD_CONSOLE_CHANNEL_ID:
        print("⚠️ Discord bot not configured — skipping command")
        return False
    import requests as req
    url = f"https://discord.com/api/v10/channels/{DISCORD_CONSOLE_CHANNEL_ID}/messages"
    try:
        r = req.post(
            url,
            json={"content": command},
            headers={
                "Authorization": f"Bot {DISCORD_BOT_TOKEN}",
                "Content-Type": "application/json"
            },
            timeout=5
        )
        if r.status_code not in (200, 201):
            print(f"❌ Discord API error: {r.status_code} {r.text}")
        return r.status_code in (200, 201)
    except Exception as e:
        print(f"❌ send_console_command error: {e}")
        return False


def get_player_streak(mc_username):
    conn = get_db()
    row = conn.execute(
        'SELECT current_streak, last_correct FROM player_streaks WHERE mc_username = ?',
        (mc_username,)
    ).fetchone()
    conn.close()
    if not row:
        return 0, None
    return row['current_streak'], row['last_correct']


def update_player_streak(mc_username, correct, today):
    conn = get_db()
    row = conn.execute(
        'SELECT current_streak, last_correct FROM player_streaks WHERE mc_username = ?',
        (mc_username,)
    ).fetchone()

    if not correct:
        conn.execute(
            '''INSERT INTO player_streaks (mc_username, current_streak, last_correct)
               VALUES (?, 0, ?)
               ON CONFLICT(mc_username) DO UPDATE SET current_streak=0''',
            (mc_username, today)
        )
        conn.commit()
        conn.close()
        return 0

    yesterday = (date.today() - timedelta(days=1)).isoformat()

    if not row:
        new_streak = 1
    elif row['last_correct'] == yesterday:
        new_streak = row['current_streak'] + 1
    elif row['last_correct'] == today:
        new_streak = row['current_streak']
    else:
        new_streak = 1

    conn.execute(
        '''INSERT INTO player_streaks (mc_username, current_streak, last_correct)
           VALUES (?, ?, ?)
           ON CONFLICT(mc_username) DO UPDATE SET
               current_streak = excluded.current_streak,
               last_correct   = excluded.last_correct''',
        (mc_username, new_streak, today)
    )
    conn.commit()
    conn.close()
    return new_streak


def is_player_online(mc_username):
    """Check if a player is currently online via RCON."""
    response = run_rcon("list")
    if response is None:
        return False
    return mc_username.lower() in response.lower()


def store_pending_reward(mc_username, streak, reward):
    conn = get_db()
    conn.execute(
        '''INSERT OR REPLACE INTO pending_rewards
           (mc_username, streak, item, amount, label, earned_date)
           VALUES (?, ?, ?, ?, ?, ?)''',
        (mc_username, streak, reward['item'], reward['amount'],
         reward['label'], date.today().isoformat())
    )
    conn.commit()
    conn.close()
    print(f"⏳ Reward queued for {mc_username}: {reward['label']}")


def give_streak_reward(mc_username, streak):
    """Always queue reward; deliver automatically when player comes online."""
    idx    = min(streak, 7) - 1
    reward = STREAK_REWARDS[idx]
    store_pending_reward(mc_username, streak, reward)
    print(f"⏳ Reward queued for {mc_username}: {reward['label']} (streak {streak})")


def deliver_pending_rewards(mc_username):
    """Deliver queued rewards via DiscordSRV console channel. Reset streak if day 7 claimed."""
    conn = get_db()
    rows = conn.execute(
        'SELECT * FROM pending_rewards WHERE mc_username = ?', (mc_username,)
    ).fetchall()
    conn.close()

    if not rows:
        return

    max_streak = 0
    for row in rows:
        give_cmd = f"give {mc_username} minecraft:{row['item']} {row['amount']}"
        msg_cmd  = (
            f'tellraw {mc_username} [{{"text":"[BrainBrawl] ","color":"gold","bold":true}},'
            f'{{"text":"Day {row["streak"]} streak reward: {row["label"]}!","color":"yellow"}}]'
        )
        send_console_command(give_cmd)
        send_console_command(msg_cmd)
        print(f"✅ Delivered to {mc_username}: {row['label']}")
        if row['streak'] > max_streak:
            max_streak = row['streak']

    conn = get_db()
    conn.execute('DELETE FROM pending_rewards WHERE mc_username = ?', (mc_username,))
    conn.commit()

    if max_streak >= 7:
        conn.execute(
            'UPDATE player_streaks SET current_streak = 0 WHERE mc_username = ?',
            (mc_username,)
        )
        conn.commit()
        send_console_command(
            f'tellraw {mc_username} [{{"text":"[BrainBrawl] ","color":"gold","bold":true}},'
            f'{{"text":"You completed a full 7-day streak! Streak reset — go again!","color":"aqua"}}]'
        )
        print(f"🔄 Streak reset for {mc_username} after day 7 claim")

    conn.close()


def send_wrong_answer_message(mc_username):
    send_console_command(
        f'tellraw {mc_username} [{{"text":"[BrainBrawl] ","color":"red","bold":true}},'
        f'{{"text":"Wrong answer! Your streak has been reset.","color":"white"}}]'
    )


def send_discord(mc_username, discord_username, answer, is_correct, question_text, date_str):
    if not DISCORD_WEBHOOK:
        return
    import requests as req
    colour = 0x6BCF7F if is_correct else 0xFF6B6B
    payload = {
        "embeds": [{
            "title": "📐 New BrainBrawl Submission!",
            "color": colour,
            "fields": [
                {"name": "Minecraft Username", "value": mc_username,     "inline": True},
                {"name": "Discord Username",   "value": discord_username,"inline": True},
                {"name": "Answer",             "value": answer,          "inline": True},
                {"name": "Result", "value": "✅ Correct!" if is_correct else "❌ Wrong", "inline": True},
                {"name": "Date",               "value": date_str,        "inline": True},
                {"name": "Question",
                 "value": question_text[:200] + ("…" if len(question_text) > 200 else ""),
                 "inline": False},
            ],
            "timestamp": datetime.utcnow().isoformat()
        }]
    }
    try:
        req.post(DISCORD_WEBHOOK, json=payload, timeout=5)
    except Exception as e:
        print(f"⚠️ Discord webhook failed: {e}")


# ─────────────────────────────────────────────────────────────────────────────
# SCHEDULER
# ─────────────────────────────────────────────────────────────────────────────

scheduler = BackgroundScheduler()
scheduler.add_job(generate_question, 'cron', hour=0, minute=1)


def check_and_deliver_pending():
    """Every 5 minutes, deliver rewards to any pending players who are now online."""
    conn = get_db()
    pending = conn.execute(
        'SELECT DISTINCT mc_username FROM pending_rewards'
    ).fetchall()
    conn.close()
    for row in pending:
        username = row['mc_username']
        if is_player_online(username):
            deliver_pending_rewards(username)


scheduler.add_job(check_and_deliver_pending, 'interval', minutes=5)
scheduler.start()


# ─────────────────────────────────────────────────────────────────────────────
# FLASK ROUTES
# ─────────────────────────────────────────────────────────────────────────────

@app.route('/')
def home():
    generate_question()
    return render_template('index.html')


@app.route('/sw.js')
def service_worker():
    return send_from_directory('static', 'sw.js', mimetype='application/javascript')


@app.route('/question')
def get_question():
    today     = date.today().isoformat()
    yesterday = (date.today() - timedelta(days=1)).isoformat()

    conn = get_db()
    q  = conn.execute('SELECT * FROM questions WHERE date = ?', (today,)).fetchone()
    yq = conn.execute('SELECT * FROM questions WHERE date = ?', (yesterday,)).fetchone()
    conn.close()

    if not q:
        generate_question()
        conn = get_db()
        q = conn.execute('SELECT * FROM questions WHERE date = ?', (today,)).fetchone()
        conn.close()

    if not q:
        return jsonify({'error': 'Question not yet available'}), 404

    return jsonify({
        'today': {
            'date':     q['date'],
            'question': q['question'],
            'option_a': q['option_a'],
            'option_b': q['option_b'],
            'option_c': q['option_c'],
            'option_d': q['option_d'],
            'option_e': q['option_e'],
            'level':    q['level'],
        },
        'yesterday': {
            'date':        yq['date'],
            'question':    yq['question'],
            'answer':      yq['answer'],
            'explanation': yq['explanation'],
            'level':       yq['level'],
        } if yq else None
    })


@app.route('/submit', methods=['POST'])
def submit():
    data             = request.get_json()
    mc_username      = data.get('mc_username', '').strip()
    discord_username = data.get('discord_username', '').strip()
    answer           = data.get('answer', '').strip().upper()
    today            = date.today().isoformat()

    if not mc_username:
        return jsonify({'success': False, 'error': 'Minecraft username required'})
    if not discord_username:
        return jsonify({'success': False, 'error': 'Discord username required'})
    if answer not in list('ABCDE'):
        return jsonify({'success': False, 'error': 'Invalid answer'})
    if len(mc_username) > 16:
        return jsonify({'success': False, 'error': 'Minecraft username too long (max 16)'})
    if len(discord_username) > 40:
        return jsonify({'success': False, 'error': 'Discord username too long'})

    conn = get_db()

    already = conn.execute(
        'SELECT id FROM submissions WHERE date = ? AND mc_username = ?',
        (today, mc_username)
    ).fetchone()
    if already:
        conn.close()
        return jsonify({'success': False, 'error': 'You have already submitted today!'})

    q = conn.execute('SELECT * FROM questions WHERE date = ?', (today,)).fetchone()
    if not q:
        conn.close()
        return jsonify({'success': False, 'error': 'No question found for today'})

    is_correct = int(answer == q['answer'])
    conn.execute(
        '''INSERT INTO submissions
           (date, mc_username, discord_username, answer, is_correct, submitted_at)
           VALUES (?, ?, ?, ?, ?, ?)''',
        (today, mc_username, discord_username, answer, is_correct, int(time.time()))
    )
    conn.commit()
    conn.close()

    send_discord(mc_username, discord_username, answer,
                 bool(is_correct), q['question'], today)

    new_streak = update_player_streak(mc_username, bool(is_correct), today)

    if is_correct:
        give_streak_reward(mc_username, new_streak)
        reward_idx = min(new_streak, 7) - 1
        reward_label = STREAK_REWARDS[reward_idx]['label']
    else:
        send_wrong_answer_message(mc_username)
        reward_label = None

    return jsonify({
        'success':      True,
        'correct':      bool(is_correct),
        'streak':       new_streak,
        'reward':       reward_label,
    })


@app.route('/claim/<mc_username>')
def claim_reward(mc_username):
    if not is_player_online(mc_username):
        return jsonify({
            'success': False,
            'message': f'{mc_username} must be online in-game to claim rewards.'
        })
    deliver_pending_rewards(mc_username)
    return jsonify({'success': True, 'message': 'Rewards delivered!'})


@app.route('/streak/<mc_username>')
def get_streak(mc_username):
    streak, last = get_player_streak(mc_username)
    return jsonify({'streak': streak, 'last_correct': last})


@app.route('/pending-rewards-list')
def pending_rewards_list():
    conn = get_db()
    rows = conn.execute('SELECT * FROM pending_rewards').fetchall()
    result = []
    for row in rows:
        sub = conn.execute(
            'SELECT discord_username FROM submissions WHERE mc_username = ? ORDER BY submitted_at DESC LIMIT 1',
            (row['mc_username'],)
        ).fetchone()
        result.append({
            'mc_username': row['mc_username'],
            'discord_username': sub['discord_username'] if sub else 'unknown',
            'item': row['item'],
            'amount': row['amount'],
            'label': row['label'],
            'earned_date': row['earned_date']
        })
    conn.close()
    if not result:
        return "NO_REWARDS"
    lines = []
    for r in result:
        lines.append(f"{r['mc_username']} | {r['label']} | Discord: {r['discord_username']} | {r['earned_date']}")
    return "\n".join(lines)


@app.route('/leaderboard')
def leaderboard():
    conn = get_db()
    users = conn.execute('''
        SELECT mc_username,
               COUNT(*)         AS total,
               SUM(is_correct)  AS correct
        FROM submissions
        GROUP BY mc_username
        ORDER BY correct DESC, total ASC
        LIMIT 20
    ''').fetchall()
    conn.close()
    return jsonify([dict(u) for u in users])


@app.route('/feedback-cooldown', methods=['GET'])
def feedback_cooldown():
    ip = request.remote_addr
    conn = get_db()
    row = conn.execute(
        'SELECT last_submitted FROM feedback_cooldowns WHERE ip = ?', (ip,)
    ).fetchone()
    conn.close()
    if not row:
        return jsonify({'remaining': 0})
    elapsed   = int(time.time()) - row['last_submitted']
    remaining = max(0, FEEDBACK_COOLDOWN - elapsed)
    return jsonify({'remaining': remaining * 1000})


@app.route('/set-feedback-cooldown', methods=['POST'])
def set_feedback_cooldown():
    ip = request.remote_addr
    conn = get_db()
    conn.execute(
        'INSERT OR REPLACE INTO feedback_cooldowns (ip, last_submitted) VALUES (?, ?)',
        (ip, int(time.time()))
    )
    conn.commit()
    conn.close()
    return jsonify({'success': True})


if __name__ == '__main__':
    app.run(debug=False)
