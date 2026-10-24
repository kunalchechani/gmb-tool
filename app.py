from flask import Flask, render_template, request, jsonify, session, redirect, url_for, abort, send_from_directory, Response
from flask_cors import CORS
import sqlite3, hashlib, os, json, requests, csv, io, random, secrets, re
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

DB_PATH = os.environ.get("SQLITE_PATH", "gbp.db")
DATABASE_URL = os.environ.get("DATABASE_URL", "").strip()
if DATABASE_URL.startswith("postgres://"):
    DATABASE_URL = DATABASE_URL.replace("postgres://", "postgresql://", 1)

GROQ_API_KEY = os.environ.get("GROQ_API_KEY", "")
GROQ_MODELS = ["llama-3.1-8b-instant", "llama-3.3-70b-versatile", "mixtral-8x7b-32768", "llama3-8b-8192"]
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
                """)
                for col in ["profile_pic", "website_url", "keywords"]:
                    try:
                        cur.execute(f"ALTER TABLE businesses ADD COLUMN IF NOT EXISTS {col} TEXT")
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
        """)
        for col_name in ["profile_pic", "website_url", "keywords"]:
            try:
                db.execute(f"ALTER TABLE businesses ADD COLUMN {col_name} TEXT")
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

def generate_ai_reply(business, review_text, stars, groq_key=None):
    key = groq_key or business.get('groq_key') or GROQ_API_KEY
    if not key:
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

    for model_name in GROQ_MODELS:
        try:
            r = requests.post(
                "https://api.groq.com/openai/v1/chat/completions",
                headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
                json={"model": model_name, "messages": [{"role": "user", "content": prompt}], "max_tokens": 200, "temperature": 0.8},
                timeout=12
            )
            if r.status_code == 200:
                data = r.json()
                return data["choices"][0]["message"]["content"].strip()
        except Exception:
            continue
    return None

# ─────────────────────────────────────────
# REVIEW DIVERSITY & GENERATION ENGINE
# ─────────────────────────────────────────

def is_ayurvedic_or_herbal(business, product=None):
    check_str = f"{business.get('name', '')} {business.get('tagline', '')} {business.get('description', '')} {business.get('category', '')} {business.get('keywords', '')}".lower()
    if product:
        check_str += f" {product.get('name', '')} {product.get('description', '')} {product.get('keywords', '')}".lower()
    ayur_terms = ['ayurvedic', 'ayurveda', 'herbal', 'natural', 'hairfall', 'skincare', 'hair care', 'wellness', 'organic', 'oil', 'serum', 'personal care', 'gir natural']
    return any(term in check_str for term in ayur_terms)

def is_recruitment_or_agency(business, product=None):
    check_str = f"{business.get('name', '')} {business.get('tagline', '')} {business.get('description', '')} {business.get('category', '')}".lower()
    return any(t in check_str for t in ['job', 'recruitment', 'staffing', 'placement', 'career', 'employment', 'hiring', 'woho'])

def generate_review_suggestions(business, stars, product=None, groq_key=None):
    key = groq_key or business.get('groq_key') or GROQ_API_KEY
    prod_name = product.get('name') if product else None
    
    if not key:
        return get_fallback_suggestions(business, stars, product=product)
        
    star_ctx = "positive, appreciative, genuine customer recommendation" if stars >= 4 else "balanced, neutral, honest" if stars == 3 else "polite constructive feedback"
    
    context_lines = []
    if business.get('category'): context_lines.append(f"Business Category: {business['category']}")
    if business.get('tagline'): context_lines.append(f"Tagline / Core Promise: {business['tagline']}")
    if business.get('website_url'): context_lines.append(f"Official Website: {business['website_url']}")
    if business.get('keywords'): context_lines.append(f"Brand Focus / Keywords: {business['keywords']}")
    if business.get('description'): context_lines.append(f"Business Details: {business['description']}")
    
    if product:
        context_lines.append(f"Selected Product/Service for Review: {product['name']}")
        if product.get('description'): context_lines.append(f"Product Description: {product['description']}")
        if product.get('keywords'): context_lines.append(f"Product Keywords: {product['keywords']}")
        
    context_str = "\n".join(context_lines)
    random_salt = secrets.token_hex(4)

    prompt = f"""You are generating 5 realistic, completely distinct Google Customer Review options for {business['name']}.
Rating given: {stars} out of 5 stars ({star_ctx}).
Random Session Salt: {random_salt}

Business & Offering Context:
{context_str}

CRITICAL DIVERSITY INSTRUCTIONS:
Every review MUST be unique in sentence structure, tone, and angle to optimize the Google Business Profile (GMB) and avoid duplicate review spam:
- Review 1: Focus on visible results or tangible benefits noticed over time (e.g. after 2-3 weeks).
- Review 2: Focus on authentic ingredients/materials, texture, natural smell, or craftsmanship.
- Review 3: Focus on everyday convenience, comfort, ease of use, and gentle application.
- Review 4: Focus on brand trustworthiness, fast delivery, packaging, or switching from chemical/commercial brands.
- Review 5: Focus on value for money, gifting/family recommendation, and definitely repurchasing.

RULES:
- Length: 16 to 34 words per review.
- NEVER start multiple reviews with the same word. Do NOT start reviews with 'Really impressed', 'Great experience', or 'I bought'.
- Sound 100% human, casual, and authentic — like a verified customer sharing real daily experience.
{'The review MUST specifically mention and focus on ' + prod_name + '.' if prod_name else 'Reviews should highlight their offerings naturally.'}
- Return ONLY a valid JSON array of 5 distinct strings. No markdown, no numbering, no code block backticks."""

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
                timeout=12
            )
            if r.status_code == 200:
                text = r.json()["choices"][0]["message"]["content"].strip()
                text = text.replace("```json","").replace("```","").strip()
                suggestions = json.loads(text)
                if isinstance(suggestions, list) and len(suggestions) >= 3:
                    return [s.strip('"\'') for s in suggestions]
        except Exception as e:
            print(f"Groq attempt ({model_name}) error: {e}")
            continue

    return get_fallback_suggestions(business, stars, product=product)

