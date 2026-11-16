"""हजेरी पोर्टल — फॅब्रिकेशन शॉपसाठी कामगार हजेरी (मराठी, Flask + PostgreSQL/SQLite).

भूमिका:
  admin       : कंत्राटदार जोडतो / बदलतो / हटवतो
  contractor  : कामगार जोडतो / बदलतो / हटवतो, रोजंदारी ठरवतो, हजेरी व अहवाल पाहतो (स्वतःची हजेरी नाही)
  employee    : हजेरी लावतो (आत / बाहेर)

लॉगिन: युजरनेम + पासवर्ड. नवीन कंत्राटदार/कामगाराचे पहिले लॉगिन युनिक कोडने होते,
त्यानंतर पासवर्ड सेट करणे सक्तीचे आहे.
"""
import csv
import hmac
import io
import os
import re
import secrets
import sqlite3
import time
from contextlib import closing
from datetime import date, datetime, timedelta, timezone
from functools import wraps

try:
    import psycopg
    from psycopg.rows import dict_row
except ImportError:  # स्थानिक SQLite वापरासाठी psycopg आवश्यक नाही
    psycopg = None

from flask import (Flask, Response, abort, flash, g, redirect,
                   render_template, request, send_from_directory, session, url_for)
from markupsafe import Markup
from werkzeug.security import check_password_hash, generate_password_hash

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.environ.get("DATABASE_PATH", os.path.join(BASE_DIR, "attendance.db"))
# Render वर DATABASE_URL दिल्यास PostgreSQL वापरतो; नसल्यास स्थानिक SQLite फाइल
DATABASE_URL = os.environ.get("DATABASE_URL", "").strip()
USE_PG = bool(DATABASE_URL)
SHOP_NAME = os.environ.get("SHOP_NAME", "हजेरी पोर्टल")
# सेट केल्यास /setup वर Admin तयार करताना ही किल्ली विचारली जाते (सार्वजनिक सर्व्हरवर सुरक्षेसाठी)
SETUP_KEY = os.environ.get("SETUP_KEY", "").strip()
IST = timezone(timedelta(hours=5, minutes=30))
# १ असल्यास सर्व आकडे देवनागरीत (०१२३) दिसतील
DEVANAGARI_DIGITS = os.environ.get("DEVANAGARI_DIGITS", "0") == "1"
# एका पूर्ण दिवसाचे तास: यापेक्षा कमी काम केल्यास त्या दिवसाची मजुरी प्रमाणात (प्रो-रेटा) मोजली जाते
FULL_DAY_HOURS = float(os.environ.get("FULL_DAY_HOURS", "8"))
CODE_ALPHABET = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"  # गोंधळ टाळण्यासाठी 0/O/1/I नाहीत
MAX_CODE_ATTEMPTS = 5
MIN_PASSWORD = 4
USERNAME_RE = re.compile(r"[a-z0-9_.\-]{3,30}")
HOME = {"admin": "admin_home", "contractor": "contractor_home", "employee": "employee_home"}

MONTHS = ["जानेवारी", "फेब्रुवारी", "मार्च", "एप्रिल", "मे", "जून", "जुलै",
          "ऑगस्ट", "सप्टेंबर", "ऑक्टोबर", "नोव्हेंबर", "डिसेंबर"]
WEEKDAYS = ["सोमवार", "मंगळवार", "बुधवार", "गुरुवार", "शुक्रवार", "शनिवार", "रविवार"]


def _load_secret():
    if os.environ.get("SECRET_KEY"):
        return os.environ["SECRET_KEY"]
    path = os.path.join(os.path.dirname(os.path.abspath(DB_PATH)), ".secret_key")
    if not os.path.exists(path):
        with open(path, "w") as f:
            f.write(secrets.token_hex(32))
        try:
            os.chmod(path, 0o600)
        except OSError:
            pass
    with open(path) as f:
        return f.read().strip()


app = Flask(__name__)
app.secret_key = _load_secret()
app.config.update(
    SESSION_COOKIE_HTTPONLY=True,
    SESSION_COOKIE_SAMESITE="Lax",
    SESSION_COOKIE_SECURE=os.environ.get("COOKIE_SECURE", "0") == "1",
    PERMANENT_SESSION_LIFETIME=timedelta(hours=12),
)

# --------------------------------------------------------------------------
# डेटाबेस
# --------------------------------------------------------------------------
SCHEMA_SQLITE = """
CREATE TABLE IF NOT EXISTS users (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    role TEXT NOT NULL CHECK (role IN ('admin', 'contractor', 'employee')),
    name TEXT NOT NULL,
    username TEXT NOT NULL UNIQUE,
    designation TEXT,
    daily_wage REAL,
    ot_rate REAL,
    password_hash TEXT,
    signup_code TEXT,
    code_attempts INTEGER NOT NULL DEFAULT 0,
    contractor_id INTEGER REFERENCES users(id) ON DELETE CASCADE,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS attendance (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    in_time TEXT NOT NULL,
    out_time TEXT
);
CREATE INDEX IF NOT EXISTS idx_att_user_in ON attendance (user_id, in_time);
CREATE TABLE IF NOT EXISTS pay_overrides (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    work_date TEXT NOT NULL,
    amount REAL NOT NULL,
    UNIQUE (user_id, work_date)
);
CREATE TABLE IF NOT EXISTS advances (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    adv_date TEXT NOT NULL,
    amount REAL NOT NULL,
    note TEXT,
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_adv_user_date ON advances (user_id, adv_date);
CREATE TABLE IF NOT EXISTS salary_payments (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    month TEXT NOT NULL,
    paid_on TEXT NOT NULL,
    days INTEGER NOT NULL,
    secs INTEGER NOT NULL,
    daily_wage REAL,
    ot_rate REAL,
    base REAL NOT NULL,
    ot_secs INTEGER NOT NULL,
    ot_pay REAL NOT NULL,
    adv REAL NOT NULL,
    carry_in REAL NOT NULL,
    net REAL NOT NULL,
    carry_out REAL NOT NULL,
    created_at TEXT NOT NULL,
    UNIQUE (user_id, month)
);
"""

SCHEMA_PG = """
CREATE TABLE IF NOT EXISTS users (
    id BIGSERIAL PRIMARY KEY,
    role TEXT NOT NULL CHECK (role IN ('admin', 'contractor', 'employee')),
    name TEXT NOT NULL,
    username TEXT NOT NULL UNIQUE,
    designation TEXT,
    daily_wage DOUBLE PRECISION,
    ot_rate DOUBLE PRECISION,
    password_hash TEXT,
    signup_code TEXT,
    code_attempts INTEGER NOT NULL DEFAULT 0,
    contractor_id BIGINT REFERENCES users(id) ON DELETE CASCADE,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS attendance (
    id BIGSERIAL PRIMARY KEY,
    user_id BIGINT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    in_time TEXT NOT NULL,
    out_time TEXT
);
CREATE INDEX IF NOT EXISTS idx_att_user_in ON attendance (user_id, in_time);
CREATE TABLE IF NOT EXISTS pay_overrides (
    id BIGSERIAL PRIMARY KEY,
    user_id BIGINT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    work_date TEXT NOT NULL,
    amount DOUBLE PRECISION NOT NULL,
    UNIQUE (user_id, work_date)
);
CREATE TABLE IF NOT EXISTS advances (
    id BIGSERIAL PRIMARY KEY,
    user_id BIGINT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    adv_date TEXT NOT NULL,
    amount DOUBLE PRECISION NOT NULL,
    note TEXT,
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_adv_user_date ON advances (user_id, adv_date);
CREATE TABLE IF NOT EXISTS salary_payments (
    id BIGSERIAL PRIMARY KEY,
    user_id BIGINT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    month TEXT NOT NULL,
    paid_on TEXT NOT NULL,
    days INTEGER NOT NULL,
    secs INTEGER NOT NULL,
    daily_wage DOUBLE PRECISION,
    ot_rate DOUBLE PRECISION,
    base DOUBLE PRECISION NOT NULL,
    ot_secs INTEGER NOT NULL,
    ot_pay DOUBLE PRECISION NOT NULL,
    adv DOUBLE PRECISION NOT NULL,
    carry_in DOUBLE PRECISION NOT NULL,
    net DOUBLE PRECISION NOT NULL,
    carry_out DOUBLE PRECISION NOT NULL,
    created_at TEXT NOT NULL,
    UNIQUE (user_id, month)
);
"""


class DB:
    """sqlite3 व psycopg दोन्हींसाठी एकसारखा छोटा रॅपर: `?` प्लेसहोल्डर, row["नाव"] प्रवेश."""

    def __init__(self, conn):
        self.conn = conn

    def execute(self, sql, params=()):
        if USE_PG:
            cur = self.conn.cursor()
            cur.execute(sql.replace("?", "%s"), params)
            return cur
        return self.conn.execute(sql, params)

    def commit(self):
        self.conn.commit()

    def close(self):
        self.conn.close()


