from fastapi import FastAPI, Request, Form, UploadFile, File
from fastapi.responses import RedirectResponse, FileResponse
from fastapi.templating import Jinja2Templates
from fastapi.staticfiles import StaticFiles
from starlette.middleware.sessions import SessionMiddleware
from passlib.hash import pbkdf2_sha256
from reportlab.platypus import SimpleDocTemplate, Paragraph, Spacer
from reportlab.lib.styles import getSampleStyleSheet

from openai import OpenAI

import sqlite3
import uuid
import os
import csv
import re
import json
import base64
import mimetypes
from datetime import datetime

app = FastAPI(title="DevisAI V9 Vision")
app.add_middleware(SessionMiddleware, secret_key="devisai_secret_key_v9_2026")

templates = Jinja2Templates(directory="templates")

UPLOAD_DIR = "uploads"
PDF_DIR = "pdfs"
DB_PATH = "database.db"
STATIC_DIR = "static"
LOGO_DIR = os.path.join(STATIC_DIR, "logos")
CATALOG_PATH = "catalogue_prix.csv"

os.makedirs(UPLOAD_DIR, exist_ok=True)
os.makedirs(PDF_DIR, exist_ok=True)
os.makedirs(STATIC_DIR, exist_ok=True)
os.makedirs(LOGO_DIR, exist_ok=True)

app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")

styles = getSampleStyleSheet()

conn = sqlite3.connect(DB_PATH, check_same_thread=False)
cursor = conn.cursor()

# ----------------------------
# DB
# ----------------------------

cursor.execute("""
CREATE TABLE IF NOT EXISTS users (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    username TEXT UNIQUE NOT NULL,
    password TEXT NOT NULL,
    plan TEXT DEFAULT 'starter',
    is_admin INTEGER DEFAULT 0
)
""")

cursor.execute("""
CREATE TABLE IF NOT EXISTS clients (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    username TEXT NOT NULL,
    name TEXT NOT NULL,
    email TEXT DEFAULT '',
    phone TEXT DEFAULT ''
)
""")

cursor.execute("""
CREATE TABLE IF NOT EXISTS quotes (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    username TEXT NOT NULL,
    client TEXT NOT NULL,
    description TEXT NOT NULL,
    total_ht REAL NOT NULL,
    tva REAL NOT NULL,
    total_ttc REAL NOT NULL,
    pdf_name TEXT NOT NULL,
    photos TEXT DEFAULT ''
)
""")

cursor.execute("""
CREATE TABLE IF NOT EXISTS contacts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL,
    email TEXT NOT NULL,
    message TEXT NOT NULL
)
""")

cursor.execute("""
CREATE TABLE IF NOT EXISTS settings (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    username TEXT UNIQUE NOT NULL,
    company_name TEXT DEFAULT 'DevisAI Premium',
    company_address TEXT DEFAULT '12 Rue de l''Innovation, 38000 Grenoble',
    company_phone TEXT DEFAULT '07 00 00 00 00',
    company_email TEXT DEFAULT 'contact@devisai.fr',
    payment_terms TEXT DEFAULT 'Paiement à 30 jours. Acompte possible selon nature du chantier.',
    quote_prefix TEXT DEFAULT 'DV',
    validity_days INTEGER DEFAULT 30,
    logo_path TEXT DEFAULT ''
)
""")

conn.commit()

quote_columns = [row[1] for row in cursor.execute("PRAGMA table_info(quotes)").fetchall()]
if "quote_number" not in quote_columns:
    cursor.execute("ALTER TABLE quotes ADD COLUMN quote_number TEXT DEFAULT ''")
if "created_at" not in quote_columns:
    cursor.execute("ALTER TABLE quotes ADD COLUMN created_at TEXT DEFAULT ''")
if "trade" not in quote_columns:
    cursor.execute("ALTER TABLE quotes ADD COLUMN trade TEXT DEFAULT ''")
if "confidence" not in quote_columns:
    cursor.execute("ALTER TABLE quotes ADD COLUMN confidence INTEGER DEFAULT 0")
if "vision_summary" not in quote_columns:
    cursor.execute("ALTER TABLE quotes ADD COLUMN vision_summary TEXT DEFAULT ''")

settings_columns = [row[1] for row in cursor.execute("PRAGMA table_info(settings)").fetchall()]
if "logo_path" not in settings_columns:
    cursor.execute("ALTER TABLE settings ADD COLUMN logo_path TEXT DEFAULT ''")

conn.commit()

# ----------------------------
# Admin par défaut
# ----------------------------

admin_user = cursor.execute(
    "SELECT * FROM users WHERE username = ?",
    ("admin",)
).fetchone()