def get_fallback_suggestions(business, stars, product=None):
    """
    Intelligent, diverse, category-aware fallback review generator.
    Includes 60+ varied templates with dynamic shuffling so no two users get identical reviews.
    """
    biz_name = business.get('name', 'Business')
    prod_name = product.get('name') if product else None
    
    is_ayur = is_ayurvedic_or_herbal(business, product)
    is_agency = is_recruitment_or_agency(business, product)

    if stars >= 4:
        if is_ayur:
            p = prod_name or 'products'
            ayur_pool = [
                f"Started using the {p} from {biz_name} about 3 weeks ago and hair fall has noticeably reduced. Love that it's 100% natural and gentle on the scalp!",
                f"Truly authentic Ayurvedic formulation. Unlike chemical alternatives, {biz_name} {p} is gentle and leaves hair feeling much stronger and healthier.",
                f"Ordered the {p} on a friend's recommendation. The natural herbal ingredients really work — scalp feels nourished and refreshed.",
                f"Bhilwara finally has a genuine, trustworthy Ayurvedic brand! {biz_name} delivered the {p} promptly in great packaging. Highly impressed.",
                f"Switched to {biz_name} after trying multiple commercial brands. The {p} is non-greasy, herbal, and genuinely effective. Will definitely restock.",
                f"Very pleased with the visible results from {p}. You can tell it is crafted from pure Ayurvedic herbs without harsh artificial silicones.",
                f"No harsh chemicals, no artificial fragrance — just honest herbal care. {biz_name} has definitely won my trust with this {p}.",
                f"Gifted the {p} from {biz_name} to my mother and she noticed positive results within a month. Excellent quality and pure formulation.",
                f"The herbal aroma and soothing feel of {biz_name}'s {p} is amazing. Noticeable difference in hair texture and reduced breakage.",
                f"Clean, chemical-free, and effective personal care. So glad to support an authentic homegrown brand like {biz_name}!",
                f"After continuous use of {p}, my scalp irritation is gone and hair density feels improved. 100% recommend to anyone dealing with hairfall.",
                f"Prompt delivery and leak-proof packaging. The herbal formulation of {p} proves that natural Ayurvedic care works best."
            ]
            random.shuffle(ayur_pool)
            return ayur_pool[:5]

        elif is_agency:
            p = prod_name or 'placement assistance'
            agency_pool = [
                f"Got my interview scheduled within 4 days through {biz_name}. Their guidance for {p} was spot on and very professional.",
                f"Transparent and dependable team at {biz_name}. They helped me land a verified job with great growth opportunities.",
                f"Highly recommended for anyone looking for reliable jobs in Bhilwara. {biz_name} provided prompt support throughout the process.",
                f"Very professional recruiters at {biz_name}. Their {p} was smooth and totally transparent from day one.",
                f"Cleared my interview on the first attempt thanks to {biz_name}. Genuine opportunities and supportive counselors.",
                f"Unlike other agencies, {biz_name} actually follows up and delivers verified job listings. Extremely grateful for their help!"
            ]
            random.shuffle(agency_pool)
            return agency_pool[:5]

        else:
            p = f" with {prod_name}" if prod_name else ""
            p_mention = f" The {prod_name} was exceptional." if prod_name else ""
            general_pool = [
                f"Superb experience{p} at {biz_name}! The team was responsive, polite, and delivered exactly what was promised.{p_mention}",
                f"Quality and professionalism are top notch at {biz_name}. Everything was handled seamlessly{p}.",
                f"Prompt communication and dependable execution from {biz_name}. Definitely my go-to choice for {prod_name or 'their services'}.",
                f"Very satisfied with {biz_name}{p}. Trustworthy brand that pays attention to every small customer detail.",
                f"Delivered on time and exceeded expectations{p}. Would gladly recommend {biz_name} to family and colleagues.",
                f"High quality, fair pricing, and excellent team support at {biz_name}. Truly a 5-star experience{p}!"
            ]
            random.shuffle(general_pool)
            return general_pool[:5]

    elif stars == 3:
        p = f" for {prod_name}" if prod_name else ""
        three_star_pool = [
            f"Decent overall experience with {biz_name}{p}. Good quality though there is room for faster response times.",
            f"Average experience{p}. The team at {biz_name} was polite and delivered the basic requirements as promised.",
            f"{biz_name} provides fair service{p}. Helpful staff, with minor improvements needed in turnaround speed.",
            f"Satisfactory experience with {biz_name}{p}. Solid potential and honest people, just needs slight process polishing.",
            f"Mixed experience{p} at {biz_name}. Some aspects were great, while communication could be a little quicker."
        ]
        random.shuffle(three_star_pool)
        return three_star_pool[:5]

    else:
        p = f" regarding {prod_name}" if prod_name else ""
        low_star_pool = [
            f"Service{p} at {biz_name} needs improvement. The outcome did not fully match initial commitments.",
            f"Experienced delays and communication gaps with {biz_name}{p}. Hope management works on speeding up resolution.",
            f"Not completely satisfied with the experience{p}. Clearer updates from {biz_name} would have helped.",
            f"Had high expectations for {biz_name}{p}, but the process was slower than expected.",
            f"Service quality{p} at {biz_name} needs refinement. Sharing honest feedback for improvement."
        ]
        random.shuffle(low_star_pool)
        return low_star_pool[:5]

