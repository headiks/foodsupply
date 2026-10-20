"""SQLite storage. JSON-поля хранятся как TEXT, наружу отдаются уже распарсенными dict."""
import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

from .config import DB_PATH, SEED_PATH

CATEGORIES = {
    "broadline": "Широкий ассортимент (дистрибьютор)",
    "fish": "Рыба и морепродукты",
    "meat": "Мясо и птица",
    "dairy": "Молочная продукция и сыры",
    "veg": "Овощи, фрукты, зелень",
    "grocery": "Бакалея: крупы, мука, рис",
    "asian": "Для японской и паназиатской кухни",
    "sauces": "Соусы, специи, приправы",
    "frozen": "Заморозка и полуфабрикаты",
    "bakery": "Выпечка и кондитерка",
    "packaging": "Упаковка и одноразовая посуда",
}
ANIMAL = {"fish", "meat", "dairy"}  # для них важен ветконтроль (Меркурий)

STATUSES = {
    "new": "Новый",
    "shortlist": "В шортлисте",
    "rfq_sent": "Запрос КП отправлен",
    "offer": "КП получено",
    "chosen": "Выбран",
    "rejected": "Отклонён",
}

JSON_FIELDS = ("categories", "delivery_regions", "certificates", "provenance", "source_pages", "company", "extracted")

SCHEMA = """
CREATE TABLE IF NOT EXISTS suppliers (
  id INTEGER PRIMARY KEY,
  name TEXT NOT NULL,
  country TEXT NOT NULL DEFAULT 'RU',
  city TEXT,
  address TEXT,
  lat REAL, lon REAL,
  website TEXT, phone TEXT, email TEXT,
  tax_id TEXT,
  categories TEXT NOT NULL DEFAULT '[]',
  products TEXT,
  delivery_regions TEXT NOT NULL DEFAULT '[]',
  delivery_terms TEXT,
  moq_value REAL, moq_unit TEXT,
  free_delivery_from REAL,
  defer_days INTEGER,
  certificates TEXT NOT NULL DEFAULT '[]',
  price_info TEXT,
  company TEXT,            -- ответ реестра (DaData): статус, дата регистрации, ОКВЭД...
  extracted TEXT,          -- последний результат извлечения с сайта
  provenance TEXT NOT NULL DEFAULT '{}',  -- поле -> {site|api, url, date}
  source_pages TEXT NOT NULL DEFAULT '[]',
  origin TEXT NOT NULL DEFAULT 'manual',  -- seed | yandex | manual
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS notes (
  id INTEGER PRIMARY KEY,
  supplier_id INTEGER NOT NULL REFERENCES suppliers(id) ON DELETE CASCADE,
  author TEXT NOT NULL,
  text TEXT NOT NULL,
  created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS statuses (
  supplier_id INTEGER PRIMARY KEY REFERENCES suppliers(id) ON DELETE CASCADE,
  status TEXT NOT NULL,
  author TEXT NOT NULL,
  updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_notes_supplier ON notes(supplier_id);
"""


def now():
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M")


def connect(path=None):
    p = path or DB_PATH
    Path(p).parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(p, check_same_thread=False)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA foreign_keys = ON")
    return con


def init(con, seed=True):
    con.executescript(SCHEMA)
    if seed and con.execute("SELECT COUNT(*) FROM suppliers").fetchone()[0] == 0:
        for rec in json.loads(SEED_PATH.read_text(encoding="utf-8")):
            insert_supplier(con, rec)
    con.commit()


def _row(r):
    if r is None:
        return None
    d = dict(r)
    for f in JSON_FIELDS:
        d[f] = json.loads(d[f]) if d.get(f) else ({} if f in ("provenance", "company", "extracted") else [])
    d["company"] = d["company"] or None
    return d


COLUMNS = ("name", "country", "city", "address", "lat", "lon", "website", "phone", "email", "tax_id", "categories",
           "products", "delivery_regions", "delivery_terms", "moq_value", "moq_unit", "free_delivery_from", "defer_days",
           "certificates", "price_info", "company", "extracted", "provenance", "source_pages", "origin")


def _dump(k, v):
    return json.dumps(v, ensure_ascii=False) if k in JSON_FIELDS and v is not None else v


def insert_supplier(con, rec):
    cols = [c for c in COLUMNS if c in rec]
    ts = now()
    cur = con.execute(
        f"INSERT INTO suppliers ({','.join(cols)}, created_at, updated_at) VALUES ({','.join('?' * len(cols))}, ?, ?)",
        [_dump(c, rec[c]) for c in cols] + [ts, ts])
    con.commit()
    return cur.lastrowid


def update_supplier(con, sid, fields):
    fields = {k: v for k, v in fields.items() if k in COLUMNS}
    if not fields:
        return
    sets = ", ".join(f"{k} = ?" for k in fields)
    con.execute(f"UPDATE suppliers SET {sets}, updated_at = ? WHERE id = ?",
                [_dump(k, v) for k, v in fields.items()] + [now(), sid])
    con.commit()


def get_supplier(con, sid):
    s = _row(con.execute("SELECT * FROM suppliers WHERE id = ?", (sid,)).fetchone())
    if s:
        st = con.execute("SELECT * FROM statuses WHERE supplier_id = ?", (sid,)).fetchone()
        s["status"] = dict(st) if st else None
    return s


def list_suppliers(con, country=None, category=None, ids=None):
    """Грубый SQL-фильтр; тонкое ранжирование — в scoring.py."""
    sql, args = ["SELECT s.*, st.status AS status_code FROM suppliers s LEFT JOIN statuses st ON st.supplier_id = s.id WHERE 1=1"], []
    if country:
        sql.append("AND s.country = ?"); args.append(country)
    if category:
        # категория или дистрибьютор широкого профиля
        sql.append("AND EXISTS (SELECT 1 FROM json_each(s.categories) j WHERE j.value IN (?, 'broadline'))"); args.append(category)
    if ids:
        sql.append(f"AND s.id IN ({','.join('?' * len(ids))})"); args.extend(ids)
    return [_row(r) for r in con.execute(" ".join(sql), args).fetchall()]


def notes_for(con, sid):
    return [dict(r) for r in con.execute("SELECT * FROM notes WHERE supplier_id = ? ORDER BY id DESC", (sid,))]


def add_note(con, sid, author, text):
    con.execute("INSERT INTO notes (supplier_id, author, text, created_at) VALUES (?, ?, ?, ?)", (sid, author, text, now()))
    con.commit()


def set_status(con, sid, status, author):
    con.execute("INSERT INTO statuses (supplier_id, status, author, updated_at) VALUES (?, ?, ?, ?) "
                "ON CONFLICT(supplier_id) DO UPDATE SET status = excluded.status, author = excluded.author, updated_at = excluded.updated_at",
                (sid, status, author, now()))
    con.commit()


def pipeline(con):
    rows = con.execute("SELECT s.id, s.name, s.city, s.categories, st.status, st.author, st.updated_at, "
                       "(SELECT COUNT(*) FROM notes n WHERE n.supplier_id = s.id) AS notes "
                       "FROM statuses st JOIN suppliers s ON s.id = st.supplier_id ORDER BY st.updated_at DESC").fetchall()
    out = {k: [] for k in STATUSES}
    for r in rows:
        d = dict(r); d["categories"] = json.loads(d["categories"])
        out.setdefault(d["status"], []).append(d)
    return out