def open_db():
    """नवीन कनेक्शन (Flask च्या बाहेरील स्क्रिप्टसाठीही वापरता येते)."""
    if USE_PG:
        if psycopg is None:
            raise RuntimeError("DATABASE_URL दिले आहे पण psycopg स्थापित नाही (pip install -r requirements.txt).")
        # prepare_threshold=None: Supabase/pgbouncer सारख्या pooler (transaction mode) सोबतही चालण्यासाठी
        return DB(psycopg.connect(DATABASE_URL, row_factory=dict_row, connect_timeout=10, prepare_threshold=None))
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return DB(conn)


def init_db():
    if USE_PG:
        db = open_db()
        try:
            # अनेक gunicorn वर्कर एकाच वेळी सुरू झाल्यास टेबल तयार करताना टक्कर टाळण्यासाठी लॉक
            db.execute("SELECT pg_advisory_xact_lock(727201)")
            for stmt in SCHEMA_PG.split(";"):
                if stmt.strip():
                    db.execute(stmt)
            # जुन्या डेटाबेससाठी नवीन कॉलम (फक्त नसेल तरच ALTER, म्हणजे अनावश्यक लॉक नाही)
            has = db.execute("SELECT 1 FROM information_schema.columns WHERE table_schema = current_schema() "
                             "AND table_name = 'users' AND column_name = 'ot_rate'").fetchone()
            if not has:
                db.execute("ALTER TABLE users ADD COLUMN ot_rate DOUBLE PRECISION")
            db.commit()
        finally:
            db.close()
        return
    os.makedirs(os.path.dirname(os.path.abspath(DB_PATH)), exist_ok=True)
    with closing(sqlite3.connect(DB_PATH)) as raw:
        cols = [r[1] for r in raw.execute("PRAGMA table_info(users)")]
        legacy = bool(cols) and "username" not in cols
    if legacy:
        # मोबाईल-आधारित जुनी आवृत्ती: जुनी फाइल जपून ठेवून नवीन डेटाबेस तयार करतो
        os.replace(DB_PATH, DB_PATH + ".old")
        print(f"जुना डेटाबेस {DB_PATH}.old म्हणून जपला; नवीन डेटाबेस तयार केला.")
    with closing(sqlite3.connect(DB_PATH)) as raw:
        raw.execute("PRAGMA journal_mode = WAL")
        raw.executescript(SCHEMA_SQLITE)
        if "ot_rate" not in [r[1] for r in raw.execute("PRAGMA table_info(users)")]:
            raw.execute("ALTER TABLE users ADD COLUMN ot_rate REAL")  # जुन्या डेटाबेससाठी
        raw.commit()


def get_db():
    if "db" not in g:
        g.db = open_db()
    return g.db


@app.teardown_appcontext
def close_db(_exc):
    db = g.pop("db", None)
    if db is not None:
        db.close()


init_db()

# --------------------------------------------------------------------------
# वेळ / फॉरमॅटिंग
# --------------------------------------------------------------------------
_TO_DEV = str.maketrans("0123456789", "०१२३४५६७८९")
_TO_ASCII = str.maketrans("०१२३४५६७८९", "0123456789")


def now():
    return datetime.now(IST).replace(tzinfo=None, microsecond=0)


def today():
    return now().date()


def ts(dt):
    return dt.strftime("%Y-%m-%d %H:%M:%S")


def parse_ts(s):
    return datetime.strptime(s[:19], "%Y-%m-%d %H:%M:%S")


def dn(text):
    text = str(text)
    return text.translate(_TO_DEV) if DEVANAGARI_DIGITS else text


def norm(s):
    return (s or "").strip().translate(_TO_ASCII)


def norm_username(s):
    return norm(s).lower()


def valid_username(u):
    return bool(USERNAME_RE.fullmatch(u))


def parse_wage(raw):
    """रोजंदारी: रिकामी = ठरवलेली नाही. परत: (किंमत, चूक?)"""
    raw = norm(raw).replace(",", "")
    if raw == "":
        return None, False
    try:
        v = float(raw)
    except ValueError:
        return None, True
    if v < 0 or v > 1_000_000:
        return None, True
    return round(v, 2), False


@app.template_filter("mdate")
def f_mdate(value, weekday=True):
    d = value if isinstance(value, date) else parse_ts(value + " 00:00:00" if len(value) == 10 else value).date()
    out = f"{d.day} {MONTHS[d.month - 1]} {d.year}"
    if weekday:
        out += f" ({WEEKDAYS[d.weekday()]})"
    return dn(out)


@app.template_filter("mtime")
def f_mtime(value):
    dt = parse_ts(value)
    h = dt.hour
    if h < 4:
        period = "रात्री"
    elif h < 12:
        period = "सकाळी"
    elif h < 16:
        period = "दुपारी"
    elif h < 20:
        period = "संध्याकाळी"
    else:
        period = "रात्री"
    return dn(f"{period} {h % 12 or 12}:{dt.minute:02d}")


