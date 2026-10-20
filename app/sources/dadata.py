"""Реестры юрлиц через DaData: РФ (ЕГРЮЛ/ЕГРИП по ИНН) и Казахстан (по БИН). Бесплатно до 10 000 запросов/сутки."""
from datetime import datetime, timezone

import httpx

from .. import config

BASE = "https://suggestions.dadata.ru/suggestions/api/4_1/rs"


class DadataError(Exception):
    pass


def enabled():
    return bool(config.DADATA_API_KEY)


def _post(method, body):
    if not enabled():
        raise DadataError("DADATA_API_KEY не задан")
    try:
        r = httpx.post(f"{BASE}/{method}", json=body, timeout=15,
                       headers={"Authorization": f"Token {config.DADATA_API_KEY}", "Accept": "application/json"})
    except httpx.HTTPError as e:
        raise DadataError(f"сеть: {type(e).__name__}") from e
    if r.status_code != 200:
        raise DadataError(f"HTTP {r.status_code}")
    return r.json().get("suggestions", [])


def _ms_date(ms):
    return datetime.fromtimestamp(ms / 1000, tz=timezone.utc).strftime("%Y-%m-%d") if ms else None


def _ru(sug):
    d = sug["data"]
    state, addr = d.get("state") or {}, (d.get("address") or {})
    fin = d.get("finance") or {}
    return {
        "source": "DaData / ЕГРЮЛ", "name": sug.get("value"), "tax_id": d.get("inn"), "ogrn": d.get("ogrn"),
        "status": state.get("status"), "registration_date": _ms_date(state.get("registration_date")),
        "okved": d.get("okved"), "address": addr.get("value"),
        "lat": _f((addr.get("data") or {}).get("geo_lat")), "lon": _f((addr.get("data") or {}).get("geo_lon")),
        "manager": ((d.get("management") or {}).get("name")), "employees": d.get("employee_count"),
        "revenue": fin.get("revenue"), "finance_year": fin.get("year"),
        "checked_at": datetime.now(timezone.utc).strftime("%Y-%m-%d"),
    }


def _kz(sug):
    d = sug["data"]
    return {
        "source": "DaData / реестр РК", "name": d.get("name_ru") or sug.get("value"), "tax_id": d.get("bin"),
        "status": {"ACTIVE": "ACTIVE"}.get(d.get("status"), d.get("status")),
        "registration_date": _ms_date(int(d["registration_date"])) if str(d.get("registration_date") or "").isdigit() else None,  # строка с мс
        "okved": d.get("oked"), "okved_name": d.get("oked_name_ru"), "address": d.get("address_ru"),
        "manager": d.get("fio"), "checked_at": datetime.now(timezone.utc).strftime("%Y-%m-%d"),
    }


def _f(x):
    try:
        return float(x)
    except (TypeError, ValueError):
        return None


def find_by_tax_id(tax_id, country="RU"):
    tax_id = (tax_id or "").strip()
    if not tax_id.isdigit():
        raise DadataError("ИНН/БИН должен состоять из цифр")
    if country == "KZ":
        res = _post("findById/party_kz", {"query": tax_id})
        return _kz(res[0]) if res else None
    res = _post("findById/party", {"query": tax_id, "branch_type": "MAIN"})
    return _ru(res[0]) if res else None


def suggest_by_name(name, city="", country="RU", count=5):
    """Кандидаты по названию — пользователь сам выбирает верный ИНН (автовыбор опасен для тёзок)."""
    if country == "KZ":
        return [_kz(s) for s in _post("suggest/party_kz", {"query": name, "count": count})]
    body = {"query": name, "count": count, "status": ["ACTIVE"]}
    res = [_ru(s) for s in _post("suggest/party", body)]
    if city:
        res.sort(key=lambda c: city.lower() not in (c.get("address") or "").lower())
    return res
