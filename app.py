from flask import Flask, render_template, request, jsonify, session, redirect, url_for, abort, send_from_directory, Response
from flask_cors import CORS
import sqlite3, hashlib, os, json, requests, csv, io, random, secrets, re, urllib.parse, time
from datetime import datetime
from functools import wraps
from werkzeug.utils import secure_filename

# ─────────────────────────────────────────
# ENVIRONMENT CONFIGURATION
# ─────────────────────────────────────────

def load_env_file():
    """Load .env file into os.environ if not already present"""
    try:
        from dotenv import load_dotenv
        load_dotenv()
        return
    except Exception:
        pass
    env_file = os.path.join(os.path.dirname(os.path.abspath(__file__)), '.env')
    if os.path.exists(env_file):
        with open(env_file, 'r', encoding='utf-8') as f:
            for line in f:
                line = line.strip()
                if line and not line.startswith('#') and '=' in line:
                    k, v = line.split('=', 1)
                    k = k.strip()
                    v = v.strip().strip("'").strip('"')
                    if k not in os.environ:
                        os.environ[k] = v

load_env_file()

app = Flask(__name__)
app.secret_key = os.environ.get("SECRET_KEY", secrets.token_hex(32))
CORS(app)

@app.template_filter('format_date')
def format_date_filter(val):
    if not val:
        return ''
    if hasattr(val, 'strftime'):
        return val.strftime('%Y-%m-%d')
    return str(val)[:10]

DB_PATH = os.environ.get("SQLITE_PATH", "gbp.db")
DATABASE_URL = os.environ.get("DATABASE_URL", "").strip()
if DATABASE_URL.startswith("postgres://"):
    DATABASE_URL = DATABASE_URL.replace("postgres://", "postgresql://", 1)

GROQ_API_KEY = os.environ.get("GROQ_API_KEY", "")
GROQ_MODELS = [
    "llama-3.3-70b-versatile",
    "llama-3.1-8b-instant",
    "llama3-70b-8192",
    "llama3-8b-8192",
    "mixtral-8x7b-32768",
    "gemma2-9b-it"
]
ADMIN_PASSWORD = os.environ.get("ADMIN_PASSWORD", "admin123")

UPLOAD_FOLDER = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'static', 'uploads')
ALLOWED_EXTENSIONS = {'png', 'jpg', 'jpeg', 'gif', 'webp'}
ALLOWED_DATA_EXTENSIONS = {'csv', 'xlsx', 'xls'}

os.makedirs(UPLOAD_FOLDER, exist_ok=True)

def allowed_file(filename):
    return '.' in filename and filename.rsplit('.', 1)[1].lower() in ALLOWED_EXTENSIONS

# ─────────────────────────────────────────
# DUAL DATABASE ADAPTER (SQLite / PostgreSQL)
# ─────────────────────────────────────────

class PGCursorWrapper:
    def __init__(self, cur, last_id=None):
        self.cur = cur
        self.lastrowid = last_id

    def fetchone(self):
        try:
            return self.cur.fetchone()
        except Exception:
            return None

    def fetchall(self):
        try:
            return self.cur.fetchall()
        except Exception:
            return []

class PGConnectionWrapper:
    def __init__(self, conn):
        self.conn = conn

    def execute(self, sql, params=()):
        # Convert SQLite '?' to PostgreSQL '%s'
        pg_sql = sql.replace('?', '%s')
        cur = self.conn.cursor()
        
        is_insert = pg_sql.strip().upper().startswith("INSERT INTO")
        has_returning = "RETURNING" in pg_sql.upper()
        if is_insert and not has_returning and any(tbl in pg_sql for tbl in ["reviews", "products", "review_templates"]):
            pg_sql = pg_sql.rstrip('; ') + " RETURNING id;"
            cur.execute(pg_sql, params)
            last_id = None
            try:
                r = cur.fetchone()
                if r:
                    last_id = r.get('id') if isinstance(r, dict) else r[0]
            except Exception:
                pass
            return PGCursorWrapper(cur, last_id=last_id)
        else:
            cur.execute(pg_sql, params)
            return PGCursorWrapper(cur)

    def commit(self):
        self.conn.commit()

    def rollback(self):
        self.conn.rollback()

    def close(self):
        self.conn.close()

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        if exc_type:
            self.rollback()
        else:
            self.commit()
        self.close()

class SQLiteConnectionWrapper:
    def __init__(self, conn):
        self.conn = conn

    def execute(self, sql, params=()):
        cur = self.conn.cursor()
        cur.execute(sql, params)
        return cur

    def commit(self):
        self.conn.commit()

    def rollback(self):
        self.conn.rollback()

    def close(self):
        self.conn.close()

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        if exc_type:
            self.rollback()
        else:
            self.commit()
        self.close()

def get_db():
    if DATABASE_URL:
        try:
            import psycopg2
            from psycopg2.extras import RealDictCursor
            conn = psycopg2.connect(DATABASE_URL, cursor_factory=RealDictCursor)
            return PGConnectionWrapper(conn)
        except Exception as e:
            print(f"⚠️ PostgreSQL connection failed: {e}. Falling back to SQLite.")
    
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return SQLiteConnectionWrapper(conn)

def init_db():
    if DATABASE_URL:
        try:
            import psycopg2
            from psycopg2.extras import RealDictCursor
            conn = psycopg2.connect(DATABASE_URL, cursor_factory=RealDictCursor)
            with conn.cursor() as cur:
                cur.execute("""
                CREATE TABLE IF NOT EXISTS businesses (
                    id          TEXT PRIMARY KEY,
                    name        TEXT NOT NULL,
                    slug        TEXT UNIQUE NOT NULL,
                    tagline     TEXT,
                    logo_url    TEXT,
                    maps_url    TEXT NOT NULL,
                    category    TEXT,
                    description TEXT,
                    passcode    TEXT NOT NULL,
                    groq_key    TEXT,
                    auto_reply  INTEGER DEFAULT 1,
                    theme_color TEXT DEFAULT '#3A2CCB',
                    profile_pic TEXT,
                    website_url TEXT,
                    keywords    TEXT,
                    whatsapp_number TEXT,
                    created_at  TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                );

                CREATE TABLE IF NOT EXISTS products (
                    id          SERIAL PRIMARY KEY,
                    business_id TEXT NOT NULL,
                    name        TEXT NOT NULL,
                    description TEXT,
                    keywords    TEXT,
                    created_at  TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    FOREIGN KEY(business_id) REFERENCES businesses(id) ON DELETE CASCADE
                );

                CREATE TABLE IF NOT EXISTS review_templates (
                    id          SERIAL PRIMARY KEY,
                    business_id TEXT NOT NULL,
                    stars       INTEGER NOT NULL,
                    text        TEXT NOT NULL,
                    FOREIGN KEY(business_id) REFERENCES businesses(id)
                );

                CREATE TABLE IF NOT EXISTS reviews (
                    id          SERIAL PRIMARY KEY,
                    business_id TEXT NOT NULL,
                    reviewer    TEXT,
                    stars       INTEGER,
                    text        TEXT,
                    reply       TEXT,
                    replied_at  TEXT,
                    created_at  TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    FOREIGN KEY(business_id) REFERENCES businesses(id)
                );

                CREATE TABLE IF NOT EXISTS groq_keys (
                    id          SERIAL PRIMARY KEY,
                    api_key     TEXT NOT NULL,
                    label       TEXT,
                    is_active   INTEGER DEFAULT 1,
                    last_status TEXT DEFAULT 'untested',
                    last_checked TIMESTAMP,
                    latency_ms  INTEGER DEFAULT 0,
                    created_at  TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                );
                """)
                for col in ["profile_pic", "website_url", "keywords", "whatsapp_number"]:
                    try:
                        cur.execute(f"ALTER TABLE businesses ADD COLUMN IF NOT EXISTS {col} TEXT")
                    except Exception:
                        pass
                if GROQ_API_KEY:
                    try:
                        cur.execute("SELECT id FROM groq_keys LIMIT 1")
                        if not cur.fetchone():
                            cur.execute(
                                "INSERT INTO groq_keys (api_key, label, is_active, last_status) VALUES (%s, %s, 1, 'untested')",
                                (GROQ_API_KEY, "Environment Primary Key")
                            )
                    except Exception:
                        pass
                conn.commit()
            conn.close()
            print("✅ PostgreSQL Database initialised successfully")
            return
        except Exception as e:
            print(f"⚠️ Could not init PostgreSQL ({e}). Initialising SQLite fallback.")

    with sqlite3.connect(DB_PATH) as db:
        db.executescript("""
        CREATE TABLE IF NOT EXISTS businesses (
            id          TEXT PRIMARY KEY,
            name        TEXT NOT NULL,
            slug        TEXT UNIQUE NOT NULL,
            tagline     TEXT,
            logo_url    TEXT,
            maps_url    TEXT NOT NULL,
            category    TEXT,
            description TEXT,
            passcode    TEXT NOT NULL,
            groq_key    TEXT,
            auto_reply  INTEGER DEFAULT 1,
            theme_color TEXT DEFAULT '#3A2CCB',
            profile_pic TEXT,
            website_url TEXT,
            keywords    TEXT,
            whatsapp_number TEXT,
            created_at  TEXT DEFAULT (datetime('now'))
        );

        CREATE TABLE IF NOT EXISTS products (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            business_id TEXT NOT NULL,
            name        TEXT NOT NULL,
            description TEXT,
            keywords    TEXT,
            created_at  TEXT DEFAULT (datetime('now')),
            FOREIGN KEY(business_id) REFERENCES businesses(id) ON DELETE CASCADE
        );

        CREATE TABLE IF NOT EXISTS review_templates (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            business_id TEXT NOT NULL,
            stars       INTEGER NOT NULL,
            text        TEXT NOT NULL,
            FOREIGN KEY(business_id) REFERENCES businesses(id)
        );

        CREATE TABLE IF NOT EXISTS reviews (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            business_id TEXT NOT NULL,
            reviewer    TEXT,
            stars       INTEGER,
            text        TEXT,
            reply       TEXT,
            replied_at  TEXT,
            created_at  TEXT DEFAULT (datetime('now')),
            FOREIGN KEY(business_id) REFERENCES businesses(id)
        );

        CREATE TABLE IF NOT EXISTS groq_keys (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            api_key     TEXT NOT NULL,
            label       TEXT,
            is_active   INTEGER DEFAULT 1,
            last_status TEXT DEFAULT 'untested',
            last_checked TEXT,
            latency_ms  INTEGER DEFAULT 0,
            created_at  TEXT DEFAULT (datetime('now'))
        );
        """)
        for col_name in ["profile_pic", "website_url", "keywords", "whatsapp_number"]:
            try:
                db.execute(f"ALTER TABLE businesses ADD COLUMN {col_name} TEXT")
            except Exception:
                pass
        if GROQ_API_KEY:
            try:
                row = db.execute("SELECT id FROM groq_keys LIMIT 1").fetchone()
                if not row:
                    db.execute(
                        "INSERT INTO groq_keys (api_key, label, is_active, last_status) VALUES (?,?,?,?)",
                        (GROQ_API_KEY, "Environment Primary Key", 1, "untested")
                    )
            except Exception:
                pass
    print("✅ SQLite Database initialised with Products & Business enhancements")

