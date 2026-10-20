"""Сбор условий поставщика с его сайта.

1) Скачиваем главную + до 5 «полезных» страниц (доставка, оплата, контакты, сертификаты, HoReCa).
2) Регулярки достают факты без LLM (работает и без ключа).
3) LLM (если есть ключ) извлекает структурированный JSON — точнее и с ассортиментом.
"""
import ipaddress
import json
import re
import socket
from urllib.parse import urljoin, urlparse

import httpx
from bs4 import BeautifulSoup

from .. import llm
from ..db import CATEGORIES

UA = {"User-Agent": "Mozilla/5.0 (compatible; FoodSupplierFinder/1.0)"}
SUB_KW = ("dostav", "delivery", "oplat", "payment", "kontakt", "contact", "horeca", "opt", "partner", "sotrud",
          "sertif", "kachest", "rekviz", "доставк", "контакт", "оптов", "сотруднич", "сертиф", "реквизит", "оплат")
MAX_PAGES, MAX_CHARS = 6, 14000


class FetchError(Exception):
    pass


def _check_host(url):
    """Защита от SSRF: сервер не должен ходить во внутреннюю сеть по ссылке от пользователя."""
    p = urlparse(url)
    if p.scheme not in ("http", "https") or not p.hostname:
        raise FetchError("Нужна ссылка http(s)")
    try:
        infos = socket.getaddrinfo(p.hostname, None)
    except socket.gaierror as e:
        raise FetchError(f"Домен не найден: {p.hostname}") from e
    for info in infos:
        ip = ipaddress.ip_address(info[4][0])
        if ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_reserved or ip.is_multicast:
            raise FetchError("Адрес во внутренней сети запрещён")


def normalize_url(url):
    url = (url or "").strip()
    if url and not url.startswith(("http://", "https://")):
        url = "https://" + url
    return url


def _text(html):
    soup = BeautifulSoup(html, "html.parser")
    for t in soup(["script", "style", "noscript", "svg"]):
        t.decompose()
    return soup, re.sub(r"\s+", " ", soup.get_text(" ")).strip()


def fetch_site(url):
    """-> (pages[{url, text}]). Каждый редирект тоже проходит проверку хоста."""
    url = normalize_url(url)
    _check_host(url)
    hook = {"request": [lambda req: _check_host(str(req.url))]}
    pages = []
    with httpx.Client(headers=UA, timeout=15, follow_redirects=True, event_hooks=hook, verify=False) as c:
        try:
            r = c.get(url)
        except httpx.HTTPError as e:
            raise FetchError(f"Сайт недоступен: {type(e).__name__}") from e
        soup, txt = _text(r.text)
        pages.append({"url": str(r.url), "text": txt})
        base_host = urlparse(str(r.url)).netloc.removeprefix("www.")
        links = []
        for a in soup.find_all("a", href=True):
            u = urljoin(str(r.url), a["href"]).split("#")[0]
            label = (a["href"] + " " + a.get_text(" ")).lower()
            if urlparse(u).netloc.removeprefix("www.") == base_host and any(k in label for k in SUB_KW) and u not in links:
                links.append(u)
        for u in links[:MAX_PAGES - 1]:
            try:
                pages.append({"url": u, "text": _text(c.get(u).text)[1]})
            except (httpx.HTTPError, FetchError):
                continue
    return pages


# ---------- регулярки (без LLM)
NUM = r"(\d[\d\s]{0,9}\d|\d)"
CUR = {"руб": "RUB", "р.": "RUB", "₽": "RUB", "тг": "KZT", "тенге": "KZT", "₸": "KZT", "кг": "кг", "шт": "шт"}
RE_MOQ = re.compile(r"(?:минимальн\w*\s(?:сумм\w*\s)?(?:заказ\w*|партия|отгрузк\w*)|отгрузк\w*\s+от)[^.|]{0,40}?" + NUM + r"\s?(руб|р\.|₽|тг|тенге|₸|кг|шт)", re.I)
RE_FREE = re.compile(r"бесплатн\w*\s+доставк\w*[^.|]{0,50}?от\s+" + NUM + r"\s?(руб|р\.|₽|тг|тенге|₸)", re.I)
RE_DEFER = re.compile(r"отсрочк\w*[^.|]{0,40}?(\d{1,3})\s?(?:календарн\w*\s)?(?:дн|день|дней)", re.I)
RE_INN = re.compile(r"ИНН[\s:/№]*(\d{10}|\d{12})\b")
RE_BIN = re.compile(r"БИН[\s:№]*(\d{12})\b")
RE_PHONE = re.compile(r"(?:\+7|8)[\s(-]*\d{3}[\s)-]*\d{3}[\s-]*\d{2}[\s-]*\d{2}")
RE_EMAIL = re.compile(r"[\w.+-]+@[\w-]+\.[a-z]{2,}", re.I)
CERTS = {"ХАССП": r"хассп|haccp", "ISO 22000": r"iso\s?22000", "Меркурий": r"меркури", "Декларации ЕАЭС": r"декларац\w* соответств|сертификат\w* соответств", "Халал": r"халал|халяль|halal"}
REGIONS = {"Вся Россия": r"по всей росси|во все регионы росси", "Москва и МО": r"по москве и (?:моск\w* )?област", "СПб и ЛО": r"(?:санкт-петербург\w*|спб) и (?:ленинградск|ло\b)", "Казахстан": r"по (?:всему )?казахстан"}


