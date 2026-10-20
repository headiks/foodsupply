import csv
import io
from urllib.parse import quote, unquote, urlencode

from fastapi import FastAPI, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from . import agent, config, db, llm, service
from .config import ROOT
from .scoring import DEFAULT_WEIGHTS, LABELS, Query, score, summary
from .sources import checko, dadata, site, yandex

app = FastAPI(title="Поиск поставщиков продуктов питания")
app.mount("/static", StaticFiles(directory=ROOT / "app" / "static"), name="static")
tpl = Jinja2Templates(directory=ROOT / "app" / "templates")
tpl.env.globals.update(CATEGORIES=db.CATEGORIES, STATUSES=db.STATUSES, LABELS=LABELS)

con = db.connect()
db.init(con)

FIELD_LABELS = {"phone": "Телефон", "email": "Email", "tax_id": "ИНН/БИН", "moq_value": "Мин. заказ",
                "free_delivery_from": "Бесплатная доставка от", "defer_days": "Отсрочка, дней", "delivery_terms": "Условия доставки",
                "certificates": "Документы", "price_info": "Цены", "products": "Ассортимент", "categories": "Категории",
                "delivery_regions": "Регионы доставки", "address": "Адрес", "lat": "Координаты", "lon": "Координаты", "company": "Реестр"}
tpl.env.globals["FIELD_LABELS"] = FIELD_LABELS


def user(request: Request):
    return unquote(request.cookies.get("user") or "")


def render(request, name, **ctx):
    ctx.update(request=request, user=user(request),
               keys={"llm": llm.enabled(), "dadata": dadata.enabled(), "yandex": yandex.enabled(), "checko": checko.enabled(),
                     "maps_js": config.YANDEX_MAPS_JS_KEY})
    return tpl.TemplateResponse(request, name, ctx)


def _f(x):
    try:
        return float(str(x).replace(" ", "").replace(",", ".")) if x not in (None, "") else None
    except ValueError:
        return None


def query_from(params) -> Query:
    w = {k: int(_f(params.get(f"w_{k}")) or 0) if params.get(f"w_{k}") not in (None, "") else v for k, v in DEFAULT_WEIGHTS.items()}
    cat = params.get("category") or ""
    return Query(category=cat if cat in db.CATEGORIES else "", text=(params.get("q") or "").strip(),
                 city=(params.get("city") or "").strip(), country=params.get("country") or "",
                 order_sum=_f(params.get("order_sum")), order_kg=_f(params.get("order_kg")),
                 need_docs=params.get("need_docs") in ("1", "on", "true"), weights=w)


def get_or_404(sid):
    s = db.get_supplier(con, sid)
    if not s:
        raise HTTPException(404, "Поставщик не найден")
    return s


# ---------- поиск
@app.get("/", response_class=HTMLResponse)
def index(request: Request):
    q = query_from(request.query_params)
    if request.headers.get("HX-Request"):  # фильтры обновляют только выдачу, URL остаётся «/»?…
        return render(request, "_results.html", q=q, **_results(q))
    cities = sorted({r[0] for r in con.execute("SELECT DISTINCT city FROM suppliers WHERE city IS NOT NULL")})
    return render(request, "index.html", q=q, weights=q.weights, cities=cities, **_results(q))


@app.get("/results", response_class=HTMLResponse)
def results(request: Request):
    q = query_from(request.query_params)
    return render(request, "_results.html", q=q, **_results(q))


def _results(q):
    ranked = service.search(con, q)
    if q.text:  # свободный текст: оставляем тех, где есть совпадение, если такие есть
        hit = [s for s in ranked if "совпадений по ассортименту: 0" not in s["score"]["parts"][0]["why"]]
        ranked = hit or ranked
    qs = urlencode({k: v for k, v in [("category", q.category), ("q", q.text), ("city", q.city), ("country", q.country),
                                      ("order_sum", q.order_sum or ""), ("order_kg", q.order_kg or "")] if v})
    return {"ranked": ranked, "summary": summary(ranked, q), "qs": qs}


# ---------- карточка
@app.get("/s/{sid}", response_class=HTMLResponse)
def supplier(request: Request, sid: int, msg: str = ""):
    s = get_or_404(sid)
    q = query_from(request.query_params)
    return render(request, "supplier.html", s=s, sc=score(s, q), q=q, notes=db.notes_for(con, sid), msg=msg,
                  back=str(request.query_params))