init_db()

# ─────────────────────────────────────────
# EXCEL / CSV PARSER FOR PRODUCTS
# ─────────────────────────────────────────

def parse_products_file(file_storage):
    if not file_storage or not file_storage.filename:
        return []
    filename = file_storage.filename.lower()
    products = []
    
    if filename.endswith('.csv'):
        raw = file_storage.read()
        for enc in ('utf-8-sig', 'utf-8', 'latin-1'):
            try:
                content = raw.decode(enc)
                break
            except Exception:
                continue
        else:
            content = raw.decode('utf-8', errors='ignore')
            
        reader = csv.reader(io.StringIO(content))
        rows = [r for r in reader if any(field.strip() for field in r)]
        if not rows:
            return []
            
        header = [c.strip().lower() for c in rows[0]]
        name_idx, desc_idx, kw_idx = -1, -1, -1
        for i, h in enumerate(header):
            if h in ('name', 'product', 'product name', 'service', 'service name', 'title', 'item') and name_idx == -1:
                name_idx = i
            elif h in ('description', 'desc', 'details', 'about', 'info') and desc_idx == -1:
                desc_idx = i
            elif h in ('keywords', 'keyword', 'tags', 'tag', 'key words') and kw_idx == -1:
                kw_idx = i
                
        has_header = (name_idx != -1)
        data_rows = rows[1:] if has_header else rows
        
        for row in data_rows:
            if not row:
                continue
            name = (row[name_idx] if name_idx != -1 and name_idx < len(row) else (row[0] if len(row) > 0 else '')).strip()
            if not name:
                continue
            desc = (row[desc_idx] if desc_idx != -1 and desc_idx < len(row) else (row[1] if len(row) > 1 else '')).strip()
            kw = (row[kw_idx] if kw_idx != -1 and kw_idx < len(row) else (row[2] if len(row) > 2 else '')).strip()
            products.append({'name': name, 'description': desc, 'keywords': kw})
            
    elif filename.endswith(('.xlsx', '.xls')):
        try:
            import openpyxl
            wb = openpyxl.load_workbook(file_storage.stream, data_only=True)
            sheet = wb.active
            rows = []
            for row in sheet.iter_rows(values_only=True):
                if any(row):
                    rows.append([str(c or '').strip() for c in row])
            if not rows:
                return []
                
            header = [c.lower() for c in rows[0]]
            name_idx, desc_idx, kw_idx = -1, -1, -1
            for i, h in enumerate(header):
                if h in ('name', 'product', 'product name', 'service', 'service name', 'title', 'item') and name_idx == -1:
                    name_idx = i
                elif h in ('description', 'desc', 'details', 'about', 'info') and desc_idx == -1:
                    desc_idx = i
                elif h in ('keywords', 'keyword', 'tags', 'tag', 'key words') and kw_idx == -1:
                    kw_idx = i
                    
            has_header = (name_idx != -1)
            data_rows = rows[1:] if has_header else rows
            
            for row in data_rows:
                if not row:
                    continue
                name = (row[name_idx] if name_idx != -1 and name_idx < len(row) else (row[0] if len(row) > 0 else '')).strip()
                if not name:
                    continue
                desc = (row[desc_idx] if desc_idx != -1 and desc_idx < len(row) else (row[1] if len(row) > 1 else '')).strip()
                kw = (row[kw_idx] if kw_idx != -1 and kw_idx < len(row) else (row[2] if len(row) > 2 else '')).strip()
                products.append({'name': name, 'description': desc, 'keywords': kw})
        except Exception as e:
            print(f"Excel parse error: {e}")
            
    return products

# ─────────────────────────────────────────
# HELPERS
# ─────────────────────────────────────────

def hash_pass(p): return hashlib.sha256(p.encode()).hexdigest()

def slugify(text):
    return re.sub(r'[^a-z0-9-]', '', text.lower().replace(' ', '-'))