@app.template_filter("mdur")
def f_mdur(seconds):
    minutes = int(seconds // 60)
    h, m = divmod(minutes, 60)
    if h and m:
        return dn(f"{h} तास {m} मिनिटे")
    if h:
        return dn(f"{h} तास")
    return dn(f"{m} मिनिटे")


@app.template_filter("mrs")
def f_mrs(value):
    if value is None:
        return "—"
    s = f"{abs(value):,.2f}".rstrip("0").rstrip(".")
    return dn(f"−₹ {s}" if value < 0 else f"₹ {s}")


@app.template_filter("mmonth")
def f_mmonth(d):
    return dn(f"{MONTHS[d.month - 1]} {d.year}")


@app.template_filter("digits")
def f_digits(value):
    return dn(value)


@app.template_filter("inputdt")
def f_inputdt(value):
    return parse_ts(value).strftime("%Y-%m-%dT%H:%M") if value else ""


# --------------------------------------------------------------------------
# हजेरी व मजुरी गणना
# --------------------------------------------------------------------------
def session_view(row, now_dt):
    """एका नोंदीसाठी कालावधी. बाहेरची वेळ नसलेली जुनी नोंद 'अपूर्ण' मानली जाते."""
    in_dt = parse_ts(row["in_time"])
    if row["out_time"]:
        secs = max(0, int((parse_ts(row["out_time"]) - in_dt).total_seconds()))
        return {"id": row["id"], "in_time": row["in_time"], "out_time": row["out_time"],
                "secs": secs, "open": False, "incomplete": False}
    if in_dt.date() == now_dt.date():
        secs = max(0, int((now_dt - in_dt).total_seconds()))
        return {"id": row["id"], "in_time": row["in_time"], "out_time": None,
                "secs": secs, "open": True, "incomplete": False}
    return {"id": row["id"], "in_time": row["in_time"], "out_time": None,
            "secs": 0, "open": True, "incomplete": True}


def rupee(x):
    """जवळच्या रुपयाला (०.५ वर चढते) — रोख देण्यासाठी पैसे नकोत."""
    return float(int(x + 0.5))


def day_pay(secs, wage, override=None):
    """पूर्ण दिवस (FULL_DAY_HOURS किंवा जास्त) = पूर्ण रोजंदारी.
    कमी तास: कंत्राटदाराने ठरवलेली रक्कम (override); ठरवली नसल्यास प्रमाणात (प्रो-रेटा) अंदाज."""
    full = FULL_DAY_HOURS * 3600
    if secs >= full:
        return round(wage, 2) if wage else None
    if override is not None:
        return round(override, 2)
    if not wage:
        return None
    return rupee(wage * secs / full)


def fetch_overrides(db, user_id, start, end_excl):
    rows = db.execute(
        "SELECT work_date, amount FROM pay_overrides WHERE user_id = ? AND work_date >= ? AND work_date < ?",
        (user_id, start.isoformat(), end_excl.isoformat())).fetchall()
    return {r["work_date"]: r["amount"] for r in rows}


def build_days(rows, now_dt, wage=None, overrides=None, ot_rate=None):
    overrides = overrides or {}
    full = FULL_DAY_HOURS * 3600
    days = {}
    for r in rows:
        v = session_view(r, now_dt)
        d = r["in_time"][:10]
        day = days.setdefault(d, {"date": d, "sessions": [], "secs": 0, "incomplete": False,
                                  "running": False, "pay": None})
        day["sessions"].append(v)
        day["secs"] += v["secs"]
        day["incomplete"] = day["incomplete"] or v["incomplete"]
        day["running"] = day["running"] or (v["open"] and not v["incomplete"])
    for day in days.values():
        ov = overrides.get(day["date"])
        day["short"] = day["secs"] < full
        # मजुरी ठरवता येईल: दिवस संपलेला (कुणी आत नाही), सर्व बाहेरच्या वेळा नोंदलेल्या, आणि तास कमी
        day["decidable"] = day["short"] and not day["running"] and not day["incomplete"]
        day["override"] = ov if day["short"] else None
        day["pay"] = day_pay(day["secs"], wage, ov)
        day["auto_pay"] = day_pay(day["secs"], wage)
        day["needs_decision"] = day["decidable"] and ov is None
        # ओव्हरटाइम: FULL_DAY_HOURS पेक्षा जास्त तास; दर ठरवलेला असेल तरच रक्कम
        extra = 0 if day["incomplete"] else max(0, day["secs"] - full)
        day["ot_secs"] = extra
        day["ot_pay"] = rupee(extra / 3600 * ot_rate) if (extra and ot_rate) else 0.0
    return sorted(days.values(), key=lambda x: x["date"], reverse=True)


def total_pay(days):
    vals = [d["pay"] for d in days if d["pay"] is not None]
    return round(sum(vals), 2) if vals else None


def total_ot(days):
    return round(sum(d["ot_pay"] for d in days), 2)


def fetch_advances(db, user_id, start, end_excl):
    return db.execute(
        "SELECT * FROM advances WHERE user_id = ? AND adv_date >= ? AND adv_date < ? ORDER BY adv_date, id",
        (user_id, start.isoformat(), end_excl.isoformat())).fetchall()


def get_payment(db, user_id, month_key):
    return db.execute("SELECT * FROM salary_payments WHERE user_id = ? AND month = ?",
                      (user_id, month_key)).fetchone()


def parse_month(raw, default=None):
    try:
        y, m = (int(x) for x in (raw or "").split("-"))
        return date(y, m, 1)
    except ValueError:
        return default


def next_month_first(first):
    return (first + timedelta(days=32)).replace(day=1)


def summarize(db, emp, start, end, now_dt):
    """कालावधीचा हिशोब: दिवस, मजुरी, ओव्हरटाइम, उचल. (मागील थकबाकी येथे नाही.)"""
    end_excl = end + timedelta(days=1)
    days = build_days(fetch_sessions(db, emp["id"], start, end_excl), now_dt, emp["daily_wage"],
                      fetch_overrides(db, emp["id"], start, end_excl), emp["ot_rate"])
    base = total_pay(days) or 0.0
    ot = total_ot(days)
    advs = fetch_advances(db, emp["id"], start, end_excl)
    adv_total = round(sum(a["amount"] for a in advs), 2)
    gross = round(base + ot, 2)
    return {"days": days, "secs": sum(d["secs"] for d in days), "base": base, "ot": ot,
            "ot_secs": sum(d["ot_secs"] for d in days), "gross": gross,
            "advances": advs, "adv_total": adv_total, "balance": round(gross - adv_total, 2),
            "pending": sum(1 for d in days if d["needs_decision"]),
            "custom": sum(1 for d in days if d["override"] is not None),
            "incomplete": any(d["incomplete"] for d in days),
            "running": any(d["running"] for d in days)}


def month_summary(db, emp, first, now_dt):
    """एका महिन्याचा पूर्ण पगार-हिशोब: मजुरी + OT − उचल − मागील महिन्याची न फिटलेली उचल."""
    last = next_month_first(first) - timedelta(days=1)
    sm = summarize(db, emp, first, last, now_dt)
    prev = get_payment(db, emp["id"], (first - timedelta(days=1)).strftime("%Y-%m"))
    carry_in = round(prev["carry_out"], 2) if prev else 0.0
    due = round(sm["gross"] - sm["adv_total"] - carry_in, 2)
    sm.update(month=first, carry_in=carry_in, due=due, net=round(max(due, 0.0), 2),
              carry_out=round(max(-due, 0.0), 2), payment=get_payment(db, emp["id"], first.strftime("%Y-%m")))
    return sm


def fetch_sessions(db, user_id, start, end_excl):
    return db.execute(
        "SELECT * FROM attendance WHERE user_id = ? AND in_time >= ? AND in_time < ? ORDER BY in_time",
        (user_id, f"{start.isoformat()} 00:00:00", f"{end_excl.isoformat()} 00:00:00"),
    ).fetchall()


def parse_date(s, default):
    try:
        return datetime.strptime(s or "", "%Y-%m-%d").date()
    except ValueError:
        return default


def get_range():
    t = today()
    start = parse_date(request.args.get("from"), t.replace(day=1))
    end = parse_date(request.args.get("to"), t)
    if start > end:
        start, end = end, start
    if (end - start).days > 366:
        start = end - timedelta(days=366)
    return start, end


# --------------------------------------------------------------------------
# सुरक्षा: CSRF, लॉगिन, प्रयत्न मर्यादा
# --------------------------------------------------------------------------
INTEGRITY = (sqlite3.IntegrityError,) + ((psycopg.IntegrityError,) if psycopg else ())
FAILS = {}


def too_many(key, limit=8, window=900):
    t = time.time()
    FAILS[key] = [x for x in FAILS.get(key, []) if t - x < window]
    return len(FAILS[key]) >= limit


def record_fail(key):
    FAILS.setdefault(key, []).append(time.time())


@app.context_processor
def inject_helpers():
    def csrf_token():
        if "_csrf" not in session:
            session["_csrf"] = secrets.token_hex(16)
        return session["_csrf"]

    def csrf_input():
        return Markup(f'<input type="hidden" name="_csrf" value="{csrf_token()}">')

    return {"csrf_token": csrf_token, "csrf_input": csrf_input,
            "dev_digits": "1" if DEVANAGARI_DIGITS else "0", "shop_name": SHOP_NAME,
            "full_day_hours": dn(f"{FULL_DAY_HOURS:g}")}


@app.before_request
def load_user():
    g.user = None
    uid = session.get("uid")
    if uid:
        g.user = get_db().execute("SELECT * FROM users WHERE id = ?", (uid,)).fetchone()
        if g.user is None:
            session.clear()
    if request.method == "POST":
        token = session.get("_csrf")
        if not token or not hmac.compare_digest(token, request.form.get("_csrf", "")):
            abort(400)


@app.after_request
def security_headers(resp):
    resp.headers.setdefault("X-Content-Type-Options", "nosniff")
    resp.headers.setdefault("X-Frame-Options", "DENY")
    resp.headers.setdefault("Referrer-Policy", "same-origin")
    return resp


def login_required(role=None):
    def deco(f):
        @wraps(f)
        def wrapper(*a, **kw):
            if g.user is None:
                return redirect(url_for("login"))
            if role and g.user["role"] != role:
                abort(403)
            return f(*a, **kw)
        return wrapper
    return deco


def new_code(db):
    while True:
        code = "".join(secrets.choice(CODE_ALPHABET) for _ in range(6))
        if not db.execute("SELECT 1 FROM users WHERE signup_code = ?", (code,)).fetchone():
            return code


def username_taken(db, username, except_id=None):
    row = db.execute("SELECT id FROM users WHERE username = ?", (username,)).fetchone()
    return bool(row) and row["id"] != except_id


def own_employee_or_404(db, emp_id):
    emp = db.execute("SELECT * FROM users WHERE id = ? AND role = 'employee' AND contractor_id = ?",
                     (emp_id, g.user["id"])).fetchone()
    if emp is None:
        abort(404)
    return emp


def contractor_or_404(db, cid):
    c = db.execute("SELECT * FROM users WHERE id = ? AND role = 'contractor'", (cid,)).fetchone()
    if c is None:
        abort(404)
    return c


@app.errorhandler(400)
def e400(_e):
    return render_template("error.html", title="विनंती अवैध",
                           msg="पान जुने झाले आहे किंवा विनंती अवैध आहे. कृपया पान रिफ्रेश करून पुन्हा प्रयत्न करा."), 400


@app.errorhandler(403)
def e403(_e):
    return render_template("error.html", title="प्रवेश नाही",
                           msg="या पानावर जाण्याची आपल्याला परवानगी नाही."), 403


@app.errorhandler(404)
def e404(_e):
    return render_template("error.html", title="पान सापडले नाही",
                           msg="आपण शोधत असलेले पान अस्तित्वात नाही."), 404


# --------------------------------------------------------------------------
# सुरुवात / लॉगिन / पहिले लॉगिन (कोडने) / पासवर्ड
# --------------------------------------------------------------------------
def admin_exists():
    return get_db().execute("SELECT 1 FROM users WHERE role = 'admin'").fetchone() is not None


# --------------------------------------------------------------------------
# PWA: manifest, service worker, ऑफलाइन पान
# --------------------------------------------------------------------------
@app.route("/manifest.webmanifest")
def manifest():
    import json
    data = {
        "name": SHOP_NAME + " — हजेरी", "short_name": SHOP_NAME[:12],
        "description": "फॅब्रिकेशन शॉप कामगार हजेरी व मजुरी",
        "lang": "mr", "dir": "ltr", "start_url": "/", "scope": "/", "id": "/",
        "display": "standalone", "orientation": "portrait",
        "background_color": "#14181c", "theme_color": "#14181c",
        "icons": [
            {"src": "/static/icons/icon-192.png", "sizes": "192x192", "type": "image/png", "purpose": "any"},
            {"src": "/static/icons/icon-512.png", "sizes": "512x512", "type": "image/png", "purpose": "any"},
            {"src": "/static/icons/icon-maskable-512.png", "sizes": "512x512", "type": "image/png", "purpose": "maskable"},
        ],
    }
    return Response(json.dumps(data, ensure_ascii=False), mimetype="application/manifest+json")


@app.route("/sw.js")
def service_worker():
    """रूटवरून द्यावी लागते, म्हणजे संपूर्ण साइटवर लागू होते."""
    resp = send_from_directory(os.path.join(BASE_DIR, "static"), "sw.js", mimetype="application/javascript")
    resp.headers["Cache-Control"] = "no-cache"
    resp.headers["Service-Worker-Allowed"] = "/"
    return resp


@app.route("/offline")
def offline():
    g.user = None  # कॅश होणाऱ्या पानात कोणाचीही लॉगिन माहिती/टोकन नको
    return render_template("offline.html")


@app.route("/healthz")
def healthz():
    get_db().execute("SELECT 1").fetchone()
    return "ok"


@app.route("/")
def index():
    if g.user is None:
        return redirect(url_for("login") if admin_exists() else url_for("setup"))
    return redirect(url_for(HOME[g.user["role"]]))


@app.route("/setup", methods=["GET", "POST"])
def setup():
    """पहिल्यांदा चालवताना एकमेव Admin खाते तयार करण्यासाठी (फक्त एकदा)."""
    if admin_exists():
        return redirect(url_for("login"))
    form = {"name": "", "username": ""}
    if request.method == "POST":
        form["name"] = request.form.get("name", "").strip()
        form["username"] = norm_username(request.form.get("username"))
        pw, pw2 = request.form.get("password", ""), request.form.get("password2", "")
        if SETUP_KEY and not hmac.compare_digest(SETUP_KEY.encode(), request.form.get("setup_key", "").strip().encode()):
            flash("सेटअप किल्ली चुकीची आहे.", "error")
        elif not form["name"]:
            flash("कृपया नाव लिहा.", "error")
        elif not valid_username(form["username"]):
            flash("युजरनेम ३ ते ३० अक्षरांचे असावे (इंग्रजी अक्षरे, अंक, _ . - चालतील; जागा नको).", "error")
        elif len(pw) < MIN_PASSWORD:
            flash(f"पासवर्ड किमान {dn(MIN_PASSWORD)} अक्षरांचा असावा.", "error")
        elif pw != pw2:
            flash("दोन्ही पासवर्ड जुळत नाहीत.", "error")
        else:
            db = get_db()
            db.execute("INSERT INTO users (role, name, username, password_hash, created_at) VALUES ('admin', ?, ?, ?, ?)",
                       (form["name"], form["username"], generate_password_hash(pw), ts(now())))
            db.commit()
            flash("Admin खाते तयार झाले. आता लॉगिन करा.", "success")
            return redirect(url_for("login"))
    return render_template("setup.html", form=form, need_key=bool(SETUP_KEY))


@app.route("/login", methods=["GET", "POST"])
def login():
    if g.user:
        return redirect(url_for("index"))
    if not admin_exists():
        return redirect(url_for("setup"))
    username = ""
    if request.method == "POST":
        username = norm_username(request.form.get("username"))
        password = request.form.get("password", "")
        key = f"login:{username}"
        if too_many(key):
            flash("खूप चुकीचे प्रयत्न झाले. कृपया १५ मिनिटांनी पुन्हा प्रयत्न करा.", "error")
        else:
            user = get_db().execute("SELECT * FROM users WHERE username = ?", (username,)).fetchone()
            if user and not user["password_hash"]:
                flash("आपण अजून पासवर्ड सेट केलेला नाही. कृपया 'पहिले लॉगिन (कोडने)' वापरा.", "error")
            elif user and check_password_hash(user["password_hash"], password):
                session.clear()
                session.permanent = True
                session["uid"] = user["id"]
                FAILS.pop(key, None)
                return redirect(url_for("index"))
            else:
                record_fail(key)
                flash("युजरनेम किंवा पासवर्ड चुकीचा आहे.", "error")
    return render_template("login.html", username=username)


@app.route("/first-login", methods=["GET", "POST"])
def first_login():
    """पहिले लॉगिन: युजरनेम + युनिक कोड. यशस्वी झाल्यावर पासवर्ड सेट करणे सक्तीचे."""
    if g.user:
        return redirect(url_for("index"))
    username = ""
    if request.method == "POST":
        db = get_db()
        username = norm_username(request.form.get("username"))
        code = norm(request.form.get("code")).upper().replace(" ", "")
        user = db.execute("SELECT * FROM users WHERE username = ? AND role IN ('contractor', 'employee')",
                          (username,)).fetchone()
        generic = "युजरनेम किंवा कोड चुकीचा आहे, किंवा पासवर्ड आधीच सेट झालेला आहे."
        if user is None or user["password_hash"] or not user["signup_code"]:
            flash(generic, "error")
        elif user["code_attempts"] >= MAX_CODE_ATTEMPTS:
            flash("खूप चुकीचे प्रयत्न झाले. कृपया आपल्या वरिष्ठांकडून नवीन कोड घ्या.", "error")
        elif not hmac.compare_digest(user["signup_code"], code):
            db.execute("UPDATE users SET code_attempts = code_attempts + 1 WHERE id = ?", (user["id"],))
            db.commit()
            flash(generic, "error")
        else:
            session.clear()
            session["setup_uid"] = user["id"]
            return redirect(url_for("set_password"))
    return render_template("first_login.html", username=username)


@app.route("/set-password", methods=["GET", "POST"])
def set_password():
    """कोड पडताळल्यानंतरचे सक्तीचे पासवर्ड-सेटिंग पान."""
    if g.user:
        return redirect(url_for("index"))
    uid = session.get("setup_uid")
    db = get_db()
    user = db.execute("SELECT * FROM users WHERE id = ?", (uid,)).fetchone() if uid else None
    if user is None or user["password_hash"] or not user["signup_code"]:
        session.pop("setup_uid", None)
        flash("कृपया आधी युजरनेम व कोड टाकून पहिले लॉगिन करा.", "error")
        return redirect(url_for("first_login"))
    if request.method == "POST":
        pw, pw2 = request.form.get("password", ""), request.form.get("password2", "")
        if len(pw) < MIN_PASSWORD:
            flash(f"पासवर्ड किमान {dn(MIN_PASSWORD)} अक्षरांचा असावा.", "error")
        elif pw != pw2:
            flash("दोन्ही पासवर्ड जुळत नाहीत.", "error")
        else:
            db.execute("UPDATE users SET password_hash = ?, signup_code = NULL, code_attempts = 0 WHERE id = ?",
                       (generate_password_hash(pw), user["id"]))
            db.commit()
            session.clear()
            session.permanent = True
            session["uid"] = user["id"]
            flash("पासवर्ड सेट झाला. आता पुढे या पासवर्डनेच लॉगिन करा. स्वागत आहे!", "success")
            return redirect(url_for("index"))
    return render_template("set_password.html", user=user)


@app.route("/logout", methods=["POST"])
def logout():
    session.clear()
    return redirect(url_for("login"))


@app.route("/password", methods=["GET", "POST"])
@login_required()
def change_password():
    if request.method == "POST":
        old = request.form.get("old", "")
        pw, pw2 = request.form.get("password", ""), request.form.get("password2", "")
        if not check_password_hash(g.user["password_hash"], old):
            flash("सध्याचा पासवर्ड चुकीचा आहे.", "error")
        elif len(pw) < MIN_PASSWORD:
            flash(f"नवीन पासवर्ड किमान {dn(MIN_PASSWORD)} अक्षरांचा असावा.", "error")
        elif pw != pw2:
            flash("दोन्ही नवीन पासवर्ड जुळत नाहीत.", "error")
        else:
            db = get_db()
            db.execute("UPDATE users SET password_hash = ? WHERE id = ?", (generate_password_hash(pw), g.user["id"]))
            db.commit()
            flash("पासवर्ड बदलला गेला.", "success")
            return redirect(url_for("index"))
    return render_template("change_password.html")


# --------------------------------------------------------------------------
# Admin: कंत्राटदार व्यवस्थापन
# --------------------------------------------------------------------------
@app.route("/admin")
@login_required("admin")
def admin_home():
    db = get_db()
    rows = db.execute(
        "SELECT c.*, (SELECT COUNT(*) FROM users e WHERE e.contractor_id = c.id AND e.role = 'employee') AS emp_count "
        "FROM users c WHERE c.role = 'contractor' ORDER BY c.name").fetchall()
    return render_template("admin_dashboard.html", contractors=rows,
                           total_emps=sum(r["emp_count"] for r in rows),
                           pending=sum(1 for r in rows if not r["password_hash"]))


@app.route("/admin/contractor/add", methods=["GET", "POST"])
@app.route("/admin/contractor/<int:cid>/edit", methods=["GET", "POST"])
@login_required("admin")
def contractor_form(cid=None):
    db = get_db()
    contractor = contractor_or_404(db, cid) if cid else None
    form = {"name": contractor["name"] if contractor else "",
            "username": contractor["username"] if contractor else ""}
    if request.method == "POST":
        form["name"] = request.form.get("name", "").strip()
        form["username"] = norm_username(request.form.get("username"))
        if not form["name"]:
            flash("कृपया कंत्राटदाराचे नाव लिहा.", "error")
        elif not valid_username(form["username"]):
            flash("युजरनेम ३ ते ३० अक्षरांचे असावे (इंग्रजी अक्षरे, अंक, _ . - चालतील; जागा नको).", "error")
        elif username_taken(db, form["username"], contractor["id"] if contractor else None):
            flash("हे युजरनेम आधीच वापरात आहे.", "error")
        elif contractor:
            db.execute("UPDATE users SET name = ?, username = ? WHERE id = ?",
                       (form["name"], form["username"], contractor["id"]))
            db.commit()
            flash("कंत्राटदाराची माहिती बदलली.", "success")
            return redirect(url_for("admin_home"))
        else:
            code = new_code(db)
            db.execute("INSERT INTO users (role, name, username, signup_code, created_at) VALUES ('contractor', ?, ?, ?, ?)",
                       (form["name"], form["username"], code, ts(now())))
            db.commit()
            flash(f"{form['name']} नोंदवले गेले. पहिल्या लॉगिनचा कोड: {code} — हा कोड कंत्राटदाराला द्या.", "success")
            return redirect(url_for("admin_home"))
    return render_template("contractor_form.html", form=form, contractor=contractor)


@app.route("/admin/contractor/<int:cid>/reset", methods=["POST"])
@login_required("admin")
def contractor_reset(cid):
    """पासवर्ड विसरल्यास: नवीन कोड तयार होतो; कंत्राटदार पुन्हा कोडने लॉगिन करून नवीन पासवर्ड सेट करतो."""
    db = get_db()
    c = contractor_or_404(db, cid)
    code = new_code(db)
    db.execute("UPDATE users SET password_hash = NULL, signup_code = ?, code_attempts = 0 WHERE id = ?", (code, c["id"]))
    db.commit()
    flash(f"{c['name']} यांचा नवीन कोड: {code} — त्यांनी 'पहिले लॉगिन (कोडने)' वापरून नवीन पासवर्ड सेट करावा.", "success")
    return redirect(url_for("admin_home"))


@app.route("/admin/contractor/<int:cid>/delete", methods=["POST"])
@login_required("admin")
def contractor_delete(cid):
    db = get_db()
    c = contractor_or_404(db, cid)
    db.execute("DELETE FROM attendance WHERE user_id IN (SELECT id FROM users WHERE contractor_id = ?)", (c["id"],))
    db.execute("DELETE FROM users WHERE contractor_id = ?", (c["id"],))
    db.execute("DELETE FROM users WHERE id = ?", (c["id"],))
    db.commit()
    flash(f"{c['name']} व त्यांचे सर्व कामगार हटवले गेले.", "success")
    return redirect(url_for("admin_home"))


# --------------------------------------------------------------------------
# कामगार: हजेरी लावणे
# --------------------------------------------------------------------------
@app.route("/employee")
@login_required("employee")
def employee_home():
    db = get_db()
    now_dt = now()
    t = today()
    wage = g.user["daily_wage"]
    first = t.replace(day=1)
    try:
        y, m = (int(x) for x in request.args.get("month", "").split("-"))
        first = date(y, m, 1)
    except ValueError:
        pass
    if first > t.replace(day=1):
        first = t.replace(day=1)
    next_first = (first + timedelta(days=32)).replace(day=1)
    prev_first = (first - timedelta(days=1)).replace(day=1)

    summ = month_summary(db, g.user, first, now_dt)
    days = summ["days"]
    month_secs = summ["secs"]

    open_row = db.execute("SELECT * FROM attendance WHERE user_id = ? AND out_time IS NULL ORDER BY in_time DESC LIMIT 1",
                          (g.user["id"],)).fetchone()
    current = session_view(open_row, now_dt) if open_row else None
    today_rows = fetch_sessions(db, g.user["id"], t, t + timedelta(days=1))
    today_secs = sum(session_view(r, now_dt)["secs"] for r in today_rows)
    return render_template(
        "employee_dashboard.html", days=days, month_secs=month_secs, month_pay=summ["gross"],
        s=summ, emp=g.user, wage=wage, current=current, today_secs=today_secs, month=first,
        is_current_month=(first == t.replace(day=1)),
        prev_month=prev_first.strftime("%Y-%m"), next_month=next_first.strftime("%Y-%m"))


@app.route("/punch", methods=["POST"])
@login_required("employee")
def punch():
    db = get_db()
    now_dt = now()
    open_row = db.execute("SELECT * FROM attendance WHERE user_id = ? AND out_time IS NULL ORDER BY in_time DESC LIMIT 1",
                          (g.user["id"],)).fetchone()
    if open_row:
        started = parse_ts(open_row["in_time"])
        if (now_dt - started).total_seconds() < 30:
            flash("आत्ताच हजेरी लावली आहे. कृपया थोडा वेळ थांबा.", "error")
        else:
            db.execute("UPDATE attendance SET out_time = ? WHERE id = ?", (ts(now_dt), open_row["id"]))
            db.commit()
            secs = int((now_dt - started).total_seconds())
            flash(f"बाहेर जाण्याची वेळ नोंदवली. या सत्रातील काम: {f_mdur(secs)}", "success")
    else:
        db.execute("INSERT INTO attendance (user_id, in_time) VALUES (?, ?)", (g.user["id"], ts(now_dt)))
        db.commit()
        flash("आत येण्याची वेळ नोंदवली. शुभ कामकाज!", "success")
    return redirect(url_for("employee_home"))


# --------------------------------------------------------------------------
# कंत्राटदार (स्वतःची हजेरी नाही)
# --------------------------------------------------------------------------
@app.route("/contractor")
@login_required("contractor")
def contractor_home():
    db = get_db()
    now_dt = now()
    t = today()
    emps = db.execute("SELECT * FROM users WHERE role = 'employee' AND contractor_id = ? ORDER BY name",
                      (g.user["id"],)).fetchall()
    cards, inside, absent, pending, out_n = [], 0, 0, 0, 0
    for e in emps:
        rows = db.execute(
            "SELECT * FROM attendance WHERE user_id = ? AND (in_time >= ? OR out_time IS NULL) ORDER BY in_time",
            (e["id"], f"{t.isoformat()} 00:00:00")).fetchall()
        views = [session_view(r, now_dt) for r in rows]
        today_secs = sum(v["secs"] for v in views if v["in_time"][:10] == t.isoformat())
        is_in = any(v["open"] and not v["incomplete"] for v in views)
        has_incomplete = any(v["incomplete"] for v in views)
        worked_today = any(v["in_time"][:10] == t.isoformat() for v in views)
        today_views = [v for v in views if v["in_time"][:10] == t.isoformat()]
        first_in = min((v["in_time"] for v in today_views), default=None)
        cur = next((v for v in views if v["open"] and not v["incomplete"]), None)
        if not e["password_hash"]:
            status = "pending"
            pending += 1
        elif is_in:
            status = "in"
            inside += 1
        elif worked_today:
            status = "out"
            out_n += 1
        else:
            status = "absent"
            absent += 1
        cards.append({"e": e, "status": status, "today_secs": today_secs, "incomplete": has_incomplete,
                      "first_in": first_in, "cur_in": cur["in_time"] if cur else None})
    present = inside + out_n
    active = len(emps) - pending
    return render_template("contractor_dashboard.html", cards=cards, inside=inside, out_n=out_n,
                           present=present, active=active,
                           absent=absent, pending=pending, total=len(emps), today=t)


@app.route("/contractor/employee/add", methods=["GET", "POST"])
@app.route("/contractor/employee/<int:emp_id>/edit", methods=["GET", "POST"])
@login_required("contractor")
def employee_form(emp_id=None):
    db = get_db()
    emp = own_employee_or_404(db, emp_id) if emp_id else None
    form = {"name": emp["name"] if emp else "", "username": emp["username"] if emp else "",
            "designation": (emp["designation"] or "") if emp else "",
            "daily_wage": ("" if not emp or emp["daily_wage"] is None else f"{emp['daily_wage']:g}"),
            "ot_rate": ("" if not emp or emp["ot_rate"] is None else f"{emp['ot_rate']:g}")}
    if request.method == "POST":
        form["name"] = request.form.get("name", "").strip()
        form["username"] = norm_username(request.form.get("username"))
        form["designation"] = request.form.get("designation", "").strip()
        form["daily_wage"] = norm(request.form.get("daily_wage"))
        form["ot_rate"] = norm(request.form.get("ot_rate"))
        wage, wage_bad = parse_wage(form["daily_wage"])
        ot_rate, ot_bad = parse_wage(form["ot_rate"])
        if not form["name"]:
            flash("कृपया कामगाराचे नाव लिहा.", "error")
        elif not valid_username(form["username"]):
            flash("युजरनेम ३ ते ३० अक्षरांचे असावे (इंग्रजी अक्षरे, अंक, _ . - चालतील; जागा नको).", "error")
        elif username_taken(db, form["username"], emp["id"] if emp else None):
            flash("हे युजरनेम आधीच वापरात आहे. कृपया दुसरे निवडा.", "error")
        elif wage_bad:
            flash("रोजंदारी योग्य रकमेत लिहा (उदा. 650 किंवा 650.50).", "error")
        elif ot_bad:
            flash("ओव्हरटाइम दर योग्य रकमेत लिहा (उदा. 100) किंवा रिकामा ठेवा.", "error")
        elif emp:
            db.execute("UPDATE users SET name = ?, username = ?, designation = ?, daily_wage = ?, ot_rate = ? WHERE id = ?",
                       (form["name"], form["username"], form["designation"] or None, wage, ot_rate, emp["id"]))
            db.commit()
            flash("कामगाराची माहिती बदलली.", "success")
            return redirect(url_for("contractor_home"))
        else:
            code = new_code(db)
            db.execute(
                "INSERT INTO users (role, name, username, designation, daily_wage, ot_rate, signup_code, contractor_id, created_at) "
                "VALUES ('employee', ?, ?, ?, ?, ?, ?, ?, ?)",
                (form["name"], form["username"], form["designation"] or None, wage, ot_rate, code, g.user["id"], ts(now())))
            db.commit()
            flash(f"{form['name']} यांना जोडले गेले. पहिल्या लॉगिनचा कोड: {code} — हा कोड कामगाराला द्या.", "success")
            return redirect(url_for("contractor_home"))
    return render_template("employee_form.html", form=form, emp=emp)


@app.route("/contractor/employee/<int:emp_id>/delete", methods=["POST"])
@login_required("contractor")
def employee_delete(emp_id):
    db = get_db()
    emp = own_employee_or_404(db, emp_id)
    db.execute("DELETE FROM attendance WHERE user_id = ?", (emp["id"],))
    db.execute("DELETE FROM users WHERE id = ?", (emp["id"],))
    db.commit()
    flash(f"{emp['name']} यांना हटवले गेले.", "success")
    return redirect(url_for("contractor_home"))


@app.route("/contractor/employee/<int:emp_id>/reset", methods=["POST"])
@login_required("contractor")
def employee_reset(emp_id):
    """पासवर्ड विसरल्यास: नवीन कोड तयार होतो व कामगार कोडने पुन्हा पासवर्ड सेट करतो."""
    db = get_db()
    emp = own_employee_or_404(db, emp_id)
    code = new_code(db)
    db.execute("UPDATE users SET password_hash = NULL, signup_code = ?, code_attempts = 0 WHERE id = ?",
               (code, emp["id"]))
    db.commit()
    flash(f"{emp['name']} यांचा नवीन कोड: {code} — कामगाराने 'पहिले लॉगिन (कोडने)' वापरून नवीन पासवर्ड सेट करावा.", "success")
    return redirect(url_for("contractor_home"))


@app.route("/contractor/employee/<int:emp_id>")
@login_required("contractor")
def employee_detail(emp_id):
    db = get_db()
    emp = own_employee_or_404(db, emp_id)
    start, end = get_range()
    summ = summarize(db, emp, start, end, now())
    days = summ["days"]
    return render_template("employee_detail.html", emp=emp, days=days, start=start, end=end, s=summ,
                           total_secs=summ["secs"], total_pay=summ["gross"],
                           pending_days=summ["pending"])


def _parse_form_dt(name):
    raw = request.form.get(name, "").strip()
    if not raw:
        return None
    try:
        return datetime.strptime(raw, "%Y-%m-%dT%H:%M")
    except ValueError:
        return False


@app.route("/contractor/employee/<int:emp_id>/entry", methods=["GET", "POST"])
@app.route("/contractor/employee/<int:emp_id>/entry/<int:entry_id>", methods=["GET", "POST"])
@login_required("contractor")
def entry_form(emp_id, entry_id=None):
    """हजेरीची नोंद जोडणे/दुरुस्त करणे (उदा. बाहेर जाण्याची वेळ नोंदवायची राहिली असल्यास)."""
    db = get_db()
    emp = own_employee_or_404(db, emp_id)
    entry = None
    if entry_id:
        entry = db.execute("SELECT * FROM attendance WHERE id = ? AND user_id = ?", (entry_id, emp["id"])).fetchone()
        if entry is None:
            abort(404)
    if request.method == "POST":
        in_dt, out_dt = _parse_form_dt("in_time"), _parse_form_dt("out_time")
        if not in_dt or out_dt is False:
            flash("कृपया योग्य तारीख व वेळ निवडा.", "error")
        elif out_dt and out_dt <= in_dt:
            flash("बाहेर जाण्याची वेळ ही आत येण्याच्या वेळेनंतरची असावी.", "error")
        elif in_dt > now() + timedelta(minutes=5) or (out_dt and out_dt > now() + timedelta(minutes=5)):
            flash("भविष्यातील वेळ नोंदवता येत नाही.", "error")
        else:
            if entry:
                db.execute("UPDATE attendance SET in_time = ?, out_time = ? WHERE id = ?",
                           (ts(in_dt), ts(out_dt) if out_dt else None, entry["id"]))
            else:
                db.execute("INSERT INTO attendance (user_id, in_time, out_time) VALUES (?, ?, ?)",
                           (emp["id"], ts(in_dt), ts(out_dt) if out_dt else None))
            db.commit()
            flash("हजेरीची नोंद जतन केली.", "success")
            return redirect(url_for("employee_detail", emp_id=emp["id"]))
    return render_template("entry_form.html", emp=emp, entry=entry,
                           max_dt=now().strftime("%Y-%m-%dT%H:%M"))


@app.route("/contractor/employee/<int:emp_id>/entry/<int:entry_id>/delete", methods=["POST"])
@login_required("contractor")
def entry_delete(emp_id, entry_id):
    db = get_db()
    emp = own_employee_or_404(db, emp_id)
    db.execute("DELETE FROM attendance WHERE id = ? AND user_id = ?", (entry_id, emp["id"]))
    db.commit()
    flash("नोंद हटवली.", "success")
    return redirect(url_for("employee_detail", emp_id=emp["id"]))


@app.route("/contractor/employee/<int:emp_id>/pay", methods=["POST"])
@login_required("contractor")
def pay_set(emp_id):
    """८ तासांपेक्षा कमी काम केलेल्या दिवसाची मजुरी कंत्राटदार ठरवतो."""
    db = get_db()
    emp = own_employee_or_404(db, emp_id)
    work_date = request.form.get("date", "")
    back = url_for("employee_detail", emp_id=emp["id"], **{
        "from": request.form.get("from", ""), "to": request.form.get("to", "")})
    try:
        d = datetime.strptime(work_date, "%Y-%m-%d").date()
    except ValueError:
        abort(400)
    day = next((x for x in build_days(fetch_sessions(db, emp["id"], d, d + timedelta(days=1)), now(),
                                      emp["daily_wage"]) if x["date"] == work_date), None)
    if day is None:
        abort(404)
    if get_payment(db, emp["id"], work_date[:7]):
        flash("या महिन्याचा पगार दिला आहे. मजुरी बदलण्यासाठी आधी 'पगार दिला' रद्द करा.", "error")
        return redirect(back)
    if request.form.get("action") == "clear":
        db.execute("DELETE FROM pay_overrides WHERE user_id = ? AND work_date = ?", (emp["id"], work_date))
        db.commit()
        flash("या दिवसाची मजुरी पुन्हा आपोआप (प्रमाणात) मोजली जाईल.", "success")
        return redirect(back)
    if not day["decidable"]:
        flash("या दिवसाची मजुरी आत्ता ठरवता येत नाही — दिवस पूर्ण असावा व सर्व बाहेरच्या वेळा नोंदलेल्या असाव्यात.", "error")
        return redirect(back)
    amount, bad = parse_wage(request.form.get("amount"))
    if bad or amount is None:
        flash("मजुरी योग्य रकमेत लिहा (उदा. 300 किंवा 0).", "error")
    elif emp["daily_wage"] and amount > emp["daily_wage"]:
        flash(f"कमी तासांच्या दिवसाची मजुरी पूर्ण रोजंदारीपेक्षा ({f_mrs(emp['daily_wage'])}) जास्त असू शकत नाही.", "error")
    else:
        if USE_PG:
            db.execute("INSERT INTO pay_overrides (user_id, work_date, amount) VALUES (?, ?, ?) "
                       "ON CONFLICT (user_id, work_date) DO UPDATE SET amount = EXCLUDED.amount",
                       (emp["id"], work_date, amount))
        else:
            db.execute("INSERT OR REPLACE INTO pay_overrides (user_id, work_date, amount) VALUES (?, ?, ?)",
                       (emp["id"], work_date, amount))
        db.commit()
        flash(f"{f_mdate(work_date)} ची मजुरी {f_mrs(amount)} ठरवली.", "success")
    return redirect(back)


# --------------------------------------------------------------------------
# हिशोब: उचल, पगार दिला, पगार पावती (PDF)
# --------------------------------------------------------------------------
FONT_REG = os.path.join(BASE_DIR, "static", "fonts", "Mukta-Regular.ttf")
FONT_BOLD = os.path.join(BASE_DIR, "static", "fonts", "Mukta-Bold.ttf")


def _salary_back(emp_id, month_key):
    return redirect(url_for("salary_page", emp_id=emp_id, month=month_key))


@app.route("/contractor/employee/<int:emp_id>/salary")
@login_required("contractor")
def salary_page(emp_id):
    db = get_db()
    emp = own_employee_or_404(db, emp_id)
    t = today()
    cur_first = t.replace(day=1)
    first = parse_month(request.args.get("month"), cur_first)
    if first > cur_first:
        first = cur_first
    summ = month_summary(db, emp, first, now())
    pay = summ["payment"]
    mismatch = bool(pay) and (abs(pay["base"] + pay["ot_pay"] - summ["gross"]) > 0.01
                              or abs(pay["adv"] - summ["adv_total"]) > 0.01)
    nxt = next_month_first(first)
    return render_template(
        "salary.html", emp=emp, s=summ, first=first, month_key=first.strftime("%Y-%m"),
        prev_month=(first - timedelta(days=1)).strftime("%Y-%m"),
        next_month=nxt.strftime("%Y-%m") if nxt <= cur_first else None,
        paid=pay, mismatch=mismatch, month_ended=(nxt <= t), today=t, last=nxt - timedelta(days=1))


@app.route("/contractor/employee/<int:emp_id>/advance", methods=["POST"])
@login_required("contractor")
def advance_add(emp_id):
    db = get_db()
    emp = own_employee_or_404(db, emp_id)
    t = today()
    adv_date = parse_date(request.form.get("date"), t)
    month_key = adv_date.strftime("%Y-%m")
    amount, bad = parse_wage(request.form.get("amount"))
    note = request.form.get("note", "").strip()[:80]
    if bad or amount is None or amount <= 0:
        flash("उचलीची रक्कम योग्य लिहा (० पेक्षा जास्त, उदा. 500).", "error")
    elif adv_date > t:
        flash("उचलीची तारीख उद्याची किंवा पुढची असू शकत नाही.", "error")
    elif get_payment(db, emp["id"], month_key):
        flash("त्या महिन्याचा पगार दिला आहे. उचल जोडण्यासाठी आधी 'पगार दिला' रद्द करा.", "error")
    else:
        db.execute("INSERT INTO advances (user_id, adv_date, amount, note, created_at) VALUES (?, ?, ?, ?, ?)",
                   (emp["id"], adv_date.isoformat(), amount, note or None, ts(now())))
        db.commit()
        flash(f"{emp['name']} यांना {f_mrs(amount)} उचल नोंदवली ({f_mdate(adv_date, False)}).", "success")
    return _salary_back(emp["id"], month_key)


@app.route("/contractor/employee/<int:emp_id>/advance/<int:adv_id>/delete", methods=["POST"])
@login_required("contractor")
def advance_delete(emp_id, adv_id):
    db = get_db()
    emp = own_employee_or_404(db, emp_id)
    adv = db.execute("SELECT * FROM advances WHERE id = ? AND user_id = ?", (adv_id, emp["id"])).fetchone()
    if adv is None:
        abort(404)
    month_key = adv["adv_date"][:7]
    if get_payment(db, emp["id"], month_key):
        flash("त्या महिन्याचा पगार दिला आहे. उचल हटवण्यासाठी आधी 'पगार दिला' रद्द करा.", "error")
    else:
        db.execute("DELETE FROM advances WHERE id = ?", (adv["id"],))
        db.commit()
        flash("उचलीची नोंद हटवली.", "success")
    return _salary_back(emp["id"], month_key)


@app.route("/contractor/employee/<int:emp_id>/salary/pay", methods=["POST"])
@login_required("contractor")
def salary_pay(emp_id):
    """'पगार दिला': त्या क्षणाचा हिशोब जतन होतो (पावतीसाठी) व उचल/मजुरीत पुढे बदल थांबतो."""
    db = get_db()
    emp = own_employee_or_404(db, emp_id)
    t = today()
    first = parse_month(request.form.get("month"))
    if first is None or first > t.replace(day=1):
        abort(400)
    key = first.strftime("%Y-%m")
    summ = month_summary(db, emp, first, now())
    if summ["payment"]:
        flash("या महिन्याचा पगार आधीच 'दिला' म्हणून नोंदवला आहे.", "error")
    elif summ["incomplete"]:
        flash("काही दिवसांची बाहेरची वेळ नोंदलेली नाही. आधी ती दुरुस्त करा, मग पगार नोंदवा.", "error")
    elif summ["running"]:
        flash("कामगार सध्या कामावर (आत) आहे. तो बाहेर पडल्यावर पगार नोंदवा, म्हणजे आजचे तास अचूक धरले जातील.", "error")
    elif not summ["days"] and not summ["adv_total"] and not summ["carry_in"]:
        flash("या महिन्यात हजेरी किंवा उचल नाही, म्हणून पगार नोंदवता येत नाही.", "error")
    else:
        paid_on = min(parse_date(request.form.get("paid_on"), t), t)
        try:
            db.execute(
                "INSERT INTO salary_payments (user_id, month, paid_on, days, secs, daily_wage, ot_rate, base, ot_secs, "
                "ot_pay, adv, carry_in, net, carry_out, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (emp["id"], key, paid_on.isoformat(), len(summ["days"]), summ["secs"], emp["daily_wage"], emp["ot_rate"],
                 summ["base"], summ["ot_secs"], summ["ot"], summ["adv_total"], summ["carry_in"], summ["net"],
                 summ["carry_out"], ts(now())))
            db.commit()
            flash(f"{emp['name']} यांचा {f_mmonth(first)} चा पगार {f_mrs(summ['net'])} 'दिला' म्हणून नोंदवला.", "success")
        except INTEGRITY:
            flash("या महिन्याचा पगार आधीच नोंदवला आहे.", "error")
    return _salary_back(emp["id"], key)


