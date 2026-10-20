"""Операции над поставщиками, общие для веб-страниц и агента."""
from datetime import datetime, timezone

from . import db
from .scoring import Query, rank
from .sources import checko, dadata, site

LIST_FIELDS = ("categories", "delivery_regions", "certificates")


def _today():
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")


def apply_fields(con, s, fields, source, url=None, overwrite=False):
    """Заполняет пустые поля (списки — объединяет) и пишет происхождение каждого изменённого поля.
    Ручные правки не затираются, если overwrite=False. Возвращает список изменённых полей."""
    upd, prov = {}, dict(s["provenance"])
    for k, v in fields.items():
        if k not in db.COLUMNS or v in (None, "", []):
            continue
        cur = s.get(k)
        if k in LIST_FIELDS:
            merged = list(dict.fromkeys((cur or []) + list(v)))
            if merged != (cur or []):
                upd[k] = merged
        elif overwrite or cur in (None, "", []):
            if cur != v:
                upd[k] = v
    for k in upd:
        prov[k] = {"site" if source == "site" else "api": url or source, "source": source, "date": _today()}
    if upd:
        upd["provenance"] = prov
        db.update_supplier(con, s["id"], upd)
    return [k for k in upd if k != "provenance"]


def enrich_registry(con, sid):
    """Каскад реестров: DaData (РФ+РК, 10k/сутки) -> Checko (РФ, 100/сутки) при ошибке или «не найдено».
    -> (changed_fields, company|None, message)"""
    s = db.get_supplier(con, sid)
    if not s.get("tax_id"):
        return [], None, "Нет ИНН/БИН — укажите его в карточке или найдите по названию"
    company, errors = None, []
    if dadata.enabled():
        try:
            company = dadata.find_by_tax_id(s["tax_id"], s["country"])
        except dadata.DadataError as e:
            errors.append(f"DaData: {e}")
    if not company and s["country"] == "RU" and checko.enabled():
        try:
            company = checko.company(s["tax_id"])
        except checko.CheckoError as e:
            errors.append(f"Checko: {e}")
    if not company:
        msg = "; ".join(errors) or ("Нет ключей реестров" if not (dadata.enabled() or checko.enabled()) else "Компания не найдена в реестре")
        return [], None, msg
    if (s.get("company") or {}).get("deep"):
        company["deep"] = s["company"]["deep"]  # не теряем углублённую проверку при повторной базовой
    db.update_supplier(con, sid, {"company": company})
    s = db.get_supplier(con, sid)
    fields = {k: company.get(k) for k in ("address", "lat", "lon")}
    fields.update(phone=(company.get("phones") or [None])[0], email=(company.get("emails") or [None])[0])
    changed = apply_fields(con, s, fields, "registry", company["source"])
    return ["company"] + changed, company, f"{company['source']}: {company.get('status')}, рег. {company.get('registration_date')}"


def enrich_deep(con, sid):
    """Углублённая проверка (Checko, только РФ): финансы, арбитраж, ФССП. -> (company, message)"""
    s = db.get_supplier(con, sid)
    if s["country"] != "RU" or not s.get("tax_id"):
        return None, "Углублённая проверка доступна для компаний РФ с ИНН"
    company = s.get("company")
    if not company:
        _, company, _ = enrich_registry(con, sid)
        company = company or {"source": "—"}
    company["deep"] = checko.deep(s["tax_id"])
    db.update_supplier(con, sid, {"company": company})
    d = company["deep"]
    rev = f"{d['revenue'] / 1e6:,.0f} млн ₽".replace(",", " ") if d.get("revenue") else "н/д"
    return company, f"Checko: выручка {rev} ({d.get('finance_year') or '—'}), дел как ответчик: {d.get('court_cases')}, ФССП: {d.get('enforcements')}"


def enrich_site(con, sid):
    """Извлечение условий с сайта. -> (changed_fields, result)"""
    s = db.get_supplier(con, sid)
    if not s.get("website"):
        return [], {"error": "У поставщика не указан сайт", "fields": {}, "method": None, "pages": []}
    res = site.extract(s["website"])
    db.update_supplier(con, sid, {"extracted": {**res, "at": _today()}})
    s = db.get_supplier(con, sid)
    changed = apply_fields(con, s, res["fields"], "site", res["pages"][0] if res["pages"] else s["website"])
    return changed, res


def search(con, q: Query, limit=None):
    rows = db.list_suppliers(con, country=q.country or None, category=q.category or None)
    ranked = rank(rows, q)
    return ranked[:limit] if limit else ranked