def business_required(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        if 'business_id' not in session:
            return redirect(url_for('login'))
        return f(*args, **kwargs)
    return decorated

def admin_required(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        if not session.get('is_admin'):
            return redirect(url_for('admin_login'))
        return f(*args, **kwargs)
    return decorated

def mask_groq_key(key):
    if not key:
        return ""
    key = str(key).strip()
    if len(key) <= 12:
        return key[:3] + "..." + key[-3:]
    return key[:7] + "..." + key[-4:]

def get_active_groq_keys(custom_key=None):
    """
    Returns an ordered list of active Groq API keys:
    1. Custom business key (if specified)
    2. Active keys configured in database (in order)
    3. GROQ_API_KEY from environment as fallback
    """
    keys = []
    if custom_key and str(custom_key).strip().startswith("gsk_"):
        keys.append(str(custom_key).strip())

    try:
        db = get_db()
        rows = db.execute("SELECT api_key FROM groq_keys WHERE is_active=1 ORDER BY id ASC").fetchall()
        for r in rows:
            k = r['api_key'] if isinstance(r, dict) or hasattr(r, '__getitem__') else r[0]
            k = str(k).strip()
            if k and k not in keys:
                keys.append(k)
    except Exception:
        pass

    if GROQ_API_KEY and GROQ_API_KEY.strip() not in keys:
        keys.append(GROQ_API_KEY.strip())

    return keys

def test_single_groq_key(api_key):
    """
    Pings Groq API to verify key validity, access, latency, and rate-limit status.
    First queries /models to discover live accessible models for this account,
    then executes a verification completion with the best matching active model.
    """
    api_key = str(api_key).strip()
    if not api_key:
        return {
            "ok": False,
            "status": "invalid_key",
            "message": "Key is empty",
            "latency_ms": 0
        }

    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json"
    }

    t0 = time.time()
    try:
        # Step 1: Query models list (verifies authentication and discovers available models for this specific key)
        m_res = requests.get(
            "https://api.groq.com/openai/v1/models",
            headers={"Authorization": f"Bearer {api_key}"},
            timeout=8
        )
        lat = int((time.time() - t0) * 1000)

        if m_res.status_code == 401:
            return {
                "ok": False,
                "status": "invalid_key",
                "status_code": 401,
                "latency_ms": lat,
                "message": "Invalid API Key (401 Unauthorized)"
            }
        elif m_res.status_code == 429:
            return {
                "ok": False,
                "status": "rate_limited",
                "status_code": 429,
                "latency_ms": lat,
                "message": "Rate Limited (429 Too Many Requests)"
            }
        elif m_res.status_code != 200:
            return {
                "ok": False,
                "status": "error",
                "status_code": m_res.status_code,
                "latency_ms": lat,
                "message": f"HTTP {m_res.status_code}: {m_res.text[:90]}"
            }

        # Auth succeeded! Find which chat models are enabled on this account
        models_data = m_res.json().get('data', [])
        available_ids = [m['id'] for m in models_data if 'whisper' not in m.get('id', '').lower() and 'guard' not in m.get('id', '').lower()]

        # Try models in priority order
        candidate_models = [m for m in GROQ_MODELS if m in available_ids]
        if not candidate_models:
            candidate_models = available_ids if available_ids else GROQ_MODELS

        last_err = ""
        for target_model in candidate_models:
            try:
                c_res = requests.post(
                    "https://api.groq.com/openai/v1/chat/completions",
                    headers=headers,
                    json={
                        "model": target_model,
                        "messages": [{"role": "user", "content": "hi"}],
                        "max_tokens": 5,
                        "temperature": 0.1
                    },
                    timeout=8
                )
                total_lat = int((time.time() - t0) * 1000)

                if c_res.status_code == 200:
                    return {
                        "ok": True,
                        "status": "active",
                        "status_code": 200,
                        "latency_ms": total_lat,
                        "message": f"Active & Operational ({target_model})",
                        "model": target_model,
                        "accessible_models": len(available_ids)
                    }
                elif c_res.status_code == 429:
                    return {
                        "ok": False,
                        "status": "rate_limited",
                        "status_code": 429,
                        "latency_ms": total_lat,
                        "message": "Rate Limited (429)"
                    }
                else:
                    last_err = f"HTTP {c_res.status_code}: {c_res.text[:80]}"
                    continue
            except Exception as e:
                last_err = str(e)[:80]
                continue

        # If models list worked but completions failed on all tested models:
        if available_ids:
            return {
                "ok": True,
                "status": "active",
                "status_code": 200,
                "latency_ms": lat,
                "message": f"Key Valid ({len(available_ids)} models available)",
                "model": candidate_models[0] if candidate_models else "available"
            }
        else:
            return {
                "ok": False,
                "status": "error",
                "latency_ms": lat,
                "message": last_err or "No chat models accessible"
            }

    except requests.exceptions.Timeout:
        return {
            "ok": False,
            "status": "timeout",
            "latency_ms": int((time.time() - t0) * 1000),
            "message": "Request Timeout (>8s)"
        }
    except Exception as e:
        return {
            "ok": False,
            "status": "error",
            "latency_ms": int((time.time() - t0) * 1000),
            "message": f"Connection Error: {str(e)[:80]}"
        }

def generate_ai_reply(business, review_text, stars, groq_key=None):
    keys = get_active_groq_keys(custom_key=groq_key or business.get('groq_key'))
    if not keys:
        return None
    star_ctx = "positive" if stars >= 4 else "neutral" if stars == 3 else "negative/critical"
    prompt = f"""You are the social media manager for {business['name']}, a {business.get('category') or 'business'} in India.
Business description: {business.get('description') or business.get('tagline') or ''}
Website: {business.get('website_url') or ''}
Keywords: {business.get('keywords') or ''}

A customer left this {stars}-star ({star_ctx}) Google review:
"{review_text}"

Write a Google Business Profile reply that:
- Is 50-80 words
- Tone: warm, genuine, appreciative, professional
- Naturally includes 1-2 keywords relevant to {business['name']}
- Addresses specific points the reviewer mentioned
- Ends with an inviting, friendly closing line

Reply ONLY with the reply text. No quotes or intro labels."""

    for key_idx, key in enumerate(keys):
        for model_name in GROQ_MODELS:
            try:
                r = requests.post(
                    "https://api.groq.com/openai/v1/chat/completions",
                    headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
                    json={"model": model_name, "messages": [{"role": "user", "content": prompt}], "max_tokens": 200, "temperature": 0.8},
                    timeout=10
                )
                if r.status_code == 200:
                    data = r.json()
                    return data["choices"][0]["message"]["content"].strip()
                elif r.status_code in (401, 429):
                    print(f"⚠️ Groq reply key #{key_idx+1} ({mask_groq_key(key)}) HTTP {r.status_code}. Trying fallback key.")
                    break
            except Exception:
                continue
    return None

# ─────────────────────────────────────────
# REVIEW DIVERSITY & REPUTATION ENGINE
# ─────────────────────────────────────────

def classify_product_domain(product_name="", product_desc="", product_keywords="", business_info=""):
    """
    Accurately classifies offerings into their true domain to prevent cross-category hallucinations
    (e.g., prevents body wash from talking about hair fall, or software talking about food).
    """
    prod_text = f"{product_name} {product_desc} {product_keywords}".lower()
    full_text = f"{prod_text} {business_info}".lower()

    # Domain keyword maps
    pest_home_keywords = ['ant', 'repellent', 'repel', 'spray', 'mosquito', 'pest', 'insect', 'cockroach', 'bug', 'cleaner', 'floor cleaner', 'disinfectant']
    body_keywords = ['body wash', 'soap', 'body lotion', 'ubtan', 'scrub', 'shower', 'bath', 'body butter', 'body oil', 'moisturizer', 'hand cream', 'foot cream']
    skin_face_keywords = ['face', 'serum', 'glow', 'acne', 'pimple', 'pigmentation', 'skin', 'blemish', 'cream', 'face wash', 'sunscreen', 'anti aging', 'toner', 'facial', 'kumkumadi']
    hair_keywords = ['hair', 'hairfall', 'shampoo', 'conditioner', 'scalp', 'dandruff', 'kesh', 'follicle', 'shedding', 'hair oil', 'anti hairfall']
    wellness_keywords = ['churna', 'juice', 'kadha', 'tea', 'capsule', 'immunity', 'digest', 'stamina', 'weight', 'detox', 'supplement', 'herbal powder']
    recruitment_keywords = ['job', 'placement', 'interview', 'hiring', 'recruitment', 'staffing', 'resume', 'candidate']
    tech_keywords = ['software', 'website', 'app', 'crm', 'web development', 'ui/ux', 'seo', 'cloud']

    # Priority 1: Match specific product fields
    if any(k in prod_text for k in pest_home_keywords):
        return 'pest_home'
    if any(k in prod_text for k in body_keywords):
        return 'body_bath'
    if any(k in prod_text for k in skin_face_keywords):
        return 'skin_face'
    if any(k in prod_text for k in hair_keywords):
        return 'hair'
    if any(k in prod_text for k in wellness_keywords):
        return 'wellness_health'
    if any(k in prod_text for k in recruitment_keywords):
        return 'recruitment'
    if any(k in prod_text for k in tech_keywords):
        return 'tech'

    # Priority 2: Match broader context
    if any(k in full_text for k in pest_home_keywords):
        return 'pest_home'
    if any(k in full_text for k in body_keywords):
        return 'body_bath'
    if any(k in full_text for k in skin_face_keywords):
        return 'skin_face'
    if any(k in full_text for k in hair_keywords):
        return 'hair'
    if any(k in full_text for k in wellness_keywords):
        return 'wellness_health'
    if any(k in full_text for k in recruitment_keywords):
        return 'recruitment'
    if any(k in full_text for k in tech_keywords):
        return 'tech'

    return 'general'

def generate_review_suggestions(business, stars, product=None, groq_key=None):
    # Brand Reputation Safeguard: Never generate public negative reviews to post on Google Maps!
    if stars < 4:
        return []

    keys = get_active_groq_keys(custom_key=groq_key or business.get('groq_key'))
    prod_name = product.get('name') if product else None
    
    if not keys:
        return get_fallback_suggestions(business, stars, product=product)

    domain = classify_product_domain(
        product.get('name', '') if product else '',
        product.get('description', '') if product else '',
        product.get('keywords', '') if product else '',
        f"{business.get('name', '')} {business.get('category', '')} {business.get('tagline', '')} {business.get('description', '')}"
    )

    domain_guidance = {
        'pest_home': "This is a NATURAL / HERBAL HOME CARE & PEST REPELLENT product (e.g. Herbal Ant Repellent Spray, mosquito repellent, natural floor cleaner). Strictly focus on: highly effective at repelling ants/pests safely, 100% safe around kids and pets, pleasant natural botanical smell (NO toxic chemical fumes or choking pesticide smell), easy spray bottle, clean home protection. ABSOLUTE RULE: DO NOT mention hair, scalp, skin, hairfall, or body wash!",
        'body_bath': "This is a BODY CARE / BATH product (e.g. body wash, soap, body lotion, scrub). Strictly focus on: refreshing shower feel, skin hydration, gentle natural lather, all-day softness, botanical aroma, zero chemical dryness. ABSOLUTE RULE: DO NOT mention hair, scalp, hairfall, or facial acne!",
        'skin_face': "This is a FACIAL SKINCARE product (e.g. face serum, face wash, cream, glow pack). Strictly focus on: radiant glowing skin, clearer complexion, smooth skin texture, fast absorption, calming redness/blemishes. ABSOLUTE RULE: DO NOT mention hair fall or scalp!",
        'hair': "This is a HAIR CARE product (e.g. hairfall kit, hair oil, shampoo). Strictly focus on: healthy nourished scalp, reduced hair shedding/breakage, stronger roots, natural shine, non-greasy feel. ABSOLUTE RULE: DO NOT mention body wash lather or facial acne!",
        'wellness_health': "This is a WELLNESS / HERBAL SUPPLEMENT product. Focus on: daily energy, natural digestion, pure Ayurvedic herbs, stamina, and overall vitality.",
        'recruitment': "This is a RECRUITMENT / STAFFING SERVICE. Focus on: verified job placements, prompt interview calls, career counseling, transparent communication.",
        'tech': "This is a SOFTWARE / DIGITAL SERVICE. Focus on: clean code architecture, prompt project delivery, responsive communication, high performance.",
        'general': "Focus on: authentic quality, dependable team, on-time delivery, excellent value for money, and personal recommendation."
    }.get(domain, "Focus on high quality and dependable customer satisfaction.")

    context_lines = []
    if business.get('category'): context_lines.append(f"Business Category: {business['category']}")
    if business.get('tagline'): context_lines.append(f"Tagline / Core Promise: {business['tagline']}")
    if business.get('website_url'): context_lines.append(f"Official Website: {business['website_url']}")
    if business.get('keywords'): context_lines.append(f"Brand Focus / Keywords: {business['keywords']}")
    if business.get('description'): context_lines.append(f"Business Details: {business['description']}")
    
    if product:
        context_lines.append(f"Specific Product Reviewed: {product['name']}")
        if product.get('description'): context_lines.append(f"Product Description: {product['description']}")
        if product.get('keywords'): context_lines.append(f"Product Keywords: {product['keywords']}")
        
    context_str = "\n".join(context_lines)
    random_salt = secrets.token_hex(4)

    prompt = f"""You are generating 5 realistic, glowing, 5-star Google Customer Review options for {business['name']}.
Session Salt: {random_salt}

PRODUCT DOMAIN CONSTRAINT:
{domain_guidance}

Business & Offering Context:
{context_str}

CRITICAL DIVERSITY INSTRUCTIONS:
Every review MUST be unique in sentence structure, tone, and angle to optimize the Google Business Profile (GMB) and avoid duplicate review spam:
- Review 1 (Results-Oriented): Focus on visible results or tangible benefits noticed after 2-3 weeks of regular use.
- Review 2 (Ingredients & Aroma): Focus on pure herbal/natural ingredients, pleasant aroma, and absence of harsh chemicals.
- Review 3 (Daily Usage Comfort): Focus on everyday convenience, comfortable texture (non-greasy, refreshing, gentle).
- Review 4 (Brand Trust & Delivery): Focus on brand authenticity, neat packaging, prompt dispatch, and switching from commercial brands.
- Review 5 (Recommendation & Value): Focus on great value for money, gifting or family recommendation, and definitely repurchasing.

RULES:
- Length: 18 to 32 words per review.
- NEVER start multiple reviews with the same word. Do NOT start reviews with 'Really impressed', 'Great experience', or 'I bought'.
- Sound 100% human, casual, and authentic — like a verified customer sharing real daily experience.
{'The review MUST specifically mention ' + prod_name + '.' if prod_name else 'Reviews should highlight their offerings naturally.'}
- Return ONLY a valid JSON array of 5 distinct strings. No markdown, no numbering, no code block backticks."""

    for key_idx, key in enumerate(keys):
        for model_name in GROQ_MODELS:
            try:
                r = requests.post(
                    "https://api.groq.com/openai/v1/chat/completions",
                    headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
                    json={
                        "model": model_name,
                        "messages": [{"role": "user", "content": prompt}],
                        "max_tokens": 500,
                        "temperature": 0.95,
                        "top_p": 0.95
                    },
                    timeout=10
                )
                if r.status_code == 200:
                    text = r.json()["choices"][0]["message"]["content"].strip()
                    text = text.replace("```json","").replace("```","").strip()
                    suggestions = json.loads(text)
                    if isinstance(suggestions, list) and len(suggestions) >= 3:
                        return [s.strip('"\'') for s in suggestions]
                elif r.status_code in (401, 429):
                    print(f"⚠️ Groq review key #{key_idx+1} ({mask_groq_key(key)}) returned HTTP {r.status_code}. Trying fallback key.")
                    break
            except Exception as e:
                print(f"Groq attempt ({model_name}) error: {e}")
                continue

    return get_fallback_suggestions(business, stars, product=product)

def get_fallback_suggestions(business, stars, product=None):
    """
    Intelligent, diverse, category-aware fallback review generator.
    Includes domain-specific pools (Body Care, Face Care, Hair Care, Wellness, Agency, Tech, General)
    so products never hallucinate unrelated benefits.
    """
    # Brand Reputation Safeguard: Never generate public negative reviews for Google Maps!
    if stars < 4:
        return []

    biz_name = business.get('name', 'Business')
    prod_name = product.get('name') if product else None
    p = prod_name or 'products'

    domain = classify_product_domain(
        product.get('name', '') if product else '',
        product.get('description', '') if product else '',
        product.get('keywords', '') if product else '',
        f"{business.get('name', '')} {business.get('category', '')} {business.get('tagline', '')} {business.get('description', '')}"
    )

    if domain == 'pest_home':
        pool = [
            f"Tried {biz_name}'s {p} in our kitchen and balcony. Worked like magic on ants within hours, and the best part is it has zero toxic fumes!",
            f"Finally a genuine 100% natural pest repellent that actually works! {biz_name} {p} is completely safe around our kids and pets. Highly recommended.",
            f"No strong, suffocating pesticide smell at all — just a fresh, pleasant botanical aroma. {biz_name} {p} cleared out ants effortlessly.",
            f"Super effective and eco-friendly! Applied {biz_name}'s {p} along the window sills and ant trails; haven't seen a single ant since. Great spray mechanism too.",
            f"Much safer and better than harsh chemical insect sprays. The herbal formulation of {biz_name}'s {p} gives total peace of mind for home hygiene.",
            f"Fast delivery and leak-proof packaging. {biz_name} {p} is an absolute must-have for every home looking for safe, chemical-free pest control.",
            f"Highly impressed by the effectiveness of {p}. Safe around the dining area, pleasant herbal scent, and completely eradicated our ant problem."
        ]
    elif domain == 'body_bath':
        pool = [
            f"Started using {biz_name}'s {p} in my daily shower routine. Leaves my skin feeling deeply hydrated, refreshed, and soft without any chemical dryness!",
            f"Truly refreshing natural body care. The botanical herbal fragrance of {biz_name}'s {p} is soothing and feels luxurious on the skin.",
            f"Finally found an authentic chemical-free cleanser! {biz_name} {p} washes off clean, lathers gently, and keeps skin smooth all day.",
            f"Switched to {biz_name} for daily body care. The {p} is gentle on sensitive skin and smells divine. Highly recommend this brand!",
            f"Very pleased with the quality of {p}. No parabens, sulfates, or synthetic perfumes — just pure, refreshing Ayurvedic skin nourishment.",
            f"Prompt delivery and leak-proof packaging. The herbal formulation of {p} leaves skin refreshed, soft, and naturally glowing.",
            f"Gifted the {p} from {biz_name} to my family and everyone loves how moisturized and clean their skin feels. Genuine quality!",
            f"Leaves a subtle herbal aroma and no slippery residue. {biz_name} {p} is worth every rupee for natural, glowing skin."
        ]
    elif domain == 'skin_face':
        pool = [
            f"Been applying {biz_name}'s {p} for 3 weeks and my complexion looks so much clearer and radiant. Absorbs quickly without any stickiness!",
            f"Authentic herbal formulation that really shows results. {biz_name} {p} calmed my skin redness and gave a natural, healthy glow.",
            f"Gentle on sensitive skin with a lovely natural aroma. {p} from {biz_name} is so much better than overpriced commercial chemical serums.",
            f"Very impressed with the visible improvement in skin texture after using {p}. Clean, pure Ayurvedic ingredients that truly work.",
            f"Leaves my skin feeling deeply nourished and glowing throughout the day. Definitely repurchasing {p} from {biz_name}!",
            f"Packaging was premium and delivery was super fast. {biz_name}'s {p} has become a permanent staple in my daily skincare regimen.",
            f"No breakouts, no harsh chemicals. The natural botanicals in {biz_name} {p} made my face feel smooth, hydrated, and radiant."
        ]
    elif domain == 'hair':
        pool = [
            f"Started using the {p} from {biz_name} about 3 weeks ago and hair fall has noticeably reduced. Love that it's 100% natural and gentle on the scalp!",
            f"Truly authentic Ayurvedic formulation. Unlike chemical alternatives, {biz_name} {p} is gentle and leaves hair feeling much stronger and healthier.",
            f"Ordered the {p} on a friend's recommendation. The natural herbal ingredients really work — scalp feels nourished and refreshed.",
            f"Bhilwara finally has a genuine, trustworthy Ayurvedic brand! {biz_name} delivered the {p} promptly in great packaging. Highly impressed.",
            f"Switched to {biz_name} after trying multiple commercial brands. The {p} is non-greasy, herbal, and genuinely effective. Will definitely restock.",
            f"Very pleased with the visible results from {p}. You can tell it is crafted from pure Ayurvedic herbs without harsh artificial silicones.",
            f"No harsh chemicals, no artificial fragrance — just honest herbal care. {biz_name} has definitely won my trust with this {p}.",
            f"The herbal aroma and soothing feel of {biz_name}'s {p} is amazing. Noticeable difference in hair texture and reduced breakage."
        ]
    elif domain == 'wellness_health':
        pool = [
            f"Started taking {p} from {biz_name} daily and feeling much more energetic. Pure Ayurvedic herbs with zero artificial additives.",
            f"Noticeable improvement in daily wellness since starting {biz_name}'s {p}. Authentic taste and genuine herbal quality.",
            f"High-grade natural preparation from {biz_name}. Clean, pure, and very effective for daily vitality and digestion.",
            f"Trustworthy Ayurvedic quality. {biz_name} delivered the {p} fresh and neatly packaged. Highly recommended!"
        ]
    elif domain == 'recruitment':
        pool = [
            f"Got my interview scheduled within 4 days through {biz_name}. Their guidance for {p} was spot on and very professional.",
            f"Transparent and dependable team at {biz_name}. They helped me land a verified job with great growth opportunities.",
            f"Highly recommended for anyone looking for reliable jobs in Bhilwara. {biz_name} provided prompt support throughout the process.",
            f"Very professional recruiters at {biz_name}. Their {p} was smooth and totally transparent from day one.",
            f"Cleared my interview on the first attempt thanks to {biz_name}. Genuine opportunities and supportive counselors.",
            f"Unlike other agencies, {biz_name} actually follows up and delivers verified job listings. Extremely grateful for their help!"
        ]
    elif domain == 'tech':
        pool = [
            f"Delivered our project ahead of schedule with clean architecture. The team at {biz_name} is proactive, skilled, and reliable.",
            f"Seamless communication and high technical standards. {biz_name} built exactly what our business needed with {p}.",
            f"High quality development and dependable support. {biz_name} exceeded all our technical expectations."
        ]
    else:
        p_str = f" with {prod_name}" if prod_name else ""
        p_mention = f" The {prod_name} was exceptional." if prod_name else ""
        pool = [
            f"Superb experience{p_str} at {biz_name}! The team was responsive, polite, and delivered exactly what was promised.{p_mention}",
            f"Quality and professionalism are top notch at {biz_name}. Everything was handled seamlessly{p_str}.",
            f"Prompt communication and dependable execution from {biz_name}. Definitely my go-to choice for {p}.",
            f"Very satisfied with {biz_name}{p_str}. Trustworthy brand that pays attention to every small customer detail.",
            f"Delivered on time and exceeded expectations{p_str}. Would gladly recommend {biz_name} to family and colleagues.",
            f"High quality, fair pricing, and excellent team support at {biz_name}. Truly a 5-star experience{p_str}!"
        ]

    random.shuffle(pool)
    return pool[:5]

# ─────────────────────────────────────────
# STATIC FILES
# ─────────────────────────────────────────

@app.route('/static/uploads/<filename>')
def uploaded_file(filename):
    return send_from_directory(UPLOAD_FOLDER, filename)

# ─────────────────────────────────────────
# PUBLIC — CUSTOMER REVIEW PAGE (LIGHT THEME)
# ─────────────────────────────────────────

def format_whatsapp_url(val, biz_name=""):
    if not val:
        return ""
    val = str(val).strip()
    if val.startswith("http://") or val.startswith("https://"):
        return val
    digits = re.sub(r'\D', '', val)
    if not digits:
        return ""
    if len(digits) == 10:
        digits = "91" + digits
    text = f"Hi {biz_name}, I wanted to share feedback regarding my recent experience."
    return f"https://wa.me/{digits}?text={urllib.parse.quote(text)}"

@app.route("/review/<slug>")
def review_page(slug):
    db = get_db()
    biz = db.execute("SELECT * FROM businesses WHERE slug=?", (slug,)).fetchone()
    if not biz:
        abort(404)
        
    biz_dict = dict(biz)
    products = db.execute("SELECT * FROM products WHERE business_id=? ORDER BY id ASC", (biz_dict['id'],)).fetchall()
    wa_num = biz_dict.get('whatsapp_number', '')
    wa_url = format_whatsapp_url(wa_num, biz_dict.get('name', ''))
    
    return render_template("review.html", 
        business=biz_dict,
        products=[dict(p) for p in products],
        whatsapp_url=wa_url
    )

@app.route("/api/suggestions/<slug>")
def get_suggestions(slug):
    stars = int(request.args.get("stars", 5))
    product_id = request.args.get("product_id")
    
    db = get_db()
    biz = db.execute("SELECT * FROM businesses WHERE slug=?", (slug,)).fetchone()
    if not biz:
        return jsonify({"error": "Business not found"}), 404
    biz = dict(biz)

    product = None
    if product_id and product_id != "all":
        try:
            p_row = db.execute("SELECT * FROM products WHERE id=? AND business_id=?", (int(product_id), biz['id'])).fetchone()
            if p_row:
                product = dict(p_row)
        except Exception:
            pass

    # Check custom templates if general
    if not product:
        templates = db.execute(
            "SELECT text FROM review_templates WHERE business_id=? AND stars=?",
            (biz['id'], stars)
        ).fetchall()
        if templates:
            t_list = [t['text'] for t in templates]
            random.shuffle(t_list)
            return jsonify({"suggestions": t_list})

    suggestions = generate_review_suggestions(biz, stars, product=product)
    return jsonify({"suggestions": suggestions})

@app.route("/api/log-review", methods=["POST"])
def log_review():
    """Log when a customer clicked submit (opens Google Maps)"""
    data = request.json or {}
    slug = data.get("slug")
    stars = data.get("stars")
    text = data.get("text", "")

    db = get_db()
    biz = db.execute("SELECT * FROM businesses WHERE slug=?", (slug,)).fetchone()
    if not biz:
        return jsonify({"ok": False}), 404

    biz = dict(biz)
    cur = db.execute(
        "INSERT INTO reviews (business_id, stars, text) VALUES (?,?,?)",
        (biz['id'], stars, text)
    )
    review_id = cur.lastrowid
    db.commit()

    # Auto-generate reply if enabled
    if biz.get('auto_reply') and text and review_id:
        reply = generate_ai_reply(biz, text, stars)
        if reply:
            db.execute(
                "UPDATE reviews SET reply=?, replied_at=? WHERE id=?",
                (reply, datetime.now().isoformat(), review_id)
            )
            db.commit()

    return jsonify({"ok": True, "maps_url": biz['maps_url']})

@app.route("/api/private-feedback", methods=["POST"])
def private_feedback():
    """Customer 1-3 star feedback routed privately to management (never posted on Google)"""
    data = request.json or {}
    slug = data.get("slug")
    stars = int(data.get("stars", 1))
    text = data.get("text", "").strip()
    contact = data.get("contact", "").strip()

    db = get_db()
    biz = db.execute("SELECT * FROM businesses WHERE slug=?", (slug,)).fetchone()
    if not biz:
        return jsonify({"ok": False, "error": "Business not found"}), 404

    biz = dict(biz)
    reviewer_title = f"Private Feedback ({contact})" if contact else "Private Feedback"
    stored_text = f"[INTERNAL RESOLUTION NEEDED] {text}" if text else "[INTERNAL FEEDBACK - Rating only]"

    cur = db.execute(
        "INSERT INTO reviews (business_id, reviewer, stars, text) VALUES (?,?,?,?)",
        (biz['id'], reviewer_title, stars, stored_text)
    )
    db.commit()
    return jsonify({"ok": True, "message": "Feedback safely received"})

# ─────────────────────────────────────────
# AUTH
# ─────────────────────────────────────────

@app.route("/login", methods=["GET", "POST"])
def login():
    if request.method == "POST":
        data = request.json or request.form
        slug = data.get("slug", "").strip()
        passcode = data.get("passcode", "").strip()
        db = get_db()
        biz = db.execute(
            "SELECT * FROM businesses WHERE slug=? AND passcode=?",
            (slug, hash_pass(passcode))
        ).fetchone()
        if biz:
            session['business_id'] = biz['id']
            session['business_slug'] = biz['slug']
            if request.is_json:
                return jsonify({"ok": True})
            return redirect(url_for('dashboard'))
        if request.is_json:
            return jsonify({"ok": False, "error": "Invalid credentials"}), 401
        return render_template("login.html", error="Invalid business ID or passcode")
    return render_template("login.html")

@app.route("/logout")
def logout():
    session.clear()
    return redirect(url_for('login'))

# ─────────────────────────────────────────
# BUSINESS DASHBOARD
# ─────────────────────────────────────────

@app.route("/dashboard")
@business_required
def dashboard():
    db = get_db()
    biz = db.execute("SELECT * FROM businesses WHERE id=?", (session['business_id'],)).fetchone()
    reviews = db.execute(
        "SELECT * FROM reviews WHERE business_id=? ORDER BY created_at DESC LIMIT 50",
        (session['business_id'],)
    ).fetchall()
    stats = db.execute(
        "SELECT COUNT(*) as total, AVG(stars) as avg_stars, "
        "SUM(CASE WHEN reply IS NOT NULL THEN 1 ELSE 0 END) as replied "
        "FROM reviews WHERE business_id=?",
        (session['business_id'],)
    ).fetchone()
    products = db.execute(
        "SELECT * FROM products WHERE business_id=? ORDER BY id DESC",
        (session['business_id'],)
    ).fetchall()
    
    return render_template("dashboard.html",
        business=dict(biz),
        reviews=[dict(r) for r in reviews],
        stats=dict(stats),
        products=[dict(p) for p in products]
    )

@app.route("/api/business/update", methods=["POST"])
@business_required
def update_business():
    data = request.json or {}
    db = get_db()
    db.execute("""UPDATE businesses SET
        name=?, tagline=?, maps_url=?, category=?, description=?,
        website_url=?, keywords=?, whatsapp_number=?,
        auto_reply=?, theme_color=?, groq_key=?
        WHERE id=?""",
        (data.get('name'), data.get('tagline'), data.get('maps_url'),
         data.get('category'), data.get('description'),
         data.get('website_url', ''), data.get('keywords', ''),
         data.get('whatsapp_number', '').strip(),
         1 if data.get('auto_reply') else 0,
         data.get('theme_color','#3A2CCB'),
         data.get('groq_key',''),
         session['business_id'])
    )
    db.commit()
    return jsonify({"ok": True})

@app.route("/api/business/upload-pic", methods=["POST"])
@business_required
def upload_profile_pic():
    if 'profile_pic' not in request.files:
        return jsonify({"ok": False, "error": "No file uploaded"}), 400
    
    file = request.files['profile_pic']
    if file.filename == '':
        return jsonify({"ok": False, "error": "No file selected"}), 400
    
    if file and allowed_file(file.filename):
        ext = file.filename.rsplit('.', 1)[1].lower()
        filename = f"{session['business_id']}_{secrets.token_hex(4)}.{ext}"
        filename = secure_filename(filename)
        filepath = os.path.join(UPLOAD_FOLDER, filename)
        file.save(filepath)
        
        db = get_db()
        old_pic = db.execute("SELECT profile_pic FROM businesses WHERE id=?", (session['business_id'],)).fetchone()
        if old_pic and old_pic['profile_pic']:
            old_path = os.path.join(UPLOAD_FOLDER, os.path.basename(old_pic['profile_pic']))
            if os.path.exists(old_path):
                try:
                    os.remove(old_path)
                except Exception:
                    pass
        
        pic_url = f"/static/uploads/{filename}"
        db.execute("UPDATE businesses SET profile_pic=? WHERE id=?", (pic_url, session['business_id']))
        db.commit()
        
        return jsonify({"ok": True, "url": pic_url})
    
    return jsonify({"ok": False, "error": "Invalid file type. Use PNG, JPG, GIF, or WebP."}), 400

@app.route("/api/business/remove-pic", methods=["POST"])
@business_required
def remove_profile_pic():
    db = get_db()
    old_pic = db.execute("SELECT profile_pic FROM businesses WHERE id=?", (session['business_id'],)).fetchone()
    if old_pic and old_pic['profile_pic']:
        old_path = os.path.join(UPLOAD_FOLDER, os.path.basename(old_pic['profile_pic']))
        if os.path.exists(old_path):
            try:
                os.remove(old_path)
            except Exception:
                pass
    db.execute("UPDATE businesses SET profile_pic=NULL WHERE id=?", (session['business_id'],))
    db.commit()
    return jsonify({"ok": True})

# ─────────────────────────────────────────
# PRODUCTS MANAGEMENT APIS (DASHBOARD)
# ─────────────────────────────────────────

@app.route("/api/business/products", methods=["GET", "POST"])
@business_required
def manage_products():
    db = get_db()
    bid = session['business_id']
    if request.method == "GET":
        rows = db.execute("SELECT * FROM products WHERE business_id=? ORDER BY id DESC", (bid,)).fetchall()
        return jsonify([dict(r) for r in rows])
        
    data = request.json or {}
    name = data.get("name", "").strip()
    if not name:
        return jsonify({"ok": False, "error": "Product name is required"}), 400
        
    db.execute("INSERT INTO products (business_id, name, description, keywords) VALUES (?,?,?,?)",
               (bid, name, data.get("description", "").strip(), data.get("keywords", "").strip()))
    db.commit()
    return jsonify({"ok": True})

@app.route("/api/business/products/<int:pid>", methods=["DELETE"])
@business_required
def delete_product(pid):
    db = get_db()
    db.execute("DELETE FROM products WHERE id=? AND business_id=?", (pid, session['business_id']))
    db.commit()
    return jsonify({"ok": True})

@app.route("/api/business/products/import", methods=["POST"])
@business_required
def import_products():
    if 'file' not in request.files:
        return jsonify({"ok": False, "error": "No file uploaded"}), 400
    file = request.files['file']
    if not file or not file.filename:
        return jsonify({"ok": False, "error": "Empty file"}), 400
        
    parsed = parse_products_file(file)
    if not parsed:
        return jsonify({"ok": False, "error": "No valid products found in file. Ensure columns include Name, Description, Keywords."}), 400
        
    db = get_db()
    bid = session['business_id']
    for p in parsed:
        db.execute("INSERT INTO products (business_id, name, description, keywords) VALUES (?,?,?,?)",
                   (bid, p['name'], p.get('description', ''), p.get('keywords', '')))
    db.commit()
    return jsonify({"ok": True, "count": len(parsed)})

@app.route("/api/sample-products-csv")
def sample_products_csv():
    csv_data = "Name,Description,Keywords\n" \
               "Hairfall Kit,Ayurvedic pure herbal formula for deep nourishment and hair fall reduction,anti hairfall ayurvedic oil shampoo\n" \
               "Ayurvedic Face Glow Serum,Cold-pressed herbal oil blend with saffron and sandalwood for radiant skin,herbal glowing skin serum\n" \
               "Herbal Body Wash,Chemical-free gentle botanical cleanser with neem and tulsi,herbal body wash natural soap free\n"
    return Response(
        csv_data,
        mimetype="text/csv",
        headers={"Content-disposition": "attachment; filename=sample_products.csv"}
    )

# ─────────────────────────────────────────
# TEMPLATES & REVIEWS
# ─────────────────────────────────────────

@app.route("/api/templates", methods=["GET", "POST", "DELETE"])
@business_required
def manage_templates():
    db = get_db()
    bid = session['business_id']
    if request.method == "GET":
        rows = db.execute("SELECT * FROM review_templates WHERE business_id=?", (bid,)).fetchall()
        return jsonify([dict(r) for r in rows])
    if request.method == "POST":
        data = request.json
        db.execute("INSERT INTO review_templates (business_id,stars,text) VALUES (?,?,?)",
                   (bid, data['stars'], data['text']))
        db.commit()
        return jsonify({"ok": True})
    if request.method == "DELETE":
        tid = request.json.get('id')
        db.execute("DELETE FROM review_templates WHERE id=? AND business_id=?", (tid, bid))
        db.commit()
        return jsonify({"ok": True})

@app.route("/api/reply/<int:review_id>", methods=["POST"])
@business_required
def manual_reply(review_id):
    data = request.json or {}
    db = get_db()
    biz = db.execute("SELECT * FROM businesses WHERE id=?", (session['business_id'],)).fetchone()
    review = db.execute("SELECT * FROM reviews WHERE id=? AND business_id=?",
                        (review_id, session['business_id'])).fetchone()
    if not review:
        return jsonify({"error": "Not found"}), 404

    if data.get('auto'):
        reply = generate_ai_reply(dict(biz), review['text'], review['stars'])
    else:
        reply = data.get('reply', '')

    db.execute("UPDATE reviews SET reply=?, replied_at=? WHERE id=?",
               (reply, datetime.now().isoformat(), review_id))
    db.commit()
    return jsonify({"ok": True, "reply": reply})

@app.route("/api/reviews")
@business_required
def get_reviews():
    db = get_db()
    rows = db.execute(
        "SELECT * FROM reviews WHERE business_id=? ORDER BY created_at DESC",
        (session['business_id'],)
    ).fetchall()
    return jsonify([dict(r) for r in rows])

# ─────────────────────────────────────────
# REGISTER NEW BUSINESS
# ─────────────────────────────────────────

@app.route("/register", methods=["GET", "POST"])
def register():
    if request.method == "POST":
        if request.is_json:
            data = request.json
            products_file = None
        else:
            data = request.form
            products_file = request.files.get("products_file")
            
        name = data.get("name","").strip()
        slug = slugify(data.get("slug") or name)
        passcode = data.get("passcode","").strip()
        maps_url = data.get("maps_url","").strip()
        website_url = data.get("website_url","").strip()
        keywords = data.get("keywords","").strip()
        whatsapp_number = data.get("whatsapp_number","").strip()
        
        if not all([name, slug, passcode, maps_url]):
            return jsonify({"ok": False, "error": "All required fields must be filled"}), 400
            
        db = get_db()
        existing = db.execute("SELECT id FROM businesses WHERE slug=?", (slug,)).fetchone()
        if existing:
            return jsonify({"ok": False, "error": "Slug already taken, choose another"}), 409
            
        bid = secrets.token_urlsafe(8)
        db.execute("""INSERT INTO businesses
            (id,name,slug,tagline,maps_url,category,description,website_url,keywords,whatsapp_number,passcode,auto_reply,theme_color)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,1,'#3A2CCB')""",
            (bid, name, slug, data.get("tagline",""), maps_url,
             data.get("category",""), data.get("description",""), website_url, keywords, whatsapp_number, hash_pass(passcode))
        )
        db.commit()
        
        imported_count = 0
        if products_file and products_file.filename:
            parsed = parse_products_file(products_file)
            for p in parsed:
                db.execute("INSERT INTO products (business_id, name, description, keywords) VALUES (?,?,?,?)",
                           (bid, p['name'], p.get('description', ''), p.get('keywords', '')))
            db.commit()
            imported_count = len(parsed)
            
        session['business_id'] = bid
        session['business_slug'] = slug
        return jsonify({"ok": True, "slug": slug, "review_url": f"/review/{slug}", "imported_products": imported_count})
        
    return render_template("register.html")

@app.route("/")
def index():
    if 'business_id' in session:
        return redirect(url_for('dashboard'))
    return redirect(url_for('login'))

# ─────────────────────────────────────────
# ADMIN PANEL
# ─────────────────────────────────────────

@app.route("/admin/login", methods=["GET", "POST"])
def admin_login():
    if request.method == "POST":
        data = request.json or request.form
        password = data.get("password", "").strip()
        if password == ADMIN_PASSWORD:
            session['is_admin'] = True
            if request.is_json:
                return jsonify({"ok": True})
            return redirect(url_for('admin_panel'))
        if request.is_json:
            return jsonify({"ok": False, "error": "Invalid admin password"}), 401
    return render_template("admin_login.html")

@app.route("/admin/logout")
def admin_logout():
    session.pop('is_admin', None)
    return redirect(url_for('admin_login'))

@app.route("/admin")
@admin_required
def admin_panel():
    db = get_db()
    businesses = db.execute("""
        SELECT b.*, 
            COUNT(DISTINCT r.id) as review_count,
            AVG(r.stars) as avg_stars,
            COUNT(DISTINCT p.id) as product_count
        FROM businesses b
        LEFT JOIN reviews r ON r.business_id = b.id
        LEFT JOIN products p ON p.business_id = b.id
        GROUP BY b.id
        ORDER BY b.created_at DESC
    """).fetchall()
    businesses = [dict(b) for b in businesses]

    all_reviews = db.execute("""
        SELECT r.*, b.name as biz_name
        FROM reviews r
        JOIN businesses b ON b.id = r.business_id
        ORDER BY r.created_at DESC
        LIMIT 100
    """).fetchall()
    all_reviews = [dict(r) for r in all_reviews]

    total_reviews = db.execute("SELECT COUNT(*) as c FROM reviews").fetchone()['c']
    avg_rating = db.execute("SELECT AVG(stars) as a FROM reviews").fetchone()['a']
    auto_on = db.execute("SELECT COUNT(*) as c FROM businesses WHERE auto_reply=1").fetchone()['c']
    total_products = db.execute("SELECT COUNT(*) as c FROM products").fetchone()['c']

    stats = {
        'total_businesses': len(businesses),
        'total_reviews': total_reviews,
        'avg_rating': avg_rating or 0,
        'auto_reply_on': auto_on,
        'total_products': total_products
    }

    raw_g_keys = db.execute("SELECT * FROM groq_keys ORDER BY id ASC").fetchall()
    groq_keys_list = []
    for k in raw_g_keys:
        kd = dict(k)
        kd['masked_key'] = mask_groq_key(kd.get('api_key', ''))
        groq_keys_list.append(kd)

    return render_template("admin.html",
        businesses=businesses,
        businesses_json=json.dumps(businesses, default=str),
        all_reviews=all_reviews,
        stats=stats,
        groq_keys=groq_keys_list,
        groq_keys_json=json.dumps(groq_keys_list, default=str),
        groq_models=GROQ_MODELS
    )

@app.route("/admin/api/business/<bid>", methods=["POST"])
@admin_required
def admin_update_business(bid):
    data = request.json or {}
    db = get_db()
    biz = db.execute("SELECT * FROM businesses WHERE id=?", (bid,)).fetchone()
    if not biz:
        return jsonify({"ok": False, "error": "Business not found"}), 404

    db.execute("""UPDATE businesses SET
        name=?, tagline=?, maps_url=?, category=?, description=?,
        website_url=?, keywords=?, whatsapp_number=?,
        auto_reply=?, groq_key=?
        WHERE id=?""",
        (data.get('name', biz['name']),
         data.get('tagline', biz['tagline']),
         data.get('maps_url', biz['maps_url']),
         data.get('category', biz['category']),
         data.get('description', biz['description']),
         data.get('website_url', biz['website_url'] or ''),
         data.get('keywords', biz['keywords'] or ''),
         data.get('whatsapp_number', (biz['whatsapp_number'] if 'whatsapp_number' in biz and biz['whatsapp_number'] else '') or '').strip(),
         1 if data.get('auto_reply') else 0,
         data.get('groq_key', biz['groq_key'] or ''),
         bid)
    )

    passcode = data.get('passcode', '').strip()
    if passcode:
        db.execute("UPDATE businesses SET passcode=? WHERE id=?", (hash_pass(passcode), bid))

    db.commit()
    return jsonify({"ok": True})

@app.route("/admin/api/business/<bid>", methods=["DELETE"])
@admin_required
def admin_delete_business(bid):
    db = get_db()
    biz = db.execute("SELECT * FROM businesses WHERE id=?", (bid,)).fetchone()
    if not biz:
        return jsonify({"ok": False, "error": "Business not found"}), 404

    if biz['profile_pic']:
        old_path = os.path.join(UPLOAD_FOLDER, os.path.basename(biz['profile_pic']))
        if os.path.exists(old_path):
            try:
                os.remove(old_path)
            except Exception:
                pass

    db.execute("DELETE FROM products WHERE business_id=?", (bid,))
    db.execute("DELETE FROM review_templates WHERE business_id=?", (bid,))
    db.execute("DELETE FROM reviews WHERE business_id=?", (bid,))
    db.execute("DELETE FROM businesses WHERE id=?", (bid,))
    db.commit()
    return jsonify({"ok": True})

@app.route("/admin/api/business/<bid>/products", methods=["GET", "POST"])
@admin_required
def admin_manage_products(bid):
    db = get_db()
    if request.method == "GET":
        rows = db.execute("SELECT * FROM products WHERE business_id=? ORDER BY id DESC", (bid,)).fetchall()
        return jsonify([dict(r) for r in rows])
        
    data = request.json or {}
    name = data.get("name", "").strip()
    if not name:
        return jsonify({"ok": False, "error": "Product name is required"}), 400
        
    db.execute("INSERT INTO products (business_id, name, description, keywords) VALUES (?,?,?,?)",
               (bid, name, data.get("description", "").strip(), data.get("keywords", "").strip()))
    db.commit()
    return jsonify({"ok": True})

@app.route("/admin/api/business/<bid>/products/<int:pid>", methods=["DELETE"])
@admin_required
def admin_delete_product(bid, pid):
    db = get_db()
    db.execute("DELETE FROM products WHERE id=? AND business_id=?", (pid, bid))
    db.commit()
    return jsonify({"ok": True})

@app.route("/admin/api/business/<bid>/products/import", methods=["POST"])
@admin_required
def admin_import_products(bid):
    if 'file' not in request.files:
        return jsonify({"ok": False, "error": "No file uploaded"}), 400
    file = request.files['file']
    parsed = parse_products_file(file)
    if not parsed:
        return jsonify({"ok": False, "error": "No valid products found in file"}), 400
        
    db = get_db()
    for p in parsed:
        db.execute("INSERT INTO products (business_id, name, description, keywords) VALUES (?,?,?,?)",
                   (bid, p['name'], p.get('description', ''), p.get('keywords', '')))
    db.commit()
    return jsonify({"ok": True, "count": len(parsed)})

# ─────────────────────────────────────────
# ADMIN — GROQ MULTI-KEY & STATUS ENGINE
# ─────────────────────────────────────────

@app.route("/admin/api/groq-keys", methods=["GET"])
@admin_required
def admin_get_groq_keys():
    db = get_db()
    rows = db.execute("SELECT * FROM groq_keys ORDER BY id ASC").fetchall()
    keys = []
    for r in rows:
        d = dict(r)
        d['masked_key'] = mask_groq_key(d.get('api_key', ''))
        keys.append(d)
    return jsonify({
        "ok": True,
        "keys": keys,
        "models": GROQ_MODELS,
        "env_key_set": bool(GROQ_API_KEY)
    })

@app.route("/admin/api/groq-keys/add", methods=["POST"])
@admin_required
def admin_add_groq_keys():
    data = request.json or {}
    raw_input = data.get("api_key", "").strip()
    label = data.get("label", "").strip()
    
    if not raw_input:
        return jsonify({"ok": False, "error": "No API key provided"}), 400

    tokens = re.split(r'[\r\n,]+', raw_input)
    added = 0
    db = get_db()
    for tok in tokens:
        k = tok.strip()
        if not k:
            continue
        existing = db.execute("SELECT id FROM groq_keys WHERE api_key=?", (k,)).fetchone()
        if existing:
            continue
        
        lbl = label if (label and len(tokens) == 1) else f"Key #{added + 1}"
        db.execute(
            "INSERT INTO groq_keys (api_key, label, is_active, last_status) VALUES (?,?,?,?)",
            (k, lbl, 1, "untested")
        )
        added += 1
    db.commit()
    return jsonify({"ok": True, "added": added})

@app.route("/admin/api/groq-keys/<int:kid>/toggle", methods=["POST"])
@admin_required
def admin_toggle_groq_key(kid):
    db = get_db()
    k = db.execute("SELECT is_active FROM groq_keys WHERE id=?", (kid,)).fetchone()
    if not k:
        return jsonify({"ok": False, "error": "Key not found"}), 404
    curr = k['is_active'] if isinstance(k, dict) or hasattr(k, '__getitem__') else k[0]
    new_state = 0 if curr == 1 else 1
    db.execute("UPDATE groq_keys SET is_active=? WHERE id=?", (new_state, kid))
    db.commit()
    return jsonify({"ok": True, "is_active": new_state})

@app.route("/admin/api/groq-keys/<int:kid>", methods=["DELETE"])
@admin_required
def admin_delete_groq_key(kid):
    db = get_db()
    db.execute("DELETE FROM groq_keys WHERE id=?", (kid,))
    db.commit()
    return jsonify({"ok": True})

@app.route("/admin/api/groq-keys/test", methods=["POST"])
@admin_required
def admin_test_groq_keys():
    data = request.json or {}
    kid = data.get("id")
    raw_key = data.get("api_key")
    
    db = get_db()
    results = []
    now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    
    if kid:
        row = db.execute("SELECT * FROM groq_keys WHERE id=?", (kid,)).fetchone()
        if not row:
            return jsonify({"ok": False, "error": "Key not found"}), 404
        r_dict = dict(row)
        res = test_single_groq_key(r_dict['api_key'])
        res['id'] = kid
        res['label'] = r_dict.get('label')
        res['masked_key'] = mask_groq_key(r_dict['api_key'])
        db.execute(
            "UPDATE groq_keys SET last_status=?, last_checked=?, latency_ms=? WHERE id=?",
            (res['status'], now_str, res.get('latency_ms', 0), kid)
        )
        db.commit()
        results.append(res)
    elif raw_key:
        res = test_single_groq_key(raw_key)
        res['masked_key'] = mask_groq_key(raw_key)
        results.append(res)
    else:
        # Test all keys in database
        rows = db.execute("SELECT * FROM groq_keys ORDER BY id ASC").fetchall()
        for row in rows:
            r_dict = dict(row)
            res = test_single_groq_key(r_dict['api_key'])
            res['id'] = r_dict['id']
            res['label'] = r_dict.get('label')
            res['masked_key'] = mask_groq_key(r_dict['api_key'])
            db.execute(
                "UPDATE groq_keys SET last_status=?, last_checked=?, latency_ms=? WHERE id=?",
                (res['status'], now_str, res.get('latency_ms', 0), r_dict['id'])
            )
            results.append(res)
        db.commit()

    return jsonify({"ok": True, "results": results})

# Health check route for Dokploy / Docker container monitoring
@app.route("/healthz")
def healthz():
    return jsonify({"status": "ok", "timestamp": datetime.now().isoformat()})

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port, debug=False)
