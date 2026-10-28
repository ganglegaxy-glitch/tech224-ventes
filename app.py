"""TECH224 Ventes — outil de gestion pour commerçants (Guinée).
Version autonome et déployable, écrite depuis zéro.
FR, GNF, mobile-first. Données de production : vides au départ.
Aucune donnée de démonstration, aucun paiement simulé.
"""
import os
import re
import secrets
from datetime import datetime, timedelta, timezone

from flask import Flask, g, jsonify, redirect, render_template_string, request, session, url_for
from werkzeug.security import check_password_hash, generate_password_hash

app = Flask(__name__)
app.secret_key = os.environ.get("SECRET_KEY", "dev-only-change-me")
if app.secret_key == "dev-only-change-me" and os.environ.get("FLASK_ENV") == "production":
    raise RuntimeError("SECRET_KEY doit être défini en production (voir .env.example)")

DATABASE_URL = os.environ.get("DATABASE_URL", "")
IS_POSTGRES = DATABASE_URL.startswith(("postgres://", "postgresql://"))
DATABASE_PATH = os.environ.get("DATABASE_PATH") or os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "data", "tech224.db"
)
PLANS = {
    "Essentiel": 79200,
    "Croissance": 199200,
    "Entreprise": None,  # sur devis
}
PAYMENT_METHODS = ["espèces", "Mobile Money", "virement", "carte"]

# ---------------- Base de données ----------------

