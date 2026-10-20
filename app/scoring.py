"""Прозрачная оценка поставщика 0–100: взвешенная сумма 6 критериев, у каждого — объяснение «почему».

Веса задаёт пользователь (ползунки), поэтому порядок выдачи всегда объясним, а не «чёрный ящик».
"""
from dataclasses import dataclass, field
from datetime import date, datetime
from statistics import median

from .config import STALE_DAYS
from .db import ANIMAL, CATEGORIES

DEFAULT_WEIGHTS = {"relevance": 25, "geo": 20, "reliability": 20, "documents": 15, "commercial": 15, "completeness": 5}
LABELS = {"relevance": "Соответствие запросу", "geo": "География", "reliability": "Надёжность компании",
          "documents": "Документы", "commercial": "Коммерческие условия", "completeness": "Полнота данных"}
CURRENCY = {"RU": "RUB", "KZ": "KZT"}
METRO = {"Москва": "Москва и МО", "Санкт-Петербург": "СПб и ЛО"}  # город запроса -> как он пишется в регионах доставки
MO_CITIES = {"Лыткарино", "Мытищи", "Электроугли", "Химки", "Балашиха", "Подольск", "Королёв", "Люберцы", "Домодедово"}


@dataclass
class Query:
    category: str = ""
    text: str = ""
    city: str = ""
    country: str = ""
    order_sum: float | None = None   # сумма разового заказа в валюте страны
    order_kg: float | None = None
    need_docs: bool = False
    weights: dict = field(default_factory=lambda: dict(DEFAULT_WEIGHTS))


def _company_age(company):
    reg = (company or {}).get("registration_date")
    if not reg:
        return None
    try:
        d = datetime.strptime(reg[:10], "%Y-%m-%d").date()
    except ValueError:
        return None
    return (date.today() - d).days / 365.25


def relevance(s, q):
    cats = set(s["categories"])
    words = [w for w in q.text.lower().split() if len(w) > 2]
    hay = f"{s['name']} {s.get('products') or ''}".lower()
    hits = sum(1 for w in words if w[:5] in hay)  # грубый стемминг: «лосося» ~ «лосось»
    if q.category:
        base, why = (1.0, f"профильная категория «{CATEGORIES[q.category]}»") if q.category in cats else \
            (0.6, "дистрибьютор широкого профиля, категория не основная")
    else:
        base, why = 0.8, "категория не задана"
    if words:
        bonus = min(0.3, 0.15 * hits)
        base = min(1.0, base * (0.8 if not hits else 1) + bonus)
        why += f"; совпадений по ассортименту: {hits}/{len(words)}"
    return base, why


def geo(s, q):
    if not q.city:
        if q.country and s["country"] != q.country:
            return 0.2, "другая страна"
        return 1.0, "город не задан"
    city, regions = q.city.strip(), s["delivery_regions"]
    same_metro = s["city"] == city or (city == "Москва" and s["city"] in MO_CITIES)
    if same_metro:
        return 1.0, f"тот же город: {s['city']}"
    if METRO.get(city) in regions or city in regions:
        return 0.85, f"доставляет в {city}"
    whole = "Вся Россия" if (q.country or "RU") == "RU" else "Казахстан"
    if whole in regions and s["country"] == (q.country or s["country"]):
        return 0.6, f"заявлена доставка: {whole.lower()}, из {s['city']}"
    if q.country and s["country"] != q.country:
        return 0.05, "другая страна"
    return 0.3, f"город поставщика: {s['city']}, доставку в {city} уточнить"


def reliability(s, q):
    c = s.get("company")
    if c:
        st = c.get("status")
        if st and st != "ACTIVE":
            return 0.0, f"статус в реестре: {st}"
        age = _company_age(c)
        if age is None:
            return 0.6, "компания действует, дата регистрации неизвестна"
        score = 1.0 if age >= 10 else 0.85 if age >= 5 else 0.65 if age >= 3 else 0.4 if age >= 1 else 0.15
        why = f"действует, на рынке {int(age)} полных лет (реестр)"
        d = c.get("deep") or {}
        if d.get("enforcements"):
            score -= 0.3; why += f"; исполнительных производств: {d['enforcements']}"
        if (d.get("net_profit") or 0) < 0:
            score -= 0.1; why += f"; убыток в {d.get('finance_year')}"
        if d.get("revenue"):
            why += f"; выручка {d['revenue'] / 1e6:,.0f} млн ₽ ({d.get('finance_year')})".replace(",", " ")
        return max(0.0, score), why
    if s.get("tax_id"):
        return 0.5, "ИНН/БИН указан на сайте, реестр не проверен — нажмите «Проверить по реестру»"
    return 0.35, "нет ИНН/БИН и данных реестра"


def documents(s, q):
    certs = set(s["certificates"])
    if not certs:
        return 0.2, "документы на сайте не найдены — запросить"
    sc = 0.0
    sc += 0.4 if "Декларации ЕАЭС" in certs else 0
    sc += 0.3 if {"ХАССП", "ISO 22000"} & certs else 0
    if ANIMAL & set(s["categories"]) or q.category in ANIMAL:
        sc += 0.3 if "Меркурий" in certs else 0
    else:
        sc += 0.2
    sc += 0.1 if "Халал" in certs else 0
    return min(1.0, max(sc, 0.3)), "найдено: " + ", ".join(sorted(certs))


