# हजेरी पोर्टल (Attendance Portal) — Fabrication Shop

Marathi-only attendance + daily-wage web app. Flask + PostgreSQL (Render/Supabase) / SQLite (local). No build step.

## Roles

| Role | What they can do |
|---|---|
| **Admin** (exactly one) | Add, edit, delete contractors. Reset a contractor's password (issues a new code). **No attendance.** |
| **Contractor** | Add, edit, delete employees. Set daily wage and OT rate. View attendance, correct entries, **decide the pay for days worked less than 8 hours**, record advances, mark salary paid, receipts, reports + CSV. Today's attendance panel. **No attendance of their own.** |
| **Employee** | Punch in / out, see hours, pay, advances and amount due; download own salary receipts. |

## Pay rules

* `FULL_DAY_HOURS` (default 8) or more worked in a day → full daily wage.
* Less than that → the **contractor decides** the amount for that day (₹0 up to the full daily wage) on the
  employee's attendance page ("ठरवा"). Until decided, a pro-rata estimate (rounded to the nearest rupee) is used and
  the day is flagged "ठरवायची बाकी".
* **Overtime (OT):** set an *OT rate per hour* on the employee (edit page). Hours beyond `FULL_DAY_HOURS` in a day
  are paid at that rate (rounded to the nearest rupee). No rate set → no OT pay, and a warning is shown.
* **Advance (उचल):** the contractor records advances on the employee's "हिशोब / उचल" page.
* **Amount due (देय)** = wages + OT − advances in the month − unrecovered advance carried from the previous month.
  If advances exceed the wages, the shortfall is carried forward and deducted from next month's salary.
* **"पगार दिला":** saves a snapshot of the month's accounts (used for the receipt). It is blocked while a past punch
  has no out-time, or while the employee is currently punched in. After marking paid, that month's advances are
  locked (use "रद्द करा" to change); if attendance is edited later, a warning shows the snapshot is outdated.
  A month cannot be un-paid while the next month is marked paid.
* **Salary receipt (PDF):** Marathi A5 receipt (shaped with fpdf2 + uharfbuzz, Mukta font bundled in `static/fonts`),
  downloadable by the contractor and by the employee for their own paid months. "WhatsApp / शेअर" opens the phone's
  share sheet with the PDF attached (falls back to a download on computers).
* **Today panel** on the contractor's home page: how many are present, currently inside, out, absent, and who.

## Login model

Username + password. First time for a contractor / employee: the person above creates the account and gets a
6-character **code** → user opens "पहिले लॉगिन (कोडने)" → sets a password (mandatory) → code is erased.
Forgot password → "पासवर्ड रीसेट" issues a fresh code. Admin forgot password → `python reset_admin.py`.

## PWA (install as an app)

The portal is an installable PWA: Android/Chrome shows an "इन्स्टॉल करा" bar (or browser menu → *Install app*);
on iPhone use Safari → Share → *Add to Home Screen*. It needs HTTPS (Render provides it).
Only static files and a Marathi offline page are cached; logged-in pages and attendance are never cached or
queued offline, so punch times are always accurate. After changing `static/sw.js`, bump `VERSION` in it.

## Deploy on Render (Blueprint, recommended)

1. Push this folder to a GitHub repo (the folder containing `app.py` and `render.yaml` should be the repo root).
2. Render Dashboard → **New → Blueprint** → select the repo. It creates the web service **and** a PostgreSQL database.
3. Open the web service → **Environment** → copy the generated `SETUP_KEY`.
4. Open the site URL → `/setup` → enter `SETUP_KEY` and create the single Admin account.
5. Free plans: the web service sleeps when idle and the free Postgres expires after a while. For the client's real
   data use paid plans (`starter` web service + a paid Postgres); change `plan:` in `render.yaml`.

Manual setup instead of Blueprint: Web Service, build `pip install -r requirements.txt`,
start `gunicorn -w 2 --threads 4 -b 0.0.0.0:$PORT app:app`, health check `/healthz`, and set `DATABASE_URL`
(from a Render Postgres "Internal Database URL"), `SECRET_KEY`, `SETUP_KEY`, `COOKIE_SECURE=1`.

## Database: Supabase (free) — already configured in `render.yaml`

`render.yaml` creates only the Render web service; `DATABASE_URL` is entered manually. In Supabase → **Connect** → copy the **Session pooler** string (port 5432; the direct host is IPv6-only on the
free plan and Render cannot reach it) and paste it as `DATABASE_URL` in Render. URL-encode special characters in the password.

## Run locally (SQLite, no setup)

```bash
python -m venv venv && source venv/bin/activate      # Windows: venv\Scripts\activate
python -m pip install -r requirements.txt
python app.py
```

Open http://localhost:5000 → first run goes to `/setup`. Data is in `attendance.db` next to `app.py`.
To test against Postgres locally set `DATABASE_URL=postgresql://user:pass@host/dbname`.

## Environment variables

| Name | Purpose | Default |
|---|---|---|
| `DATABASE_URL` | PostgreSQL connection string; if set, Postgres is used instead of SQLite | *(unset → SQLite)* |
| `DATABASE_PATH` | SQLite file location (only when `DATABASE_URL` is unset) | `./attendance.db` |
| `SECRET_KEY` | Session signing key (**set it on Render**) | auto-generated `.secret_key` file |
| `SETUP_KEY` | If set, required on `/setup` to create the Admin | *(unset)* |
| `COOKIE_SECURE` | `1` to send cookies only over HTTPS | `0` |
| `SHOP_NAME` | Name shown in header / login page | `हजेरी पोर्टल` |
| `FULL_DAY_HOURS` | Hours that count as a full day | `8` |
| `DEVANAGARI_DIGITS` | `1` to show numbers as ०१२३ | `0` |

## Notes

* Fonts (Khand, Mukta) load from Google Fonts; system Devanagari fonts are the fallback.
* Date/time pickers use the browser's native widget, so their labels follow the phone/browser language.
* Login/code attempt limits are kept in memory per worker (fine for one shop).
* Security basics: password hashing, CSRF tokens on every POST, HttpOnly/SameSite cookies, ownership checks.
* A previous mobile-number version of the SQLite DB is renamed to `attendance.db.old` automatically.

* Upgrading: tables/columns for OT, advances and salary payments are created automatically at start-up; existing data is kept.