def _back(sid, msg, request):
    params = {k: v for k, v in request.query_params.items() if k != "msg"}
    params["msg"] = msg
    return RedirectResponse(f"/s/{sid}?{urlencode(params)}", status_code=303)


@app.post("/s/{sid}/registry")
def do_registry(request: Request, sid: int):
    get_or_404(sid)
    try:
        _, _, msg = service.enrich_registry(con, sid)
    except dadata.DadataError as e:
        msg = f"Реестр недоступен: {e}"
    return _back(sid, msg, request)


@app.post("/s/{sid}/deep")
def do_deep(request: Request, sid: int):
    get_or_404(sid)
    try:
        _, msg = service.enrich_deep(con, sid)
    except checko.CheckoError as e:
        msg = f"Checko недоступен: {e}"
    return _back(sid, msg, request)


@app.get("/s/{sid}/find-inn", response_class=HTMLResponse)
def find_inn(request: Request, sid: int):
    s = get_or_404(sid)
    try:
        cands, err = dadata.suggest_by_name(s["name"].strip("«»\"").split("(")[0], s["city"], s["country"]), None
    except dadata.DadataError as e:
        cands, err = [], str(e)
    return render(request, "_inn_candidates.html", s=s, cands=cands, err=err)


@app.post("/s/{sid}/set-inn")
def set_inn(request: Request, sid: int, tax_id: str = Form(...)):
    s = get_or_404(sid)
    if not tax_id.isdigit() or len(tax_id) not in (10, 12):
        return _back(sid, "ИНН/БИН: 10 или 12 цифр", request)
    service.apply_fields(con, s, {"tax_id": tax_id}, "manual", f"указан: {user(request) or 'аноним'}", overwrite=True)
    return do_registry(request, sid)


@app.post("/s/{sid}/extract")
def do_extract(request: Request, sid: int):
    get_or_404(sid)
    try:
        changed, res = service.enrich_site(con, sid)
        how = "LLM" if (res.get("method") or "").startswith("llm") else "регулярные выражения"
        msg = (res.get("error") or "") + (f" Обновлено полей: {len(changed)} ({how})" if changed else f" Новых данных не найдено ({how})")
    except site.FetchError as e:
        msg = f"Не удалось прочитать сайт: {e}"
    return _back(sid, msg.strip(), request)


@app.post("/s/{sid}/edit")
async def edit(request: Request, sid: int):
    s = get_or_404(sid)
    form = await request.form()
    fields = {}
    for k in ("phone", "email", "website", "delivery_terms", "price_info", "products", "city"):
        if k in form:
            fields[k] = form[k].strip() or None
    for k in ("moq_value", "free_delivery_from"):
        if k in form:
            fields[k] = _f(form[k])
    if "defer_days" in form:
        fields["defer_days"] = int(_f(form["defer_days"]) or 0) or None
    if "moq_unit" in form:
        fields["moq_unit"] = form["moq_unit"] or None
    fields["categories"] = [c for c in form.getlist("categories") if c in db.CATEGORIES] or s["categories"]
    fields["certificates"] = form.getlist("certificates")
    regs = [r.strip() for r in (form.get("delivery_regions") or "").split(",") if r.strip()]
    fields["delivery_regions"] = regs or s["delivery_regions"]
    changed = {k: v for k, v in fields.items() if v != s.get(k)}
    if changed:
        prov = dict(s["provenance"])
        for k in changed:
            prov[k] = {"source": "manual", "api": f"вручную: {user(request) or 'аноним'}", "date": db.now()[:10]}
        db.update_supplier(con, sid, {**changed, "provenance": prov})
    return _back(sid, f"Сохранено полей: {len(changed)}", request)


@app.post("/s/{sid}/note")
def note(request: Request, sid: int, text: str = Form(...)):
    get_or_404(sid)
    if not user(request):
        return RedirectResponse(f"/login?next=/s/{sid}", status_code=303)
    if text.strip():
        db.add_note(con, sid, user(request), text.strip()[:2000])
    return _back(sid, "Заметка добавлена", request)


@app.post("/s/{sid}/status")
def status(request: Request, sid: int, status: str = Form(...)):
    get_or_404(sid)
    if not user(request):
        return RedirectResponse(f"/login?next=/s/{sid}", status_code=303)
    if status in db.STATUSES:
        db.set_status(con, sid, status, user(request))
    return _back(sid, f"Статус: {db.STATUSES.get(status, status)}", request)