@app.route("/contractor/employee/<int:emp_id>/salary/unpay", methods=["POST"])
@login_required("contractor")
def salary_unpay(emp_id):
    db = get_db()
    emp = own_employee_or_404(db, emp_id)
    first = parse_month(request.form.get("month"))
    if first is None:
        abort(400)
    key = first.strftime("%Y-%m")
    if get_payment(db, emp["id"], next_month_first(first).strftime("%Y-%m")):
        flash("पुढील महिन्याचा पगार दिला आहे. आधी तो रद्द करा.", "error")
    else:
        db.execute("DELETE FROM salary_payments WHERE user_id = ? AND month = ?", (emp["id"], key))
        db.commit()
        flash("'पगार दिला' ची नोंद रद्द केली. आता उचल/मजुरी पुन्हा बदलता येईल.", "success")
    return _salary_back(emp["id"], key)


def build_slip_pdf(emp, contractor_name, pay, advs, first):
    from fpdf import FPDF  # फक्त पावती बनवताना लागते

    pdf = FPDF(format=(148, 210), unit="mm")  # A5
    pdf.set_auto_page_break(True, 12)
    pdf.set_margins(10, 10, 10)
    pdf.set_text_shaping(True)  # देवनागरी जोडाक्षरे/मात्रा नीट येण्यासाठी
    pdf.add_font("Mukta", "", FONT_REG)
    pdf.add_font("Mukta", "B", FONT_BOLD)
    pdf.add_page()
    W = pdf.w - 20
    rs = f_mrs

    pdf.set_fill_color(20, 24, 28)
    pdf.rect(0, 0, pdf.w, 26, "F")
    pdf.set_fill_color(255, 196, 0)
    pdf.rect(0, 26, pdf.w, 2.5, "F")
    pdf.set_xy(10, 4.5)
    pdf.set_text_color(255, 255, 255)
    pdf.set_font("Mukta", "B", 17)
    pdf.cell(W, 9, SHOP_NAME)
    pdf.set_xy(10, 14)
    pdf.set_font("Mukta", "", 12.5)
    pdf.set_text_color(255, 196, 0)
    pdf.cell(W, 7, f"पगार पावती — {f_mmonth(first)}")
    pdf.set_text_color(20, 24, 28)
    pdf.set_y(33)

    def info(label, value):
        pdf.set_font("Mukta", "", 10.5)
        pdf.set_text_color(93, 106, 117)
        pdf.cell(32, 6, label)
        pdf.set_font("Mukta", "B", 11.5)
        pdf.set_text_color(20, 24, 28)
        pdf.cell(W - 32, 6, value, new_x="LMARGIN", new_y="NEXT")

    info("कामगार", emp["name"])
    if emp["designation"]:
        info("पदनाम", emp["designation"])
    info("कंत्राटदार", contractor_name)
    if pay["daily_wage"]:
        info("रोजंदारी", f"{rs(pay['daily_wage'])} / दिवस")
    if pay["ot_rate"]:
        info("ओव्हरटाइम दर", f"{rs(pay['ot_rate'])} / तास")
    pdf.ln(2)
    pdf.set_draw_color(200, 205, 210)

    def row(label, value="", bold=False, fill=None, size=11.5, color=None):
        pdf.set_font("Mukta", "B" if bold else "", size)
        pdf.set_text_color(*(color or (20, 24, 28)))
        if fill:
            pdf.set_fill_color(*fill)
        pdf.cell(W * 0.62, 7.6, label, border="B", fill=bool(fill))
        pdf.cell(W * 0.38, 7.6, value, border="B", align="R", new_x="LMARGIN", new_y="NEXT", fill=bool(fill))

    row("हजेरीचे दिवस", dn(str(pay["days"])))
    row("एकूण काम", f_mdur(pay["secs"]))
    row("मजुरी (रोजंदारीनुसार)", rs(pay["base"]))
    if pay["ot_secs"]:
        label = f"ओव्हरटाइम ({f_mdur(pay['ot_secs'])})"
        row(label if pay["ot_rate"] else label + " — दर नाही", rs(pay["ot_pay"]))
    gross = round(pay["base"] + pay["ot_pay"], 2)
    row("एकूण मजुरी", rs(gross), bold=True, fill=(244, 246, 247))
    if advs:
        row("वजा: उचल", "", bold=True)
        for a in advs:
            txt = f_mdate(a["adv_date"], False) + (f" · {a['note'][:28]}" if a["note"] else "")
            row("    " + txt, "− " + rs(a["amount"]).replace("₹ ", "₹"), size=10.5)
    if pay["carry_in"]:
        row("वजा: मागील महिन्याची उचल थकबाकी", "− " + rs(pay["carry_in"]).replace("₹ ", "₹"))
    pdf.ln(1.5)
    row("देय रक्कम (दिली)", rs(pay["net"]), bold=True, fill=(255, 196, 0), size=14)
    pdf.set_font("Mukta", "", 10.5)
    pdf.set_text_color(93, 106, 117)
    pdf.cell(W, 6.5, f"पगार दिल्याची तारीख: {f_mdate(pay['paid_on'], False)}", new_x="LMARGIN", new_y="NEXT")
    if pay["carry_out"]:
        pdf.set_text_color(138, 90, 0)
        pdf.multi_cell(W, 5.5, f"न फिटलेली उचल {rs(pay['carry_out'])} पुढील महिन्याच्या पगारातून वजा होईल.",
                       new_x="LMARGIN", new_y="NEXT")

    pdf.ln(16)
    pdf.set_draw_color(20, 24, 28)
    y = pdf.get_y()
    pdf.line(10, y, 10 + W * 0.42, y)
    pdf.line(10 + W * 0.58, y, 10 + W, y)
    pdf.set_font("Mukta", "", 10)
    pdf.set_text_color(93, 106, 117)
    pdf.cell(W * 0.58, 6, "कामगाराची सही")
    pdf.cell(W * 0.42, 6, "कंत्राटदाराची सही", new_x="LMARGIN", new_y="NEXT")
    pdf.ln(3)
    pdf.set_font("Mukta", "", 8.5)
    pdf.cell(W, 5, f"ही पावती {SHOP_NAME} द्वारे तयार केली आहे.", align="C")
    return bytes(pdf.output())