if not admin_user:
    cursor.execute(
        "INSERT INTO users (username, password, plan, is_admin) VALUES (?, ?, ?, ?)",
        ("admin", pbkdf2_sha256.hash("admin1234"), "business", 1)
    )
    conn.commit()

PLAN_LIMITS = {
    "starter": 5,
    "pro": 50,
    "business": 999999
}

LABOR_RATE_PER_HOUR = {
    "electricite": 55.0,
    "plomberie": 52.0,
    "renovation": 48.0
}

TRAVEL_FLAT_RATE = 35.0


# ----------------------------
# Catalog
# ----------------------------

def load_catalog():
    catalog = []
    if not os.path.exists(CATALOG_PATH):
        return catalog

    with open(CATALOG_PATH, newline="", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        for row in reader:
            catalog.append({
                "categorie": row["categorie"].strip(),
                "article": row["article"].strip(),
                "prix_achat": float(row["prix_achat"]),
                "prix_vente": float(row["prix_vente"]),
                "temps_pose_h": float(row.get("temps_pose_h", 0) or 0),
                "mots_cles": [x.strip().lower() for x in row.get("mots_cles", "").split() if x.strip()],
                "suggestions": [x.strip() for x in row.get("suggestions", "").split("|") if x.strip()]
            })
    return catalog


CATALOG = load_catalog()


# ----------------------------
# Helpers
# ----------------------------

def get_current_user(request: Request):
    return request.session.get("user")


def get_user_record(username: str):
    if not username:
        return None
    return cursor.execute(
        "SELECT id, username, password, plan, is_admin FROM users WHERE username = ?",
        (username,)
    ).fetchone()


def get_or_create_settings(username: str):
    if not username:
        return None

    settings = cursor.execute(
        """
        SELECT username, company_name, company_address, company_phone,
               company_email, payment_terms, quote_prefix, validity_days, logo_path
        FROM settings
        WHERE username = ?
        """,
        (username,)
    ).fetchone()

    if settings:
        return settings

    cursor.execute(
        """
        INSERT INTO settings (
            username, company_name, company_address, company_phone,
            company_email, payment_terms, quote_prefix, validity_days, logo_path
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            username,
            "DevisAI Premium",
            "12 Rue de l'Innovation, 38000 Grenoble",
            "07 00 00 00 00",
            "contact@devisai.fr",
            "Paiement à 30 jours. Acompte possible selon nature du chantier.",
            "DV",
            30,
            ""
        )
    )
    conn.commit()

    return cursor.execute(
        """
        SELECT username, company_name, company_address, company_phone,
               company_email, payment_terms, quote_prefix, validity_days, logo_path
        FROM settings
        WHERE username = ?
        """,
        (username,)
    ).fetchone()


def get_plan_limit(plan: str):
    return PLAN_LIMITS.get(plan, 5)


def get_quotes_count(username: str):
    result = cursor.execute(
        "SELECT COUNT(*) FROM quotes WHERE username = ?",
        (username,)
    ).fetchone()
    return result[0] if result else 0


def generate_quote_number(username: str):
    settings = get_or_create_settings(username)
    prefix = settings[6] if settings and settings[6] else "DV"
    current_year = datetime.now().year
    count = cursor.execute(
        "SELECT COUNT(*) FROM quotes WHERE username = ?",
        (username,)
    ).fetchone()[0] + 1
    return f"{prefix}-{current_year}-{count:04d}"


def detect_trade(description: str):
    desc = description.lower()

    scores = {
        "electricite": 0,
        "plomberie": 0,
        "renovation": 0
    }

    electricite_keywords = [
        "tableau", "prise", "spot", "borne", "differentiel", "différentiel",
        "vmc", "eclairage", "éclairage", "circuit", "disjoncteur", "interrupteur",
        "module", "rangée", "rangee"
    ]
    plomberie_keywords = [
        "evier", "évier", "lavabo", "wc", "robinet", "chauffe-eau", "chauffe eau",
        "canalisation", "plomberie", "douche", "baignoire", "fuite", "mitigeur"
    ]
    renovation_keywords = [
        "renovation", "rénovation", "placo", "peinture", "sol", "carrelage",
        "cloison", "isolation", "faux plafond"
    ]

    for word in electricite_keywords:
        if word in desc:
            scores["electricite"] += 1

    for word in plomberie_keywords:
        if word in desc:
            scores["plomberie"] += 1

    for word in renovation_keywords:
        if word in desc:
            scores["renovation"] += 1

    best_trade = max(scores, key=scores.get)
    if scores[best_trade] == 0:
        return "electricite"
    return best_trade


def extract_quantity(desc: str, keywords: list[str], default_qty: int = 1):
    for keyword in keywords:
        pattern_1 = rf"(\d+)\s+{re.escape(keyword)}"
        match_1 = re.search(pattern_1, desc)
        if match_1:
            return max(1, int(match_1.group(1)))

        pattern_2 = rf"{re.escape(keyword)}\s+(\d+)"
        match_2 = re.search(pattern_2, desc)
        if match_2:
            return max(1, int(match_2.group(1)))
    return default_qty


def find_catalog_item_by_article(article_name: str):
    for item in CATALOG:
        if item["article"].lower() == article_name.lower():
            return item
    return None


def merge_text_and_vision_description(description: str, vision_data: dict | None):
    if not vision_data:
        return description

    parts = [description.strip()]

    inferred_trade = vision_data.get("trade", "")
    if inferred_trade:
        parts.append(f"type chantier {inferred_trade}")

    observations = vision_data.get("observations", [])
    if observations:
        parts.append(" ".join(observations))

    inferred_items = vision_data.get("inferred_items", [])
    for item in inferred_items:
        qty = item.get("qty", 1)
        label = item.get("label", "")
        if label:
            parts.append(f"{qty} {label}")

    return " ".join([p for p in parts if p])


def find_catalog_lines(description: str, trade: str):
    desc = description.lower()
    lines = []
    matched_articles = set()

    for item in CATALOG:
        if item["categorie"] != trade:
            continue

        matched = False
        for kw in item["mots_cles"]:
            if kw in desc:
                matched = True
                break

        if not matched and item["article"].lower() not in desc:
            continue

        qty = extract_quantity(desc, item["mots_cles"], 1)

        if "tableau" in item["article"].lower():
            qty = 1
        if "chauffe eau" in item["article"].lower():
            qty = 1
        if "wc suspendu" in item["article"].lower():
            qty = 1

        unit_price = item["prix_vente"]
        total = round(unit_price * qty, 2)

        lines.append({
            "article": item["article"],
            "qty": qty,
            "price": unit_price,
            "total": total,
            "temps_pose_h": item["temps_pose_h"],
            "suggested": False
        })
        matched_articles.add(item["article"].lower())

    suggested_names = set()
    for line in lines:
        item = find_catalog_item_by_article(line["article"])
        if not item:
            continue
        for suggestion in item["suggestions"]:
            suggested_names.add(suggestion.lower())

    for suggestion_name in suggested_names:
        if suggestion_name in matched_articles:
            continue
        suggestion_item = find_catalog_item_by_article(suggestion_name)
        if not suggestion_item or suggestion_item["categorie"] != trade:
            continue

        lines.append({
            "article": suggestion_item["article"],
            "qty": 1,
            "price": suggestion_item["prix_vente"],
            "total": round(suggestion_item["prix_vente"], 2),
            "temps_pose_h": suggestion_item["temps_pose_h"],
            "suggested": True
        })
        matched_articles.add(suggestion_item["article"].lower())

    dedup = {}
    for line in lines:
        key = line["article"]
        if key not in dedup:
            dedup[key] = line
        else:
            dedup[key]["qty"] += line["qty"]
            dedup[key]["total"] = round(dedup[key]["qty"] * dedup[key]["price"], 2)
            dedup[key]["suggested"] = dedup[key]["suggested"] and line["suggested"]

    return list(dedup.values())


def estimate_quote_real(description: str, vision_data: dict | None = None):
    merged_description = merge_text_and_vision_description(description, vision_data)
    trade = detect_trade(merged_description)
    lines = find_catalog_lines(merged_description, trade)

    total_material = 0.0
    total_pose_hours = 0.0
    confidence = 30
    details = []

    for line in lines:
        total_material += line["total"]
        total_pose_hours += line["temps_pose_h"] * line["qty"]

        suffix = " (suggestion IA)" if line["suggested"] else ""
        details.append(
            f"{line['article']} — {line['qty']} x {line['price']:.2f} €{suffix}"
        )

        confidence += 8 if not line["suggested"] else 3

    if trade == "electricite":
        details.insert(0, "Étude et préparation du chantier électrique")
    elif trade == "plomberie":
        details.insert(0, "Étude et préparation du chantier plomberie")
    else:
        details.insert(0, "Étude et préparation du chantier rénovation")

    rate = LABOR_RATE_PER_HOUR.get(trade, 50.0)
    labor = round((total_pose_hours * rate) + TRAVEL_FLAT_RATE, 2)

    desc = merged_description.lower()
    if "renovation complete" in desc or "rénovation complète" in desc:
        labor += 250
        confidence += 5
    if "mise en conformité" in desc:
        labor += 120
        confidence += 5
    if "encastré" in desc:
        labor += 90
        confidence += 5

    if vision_data:
        confidence += 10

    if not lines:
        details.append("Aucun article catalogue reconnu précisément")
        total_material = 120.0
        labor = max(labor, 220.0)
        confidence = 20 if not vision_data else 35

    confidence = min(confidence, 95)
    total_ht = round(total_material + labor, 2)

    return lines, labor, total_ht, details, trade, confidence, merged_description


def image_file_to_data_url(path: str):
    mime_type, _ = mimetypes.guess_type(path)
    if not mime_type:
        mime_type = "image/jpeg"

    with open(path, "rb") as f:
        encoded = base64.b64encode(f.read()).decode("utf-8")

    return f"data:{mime_type};base64,{encoded}"


def get_openai_client():
    api_key = os.getenv("OPENAI_API_KEY", "").strip()
    if not api_key:
        return None
    return OpenAI(api_key=api_key)


def analyze_images_with_openai(description: str, image_paths: list[str]):
    """
    Retourne un dict du type :
    {
      "trade": "electricite",
      "observations": [...],
      "inferred_items": [{"label":"tableau", "qty":1}, ...],
      "summary": "..."
    }
    """
    client = get_openai_client()
    if not client or not image_paths:
        return None

    content = [
        {
            "type": "input_text",
            "text": (
                "Tu es un assistant chantier bâtiment. "
                "Analyse les photos et la description. "
                "Réponds UNIQUEMENT en JSON valide avec les clés : "
                "trade, observations, inferred_items, summary. "
                "trade doit être l'un de : electricite, plomberie, renovation. "
                "observations = liste courte de constats. "
                "inferred_items = liste d'objets {label, qty}. "
                "summary = résumé court. "
                f"Description utilisateur : {description}"
            )
        }
    ]

    for path in image_paths[:5]:
        content.append({
            "type": "input_image",
            "image_url": image_file_to_data_url(path)
        })

    try:
        response = client.responses.create(
            model="gpt-5.4-mini",
            input=[{
                "role": "user",
                "content": content
            }]
        )

        raw_text = getattr(response, "output_text", "") or ""
        raw_text = raw_text.strip()

        if not raw_text:
            return None

        data = json.loads(raw_text)

        if not isinstance(data, dict):
            return None

        if "observations" not in data or not isinstance(data.get("observations"), list):
            data["observations"] = []

        if "inferred_items" not in data or not isinstance(data.get("inferred_items"), list):
            data["inferred_items"] = []

        if "summary" not in data:
            data["summary"] = ""

        if data.get("trade") not in ["electricite", "plomberie", "renovation"]:
            data["trade"] = detect_trade(description)

        return data

    except Exception:
        return None


def create_pdf(
    settings,
    quote_number: str,
    created_at: str,
    client: str,
    description: str,
    details: list[str],
    lines: list[dict],
    labor: float,
    total_ht: float,
    tva: float,
    total_ttc: float,
    trade: str,
    confidence: int,
    vision_summary: str = ""
):
    filename = f"devis_{uuid.uuid4().hex}.pdf"
    path = os.path.join(PDF_DIR, filename)

    doc = SimpleDocTemplate(
        path,
        rightMargin=40,
        leftMargin=40,
        topMargin=40,
        bottomMargin=40
    )

    story = []

    title_style = styles["Title"]
    normal = styles["Normal"]
    heading = styles["Heading2"]

    company_name = settings[1]
    company_address = settings[2]
    company_phone = settings[3]
    company_email = settings[4]
    payment_terms = settings[5]
    validity_days = settings[7]

    trade_label = {
        "electricite": "Électricité",
        "plomberie": "Plomberie",
        "renovation": "Rénovation"
    }.get(trade, "Travaux")

    story.append(Paragraph(company_name, title_style))
    story.append(Spacer(1, 8))
    story.append(Paragraph(company_address, normal))
    story.append(Paragraph(f"Tél : {company_phone}", normal))
    story.append(Paragraph(f"Email : {company_email}", normal))
    story.append(Spacer(1, 16))

    story.append(Paragraph("DEVIS", heading))
    story.append(Spacer(1, 8))
    story.append(Paragraph(f"Numéro de devis : {quote_number}", normal))
    story.append(Paragraph(f"Date : {created_at}", normal))
    story.append(Paragraph(f"Type de chantier : {trade_label}", normal))
    story.append(Paragraph(f"Indice de confiance estimation : {confidence}%", normal))
    story.append(Paragraph(f"Validité : {validity_days} jours", normal))
    story.append(Spacer(1, 16))

    story.append(Paragraph(f"<b>Client :</b> {client}", normal))
    story.append(Spacer(1, 12))

    story.append(Paragraph("<b>Description du chantier :</b>", normal))
    story.append(Paragraph(description.replace("\n", "<br/>"), normal))
    story.append(Spacer(1, 12))

    if vision_summary:
        story.append(Paragraph("<b>Analyse photo IA :</b>", normal))
        story.append(Paragraph(vision_summary, normal))
        story.append(Spacer(1, 12))

    story.append(Paragraph("<b>Détail matériel estimé :</b>", normal))
    for line in lines:
        suffix = " (suggestion IA)" if line["suggested"] else ""
        story.append(
            Paragraph(
                f"• {line['article']} — {line['qty']} x {line['price']:.2f} € = {line['total']:.2f} €{suffix}",
                normal
            )
        )

    story.append(Spacer(1, 12))
    story.append(Paragraph(f"<b>Main d'oeuvre estimée :</b> {labor:.2f} €", normal))
    story.append(Spacer(1, 12))

    story.append(Paragraph("<b>Prestations retenues :</b>", normal))
    for item in details:
        story.append(Paragraph(f"• {item}", normal))

    story.append(Spacer(1, 12))
    story.append(Paragraph(f"<b>Total HT :</b> {total_ht:.2f} €", normal))
    story.append(Paragraph(f"<b>TVA :</b> {tva:.2f} €", normal))
    story.append(Paragraph(f"<b>Total TTC :</b> {total_ttc:.2f} €", normal))
    story.append(Spacer(1, 16))

    story.append(Paragraph("<b>Conditions de paiement :</b>", normal))
    story.append(Paragraph(payment_terms, normal))
    story.append(Spacer(1, 12))

    story.append(Paragraph("Merci pour votre confiance.", normal))

    doc.build(story)
    return filename


def render_template(request: Request, template_name: str, extra: dict | None = None):
    user = get_current_user(request)
    record = get_user_record(user) if user else None
    company_settings = get_or_create_settings(user) if user else None

    base_context = {
        "request": request,
        "user": user,
        "record": record,
        "company_settings": company_settings,
        "current_year": datetime.now().year,
    }

    if extra:
        base_context.update(extra)

    return templates.TemplateResponse(template_name, base_context)


# ----------------------------
# Routes
# ----------------------------

@app.get("/")
def home(request: Request):
    return render_template(request, "index.html")


@app.get("/pricing")
def pricing(request: Request):
    return render_template(request, "pricing.html")


@app.get("/contact")
def contact_page(request: Request):
    return render_template(request, "contact.html", {"success": None})


@app.post("/contact")
def contact_submit(
    request: Request,
    name: str = Form(...),
    email: str = Form(...),
    message: str = Form(...)
):
    cursor.execute(
        "INSERT INTO contacts (name, email, message) VALUES (?, ?, ?)",
        (name.strip(), email.strip(), message.strip())
    )
    conn.commit()

    return render_template(request, "contact.html", {
        "success": "Votre message a bien été envoyé."
    })


@app.get("/free-trial")
def free_trial(request: Request):
    return render_template(request, "free_trial.html")


@app.get("/register")
def register_page(request: Request):
    return render_template(request, "register.html", {"error": None})


@app.post("/register")
def register(request: Request, username: str = Form(...), password: str = Form(...)):
    username = username.strip()
    password = password.strip()

    if len(username) < 3:
        return render_template(request, "register.html", {
            "error": "Le nom d'utilisateur doit contenir au moins 3 caractères."
        })

    if len(password) < 4:
        return render_template(request, "register.html", {
            "error": "Le mot de passe doit contenir au moins 4 caractères."
        })

    try:
        cursor.execute(
            "INSERT INTO users (username, password, plan, is_admin) VALUES (?, ?, ?, ?)",
            (username, pbkdf2_sha256.hash(password), "starter", 0)
        )
        conn.commit()
    except sqlite3.IntegrityError:
        return render_template(request, "register.html", {
            "error": "Cet utilisateur existe déjà."
        })

    get_or_create_settings(username)
    request.session["user"] = username
    return RedirectResponse("/dashboard", status_code=303)


@app.get("/login")
def login_page(request: Request):
    return render_template(request, "login.html", {"error": None})


@app.post("/login")
def login(request: Request, username: str = Form(...), password: str = Form(...)):
    username = username.strip()

    user = cursor.execute(
        "SELECT id, username, password, plan, is_admin FROM users WHERE username = ?",
        (username,)
    ).fetchone()

    if not user or not pbkdf2_sha256.verify(password, user[2]):
        return render_template(request, "login.html", {
            "error": "Identifiants invalides."
        })

    request.session["user"] = username
    return RedirectResponse("/dashboard", status_code=303)


@app.get("/logout")
def logout(request: Request):
    request.session.clear()
    return RedirectResponse("/", status_code=303)


@app.get("/dashboard")
def dashboard(
    request: Request,
    q: str = "",
    trade: str = "",
    sort: str = "date_desc"
):
    user = get_current_user(request)
    if not user:
        return RedirectResponse("/login", status_code=303)

    record = get_user_record(user)
    plan_limit = get_plan_limit(record[3])
    quotes_count = get_quotes_count(user)

    query = """
        SELECT id, client, description, total_ht, tva, total_ttc, pdf_name, quote_number, created_at, trade
        FROM quotes
        WHERE username = ?
    """
    params = [user]

    if q.strip():
        query += " AND (client LIKE ? OR description LIKE ? OR quote_number LIKE ?)"
        like_term = f"%{q.strip()}%"
        params.extend([like_term, like_term, like_term])

    if trade.strip():
        query += " AND trade = ?"
        params.append(trade.strip())

    if sort == "amount_desc":
        query += " ORDER BY total_ttc DESC"
    elif sort == "amount_asc":
        query += " ORDER BY total_ttc ASC"
    elif sort == "date_asc":
        query += " ORDER BY id ASC"
    else:
        query += " ORDER BY id DESC"

    quotes = cursor.execute(query, params).fetchall()

    clients = cursor.execute(
        """
        SELECT id, name, email, phone
        FROM clients
        WHERE username = ?
        ORDER BY id DESC
        """,
        (user,)
    ).fetchall()

    total_revenue = cursor.execute(
        "SELECT COALESCE(SUM(total_ttc), 0) FROM quotes WHERE username = ?",
        (user,)
    ).fetchone()[0]

    electricite_count = cursor.execute(
        "SELECT COUNT(*) FROM quotes WHERE username = ? AND trade = 'electricite'",
        (user,)
    ).fetchone()[0]

    plomberie_count = cursor.execute(
        "SELECT COUNT(*) FROM quotes WHERE username = ? AND trade = 'plomberie'",
        (user,)
    ).fetchone()[0]

    renovation_count = cursor.execute(
        "SELECT COUNT(*) FROM quotes WHERE username = ? AND trade = 'renovation'",
        (user,)
    ).fetchone()[0]

    return render_template(request, "dashboard.html", {
        "quotes": quotes,
        "clients": clients,
        "quotes_count": quotes_count,
        "plan_limit": plan_limit,
        "error": None,
        "search_q": q,
        "search_trade": trade,
        "search_sort": sort,
        "total_revenue": total_revenue,
        "electricite_count": electricite_count,
        "plomberie_count": plomberie_count,
        "renovation_count": renovation_count
    })


@app.get("/profile")
def profile(request: Request):
    user = get_current_user(request)
    if not user:
        return RedirectResponse("/login", status_code=303)

    total_quotes = get_quotes_count(user)
    total_clients = cursor.execute(
        "SELECT COUNT(*) FROM clients WHERE username = ?",
        (user,)
    ).fetchone()[0]

    return render_template(request, "profile.html", {
        "total_quotes": total_quotes,
        "total_clients": total_clients
    })


@app.get("/billing")
def billing(request: Request):
    user = get_current_user(request)
    if not user:
        return RedirectResponse("/login", status_code=303)

    return render_template(request, "billing.html", {"success": None})


@app.post("/billing")
def billing_submit(request: Request, plan: str = Form(...)):
    user = get_current_user(request)
    if not user:
        return RedirectResponse("/login", status_code=303)

    if plan not in ["starter", "pro", "business"]:
        return RedirectResponse("/billing", status_code=303)

    cursor.execute(
        "UPDATE users SET plan = ? WHERE username = ?",
        (plan, user)
    )
    conn.commit()

    return render_template(request, "billing.html", {
        "success": f"Formule mise à jour vers {plan.upper()}."
    })


@app.get("/settings")
def settings_page(request: Request):
    user = get_current_user(request)
    if not user:
        return RedirectResponse("/login", status_code=303)

    settings = get_or_create_settings(user)

    return render_template(request, "settings.html", {
        "settings_data": settings,
        "success": None
    })


@app.post("/settings")
async def settings_submit(
    request: Request,
    company_name: str = Form(...),
    company_address: str = Form(...),
    company_phone: str = Form(...),
    company_email: str = Form(...),
    payment_terms: str = Form(...),
    quote_prefix: str = Form(...),
    validity_days: int = Form(...),
    logo: UploadFile | None = File(None)
):
    user = get_current_user(request)
    if not user:
        return RedirectResponse("/login", status_code=303)

    current_settings = get_or_create_settings(user)
    logo_path = current_settings[8] if current_settings else ""

    if logo and logo.filename:
        ext = os.path.splitext(logo.filename)[1].lower()
        safe_name = f"{user}_{uuid.uuid4().hex}{ext}"
        full_path = os.path.join(LOGO_DIR, safe_name)

        with open(full_path, "wb") as f:
            f.write(await logo.read())

        logo_path = f"/static/logos/{safe_name}"

    cursor.execute(
        """
        UPDATE settings
        SET company_name = ?, company_address = ?, company_phone = ?,
            company_email = ?, payment_terms = ?, quote_prefix = ?, validity_days = ?, logo_path = ?
        WHERE username = ?
        """,
        (
            company_name.strip(),
            company_address.strip(),
            company_phone.strip(),
            company_email.strip(),
            payment_terms.strip(),
            quote_prefix.strip().upper(),
            validity_days,
            logo_path,
            user
        )
    )
    conn.commit()

    settings = get_or_create_settings(user)

    return render_template(request, "settings.html", {
        "settings_data": settings,
        "success": "Paramètres entreprise mis à jour."
    })


@app.get("/clients")
def clients_page(request: Request, q: str = ""):
    user = get_current_user(request)
    if not user:
        return RedirectResponse("/login", status_code=303)

    query = """
        SELECT id, name, email, phone
        FROM clients
        WHERE username = ?
    """
    params = [user]

    if q.strip():
        query += " AND (name LIKE ? OR email LIKE ? OR phone LIKE ?)"
        like_term = f"%{q.strip()}%"
        params.extend([like_term, like_term, like_term])

    query += " ORDER BY id DESC"

    clients = cursor.execute(query, params).fetchall()

    return render_template(request, "clients.html", {
        "clients": clients,
        "client_search_q": q
    })


@app.post("/clients/add")
def add_client(
    request: Request,
    name: str = Form(...),
    email: str = Form(""),
    phone: str = Form("")
):
    user = get_current_user(request)
    if not user:
        return RedirectResponse("/login", status_code=303)

    cursor.execute(
        "INSERT INTO clients (username, name, email, phone) VALUES (?, ?, ?, ?)",
        (user, name.strip(), email.strip(), phone.strip())
    )
    conn.commit()

    return RedirectResponse("/clients", status_code=303)


@app.get("/clients/delete/{client_id}")
def delete_client(client_id: int, request: Request):
    user = get_current_user(request)
    if not user:
        return RedirectResponse("/login", status_code=303)

    cursor.execute(
        "DELETE FROM clients WHERE id = ? AND username = ?",
        (client_id, user)
    )
    conn.commit()

    return RedirectResponse("/clients", status_code=303)


@app.post("/generate")
async def generate(
    request: Request,
    client: str = Form(...),
    description: str = Form(...),
    tva_rate: float = Form(...),
    photos: list[UploadFile] | None = File(None)
):
    user = get_current_user(request)
    if not user:
        return RedirectResponse("/login", status_code=303)

    record = get_user_record(user)
    settings = get_or_create_settings(user)
    quotes_count = get_quotes_count(user)
    plan_limit = get_plan_limit(record[3])

    if quotes_count >= plan_limit:
        return RedirectResponse("/dashboard", status_code=303)

    saved_files = []
    saved_paths = []

    if photos:
        for photo in photos:
            if photo and photo.filename:
                safe_name = f"{uuid.uuid4().hex}_{photo.filename}"
                photo_path = os.path.join(UPLOAD_DIR, safe_name)
                with open(photo_path, "wb") as f:
                    f.write(await photo.read())
                saved_files.append(safe_name)
                saved_paths.append(photo_path)

    vision_data = analyze_images_with_openai(description, saved_paths)
    vision_summary = vision_data.get("summary", "") if vision_data else ""

    lines, labor, total_ht, details, trade, confidence, merged_description = estimate_quote_real(
        description=description,
        vision_data=vision_data
    )

    tva = round(total_ht * tva_rate, 2)
    total_ttc = round(total_ht + tva, 2)

    photos_text = ", ".join(saved_files)
    quote_number = generate_quote_number(user)
    created_at = datetime.now().strftime("%d/%m/%Y")

    pdf_name = create_pdf(
        settings=settings,
        quote_number=quote_number,
        created_at=created_at,
        client=client,
        description=merged_description,
        details=details,
        lines=lines,
        labor=labor,
        total_ht=total_ht,
        tva=tva,
        total_ttc=total_ttc,
        trade=trade,
        confidence=confidence,
        vision_summary=vision_summary
    )

    cursor.execute(
        """
        INSERT INTO quotes (
            username, client, description, total_ht, tva, total_ttc,
            pdf_name, photos, quote_number, created_at, trade, confidence, vision_summary
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            user,
            client,
            merged_description,
            total_ht,
            tva,
            total_ttc,
            pdf_name,
            photos_text,
            quote_number,
            created_at,
            trade,
            confidence,
            vision_summary
        )
    )
    conn.commit()

    return render_template(request, "result.html", {
        "client": client,
        "description": merged_description,
        "details": details,
        "lines": lines,
        "labor": labor,
        "total_ht": total_ht,
        "tva": tva,
        "total_ttc": total_ttc,
        "pdf_name": pdf_name,
        "trade": trade,
        "quote_number": quote_number,
        "created_at": created_at,
        "confidence": confidence,
        "vision_summary": vision_summary
    })


@app.get("/quote/edit/{quote_id}")
def edit_quote_page(quote_id: int, request: Request):
    user = get_current_user(request)
    if not user:
        return RedirectResponse("/login", status_code=303)

    quote = cursor.execute(
        """
        SELECT id, client, description, total_ht, tva, total_ttc
        FROM quotes
        WHERE id = ? AND username = ?
        """,
        (quote_id, user)
    ).fetchone()

    if not quote:
        return RedirectResponse("/dashboard", status_code=303)

    return render_template(request, "edit_quote.html", {
        "quote": quote
    })


@app.post("/quote/edit/{quote_id}")
def edit_quote_submit(
    quote_id: int,
    request: Request,
    client: str = Form(...),
    description: str = Form(...),
    total_ht: float = Form(...),
    tva: float = Form(...),
    total_ttc: float = Form(...)
):
    user = get_current_user(request)
    if not user:
        return RedirectResponse("/login", status_code=303)

    cursor.execute(
        """
        UPDATE quotes
        SET client = ?, description = ?, total_ht = ?, tva = ?, total_ttc = ?
        WHERE id = ? AND username = ?
        """,
        (client, description, total_ht, tva, total_ttc, quote_id, user)
    )
    conn.commit()

    return RedirectResponse("/dashboard", status_code=303)


@app.get("/delete-quote/{quote_id}")
def delete_quote(quote_id: int, request: Request):
    user = get_current_user(request)
    if not user:
        return RedirectResponse("/login", status_code=303)

    quote = cursor.execute(
        "SELECT pdf_name FROM quotes WHERE id = ? AND username = ?",
        (quote_id, user)
    ).fetchone()

    if quote:
        pdf_path = os.path.join(PDF_DIR, quote[0])
        if os.path.exists(pdf_path):
            os.remove(pdf_path)

        cursor.execute(
            "DELETE FROM quotes WHERE id = ? AND username = ?",
            (quote_id, user)
        )
        conn.commit()

    return RedirectResponse("/dashboard", status_code=303)


@app.get("/pdf/{filename}")
def get_pdf(filename: str, request: Request):
    user = get_current_user(request)
    if not user:
        return RedirectResponse("/login", status_code=303)

    path = os.path.join(PDF_DIR, filename)
    return FileResponse(path, media_type="application/pdf", filename=filename)


@app.get("/admin")
def admin_page(request: Request):
    user = get_current_user(request)
    if not user:
        return RedirectResponse("/login", status_code=303)

    record = get_user_record(user)
    if not record or record[4] != 1:
        return RedirectResponse("/dashboard", status_code=303)

    users = cursor.execute(
        "SELECT username, plan, is_admin FROM users ORDER BY id DESC"
    ).fetchall()

    contacts = cursor.execute(
        "SELECT name, email, message FROM contacts ORDER BY id DESC"
    ).fetchall()

    quotes_total = cursor.execute("SELECT COUNT(*) FROM quotes").fetchone()[0]
    clients_total = cursor.execute("SELECT COUNT(*) FROM clients").fetchone()[0]
    users_total = cursor.execute("SELECT COUNT(*) FROM users").fetchone()[0]

    return render_template(request, "admin.html", {
        "users": users,
        "contacts": contacts,
        "quotes_total": quotes_total,
        "clients_total": clients_total,
        "users_total": users_total
    })


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="127.0.0.1", port=8000)
@app.get("/test-openai")
def test_openai():

    import os
    from openai import OpenAI

    api_key = os.getenv("OPENAI_API_KEY")

    if not api_key:
        return {"status": "clé OpenAI non trouvée"}

    try:

        client = OpenAI(api_key=api_key)

        response = client.responses.create(
            model="gpt-5.4-mini",
            input="Dis simplement : connexion OpenAI réussie"
        )

        return {
            "status": "OpenAI connecté",
            "response": response.output_text
        }

    except Exception as e:

        return {
            "status": "erreur OpenAI",
            "error": str(e)
        }