@app.post("/s/{sid}/rfq", response_class=HTMLResponse)
def rfq(request: Request, sid: int, volume: str = Form(""), frequency: str = Form(""), points: str = Form(""),
        city: str = Form(""), extra: str = Form("")):
    s = get_or_404(sid)
    text, mode = rfq_text(s, volume, frequency, points, city, extra, user(request))
    subject = quote(f"Запрос коммерческого предложения — {s['name']}")
    mailto = f"mailto:{s['email']}?subject={subject}&body={quote(text)}" if s.get("email") else None
    return render(request, "_rfq.html", s=s, text=text, mode=mode, mailto=mailto)


def rfq_text(s, volume, frequency, points, city, extra, author):
    cats = ", ".join(db.CATEGORIES[c].lower() for c in s["categories"] if c != "broadline") or "продукты питания"
    asks = ["прайс-лист с оптовыми ценами (₽/кг или ₽/шт, с НДС)", "минимальный объём заказа и график поставок",
            "условия доставки до наших точек и её стоимость", "условия оплаты и возможность отсрочки",
            "декларации ЕАЭС / сертификаты" + (", подтверждение работы в ФГИС «Меркурий»" if db.ANIMAL & set(s["categories"]) else "")]
    base = (f"Здравствуйте!\n\nМы — сеть заведений общественного питания{f' ({points} точек)' if points else ''}"
            f"{f', город {city}' if city else ''}. Рассматриваем {s['name']} как поставщика: {cats}.\n\n"
            f"Планируемый объём: {volume or 'уточним по прайсу'}{f', периодичность: {frequency}' if frequency else ''}.\n\n"
            "Просим направить:\n" + "\n".join(f"— {a};" for a in asks) +
            (f"\n\nДополнительно: {extra}" if extra else "") +
            f"\n\nС уважением,\n{author or 'отдел закупок'}")
    if not llm.enabled():
        return base, "шаблон"
    try:
        reply, model = llm.chat([
            {"role": "system", "content": "Ты помогаешь закупщику HoReCa. Перепиши письмо-запрос КП деловым, кратким и вежливым языком. "
                                          "Сохрани все пункты и факты, ничего не добавляй от себя. Верни только текст письма."},
            {"role": "user", "content": base}], temperature=0.3, max_tokens=800)
        return reply, f"LLM: {model}"
    except llm.LLMUnavailable:
        return base, "шаблон (LLM недоступна)"


# ---------- сравнение, воронка, экспорт
@app.get("/compare", response_class=HTMLResponse)
def compare(request: Request, ids: str = ""):
    id_list = [int(i) for i in ids.split(",") if i.strip().isdigit()][:4]
    q = query_from(request.query_params)
    rows = [s | {"score": score(s, q)} for s in db.list_suppliers(con, ids=id_list)] if id_list else []
    rows.sort(key=lambda s: -s["score"]["total"])
    best = [max(s["score"]["parts"][i]["value"] for s in rows) for i in range(len(rows[0]["score"]["parts"]))] if rows else []
    return render(request, "compare.html", rows=rows, q=q, best=best)


@app.get("/pipeline", response_class=HTMLResponse)
def pipeline(request: Request):
    return render(request, "pipeline.html", board=db.pipeline(con))


@app.get("/export.csv")
def export(request: Request, ids: str = ""):
    q = query_from(request.query_params)
    if ids:
        rows = db.list_suppliers(con, ids=[int(i) for i in ids.split(",") if i.strip().isdigit()])
    else:
        rows = [s for s in db.list_suppliers(con) if s.get("status_code") in ("shortlist", "rfq_sent", "offer", "chosen")]
    buf = io.StringIO()
    w = csv.writer(buf, delimiter=";")
    w.writerow(["Балл", "Название", "Город", "Категории", "Сайт", "Телефон", "Email", "ИНН/БИН", "Мин. заказ",
                "Бесплатная доставка от", "Отсрочка, дн", "Документы", "Доставка", "Статус"])
    for s in sorted(rows, key=lambda s: -score(s, q)["total"]):
        w.writerow([score(s, q)["total"], s["name"], s["city"], ", ".join(db.CATEGORIES.get(c, c) for c in s["categories"]),
                    s.get("website"), s.get("phone"), s.get("email"), s.get("tax_id"),
                    f"{s['moq_value']:.0f} {s['moq_unit']}" if s.get("moq_value") else "", s.get("free_delivery_from") or "",
                    s.get("defer_days") or "", ", ".join(s["certificates"]), s.get("delivery_terms") or "",
                    db.STATUSES.get(s.get("status_code") or "", "")])
    data = "﻿" + buf.getvalue()  # BOM — чтобы Excel открыл кириллицу
    return StreamingResponse(iter([data]), media_type="text/csv; charset=utf-8",
                             headers={"Content-Disposition": "attachment; filename=suppliers.csv"})


