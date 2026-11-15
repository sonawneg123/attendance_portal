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
"""

SCHEMA_PG = """
CREATE TABLE IF NOT EXISTS users (
    id BIGSERIAL PRIMARY KEY,
    role TEXT NOT NULL CHECK (role IN ('admin', 'contractor', 'employee')),
    name TEXT NOT NULL,
    username TEXT NOT NULL UNIQUE,
    designation TEXT,
    daily_wage DOUBLE PRECISION,
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
        return DB(psycopg.connect(DATABASE_URL, row_factory=dict_row, connect_timeout=10))
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
    s = f"{value:,.2f}".rstrip("0").rstrip(".")
    return dn(f"₹ {s}")


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
    return round(wage * secs / full, 2)


def fetch_overrides(db, user_id, start, end_excl):
    rows = db.execute(
        "SELECT work_date, amount FROM pay_overrides WHERE user_id = ? AND work_date >= ? AND work_date < ?",
        (user_id, start.isoformat(), end_excl.isoformat())).fetchall()
    return {r["work_date"]: r["amount"] for r in rows}


def build_days(rows, now_dt, wage=None, overrides=None):
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
    return sorted(days.values(), key=lambda x: x["date"], reverse=True)


def total_pay(days):
    vals = [d["pay"] for d in days if d["pay"] is not None]
    return round(sum(vals), 2) if vals else None


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

    days = build_days(fetch_sessions(db, g.user["id"], first, next_first), now_dt, wage,
                      fetch_overrides(db, g.user["id"], first, next_first))
    month_secs = sum(d["secs"] for d in days)

    open_row = db.execute("SELECT * FROM attendance WHERE user_id = ? AND out_time IS NULL ORDER BY in_time DESC LIMIT 1",
                          (g.user["id"],)).fetchone()
    current = session_view(open_row, now_dt) if open_row else None
    today_rows = fetch_sessions(db, g.user["id"], t, t + timedelta(days=1))
    today_secs = sum(session_view(r, now_dt)["secs"] for r in today_rows)
    return render_template(
        "employee_dashboard.html", days=days, month_secs=month_secs, month_pay=total_pay(days),
        wage=wage, current=current, today_secs=today_secs, month=first,
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
    cards, inside, absent, pending = [], 0, 0, 0
    for e in emps:
        rows = db.execute(
            "SELECT * FROM attendance WHERE user_id = ? AND (in_time >= ? OR out_time IS NULL) ORDER BY in_time",
            (e["id"], f"{t.isoformat()} 00:00:00")).fetchall()
        views = [session_view(r, now_dt) for r in rows]
        today_secs = sum(v["secs"] for v in views if v["in_time"][:10] == t.isoformat())
        is_in = any(v["open"] and not v["incomplete"] for v in views)
        has_incomplete = any(v["incomplete"] for v in views)
        worked_today = any(v["in_time"][:10] == t.isoformat() for v in views)
        if not e["password_hash"]:
            status = "pending"
            pending += 1
        elif is_in:
            status = "in"
            inside += 1
        elif worked_today:
            status = "out"
        else:
            status = "absent"
            absent += 1
        cards.append({"e": e, "status": status, "today_secs": today_secs, "incomplete": has_incomplete})
    return render_template("contractor_dashboard.html", cards=cards, inside=inside,
                           absent=absent, pending=pending, total=len(emps), today=t)


@app.route("/contractor/employee/add", methods=["GET", "POST"])
@app.route("/contractor/employee/<int:emp_id>/edit", methods=["GET", "POST"])
@login_required("contractor")
def employee_form(emp_id=None):
    db = get_db()
    emp = own_employee_or_404(db, emp_id) if emp_id else None
    form = {"name": emp["name"] if emp else "", "username": emp["username"] if emp else "",
            "designation": (emp["designation"] or "") if emp else "",
            "daily_wage": ("" if not emp or emp["daily_wage"] is None else f"{emp['daily_wage']:g}")}
    if request.method == "POST":
        form["name"] = request.form.get("name", "").strip()
        form["username"] = norm_username(request.form.get("username"))
        form["designation"] = request.form.get("designation", "").strip()
        form["daily_wage"] = norm(request.form.get("daily_wage"))
        wage, wage_bad = parse_wage(form["daily_wage"])
        if not form["name"]:
            flash("कृपया कामगाराचे नाव लिहा.", "error")
        elif not valid_username(form["username"]):
            flash("युजरनेम ३ ते ३० अक्षरांचे असावे (इंग्रजी अक्षरे, अंक, _ . - चालतील; जागा नको).", "error")
        elif username_taken(db, form["username"], emp["id"] if emp else None):
            flash("हे युजरनेम आधीच वापरात आहे. कृपया दुसरे निवडा.", "error")
        elif wage_bad:
            flash("रोजंदारी योग्य रकमेत लिहा (उदा. 650 किंवा 650.50).", "error")
        elif emp:
            db.execute("UPDATE users SET name = ?, username = ?, designation = ?, daily_wage = ? WHERE id = ?",
                       (form["name"], form["username"], form["designation"] or None, wage, emp["id"]))
            db.commit()
            flash("कामगाराची माहिती बदलली.", "success")
            return redirect(url_for("contractor_home"))
        else:
            code = new_code(db)
            db.execute(
                "INSERT INTO users (role, name, username, designation, daily_wage, signup_code, contractor_id, created_at) "
                "VALUES ('employee', ?, ?, ?, ?, ?, ?, ?)",
                (form["name"], form["username"], form["designation"] or None, wage, code, g.user["id"], ts(now())))
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
    end_excl = end + timedelta(days=1)
    days = build_days(fetch_sessions(db, emp["id"], start, end_excl), now(), emp["daily_wage"],
                      fetch_overrides(db, emp["id"], start, end_excl))
    return render_template("employee_detail.html", emp=emp, days=days, start=start, end=end,
                           total_secs=sum(d["secs"] for d in days), total_pay=total_pay(days),
                           pending_days=sum(1 for d in days if d["needs_decision"]))


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


@app.route("/contractor/report")
@login_required("contractor")
def report():
    db = get_db()
    start, end = get_range()
    now_dt = now()
    emps = db.execute("SELECT * FROM users WHERE role = 'employee' AND contractor_id = ? ORDER BY name",
                      (g.user["id"],)).fetchall()
    rows, grand_secs, grand_pay = [], 0, 0.0
    for e in emps:
        end_excl = end + timedelta(days=1)
        days = build_days(fetch_sessions(db, e["id"], start, end_excl), now_dt, e["daily_wage"],
                          fetch_overrides(db, e["id"], start, end_excl))
        secs = sum(d["secs"] for d in days)
        pay = total_pay(days)
        grand_secs += secs
        grand_pay += pay or 0
        rows.append({"e": e, "days": len(days), "secs": secs, "pay": pay,
                     "incomplete": any(d["incomplete"] for d in days),
                     "pending": sum(1 for d in days if d["needs_decision"]),
                     "custom": sum(1 for d in days if d["override"] is not None)})
    grand_pay = round(grand_pay, 2)
    if request.args.get("csv") == "1":
        buf = io.StringIO()
        w = csv.writer(buf)
        w.writerow(["नाव", "युजरनेम", "पदनाम", "हजेरीचे दिवस", "एकूण वेळ", "एकूण तास (दशांश)",
                    "रोजंदारी (₹)", "एकूण मजुरी (₹)", "कमी तासांचे दिवस (मजुरी ठरवली)", "मजुरी ठरवायची बाकी"])
        for r in rows:
            w.writerow([r["e"]["name"], r["e"]["username"], r["e"]["designation"] or "",
                        r["days"], f_mdur(r["secs"]), round(r["secs"] / 3600, 2),
                        "" if r["e"]["daily_wage"] is None else r["e"]["daily_wage"],
                        "" if r["pay"] is None else r["pay"], r["custom"], r["pending"]])
        data = "﻿" + buf.getvalue()  # Excel मध्ये मराठी नीट दिसण्यासाठी BOM
        return Response(data, mimetype="text/csv; charset=utf-8", headers={
            "Content-Disposition": f"attachment; filename=hajeri_{start.isoformat()}_{end.isoformat()}.csv"})
    return render_template("report.html", rows=rows, start=start, end=end,
                           grand_secs=grand_secs, grand_pay=grand_pay)


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 5000)), debug=os.environ.get("FLASK_DEBUG") == "1")
