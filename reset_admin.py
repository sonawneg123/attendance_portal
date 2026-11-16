"""Admin चा पासवर्ड विसरल्यास सर्व्हरवर चालवा:  python reset_admin.py

Admin चा पासवर्ड दुसरा कोणीही रीसेट करू शकत नाही, म्हणून ही स्क्रिप्ट थेट डेटाबेसमध्ये नवीन पासवर्ड ठेवते.
DATABASE_URL (PostgreSQL) किंवा DATABASE_PATH (SQLite) वातावरण-चल असल्यास तोच डेटाबेस वापरला जातो.
Render वर: Dashboard → आपली वेब सर्व्हिस → Shell मध्ये हा आदेश चालवा (Shell फक्त पेड प्लॅनवर).
"""
import getpass

from werkzeug.security import generate_password_hash

from app import open_db  # app इम्पोर्ट केल्यावर टेबल तयार असल्याची खात्री होते

db = open_db()
admin = db.execute("SELECT id, username FROM users WHERE role = 'admin'").fetchone()
if admin is None:
    raise SystemExit("Admin खाते अजून तयार झालेले नाही. आधी ॲप चालवून /setup उघडा.")
print(f"Admin युजरनेम: {admin['username']}")
pw = getpass.getpass("नवीन पासवर्ड (किमान 4 अक्षरे): ")
if len(pw) < 4:
    raise SystemExit("पासवर्ड खूप लहान आहे.")
if pw != getpass.getpass("पासवर्ड पुन्हा लिहा: "):
    raise SystemExit("पासवर्ड जुळत नाहीत.")
db.execute("UPDATE users SET password_hash = ? WHERE id = ?", (generate_password_hash(pw), admin["id"]))
db.commit()
db.close()
print("Admin चा पासवर्ड बदलला.")