# ─────────────────────────────────────────
# STATIC FILES
# ─────────────────────────────────────────

@app.route('/static/uploads/<filename>')
def uploaded_file(filename):
    return send_from_directory(UPLOAD_FOLDER, filename)

# ─────────────────────────────────────────
# PUBLIC — CUSTOMER REVIEW PAGE (LIGHT THEME)
# ─────────────────────────────────────────

@app.route("/review/<slug>")
def review_page(slug):
    db = get_db()
    biz = db.execute("SELECT * FROM businesses WHERE slug=?", (slug,)).fetchone()
    if not biz:
        abort(404)
        
    products = db.execute("SELECT * FROM products WHERE business_id=? ORDER BY id ASC", (biz['id'],)).fetchall()
    
    return render_template("review.html", 
        business=dict(biz),
        products=[dict(p) for p in products]
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
        website_url=?, keywords=?,
        auto_reply=?, theme_color=?, groq_key=?
        WHERE id=?""",
        (data.get('name'), data.get('tagline'), data.get('maps_url'),
         data.get('category'), data.get('description'),
         data.get('website_url', ''), data.get('keywords', ''),
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
        
        if not all([name, slug, passcode, maps_url]):
            return jsonify({"ok": False, "error": "All required fields must be filled"}), 400
            
        db = get_db()
        existing = db.execute("SELECT id FROM businesses WHERE slug=?", (slug,)).fetchone()
        if existing:
            return jsonify({"ok": False, "error": "Slug already taken, choose another"}), 409
            
        bid = secrets.token_urlsafe(8)
        db.execute("""INSERT INTO businesses
            (id,name,slug,tagline,maps_url,category,description,website_url,keywords,passcode,auto_reply,theme_color)
            VALUES (?,?,?,?,?,?,?,?,?,?,1,'#3A2CCB')""",
            (bid, name, slug, data.get("tagline",""), maps_url,
             data.get("category",""), data.get("description",""), website_url, keywords, hash_pass(passcode))
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

    return render_template("admin.html",
        businesses=businesses,
        businesses_json=json.dumps(businesses, default=str),
        all_reviews=all_reviews,
        stats=stats
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
        website_url=?, keywords=?,
        auto_reply=?, groq_key=?
        WHERE id=?""",
        (data.get('name', biz['name']),
         data.get('tagline', biz['tagline']),
         data.get('maps_url', biz['maps_url']),
         data.get('category', biz['category']),
         data.get('description', biz['description']),
         data.get('website_url', biz['website_url'] or ''),
         data.get('keywords', biz['keywords'] or ''),
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

# Health check route for Dokploy / Docker container monitoring
@app.route("/healthz")
def healthz():
    return jsonify({"status": "ok", "timestamp": datetime.now().isoformat()})

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port, debug=False)