# ---------- живой поиск и добавление
@app.get("/live", response_class=HTMLResponse)
def live(request: Request, text: str = ""):
    items, err = [], None
    if text:
        try:
            items = yandex.search(text)
        except yandex.YandexError as e:
            err = str(e)
    return render(request, "live.html", text=text, items=items, err=err)


@app.post("/add")
def add(request: Request, name: str = Form(...), website: str = Form(""), city: str = Form(""), country: str = Form("RU"),
        tax_id: str = Form(""), phone: str = Form(""), address: str = Form(""), lat: str = Form(""), lon: str = Form(""),
        categories: list[str] = Form([]), origin: str = Form("manual"), run_enrich: str = Form("")):
    name = name.strip()[:200]
    if not name:
        raise HTTPException(400, "Нужно название")
    src = "Яндекс.Карты" if origin == "yandex" else f"вручную: {user(request) or 'аноним'}"
    rec = {"name": name, "website": site.normalize_url(website) or None, "city": city.strip() or None,
           "country": country if country in ("RU", "KZ") else "RU", "tax_id": tax_id.strip() or None,
           "phone": phone.strip() or None, "address": address.strip() or None, "lat": _f(lat), "lon": _f(lon),
           "categories": [c for c in categories if c in db.CATEGORIES], "origin": origin if origin in ("yandex", "manual") else "manual"}
    rec["provenance"] = {k: {"source": origin, "api": src, "date": db.now()[:10]} for k, v in rec.items() if v not in (None, [], "")}
    sid = db.insert_supplier(con, rec)
    msg = "Поставщик добавлен"
    if run_enrich:
        try:
            changed, res = service.enrich_site(con, sid)
            msg += f"; с сайта обновлено полей: {len(changed)}"
        except site.FetchError as e:
            msg += f"; сайт не прочитан: {e}"
    return RedirectResponse(f"/s/{sid}?msg={quote(msg)}", status_code=303)


@app.get("/add", response_class=HTMLResponse)
def add_form(request: Request):
    return render(request, "add.html")


# ---------- агент
@app.get("/agent", response_class=HTMLResponse)
def agent_page(request: Request):
    return render(request, "agent.html")


@app.post("/agent/ask", response_class=HTMLResponse)
def agent_ask(request: Request, text: str = Form(...)):
    res = agent.run(con, text.strip()[:1000])
    q = agent.parse_query(text)
    cards = [s | {"score": score(s, q)} for s in db.list_suppliers(con, ids=res["supplier_ids"])] if res["supplier_ids"] else []
    order = {sid: i for i, sid in enumerate(res["supplier_ids"])}
    cards.sort(key=lambda s: order.get(s["id"], 99))
    return render(request, "_agent_result.html", res=res, cards=cards, ids=",".join(map(str, res["supplier_ids"])))


# ---------- прочее
@app.get("/about", response_class=HTMLResponse)
def about(request: Request):
    stats = con.execute("SELECT COUNT(*), SUM(country='KZ'), SUM(tax_id IS NOT NULL), SUM(company IS NOT NULL) FROM suppliers").fetchone()
    return render(request, "about.html", stats=stats, weights=DEFAULT_WEIGHTS)


@app.get("/login", response_class=HTMLResponse)
def login_form(request: Request, next: str = "/"):
    return render(request, "login.html", next=next)


@app.post("/login")
def login(name: str = Form(...), next: str = Form("/")):
    target = next if next.startswith("/") and not next.startswith("//") else "/"  # без open redirect
    r = RedirectResponse(target, status_code=303)
    r.set_cookie("user", quote(name.strip()[:40]), max_age=60 * 60 * 24 * 30, httponly=True, samesite="lax")
    return r


@app.get("/logout")
def logout():
    r = RedirectResponse("/", status_code=303)
    r.delete_cookie("user")
    return r


@app.get("/health")
def health():
    return {"ok": True, "suppliers": con.execute("SELECT COUNT(*) FROM suppliers").fetchone()[0]}
