"""Checko (бывш. Ofdata): финансы (ГИР БО), арбитраж, ФССП — то, чего нет в бесплатной DaData. Только РФ.
Бесплатно 100 запросов/сутки, поэтому вызывается точечно (кнопка «углублённая проверка» / фолбэк каскада)."""
from datetime import datetime, timezone

import httpx

from .. import config

BASE = "https://api.checko.ru/v2"


class CheckoError(Exception):
    pass


def enabled():
    return bool(config.CHECKO_API_KEY)


def _get(method, inn, **extra):
    if not enabled():
        raise CheckoError("CHECKO_API_KEY не задан")
    transport = httpx.HTTPTransport(retries=1)  # один повтор при обрыве соединения — у API бывают сетевые сбои
    try:
        with httpx.Client(transport=transport, timeout=40) as c:
            r = c.get(f"{BASE}/{method}", params={"key": config.CHECKO_API_KEY, "inn": inn, **extra})
    except httpx.HTTPError as e:
        raise CheckoError(f"сеть: {type(e).__name__}") from e
    body = r.json() if r.headers.get("content-type", "").startswith("application/json") else {}
    meta = body.get("meta") or {}
    if r.status_code != 200 or meta.get("status") == "error":
        raise CheckoError(meta.get("message") or f"HTTP {r.status_code}")
    return body.get("data") or {}


def _name(v):
    """Checko отдаёт справочники то строкой, то объектом {Код, Наим}."""
    return v.get("Наим") or v.get("Код") if isinstance(v, dict) else v


def _first(d, *keys):
    return next((d[k] for k in keys if isinstance(d, dict) and d.get(k) not in (None, "")), None)


def company(inn):
    d = _get("company", inn)
    status = _name(d.get("Статус")) or ""
    ruk = d.get("Руковод") or []
    contacts = d.get("Контакты") or {}
    return {
        "source": "Checko / ЕГРЮЛ", "name": d.get("НаимСокр") or d.get("НаимПолн"), "tax_id": d.get("ИНН"), "ogrn": d.get("ОГРН"),
        # Checko пишет статус словами; приводим к кодам DaData, на которые опирается скоринг
        "status": "ACTIVE" if status.lower().startswith("действ") else (status.upper() or None),
        "registration_date": d.get("ДатаРег"), "okved": _first(d.get("ОКВЭД") or {}, "Код"),
        "okved_name": _first(d.get("ОКВЭД") or {}, "Наим"), "address": _first(d.get("ЮрАдрес") or {}, "АдресРФ"),
        "manager": ruk[0].get("ФИО") if ruk and isinstance(ruk[0], dict) else None, "employees": d.get("СЧР"),
        "phones": (contacts.get("Тел") or [])[:3], "emails": (contacts.get("Емэйл") or [])[:3],
        "checked_at": datetime.now(timezone.utc).strftime("%Y-%m-%d"),
    }


def deep(inn):
    """Финансы + суды + исполнительные производства. -> dict для company['deep']"""
    out = {"source": "Checko", "checked_at": datetime.now(timezone.utc).strftime("%Y-%m-%d")}
    fin = _get("finances", inn)
    years = sorted((y for y in fin if y.isdigit()), reverse=True)
    if years:
        last = fin[years[0]]
        out.update(finance_year=int(years[0]), revenue=last.get("2110"), net_profit=last.get("2400"))
        if len(years) > 1 and last.get("2110") and fin[years[1]].get("2110"):
            out["revenue_growth"] = round(100 * (last["2110"] / fin[years[1]]["2110"] - 1))
    # Только дела, где компания ответчик: собственные иски к должникам — не риск.
    cases = _get("legal-cases", inn, role="defendant")
    out["court_cases"] = cases.get("ЗапВсего") or 0
    out["court_sum"] = cases.get("ОбщСуммИск")
    enf = _get("enforcements", inn)
    out["enforcements"] = enf.get("ЗапВсего") or enf.get("ОбщКолич") or 0
    out["enforcement_debt"] = enf.get("ОстЗадолж") or None  # остаток долга, а не исторический оборот
    return out