def _n(s):
    return float(re.sub(r"\s", "", s))


def extract_regex(pages):
    full = " || ".join(p["text"] for p in pages)
    out, src = {}, pages[0]["url"]
    if m := RE_MOQ.search(full):
        out["moq_value"], out["moq_unit"] = _n(m.group(1)), CUR[m.group(2).lower()]
    if m := RE_FREE.search(full):
        out["free_delivery_from"] = _n(m.group(1))
    if m := RE_DEFER.search(full):
        out["defer_days"] = int(m.group(1))
    if m := (RE_INN.search(full) or RE_BIN.search(full)):
        out["tax_id"] = m.group(1)
    if m := RE_PHONE.search(full):
        out["phone"] = m.group(0).strip()
    if m := next((e for e in RE_EMAIL.findall(full) if not e.lower().startswith(("hr", "job", "resume"))), None):
        out["email"] = m
    certs = [k for k, v in CERTS.items() if re.search(v, full, re.I)]
    if certs:
        out["certificates"] = certs
    regs = [k for k, v in REGIONS.items() if re.search(v, full, re.I)]
    if regs:
        out["delivery_regions"] = regs
    return out, src


# ---------- LLM
PROMPT = """Ты — аналитик по закупкам для сети ресторанов. Ниже текст страниц сайта поставщика продуктов/упаковки.
Извлеки ТОЛЬКО факты, явно присутствующие в тексте. Если факта нет — null. Не выдумывай.
Верни один JSON-объект без пояснений:
{
 "products": "краткое описание ассортимента, до 200 символов",
 "categories": [подмножество из: %s],
 "moq_value": число или null, "moq_unit": "RUB"|"KZT"|"кг"|"шт"|null,
 "free_delivery_from": число или null,
 "delivery_terms": "условия доставки кратко" или null,
 "delivery_regions": ["города/регионы доставки"],
 "payment_terms": "условия оплаты" или null, "defer_days": число или null,
 "certificates": [подмножество из: "Декларации ЕАЭС","ХАССП","ISO 22000","Меркурий","Халал"],
 "price_info": "примеры цен с единицами, если есть" или null,
 "phone": строка или null, "email": строка или null, "tax_id": "ИНН/БИН" или null
}"""


def extract_llm(pages):
    text = ""
    for p in pages:
        chunk = f"\n=== {p['url']}\n{p['text'][:4000]}"
        if len(text) + len(chunk) > MAX_CHARS:
            break
        text += chunk
    reply, model = llm.chat([
        {"role": "system", "content": PROMPT % ", ".join(f'"{k}"' for k in CATEGORIES)},
        {"role": "user", "content": text},
    ], temperature=0, max_tokens=2500)
    data = llm.parse_json(reply)
    clean = {k: v for k, v in data.items() if v not in (None, "", [], {})}
    if "categories" in clean:
        clean["categories"] = [c for c in clean["categories"] if c in CATEGORIES]
    for k in ("moq_value", "free_delivery_from", "defer_days"):
        if k in clean:
            try:
                clean[k] = float(clean[k]) if k != "defer_days" else int(clean[k])
            except (TypeError, ValueError):
                clean.pop(k)
    if "delivery_terms" in clean and clean.get("payment_terms"):
        clean["delivery_terms"] += f". Оплата: {clean['payment_terms']}"
    clean.pop("payment_terms", None)
    return clean, model


def extract(url):
    """-> {"fields": {...}, "method": "llm:<model>"|"regex", "pages": [...], "error": str|None}"""
    pages = fetch_site(url)
    fields, _ = extract_regex(pages)
    method, err = "regex", None
    if llm.enabled():
        try:
            lf, model = extract_llm(pages)
            fields.update(lf)  # LLM точнее, но регулярные находки остаются там, где LLM промолчал
            method = f"llm:{model}"
        except (llm.LLMUnavailable, ValueError, json.JSONDecodeError) as e:
            err = f"LLM недоступна, использованы регулярные выражения ({e})"
    return {"fields": fields, "method": method, "pages": [p["url"] for p in pages], "error": err}