def get_conn():
    if IS_POSTGRES:
        import psycopg2
        import psycopg2.extras
        conn = psycopg2.connect(DATABASE_URL, cursor_factory=psycopg2.extras.RealDictCursor)
        return conn
    import sqlite3
    os.makedirs(os.path.dirname(DATABASE_PATH), exist_ok=True)
    conn = sqlite3.connect(DATABASE_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def sql_q(sql):
    """Convertit les ? SQLite en %s pour Postgres."""
    return sql.replace("?", "%s") if IS_POSTGRES else sql


def run(conn, sql, params=(), fetch=None):
    cur = conn.cursor()
    cur.execute(sql_q(sql), params)
    if fetch == "one":
        row = cur.fetchone()
        return dict(row) if row else None
    if fetch == "all":
        return [dict(r) for r in cur.fetchall()]
    if fetch == "id":
        if IS_POSTGRES:
            row = cur.fetchone()
            return row["id"] if row else None
        return cur.lastrowid
    return cur


def insert(conn, sql, params=()):
    if IS_POSTGRES:
        return run(conn, sql + " RETURNING id", params, fetch="id")
    return run(conn, sql, params, fetch="id")


SCHEMA_SQLITE = """
CREATE TABLE IF NOT EXISTS merchants (
  id INTEGER PRIMARY KEY AUTOINCREMENT, phone TEXT UNIQUE NOT NULL,
  pin_hash TEXT NOT NULL, plan TEXT NOT NULL DEFAULT 'Essentiel',
  trial_ends_at TEXT NOT NULL, created_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS sessions (
  token TEXT PRIMARY KEY, merchant_id INTEGER NOT NULL, created_at TEXT NOT NULL,
  FOREIGN KEY (merchant_id) REFERENCES merchants(id) ON DELETE CASCADE);
CREATE TABLE IF NOT EXISTS shops (
  id INTEGER PRIMARY KEY AUTOINCREMENT, merchant_id INTEGER NOT NULL, name TEXT NOT NULL,
  created_at TEXT NOT NULL, FOREIGN KEY (merchant_id) REFERENCES merchants(id) ON DELETE CASCADE);
CREATE TABLE IF NOT EXISTS products (
  id INTEGER PRIMARY KEY AUTOINCREMENT, merchant_id INTEGER NOT NULL, shop_id INTEGER NOT NULL,
  name TEXT NOT NULL, price_gnf INTEGER NOT NULL, stock INTEGER NOT NULL DEFAULT 0,
  low_stock_threshold INTEGER NOT NULL DEFAULT 5, created_at TEXT NOT NULL,
  FOREIGN KEY (merchant_id) REFERENCES merchants(id) ON DELETE CASCADE,
  FOREIGN KEY (shop_id) REFERENCES shops(id) ON DELETE CASCADE);
CREATE TABLE IF NOT EXISTS customers (
  id INTEGER PRIMARY KEY AUTOINCREMENT, merchant_id INTEGER NOT NULL, shop_id INTEGER,
  name TEXT NOT NULL, phone TEXT NOT NULL DEFAULT '', created_at TEXT NOT NULL,
  FOREIGN KEY (merchant_id) REFERENCES merchants(id) ON DELETE CASCADE,
  FOREIGN KEY (shop_id) REFERENCES shops(id) ON DELETE SET NULL);
CREATE TABLE IF NOT EXISTS sales (
  id INTEGER PRIMARY KEY AUTOINCREMENT, merchant_id INTEGER NOT NULL, shop_id INTEGER NOT NULL,
  customer_id INTEGER, payment_method TEXT NOT NULL, total_gnf INTEGER NOT NULL,
  reference TEXT NOT NULL, created_at TEXT NOT NULL,
  FOREIGN KEY (merchant_id) REFERENCES merchants(id) ON DELETE CASCADE,
  FOREIGN KEY (shop_id) REFERENCES shops(id) ON DELETE CASCADE,
  FOREIGN KEY (customer_id) REFERENCES customers(id) ON DELETE SET NULL);
CREATE TABLE IF NOT EXISTS sale_items (
  id INTEGER PRIMARY KEY AUTOINCREMENT, sale_id INTEGER NOT NULL, product_id INTEGER NOT NULL,
  quantity INTEGER NOT NULL, unit_price_gnf INTEGER NOT NULL, line_total_gnf INTEGER NOT NULL,
  FOREIGN KEY (sale_id) REFERENCES sales(id) ON DELETE CASCADE,
  FOREIGN KEY (product_id) REFERENCES products(id) ON DELETE CASCADE);
CREATE UNIQUE INDEX IF NOT EXISTS idx_sales_ref ON sales(reference);
"""

SCHEMA_PG = SCHEMA_SQLITE.replace("INTEGER PRIMARY KEY AUTOINCREMENT", "SERIAL PRIMARY KEY")


def init_db():
    conn = get_conn()
    try:
        for stmt in (SCHEMA_PG if IS_POSTGRES else SCHEMA_SQLITE).split(";"):
            if stmt.strip():
                run(conn, stmt)
        conn.commit()
    finally:
        conn.close()


def now_iso():
    return datetime.now(timezone.utc).isoformat()


def today_utc():
    return datetime.now(timezone.utc).date()


# ---------------- Authentification / isolation ----------------

def current_merchant():
    """Retourne le commerçant authentifié (token Bearer ou session), sinon None."""
    auth = request.headers.get("Authorization", "")
    token = auth[7:] if auth.startswith("Bearer ") else None
    if token:
        conn = get_conn()
        try:
            row = run(conn, "SELECT m.* FROM merchants m JOIN sessions s ON s.merchant_id=m.id WHERE s.token=?", (token,), fetch="one")
            return row
        finally:
            conn.close()
    mid = session.get("merchant_id")
    if not mid:
        return None
    conn = get_conn()
    try:
        return run(conn, "SELECT * FROM merchants WHERE id=?", (mid,), fetch="one")
    finally:
        conn.close()


def require_merchant():
    m = current_merchant()
    if not m:
        return None, (jsonify({"error": "authentification requise"}), 401)
    return m, None


def merchant_view(m):
    trial = datetime.fromisoformat(m["trial_ends_at"]).date()
    remaining = max(0, (trial - today_utc()).days)
    return {"id": m["id"], "phone": m["phone"], "plan": m["plan"],
            "plan_price_gnf": PLANS.get(m["plan"]), "trial_ends_at": m["trial_ends_at"],
            "trial_days_remaining": remaining}


def get_shop(conn, merchant_id, shop_id):
    return run(conn, "SELECT * FROM shops WHERE id=? AND merchant_id=?", (shop_id, merchant_id), fetch="one")


def get_product(conn, merchant_id, product_id):
    return run(conn, "SELECT * FROM products WHERE id=? AND merchant_id=?", (product_id, merchant_id), fetch="one")


def get_customer(conn, merchant_id, customer_id):
    return run(conn, "SELECT * FROM customers WHERE id=? AND merchant_id=?", (customer_id, merchant_id), fetch="one")


def get_sale(conn, merchant_id, sale_id):
    return run(conn, "SELECT * FROM sales WHERE id=? AND merchant_id=?", (sale_id, merchant_id), fetch="one")

# ---------------- API JSON ----------------

@app.get("/health")
def health():
    return jsonify({"ok": True, "service": "tech224-ventes"})


@app.post("/api/register")
def api_register():
    d = request.get_json(force=True, silent=True) or {}
    phone = str(d.get("phone", "")).strip()
    pin = str(d.get("pin", ""))
    plan = d.get("plan", "Essentiel")
    if not re.fullmatch(r"\+?\d{8,15}", phone) or len(pin) < 4:
        return jsonify({"error": "téléphone ou code PIN invalide (PIN : 4 chiffres minimum)"}), 400
    if plan not in PLANS:
        return jsonify({"error": "formule inconnue"}), 400
    conn = get_conn()
    try:
        if run(conn, "SELECT id FROM merchants WHERE phone=?", (phone,), fetch="one"):
            return jsonify({"error": "ce numéro est déjà inscrit"}), 409
        trial = (datetime.now(timezone.utc) + timedelta(days=14)).isoformat()
        mid = insert(conn, "INSERT INTO merchants (phone, pin_hash, plan, trial_ends_at, created_at) VALUES (?,?,?,?,?)",
                     (phone, generate_password_hash(pin), plan, trial, now_iso()))
        token = secrets.token_hex(32)
        run(conn, "INSERT INTO sessions (token, merchant_id, created_at) VALUES (?,?,?)", (token, mid, now_iso()))
        conn.commit()
        m = run(conn, "SELECT * FROM merchants WHERE id=?", (mid,), fetch="one")
        return jsonify({"merchant": merchant_view(m), "token": token}), 201
    finally:
        conn.close()


@app.post("/api/login")
def api_login():
    d = request.get_json(force=True, silent=True) or {}
    conn = get_conn()
    try:
        m = run(conn, "SELECT * FROM merchants WHERE phone=?", (str(d.get("phone", "")).strip(),), fetch="one")
        if not m or not check_password_hash(m["pin_hash"], str(d.get("pin", ""))):
            return jsonify({"error": "téléphone ou code PIN incorrect"}), 401
        token = secrets.token_hex(32)
        run(conn, "INSERT INTO sessions (token, merchant_id, created_at) VALUES (?,?,?)", (token, m["id"], now_iso()))
        conn.commit()
        session["merchant_id"] = m["id"]
        return jsonify({"merchant": merchant_view(m), "token": token})
    finally:
        conn.close()


@app.post("/api/logout")
def api_logout():
    auth = request.headers.get("Authorization", "")
    if auth.startswith("Bearer "):
        conn = get_conn()
        try:
            run(conn, "DELETE FROM sessions WHERE token=?", (auth[7:],)); conn.commit()
        finally:
            conn.close()
    session.pop("merchant_id", None)
    return jsonify({"ok": True})


@app.get("/api/me")
def api_me():
    m, err = require_merchant()
    return (jsonify({"merchant": merchant_view(m)}), 200) if m else err


@app.post("/api/me/plan")
def api_plan():
    m, err = require_merchant()
    if err: return err
    plan = (request.get_json(force=True, silent=True) or {}).get("plan")
    if plan not in PLANS:
        return jsonify({"error": "formule inconnue"}), 400
    conn = get_conn()
    try:
        run(conn, "UPDATE merchants SET plan=? WHERE id=?", (plan, m["id"])); conn.commit()
    finally:
        conn.close()
    m["plan"] = plan
    return jsonify({"merchant": merchant_view(m)})


@app.post("/api/shops")
def api_create_shop():
    m, err = require_merchant()
    if err: return err
    name = str((request.get_json(force=True, silent=True) or {}).get("name", "")).strip()
    if not name:
        return jsonify({"error": "nom de boutique requis"}), 400
    conn = get_conn()
    try:
        sid = insert(conn, "INSERT INTO shops (merchant_id, name, created_at) VALUES (?,?,?)", (m["id"], name, now_iso()))
        conn.commit()
        return jsonify(run(conn, "SELECT * FROM shops WHERE id=?", (sid,), fetch="one")), 201
    finally:
        conn.close()


@app.get("/api/shops")
def api_list_shops():
    m, err = require_merchant()
    if err: return err
    conn = get_conn()
    try:
        return jsonify(run(conn, "SELECT * FROM shops WHERE merchant_id=? ORDER BY id", (m["id"],), fetch="all"))
    finally:
        conn.close()


@app.get("/api/shops/<int:shop_id>")
def api_get_shop(shop_id):
    m, err = require_merchant()
    if err: return err
    conn = get_conn()
    try:
        s = get_shop(conn, m["id"], shop_id)
        return (jsonify(s), 200) if s else (jsonify({"error": "boutique introuvable"}), 404)
    finally:
        conn.close()


@app.post("/api/shops/<int:shop_id>/products")
def api_create_product(shop_id):
    m, err = require_merchant()
    if err: return err
    d = request.get_json(force=True, silent=True) or {}
    conn = get_conn()
    try:
        if not get_shop(conn, m["id"], shop_id):
            return jsonify({"error": "boutique introuvable"}), 404
        name = str(d.get("name", "")).strip()
        price, stock, thr = int(d.get("price_gnf", 0)), int(d.get("stock", 0)), int(d.get("low_stock_threshold", 5))
        if not name or price <= 0 or stock < 0 or thr < 0:
            return jsonify({"error": "nom, prix > 0 et stock >= 0 requis"}), 400
        pid = insert(conn, "INSERT INTO products (merchant_id, shop_id, name, price_gnf, stock, low_stock_threshold, created_at) VALUES (?,?,?,?,?,?,?)",
                     (m["id"], shop_id, name, price, stock, thr, now_iso()))
        conn.commit()
        return jsonify(run(conn, "SELECT * FROM products WHERE id=?", (pid,), fetch="one")), 201
    except (ValueError, TypeError):
        return jsonify({"error": "valeurs numériques invalides"}), 400
    finally:
        conn.close()


@app.get("/api/shops/<int:shop_id>/products")
def api_list_products(shop_id):
    m, err = require_merchant()
    if err: return err
    conn = get_conn()
    try:
        if not get_shop(conn, m["id"], shop_id):
            return jsonify({"error": "boutique introuvable"}), 404
        return jsonify(run(conn, "SELECT * FROM products WHERE shop_id=? AND merchant_id=? ORDER BY id", (shop_id, m["id"]), fetch="all"))
    finally:
        conn.close()


@app.route("/api/products/<int:pid>", methods=["PUT", "DELETE"])
def api_product(pid):
    m, err = require_merchant()
    if err: return err
    conn = get_conn()
    try:
        p = get_product(conn, m["id"], pid)
        if not p:
            return jsonify({"error": "produit introuvable"}), 404
        if request.method == "DELETE":
            run(conn, "DELETE FROM products WHERE id=?", (pid,)); conn.commit()
            return jsonify({"ok": True})
        d = request.get_json(force=True, silent=True) or {}
        run(conn, "UPDATE products SET name=?, price_gnf=?, stock=?, low_stock_threshold=? WHERE id=?",
            (str(d.get("name", p["name"])).strip(), int(d.get("price_gnf", p["price_gnf"])),
             int(d.get("stock", p["stock"])), int(d.get("low_stock_threshold", p["low_stock_threshold"])), pid))
        conn.commit()
        return jsonify(run(conn, "SELECT * FROM products WHERE id=?", (pid,), fetch="one"))
    except (ValueError, TypeError):
        return jsonify({"error": "valeurs numériques invalides"}), 400
    finally:
        conn.close()


@app.post("/api/customers")
def api_create_customer():
    m, err = require_merchant()
    if err: return err
    d = request.get_json(force=True, silent=True) or {}
    name = str(d.get("name", "")).strip()
    if not name:
        return jsonify({"error": "nom du client requis"}), 400
    shop_id = d.get("shop_id")
    conn = get_conn()
    try:
        if shop_id and not get_shop(conn, m["id"], int(shop_id)):
            return jsonify({"error": "boutique introuvable"}), 404
        cid = insert(conn, "INSERT INTO customers (merchant_id, shop_id, name, phone, created_at) VALUES (?,?,?,?,?)",
                     (m["id"], shop_id, name, str(d.get("phone", "")).strip(), now_iso()))
        conn.commit()
        return jsonify(run(conn, "SELECT * FROM customers WHERE id=?", (cid,), fetch="one")), 201
    finally:
        conn.close()


@app.get("/api/customers")
def api_list_customers():
    m, err = require_merchant()
    if err: return err
    conn = get_conn()
    try:
        return jsonify(run(conn, "SELECT * FROM customers WHERE merchant_id=? ORDER BY id", (m["id"],), fetch="all"))
    finally:
        conn.close()


@app.get("/api/customers/<int:cid>")
def api_get_customer(cid):
    m, err = require_merchant()
    if err: return err
    conn = get_conn()
    try:
        c = get_customer(conn, m["id"], cid)
        if not c:
            return jsonify({"error": "client introuvable"}), 404
        c["sales"] = run(conn, "SELECT * FROM sales WHERE customer_id=? AND merchant_id=? ORDER BY id DESC", (cid, m["id"]), fetch="all")
        return jsonify(c)
    finally:
        conn.close()


@app.route("/api/customers/<int:cid>", methods=["PUT", "DELETE"])
def api_customer(cid):
    m, err = require_merchant()
    if err: return err
    conn = get_conn()
    try:
        c = get_customer(conn, m["id"], cid)
        if not c:
            return jsonify({"error": "client introuvable"}), 404
        if request.method == "DELETE":
            run(conn, "UPDATE sales SET customer_id=NULL WHERE customer_id=?", (cid,))
            run(conn, "DELETE FROM customers WHERE id=?", (cid,)); conn.commit()
            return jsonify({"ok": True})
        d = request.get_json(force=True, silent=True) or {}
        run(conn, "UPDATE customers SET name=?, phone=? WHERE id=?",
            (str(d.get("name", c["name"])).strip(), str(d.get("phone", c["phone"])).strip(), cid))
        conn.commit()
        return jsonify(run(conn, "SELECT * FROM customers WHERE id=?", (cid,), fetch="one"))
    finally:
        conn.close()

@app.post("/api/sales")
def api_create_sale():
    m, err = require_merchant()
    if err: return err
    d = request.get_json(force=True, silent=True) or {}
    try:
        shop_id, items = int(d["shop_id"]), d.get("items", [])
        method = d.get("payment_method", "")
        customer_id = d.get("customer_id")
    except (KeyError, ValueError, TypeError):
        return jsonify({"error": "boutique, articles et moyen de paiement requis"}), 400
    if method not in PAYMENT_METHODS or not items:
        return jsonify({"error": "moyen de paiement invalide ou panier vide"}), 400
    conn = get_conn()
    try:
        if not get_shop(conn, m["id"], shop_id):
            return jsonify({"error": "boutique introuvable"}), 404
        if customer_id and not get_customer(conn, m["id"], int(customer_id)):
            return jsonify({"error": "client introuvable"}), 404
        # Total calculé côté serveur, jamais celui envoyé par le client.
        lines, total = [], 0
        for it in items:
            p = get_product(conn, m["id"], int(it["product_id"]))
            qty = int(it["quantity"])
            if not p or p["shop_id"] != shop_id or qty <= 0:
                return jsonify({"error": "article invalide"}), 400
            if p["stock"] < qty:
                return jsonify({"error": f"stock insuffisant pour {p['name']}"}), 400
            lines.append((p, qty)); total += p["price_gnf"] * qty
        count = run(conn, "SELECT COUNT(*) AS n FROM sales WHERE merchant_id=?", (m["id"],), fetch="one")["n"]
        ref = f"T224-{m['id']}-{count + 1:04d}"
        sid = insert(conn, "INSERT INTO sales (merchant_id, shop_id, customer_id, payment_method, total_gnf, reference, created_at) VALUES (?,?,?,?,?,?,?)",
                     (m["id"], shop_id, customer_id, method, total, ref, now_iso()))
        for p, qty in lines:
            run(conn, "INSERT INTO sale_items (sale_id, product_id, quantity, unit_price_gnf, line_total_gnf) VALUES (?,?,?,?,?)",
                (sid, p["id"], qty, p["price_gnf"], p["price_gnf"] * qty))
            run(conn, "UPDATE products SET stock=stock-? WHERE id=?", (qty, p["id"]))
        conn.commit()
        sale = run(conn, "SELECT * FROM sales WHERE id=?", (sid,), fetch="one")
        sale["items"] = run(conn, "SELECT * FROM sale_items WHERE sale_id=?", (sid,), fetch="all")
        return jsonify(sale), 201
    except (ValueError, TypeError, KeyError):
        return jsonify({"error": "articles invalides"}), 400
    finally:
        conn.close()


@app.get("/api/sales/<int:sid>")
def api_get_sale(sid):
    m, err = require_merchant()
    if err: return err
    conn = get_conn()
    try:
        s = get_sale(conn, m["id"], sid)
        if not s:
            return jsonify({"error": "vente introuvable"}), 404
        s["items"] = run(conn, "SELECT si.*, p.name AS product_name FROM sale_items si JOIN products p ON p.id=si.product_id WHERE si.sale_id=?", (sid,), fetch="all")
        return jsonify(s)
    finally:
        conn.close()


@app.delete("/api/sales/<int:sid>")
def api_delete_sale(sid):
    m, err = require_merchant()
    if err: return err
    conn = get_conn()
    try:
        s = get_sale(conn, m["id"], sid)
        if not s:
            return jsonify({"error": "vente introuvable"}), 404
        items = run(conn, "SELECT * FROM sale_items WHERE sale_id=?", (sid,), fetch="all")
        for it in items:
            run(conn, "UPDATE products SET stock=stock+? WHERE id=?", (it["quantity"], it["product_id"]))
        run(conn, "DELETE FROM sale_items WHERE sale_id=?", (sid,))
        run(conn, "DELETE FROM sales WHERE id=?", (sid,)); conn.commit()
        return jsonify({"ok": True, "stock_restored": True})
    finally:
        conn.close()


def _report(conn, merchant_id, days):
    since = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
    row = run(conn, "SELECT COUNT(*) AS n, COALESCE(SUM(total_gnf),0) AS total FROM sales WHERE merchant_id=? AND created_at>=?", (merchant_id, since), fetch="one")
    return {"days": days, "sales_count": row["n"], "total_gnf": row["total"]}


@app.get("/api/dashboard")
def api_dashboard():
    m, err = require_merchant()
    if err: return err
    conn = get_conn()
    try:
        today = today_utc().isoformat()
        row = run(conn, "SELECT COUNT(*) AS n, COALESCE(SUM(total_gnf),0) AS total FROM sales WHERE merchant_id=? AND substr(created_at,1,10)=?", (m["id"], today), fetch="one")
        low = run(conn, "SELECT * FROM products WHERE merchant_id=? AND stock<=low_stock_threshold ORDER BY stock", (m["id"],), fetch="all")
        chart = []
        for i in range(6, -1, -1):
            day = (today_utc() - timedelta(days=i)).isoformat()
            r = run(conn, "SELECT COALESCE(SUM(total_gnf),0) AS total FROM sales WHERE merchant_id=? AND substr(created_at,1,10)=?", (m["id"], day), fetch="one")
            chart.append({"date": day, "total_gnf": r["total"]})
        return jsonify({"merchant": merchant_view(m), "today_revenue_gnf": row["total"],
                        "today_sales_count": row["n"], "low_stock": low, "last_7_days": chart})
    finally:
        conn.close()


@app.get("/api/reports")
def api_reports():
    m, err = require_merchant()
    if err: return err
    try:
        days = int(request.args.get("days", "7"))
    except ValueError:
        return jsonify({"error": "days invalide"}), 400
    if days not in (7, 30, 90):
        return jsonify({"error": "choisir 7, 30 ou 90 jours"}), 400
    conn = get_conn()
    try:
        return jsonify(_report(conn, m["id"], days))
    finally:
        conn.close()

# ---------------- Interface web mobile-first ----------------
BASE = """<!doctype html><html lang="fr"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>TECH224 Ventes</title><style>
body{font-family:system-ui,sans-serif;margin:0;padding:16px;background:#f6f7f9;color:#111;max-width:640px;margin-inline:auto}
h1{font-size:22px}h2{font-size:18px}input,select,button{width:100%;padding:12px;margin:6px 0;border:1px solid #ccc;border-radius:10px;font-size:16px;box-sizing:border-box}
button{background:#0b5cff;color:#fff;border:none;font-weight:700} a{color:#0b5cff}
.card{background:#fff;padding:14px;border-radius:12px;margin:10px 0;box-shadow:0 1px 4px #0001}
.banner{background:#fff3cd;padding:12px;border-radius:10px}.danger{background:#c00}
</style></head><body><h1>TECH224 Ventes</h1><p><a href="/dashboard">Tableau de bord</a> · <a href="/pos">Caisse</a> · <a href="/shops">Boutiques</a> · <a href="/customers">Clients</a> · <a href="/reports">Rapports</a></p>{{body|safe}}</body></html>"""


def page(body):
    return render_template_string(BASE, body=body)


HOME = """<!doctype html><html lang="fr"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>TECH224 Ventes — Gérez plus de ventes</title><style>
*{box-sizing:border-box}body{margin:0;font-family:system-ui,-apple-system,Segoe UI,Roboto,sans-serif;background:#f7f1e3;color:#1d2b22;line-height:1.55}
a{color:inherit}.wrap{max-width:1120px;margin:0 auto;padding:0 18px}
.topbar{background:#123524;color:#f7f1e3;font-size:14px}.topbar .wrap{display:flex;gap:12px;justify-content:space-between;flex-wrap:wrap;padding-top:9px;padding-bottom:9px}.topbar strong{color:#ffb27a}
header.site{position:sticky;top:0;background:#fbf7ec;border-bottom:1px solid #e5dcc3;z-index:10}header.site .wrap{display:flex;align-items:center;gap:18px;flex-wrap:wrap;padding-top:14px;padding-bottom:14px}
.logo{font-family:Georgia,serif;font-weight:700;letter-spacing:.06em;font-size:20px;color:#123524}.logo small{display:block;font-size:11px;letter-spacing:.22em;color:#c75a1a}
nav{margin-left:auto;display:flex;gap:14px;flex-wrap:wrap;font-size:14px}nav a{text-decoration:none}
.btn{display:inline-block;text-decoration:none;border-radius:999px;padding:12px 20px;font-weight:700;border:2px solid #c75a1a}.btn-primary{background:#c75a1a;color:#fff}.btn-ghost{color:#123524}.btn-light{background:#fff;color:#123524;border-color:#fff}.btn-dark{background:#123524;color:#fff;border-color:#123524}
.eyebrow{color:#c75a1a;text-transform:uppercase;letter-spacing:.18em;font-size:12px;font-weight:800}
h1,h2{font-family:Georgia,serif;line-height:1.15;color:#123524}h1{font-size:clamp(34px,6vw,58px);margin:14px 0}h1 em{color:#c75a1a;font-style:italic}h2{font-size:clamp(26px,4vw,38px);text-align:center;margin:0 0 12px}h3{margin:0 0 8px;color:#123524}
section{padding:56px 0}.lead{font-size:18px;max-width:760px}.center{text-align:center}.center .lead{margin:0 auto}
.hero{background:#f7f1e3}.hero .actions,.cta .actions{display:flex;gap:12px;flex-wrap:wrap;margin:22px 0}.ticks{display:flex;gap:18px;flex-wrap:wrap;font-size:14px;font-weight:600}
.dark{background:#123524;color:#f7f1e3}.dark h2{color:#fff}.dark .eyebrow{color:#ffb27a}
.grid{display:grid;grid-template-columns:1fr;gap:16px;margin-top:30px}.card{background:#fff;border-radius:18px;padding:22px;box-shadow:0 8px 24px rgba(18,53,36,.08)}.dark .card{background:#1d4632;color:#f7f1e3}.dark .card h3{color:#fff}.icon{font-size:26px}
.mock{background:#fff;border-radius:20px;padding:18px;box-shadow:0 12px 30px rgba(18,53,36,.14);max-width:820px;margin:28px auto 0}.mock .tabs{display:flex;gap:8px;flex-wrap:wrap}.mock .tabs span{padding:8px 14px;border-radius:999px;background:#f0e9d8}.mock .tabs span.on{background:#123524;color:#fff}.mock table{width:100%;border-collapse:collapse;margin-top:16px}.mock th,.mock td{text-align:left;padding:10px;border-bottom:1px solid #eee;font-size:14px}.note{font-size:13px;color:#5a6a60;text-align:center}
table.compare{width:100%;border-collapse:collapse;background:#fff;border-radius:18px;overflow:hidden;margin-top:28px}table.compare th,table.compare td{padding:14px;text-align:left;border-bottom:1px solid #eee}table.compare th{background:#123524;color:#fff}
.steps{display:grid;grid-template-columns:1fr;gap:14px;margin-top:28px}.step{background:#fff;border-radius:16px;padding:20px}.step b{color:#c75a1a;font-size:22px}
.price{border:2px solid #e5dcc3}.price.hot{border-color:#c75a1a}.price .amount{font-family:Georgia,serif;font-size:30px;color:#123524}.price .per{font-size:14px;color:#5a6a60}
.faq{display:grid;grid-template-columns:1fr;gap:14px;margin-top:28px}.faq details{background:#fff;border-radius:14px;padding:16px 18px}.faq summary{font-weight:700;cursor:pointer}
.cta{background:#123524;color:#fff;text-align:center}.cta h2{color:#fff}
footer{background:#0d2a1c;color:#d9e2d6;padding:40px 0}footer .cols{display:grid;grid-template-columns:1fr;gap:24px}footer a{text-decoration:none}footer .fine{border-top:1px solid #2a4a3a;margin-top:28px;padding-top:18px;font-size:13px}
@media(min-width:720px){.grid,.steps{grid-template-columns:1fr 1fr 1fr}.faq{grid-template-columns:1fr 1fr}footer .cols{grid-template-columns:1.4fr 1fr 1fr}}
</style></head><body>
<div class="topbar"><div class="wrap"><span><strong>14 jours d'essai offerts</strong></span><span>Téléphone : +224 624 683 196</span></div></div>
<header class="site"><div class="wrap"><div class="logo">TECH224 VENTES<small>CAISSE</small></div>
<nav><a href="#fonctionnalites">Fonctionnalités</a><a href="#produit">Le produit</a><a href="#tarifs">Tarifs</a><a href="#faq">FAQ</a></nav>
<a class="btn btn-ghost" href="/login">Se connecter</a><a class="btn btn-primary" href="/register">Essai 14 jours</a></div></header>
<main>
<section class="hero"><div class="wrap"><p class="eyebrow">Pour les commerces d'Afrique de l'Ouest</p>
<h1>Gérez plus de <em>ventes</em>, sans plus de paperasse.</h1>
<p class="lead">TECH224 Ventes rassemble votre caisse, votre stock, votre facturation, vos clients et vos rapports — en francs guinéens, depuis votre téléphone ou votre ordinateur.</p>
<div class="actions"><a class="btn btn-primary" href="/register">Démarrer l'essai gratuit</a><a class="btn btn-ghost" href="#produit">Voir le produit</a></div>
<p class="ticks"><span>✓ Sans carte bancaire</span><span>✓ Données hébergées et sauvegardées</span><span>✓ 14 jours d'essai</span></p></div></section>
<section id="fonctionnalites"><div class="wrap"><div class="center"><p class="eyebrow">Fonctionnalités</p><h2>Tout ce qu'il faut pour vendre, au même endroit</h2><p class="lead">Chaque outil correspond à une action réelle de l'application : enregistrer, suivre et retrouver vos ventes.</p></div>
<div class="grid">
<div class="card"><div class="icon">🧾</div><h3>Une caisse rapide</h3><p>Ajoutez des produits au panier, choisissez le moyen de paiement et encaissez. Le total est calculé par le serveur, et le stock est mis à jour à chaque vente.</p></div>
<div class="card"><div class="icon">📦</div><h3>Un stock intelligent</h3><p>Suivez les quantités disponibles et repérez les produits proches de la rupture grâce à votre seuil d'alerte, boutique par boutique.</p></div>
<div class="card"><div class="icon">🧮</div><h3>Facturation et suivi</h3><p>Chaque vente reçoit une référence numérotée. Retrouvez-la ensuite, et supprimez-la si besoin : le stock est alors restauré.</p></div>
<div class="card"><div class="icon">👥</div><h3>Des fiches clients</h3><p>Enregistrez vos clients avec leur téléphone et retrouvez l'historique de leurs achats, sans carnet séparé.</p></div>
<div class="card"><div class="icon">📊</div><h3>Des rapports clairs</h3><p>Consultez le chiffre du jour, les ventes récentes et des totaux sur 7, 30 ou 90 jours, pour décider en connaissance de cause.</p></div>
<div class="card"><div class="icon">🏪</div><h3>Plusieurs boutiques</h3><p>Créez plusieurs boutiques sous le même compte commerçant. Chaque boutique garde ses propres produits, ventes et clients.</p></div>
</div></div></section>
<section id="produit" class="dark"><div class="wrap"><div class="center"><p class="eyebrow">Le produit</p><h2>Une caisse pensée pour le comptoir</h2><p class="lead">Des écrans simples, lisibles sur téléphone, pour enregistrer une vente sans formation longue.</p></div>
<div class="mock"><div class="tabs"><span class="on">Ventes</span><span>Stock</span><span>Clients</span></div>
<table><tr><th>Article</th><th>Prix</th><th>Stock</th></tr><tr><td>Votre produit</td><td>— GNF</td><td>—</td></tr><tr><td colspan="3">Aucune vente pour le moment : vos produits et vos ventes apparaîtront ici.</td></tr></table></div>
<p class="note">Illustration de l'interface — elle ne présente ni vente réelle, ni client réel.</p></div></section>
<section><div class="wrap"><div class="center"><p class="eyebrow">Bénéfices</p><h2>Moins de papier, plus de visibilité</h2></div>
<table class="compare"><tr><th>Avant</th><th>Avec TECH224 Ventes</th></tr>
<tr><td>Les ventes sont notées à la main, dans plusieurs cahiers.</td><td>Chaque vente est enregistrée, datée et retrouvable par sa référence.</td></tr>
<tr><td>Le stock est vérifié en comptant, au feeling.</td><td>Le stock baisse à chaque vente et les produits en alerte sont visibles.</td></tr>
<tr><td>Il est difficile de savoir ce qui s'est vendu cette semaine.</td><td>Le tableau de bord et les rapports résument les ventes sur 7, 30 ou 90 jours.</td></tr>
<tr><td>Les informations clients sont dispersées.</td><td>Chaque client a une fiche, avec son historique d'achats.</td></tr></table></div></section>
<section class="dark"><div class="wrap"><div class="center"><p class="eyebrow">Démarrer</p><h2>Commencez en cinq étapes</h2></div>
<div class="steps">
<div class="step"><b>1</b><h3>Créez votre compte</h3><p>Inscrivez-vous avec votre numéro de téléphone et choisissez votre code PIN.</p></div>
<div class="step"><b>2</b><h3>Créez votre boutique</h3><p>Donnez un nom à votre boutique. Vous pourrez en ajouter d'autres plus tard.</p></div>
<div class="step"><b>3</b><h3>Ajoutez vos produits</h3><p>Pour chaque produit, indiquez son prix en GNF, son stock et son seuil d'alerte.</p></div>
<div class="step"><b>4</b><h3>Connectez-vous en sécurité</h3><p>Utilisez votre téléphone et votre code PIN pour retrouver votre compte et vos données.</p></div>
<div class="step"><b>5</b><h3>Encaissez votre première vente</h3><p>Depuis la caisse, ajoutez les produits, choisissez le moyen de paiement et validez.</p></div>
</div></div></section>
<section id="tarifs"><div class="wrap"><div class="center"><p class="eyebrow">Tarifs</p><h2>Des formules simples, en francs guinéens</h2><p class="lead">Commencez par 14 jours d'essai, puis choisissez la formule qui correspond à votre activité.</p></div>
<div class="grid">
<div class="card price"><h3>Essentiel</h3><p class="amount">79 200 <span class="per">GNF/mois</span></p><p>Pour démarrer : une boutique, caisse, stock, clients et rapports.</p><a class="btn btn-dark" href="/register">Démarrer l'essai gratuit</a></div>
<div class="card price hot"><h3>Croissance</h3><p class="amount">199 200 <span class="per">GNF/mois</span></p><p>Pour développer votre activité : plusieurs boutiques et un suivi plus large.</p><a class="btn btn-primary" href="/register">Démarrer l'essai gratuit</a></div>
<div class="card price"><h3>Entreprise</h3><p class="amount">Sur devis</p><p>Pour les structures qui ont besoin d'un accompagnement adapté.</p><a class="btn btn-dark" href="https://wa.me/224624683196">Demander un devis</a></div>
</div></div></section>
<section id="faq"><div class="wrap"><div class="center"><p class="eyebrow">Questions fréquentes</p><h2>Ce qu'on nous demande souvent</h2></div>
<div class="faq">
<details><summary>Combien de temps pour démarrer ?</summary><p>Quelques minutes suffisent en général : créer un compte, créer une boutique, ajouter un premier produit, puis encaisser une première vente.</p></details>
<details><summary>L'application fonctionne-t-elle sans connexion ?</summary><p>Non. TECH224 Ventes est une application web : elle a besoin d'une connexion internet pour enregistrer et retrouver vos ventes. Elle ne synchronise pas de ventes hors ligne.</p></details>
<details><summary>Quels moyens de paiement sont acceptés en caisse ?</summary><p>Vous pouvez enregistrer une vente payée en espèces, par Mobile Money, par virement ou par carte. L'application enregistre le moyen choisi ; elle ne débite pas elle-même un compte Mobile Money.</p></details>
<details><summary>Mes données sont-elles en sécurité ?</summary><p>Votre compte est protégé par votre code PIN, qui n'est pas stocké en clair, et chaque commerçant ne voit que ses propres boutiques, produits, ventes et clients.</p></details>
<details><summary>Puis-je changer de formule plus tard ?</summary><p>Oui. Vous pouvez passer d'une formule à l'autre depuis votre compte, selon l'évolution de votre activité.</p></details>
<details><summary>Le support est-il en français ?</summary><p>Oui. L'application est en français, et vous pouvez nous joindre par téléphone ou WhatsApp au +224 624 683 196.</p></details>
</div></div></section>
<section class="cta"><div class="wrap"><p class="eyebrow">Prêt ?</p><h2>Prêt à gérer plus de ventes ?</h2><p class="lead" style="margin:0 auto">Créez votre compte, testez l'outil pendant 14 jours, puis décidez en toute tranquillité.</p>
<div class="actions" style="justify-content:center"><a class="btn btn-light" href="/register">Démarrer l'essai gratuit</a><a class="btn" style="color:#fff;border-color:#fff" href="https://wa.me/224624683196">Nous écrire sur WhatsApp</a></div>
<p>Téléphone / WhatsApp : +224 624 683 196 · Bureau : Cimenterie, Conakry</p></div></section>
</main>
<footer><div class="wrap"><div class="cols"><div><div class="logo" style="color:#fff">TECH224 VENTES</div><p>Un outil de gestion pour commerçants : caisse, stock, facturation, clients et rapports, en francs guinéens.</p></div>
<div><h3 style="color:#fff">Produit</h3><p><a href="#fonctionnalites">Fonctionnalités</a><br><a href="#produit">Le produit</a><br><a href="#tarifs">Tarifs</a><br><a href="#faq">FAQ</a></p></div>
<div><h3 style="color:#fff">Contact</h3><p>Téléphone / WhatsApp : +224 624 683 196<br>Bureau : Cimenterie, Conakry, Guinée<br><a href="/login">Se connecter</a> · <a href="/register">Créer un compte commerçant</a></p></div></div>
<p class="fine">© 2026 TECH224 SARL — Conakry, Guinée · RC GN-2019-B-04421</p></div></footer>
</body></html>"""


@app.get("/")
def index():
    m = current_merchant()
    if m:
        return redirect("/dashboard")
    return render_template_string(HOME)


@app.route("/register", methods=["GET", "POST"])
def ui_register():
    if request.method == "POST":
        conn = get_conn()
        try:
            phone, pin = request.form["phone"].strip(), request.form["pin"]
            if run(conn, "SELECT id FROM merchants WHERE phone=?", (phone,), fetch="one"):
                return page("<p>Ce numéro est déjà inscrit. <a href='/login'>Se connecter</a></p>")
            trial = (datetime.now(timezone.utc) + timedelta(days=14)).isoformat()
            mid = insert(conn, "INSERT INTO merchants (phone, pin_hash, plan, trial_ends_at, created_at) VALUES (?,?,?,?,?)",
                         (phone, generate_password_hash(pin), request.form.get("plan", "Essentiel"), trial, now_iso()))
            conn.commit(); session["merchant_id"] = mid
            return redirect("/dashboard")
        finally:
            conn.close()
    return page("""<div class="card"><h2>Créer un compte</h2><form method="post">
<input name="phone" placeholder="Téléphone, ex. +224624683196" required>
<input name="pin" type="password" placeholder="Code PIN (4 chiffres minimum)" required>
<select name="plan"><option>Essentiel</option><option>Croissance</option><option>Entreprise</option></select>
<button>Créer mon compte — 14 jours d'essai</button></form></div>""")


@app.route("/login", methods=["GET", "POST"])
def ui_login():
    if request.method == "POST":
        conn = get_conn()
        try:
            m = run(conn, "SELECT * FROM merchants WHERE phone=?", (request.form["phone"].strip(),), fetch="one")
            if m and check_password_hash(m["pin_hash"], request.form["pin"]):
                session["merchant_id"] = m["id"]; return redirect("/dashboard")
        finally:
            conn.close()
        return page("<p>Téléphone ou code PIN incorrect.</p>")
    return page("""<div class="card"><h2>Se connecter</h2><form method="post">
<input name="phone" placeholder="Téléphone" required><input name="pin" type="password" placeholder="Code PIN" required>
<button>Se connecter</button></form></div>""")


@app.get("/logout")
def ui_logout():
    session.pop("merchant_id", None); return redirect("/")


def _ui_guard():
    m = current_merchant()
    return m or redirect("/login")


@app.get("/dashboard")
def ui_dashboard():
    m = _ui_guard()
    if not isinstance(m, dict): return m
    conn = get_conn()
    try:
        today = today_utc().isoformat()
        row = run(conn, "SELECT COUNT(*) AS n, COALESCE(SUM(total_gnf),0) AS total FROM sales WHERE merchant_id=? AND substr(created_at,1,10)=?", (m["id"], today), fetch="one")
        low = run(conn, "SELECT * FROM products WHERE merchant_id=? AND stock<=low_stock_threshold ORDER BY stock", (m["id"],), fetch="all")
        v = merchant_view(m)
        low_html = "".join(f"<li>{p['name']} — stock {p['stock']}</li>" for p in low) or "<li>Aucun produit en alerte.</li>"
        return page(f"""<div class="banner">Essai gratuit : {v['trial_days_remaining']} jour(s) restant(s) · Formule {v['plan']}</div>
<div class="card"><h2>Aujourd'hui</h2><p><b>{row['total']:,} GNF</b> · {row['n']} vente(s)</p></div>
<div class="card"><h2>Stock faible</h2><ul>{low_html}</ul></div>
<p><a href="/logout">Se déconnecter</a></p>""")
    finally:
        conn.close()


@app.get("/shops")
def ui_shops():
    m = _ui_guard()
    if not isinstance(m, dict): return m
    return page("""<div class="card"><h2>Mes boutiques</h2><form id="f"><input name="name" placeholder="Nom de la boutique" required><button>Ajouter</button></form><ul id="l"></ul></div>
<script>async function load(){let r=await fetch('/api/shops');let d=await r.json();document.getElementById('l').innerHTML=d.map(s=>`<li>${s.name} — <a href="/pos?shop=${s.id}">Caisse</a></li>`).join('')}
document.getElementById('f').onsubmit=async e=>{e.preventDefault();await fetch('/api/shops',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({name:e.target.name.value})});load()};load()</script>""")


@app.get("/pos")
def ui_pos():
    m = _ui_guard()
    if not isinstance(m, dict): return m
    return page("""<div class="card"><h2>Caisse</h2><p>Choisissez une boutique, ajoutez des produits, puis encaissez. Le total est calculé par le serveur.</p>
<input id="shop" type="number" placeholder="N° de boutique"><button onclick="load()">Afficher les produits</button><ul id="l"></ul>
<label>Moyen de paiement</label><select id="pay"><option>espèces</option><option>Mobile Money</option><option>virement</option><option>carte</option></select>
<button onclick="sell()">Encaisser la vente</button><p id="msg"></p></div>
<script>let cart={},shopId=0;async function load(){shopId=document.getElementById('shop').value;let r=await fetch('/api/shops/'+shopId+'/products');let d=await r.json();document.getElementById('l').innerHTML=d.map(p=>`<li>${p.name} — ${p.price_gnf} GNF (stock ${p.stock}) <button onclick="cart[${p.id}]=(cart[${p.id}]||0)+1;document.getElementById('msg').textContent='Panier : '+JSON.stringify(cart)">+1</button></li>`).join('')}
async function sell(){let items=Object.entries(cart).map(([product_id,quantity])=>({product_id:+product_id,quantity}));let r=await fetch('/api/sales',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({shop_id:+shopId,payment_method:document.getElementById('pay').value,items})});let d=await r.json();document.getElementById('msg').textContent=r.ok?`Vente ${d.reference} — ${d.total_gnf} GNF`:d.error;cart={}}</script>""")


@app.get("/customers")
def ui_customers():
    m = _ui_guard()
    if not isinstance(m, dict): return m
    return page("""<div class="card"><h2>Clients</h2><form id="f"><input name="name" placeholder="Nom" required><input name="phone" placeholder="Téléphone"><button>Ajouter</button></form><ul id="l"></ul></div>
<script>async function load(){let r=await fetch('/api/customers');let d=await r.json();document.getElementById('l').innerHTML=d.map(c=>`<li>${c.name} — ${c.phone}</li>`).join('')}
document.getElementById('f').onsubmit=async e=>{e.preventDefault();await fetch('/api/customers',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({name:e.target.name.value,phone:e.target.phone.value})});load()};load()</script>""")


@app.get("/reports")
def ui_reports():
    m = _ui_guard()
    if not isinstance(m, dict): return m
    conn = get_conn()
    try:
        rows = "".join(f"<li>{d} jours : {_report(conn, m['id'], d)['total_gnf']:,} GNF ({_report(conn, m['id'], d)['sales_count']} vente(s))</li>" for d in (7, 30, 90))
        return page(f"<div class='card'><h2>Rapports</h2><ul>{rows}</ul></div>")
    finally:
        conn.close()


init_db()

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", "5000")))