def commercial(s, q):
    parts, sc = [], 0.5
    cur = CURRENCY.get(s["country"])
    moq, unit = s.get("moq_value"), s.get("moq_unit")
    if moq is not None and unit == cur and q.order_sum:
        fits = moq <= q.order_sum
        sc = 0.85 if fits else max(0.1, 0.6 * q.order_sum / moq)
        parts.append(f"мин. заказ {moq:,.0f} {'≤' if fits else '>'} вашего {q.order_sum:,.0f}".replace(",", " "))
    elif moq is not None and unit == "кг" and q.order_kg:
        sc = 0.85 if moq <= q.order_kg else max(0.1, 0.6 * q.order_kg / moq)
        parts.append(f"мин. партия {moq:.0f} кг" + (" — подходит" if moq <= q.order_kg else " — больше вашей"))
    elif moq is not None:
        sc = 0.65; parts.append(f"мин. заказ известен: {moq:,.0f} {unit}".replace(",", " "))
    else:
        parts.append("мин. заказ неизвестен")
    fd = s.get("free_delivery_from")
    if fd and q.order_sum and fd <= q.order_sum:
        sc += 0.1; parts.append("доставка бесплатна для вашего объёма")
    if s.get("defer_days"):
        sc += 0.1; parts.append(f"отсрочка {s['defer_days']} дн.")
    if s.get("price_info"):
        sc += 0.05; parts.append("есть данные о ценах")
    return min(1.0, sc), "; ".join(parts)


KEY_FIELDS = ("phone", "email", "website", "tax_id", "moq_value", "delivery_terms", "certificates", "price_info", "company", "products")


def completeness(s, q=None):
    filled = sum(1 for f in KEY_FIELDS if s.get(f))
    return filled / len(KEY_FIELDS), f"заполнено {filled} из {len(KEY_FIELDS)} ключевых полей"


CRITERIA = {"relevance": relevance, "geo": geo, "reliability": reliability, "documents": documents,
            "commercial": commercial, "completeness": completeness}


def flags(s, q):
    out = []
    c = s.get("company") or {}
    if c.get("status") and c["status"] != "ACTIVE":
        out.append(("red", f"Реестр: {c['status']}"))
    age = _company_age(c)
    if age is not None and age < 1:
        out.append(("red", "Компании меньше года"))
    d = c.get("deep") or {}
    if d.get("enforcements"):
        out.append(("red", f"ФССП: {d['enforcements']} производств"))
    if (d.get("net_profit") or 0) < 0:
        out.append(("amber", "Убыток по последней отчётности"))
    if d.get("court_sum") and d.get("revenue") and d["court_sum"] > 0.05 * d["revenue"]:
        out.append(("amber", "Иски к компании > 5% выручки"))
    if not s.get("tax_id"):
        out.append(("amber", "Нет ИНН/БИН"))
    if not (s.get("phone") or s.get("email")):
        out.append(("amber", "Нет прямых контактов"))
    if (ANIMAL & set(s["categories"])) and "Меркурий" not in s["certificates"]:
        out.append(("amber", "Не подтверждён «Меркурий»"))
    cur = CURRENCY.get(s["country"])
    if q.order_sum and s.get("moq_value") and s.get("moq_unit") == cur and s["moq_value"] > q.order_sum:
        out.append(("amber", "Мин. заказ выше вашего"))
    try:
        if (datetime.now() - datetime.strptime(s["updated_at"][:10], "%Y-%m-%d")).days > STALE_DAYS:
            out.append(("grey", "Данные старше 180 дней"))
    except (KeyError, ValueError):
        pass
    return out


def score(s, q):
    parts, total_w, acc = [], 0, 0.0
    for key, fn in CRITERIA.items():
        w = max(0, q.weights.get(key, 0))
        v, why = fn(s, q)
        parts.append({"key": key, "label": LABELS[key], "value": round(v, 2), "weight": w, "why": why})
        total_w += w
        acc += w * v
    return {"total": round(100 * acc / total_w) if total_w else 0, "parts": parts, "flags": flags(s, q)}


def rank(suppliers, q):
    out = []
    for s in suppliers:
        if q.need_docs and not s["certificates"]:
            continue
        s = dict(s)
        s["score"] = score(s, q)
        out.append(s)
    out.sort(key=lambda x: (-x["score"]["total"], x["name"]))
    return out


def summary(ranked, q):
    """Сводка по выдаче: помогает понять рынок, а не только конкретного поставщика."""
    n = len(ranked)
    if not n:
        return None
    cur = CURRENCY.get(q.country or "RU")
    moqs = [s["moq_value"] for s in ranked if s.get("moq_value") and s.get("moq_unit") == cur]
    local = sum(1 for s in ranked if q.city and geo(s, q)[0] >= 0.85)
    return {
        "count": n,
        "with_docs": round(100 * sum(1 for s in ranked if s["certificates"]) / n),
        "with_moq": round(100 * sum(1 for s in ranked if s.get("moq_value")) / n),
        "with_registry": round(100 * sum(1 for s in ranked if s.get("company")) / n),
        "moq_median": median(moqs) if moqs else None,
        "currency": "₽" if cur == "RUB" else "₸",
        "local": local,
        "top_score": ranked[0]["score"]["total"],
    }