@app.route("/slip/<int:emp_id>/<month>.pdf")
@login_required()
def slip(emp_id, month):
    """पगार पावती: कंत्राटदार (स्वतःच्या कामगाराची) किंवा कामगार (स्वतःची) — फक्त पगार 'दिला' नंतर."""
    db = get_db()
    if g.user["role"] == "contractor":
        emp = own_employee_or_404(db, emp_id)
    elif g.user["role"] == "employee" and g.user["id"] == emp_id:
        emp = g.user
    else:
        abort(404)
    first = parse_month(month)
    if first is None:
        abort(404)
    pay = get_payment(db, emp["id"], first.strftime("%Y-%m"))
    if pay is None:
        abort(404)
    advs = fetch_advances(db, emp["id"], first, next_month_first(first))
    c = db.execute("SELECT name FROM users WHERE id = ?", (emp["contractor_id"],)).fetchone()
    data = build_slip_pdf(emp, c["name"] if c else "", pay, advs, first)
    resp = Response(data, mimetype="application/pdf")
    resp.headers["Content-Disposition"] = f'inline; filename="salary_{emp["username"]}_{first.strftime("%Y-%m")}.pdf"'
    resp.headers["Cache-Control"] = "private, no-store"
    return resp


@app.route("/contractor/report")
@login_required("contractor")
def report():
    db = get_db()
    start, end = get_range()
    now_dt = now()
    emps = db.execute("SELECT * FROM users WHERE role = 'employee' AND contractor_id = ? ORDER BY name",
                      (g.user["id"],)).fetchall()
    single_month = start.year == end.year and start.month == end.month
    rows = []
    tot = {"secs": 0, "base": 0.0, "ot": 0.0, "adv": 0.0, "balance": 0.0}
    for e in emps:
        sm = summarize(db, e, start, end, now_dt)
        pay = get_payment(db, e["id"], start.strftime("%Y-%m")) if single_month else None
        tot["secs"] += sm["secs"]; tot["base"] += sm["base"]; tot["ot"] += sm["ot"]
        tot["adv"] += sm["adv_total"]; tot["balance"] += sm["balance"]
        rows.append({"e": e, "days": len(sm["days"]), "secs": sm["secs"], "pay": sm["base"], "ot": sm["ot"],
                     "ot_secs": sm["ot_secs"], "adv": sm["adv_total"], "balance": sm["balance"],
                     "incomplete": sm["incomplete"], "pending": sm["pending"], "custom": sm["custom"],
                     "paid": pay})
    tot = {k: (round(v, 2) if k != "secs" else v) for k, v in tot.items()}
    grand_secs, grand_pay = tot["secs"], tot["base"]
    if request.args.get("csv") == "1":
        buf = io.StringIO()
        w = csv.writer(buf)
        w.writerow(["नाव", "युजरनेम", "पदनाम", "हजेरीचे दिवस", "एकूण वेळ", "एकूण तास (दशांश)",
                    "रोजंदारी (₹)", "मजुरी (₹)", "ओव्हरटाइम तास", "ओव्हरटाइम (₹)", "उचल (₹)",
                    "देय = मजुरी + OT − उचल (₹)", "कमी तासांचे दिवस (मजुरी ठरवली)", "मजुरी ठरवायची बाकी",
                    "पगार दिला (एका महिन्याचा अहवाल असल्यास)"])
        for r in rows:
            w.writerow([r["e"]["name"], r["e"]["username"], r["e"]["designation"] or "",
                        r["days"], f_mdur(r["secs"]), round(r["secs"] / 3600, 2),
                        "" if r["e"]["daily_wage"] is None else r["e"]["daily_wage"],
                        r["pay"], round(r["ot_secs"] / 3600, 2), r["ot"], r["adv"], r["balance"],
                        r["custom"], r["pending"],
                        ("होय" if r["paid"] else "नाही") if single_month else ""])
        data = "﻿" + buf.getvalue()  # Excel मध्ये मराठी नीट दिसण्यासाठी BOM
        return Response(data, mimetype="text/csv; charset=utf-8", headers={
            "Content-Disposition": f"attachment; filename=hajeri_{start.isoformat()}_{end.isoformat()}.csv"})
    return render_template("report.html", rows=rows, start=start, end=end, tot=tot,
                           single_month=single_month, grand_secs=grand_secs, grand_pay=grand_pay)


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 5000)), debug=os.environ.get("FLASK_DEBUG") == "1")
