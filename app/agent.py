"""Агент-закупщик: запрос свободным текстом -> вызовы инструментов -> шортлист с объяснением.

Протокол «JSON в тексте» (а не нативный tool calling) — работает с любой бесплатной моделью OpenRouter.
Без ключа LLM работает детерминированный режим: разбор запроса правилами + тот же поиск/скоринг.
"""
import json
import re

from . import db, llm, service
from .scoring import Query
from .sources import checko, dadata, site, yandex

MAX_STEPS = 8

CAT_WORDS = {
    # упаковка первой: «упаковка для суши» — это упаковка, а не продукты
    "packaging": ["упаковк", "контейнер", "ланч", "посуд", "пакет", "стакан", "коробк", "одноразов"],
    "asian": ["суши", "ролл", "нори", "васаби", "имбир", "японск", "паназиат", "вок", "поке", "соев"],
    "fish": ["рыб", "лосос", "форел", "тунец", "тунц", "кревет", "морепрод", "икр", "угор", "кальмар"],
    "meat": ["мяс", "говяд", "свинин", "куриц", "курин", "птиц", "баранин", "индейк", "фарш", "стейк", "мраморн"],
    "dairy": ["молок", "молочн", "сыр", "моцарел", "сливк", "творог", "сметан", "кефир"],
    "veg": ["овощ", "фрукт", "зелен", "томат", "огур", "ягод", "салат"],
    "grocery": ["круп", "мук", "макарон", "сахар", "бакале", " рис"],
    "sauces": ["соус", "майонез", "кетчуп", "специ", "приправ"],
    "frozen": ["заморож", "полуфабрик", "фри", "пельмен", "мороженое"],
    "bakery": ["выпеч", "хлеб", "тесто", "кондитер", "десерт", "сладост"],
}
CITY_WORDS = {
    "москв": "Москва", "мск": "Москва", "петербург": "Санкт-Петербург", "спб": "Санкт-Петербург", "питер": "Санкт-Петербург",
    "екатеринбург": "Екатеринбург", "екб": "Екатеринбург", "новосибирск": "Новосибирск", "казан": "Казань",
    "краснодар": "Краснодар", "нижн": "Нижний Новгород", "челябинск": "Челябинск", "воронеж": "Воронеж",
    "алмат": "Алматы", "астан": "Астана", "шымкент": "Шымкент", "караганд": "Караганда",
}
KZ_CITIES = {"Алматы", "Астана", "Шымкент", "Караганда", "Петропавловск", "Костанай", "Уральск"}


def parse_query(text):
    """Правила: категория, город, страна, сумма/вес заказа, требования к документам."""
    t = text.lower()
    q = Query(text=text)
    # приоритет — порядок CAT_WORDS: «рыба для суши» -> asian, «рис» уступает «суши»
    for cat, words in CAT_WORDS.items():
        if any(w in t for w in words):
            q.category = cat
            break
    for w, city in CITY_WORDS.items():
        if w in t:
            q.city = city
            break
    q.country = "KZ" if (q.city in KZ_CITIES or re.search(r"казахстан|тенге|₸|\bтг\b", t)) else "RU"
    if m := re.search(r"(\d[\d\s]*)\s*(тыс\.?|т\.|k|к)?\s*(₽|руб|р\b|₸|тенге|тг)", t):
        q.order_sum = float(re.sub(r"\s", "", m.group(1))) * (1000 if m.group(2) else 1)
    if m := re.search(r"(\d[\d\s]*)\s*(кг|kg|тонн|т\b)", t):
        kg = float(re.sub(r"\s", "", m.group(1)))
        q.order_kg = kg * 1000 if m.group(2).startswith("т") else kg
    q.need_docs = bool(re.search(r"сертиф|деклар|документ|хассп|меркури", t))
    return q


def _brief(s):
    return {"id": s["id"], "name": s["name"], "city": s["city"], "score": s["score"]["total"],
            "categories": s["categories"], "products": (s.get("products") or "")[:160],
            "moq": f"{s['moq_value']:.0f} {s['moq_unit']}" if s.get("moq_value") else None,
            "delivery": s.get("delivery_terms"), "certificates": s["certificates"], "tax_id": s.get("tax_id"),
            "flags": [f[1] for f in s["score"]["flags"]]}


# ---------- инструменты
def t_search(con, args):
    q = Query(category=args.get("category") or "", text=args.get("text") or "", city=args.get("city") or "",
              country=args.get("country") or "", order_sum=_num(args.get("order_sum")), order_kg=_num(args.get("order_kg")),
              need_docs=bool(args.get("need_docs")))
    if q.category and q.category not in db.CATEGORIES:
        q.category = ""
    return [_brief(s) for s in service.search(con, q, limit=8)]


def t_details(con, args):
    s = db.get_supplier(con, int(args["id"]))
    if not s:
        return {"error": "нет такого поставщика"}
    keep = ("id", "name", "city", "country", "website", "phone", "email", "tax_id", "products", "delivery_regions",
            "delivery_terms", "moq_value", "moq_unit", "free_delivery_from", "defer_days", "certificates", "price_info", "company")
    return {k: s.get(k) for k in keep}


def t_registry(con, args):
    try:
        changed, company, msg = service.enrich_registry(con, int(args["id"]))
        return {"message": msg, "company": company}
    except dadata.DadataError as e:
        return {"error": str(e)}


def t_deep(con, args):
    try:
        company, msg = service.enrich_deep(con, int(args["id"]))
        return {"message": msg, "deep": (company or {}).get("deep")}
    except checko.CheckoError as e:
        return {"error": str(e)}


def t_site(con, args):
    try:
        changed, res = service.enrich_site(con, int(args["id"]))
        return {"updated_fields": changed, "found": res.get("fields"), "method": res.get("method"), "error": res.get("error")}
    except site.FetchError as e:
        return {"error": str(e)}


def t_yandex(con, args):
    try:
        return yandex.search(args.get("text", ""), results=8)
    except yandex.YandexError as e:
        return {"error": str(e)}


TOOLS = {"search_suppliers": t_search, "supplier_details": t_details, "check_registry": t_registry, "deep_check": t_deep,
         "extract_from_site": t_site, "yandex_search": t_yandex}

SYSTEM = """Ты — ассистент закупщика сети ресторанов/доставки (HoReCa, Retail) в России и Казахстане.
Подбираешь поставщиков продуктов питания и упаковки. Отвечаешь по-русски.

Инструменты:
- search_suppliers {"category": один из %s или "", "text": "ключевые слова", "city": "город", "country": "RU"|"KZ", "order_sum": число, "order_kg": число, "need_docs": bool} — поиск и скоринг 0–100 по базе.
- supplier_details {"id": int} — полная карточка.
- check_registry {"id": int} — проверка юрлица в реестре (статус, возраст компании).
- deep_check {"id": int} — финансы, арбитраж, исполнительные производства (только РФ, лимит — вызывай для 1–3 финалистов).
- extract_from_site {"id": int} — прочитать сайт поставщика и извлечь мин. заказ, доставку, сертификаты (медленно, не более 2 раз).
- yandex_search {"text": "что + где, 2–4 слова, например «рыба оптом Казань»"} — живой поиск организаций на Яндекс.Картах (только РФ), если в базе мало вариантов. Длинные фразы дают пустой ответ.

Каждый ответ — ровно один JSON-объект, без текста вокруг:
{"thought": "что делаю и зачем", "tool": "имя", "args": {...}}
или финал:
{"thought": "...", "final": {"answer": "текст для закупщика", "supplier_ids": [id, ...]}}

Правила: опирайся только на данные инструментов, ничего не выдумывай. Сначала search_suppliers.
В финале: 3–5 поставщиков с причинами выбора (балл, город/доставка, мин. заказ, документы), риски/пробелы в данных
и следующий шаг (что запросить в КП). Если данных нет — честно скажи об этом."""


def _num(x):
    try:
        return float(x) if x not in (None, "") else None
    except (TypeError, ValueError):
        return None


def _preview(res):
    s = json.dumps(res, ensure_ascii=False)
    return s if len(s) < 600 else s[:600] + "…"


def run_rules(con, text):
    q = parse_query(text)
    ranked = service.search(con, q, limit=5)
    trace = [{"thought": "Разбор запроса правилами (LLM не подключена)", "tool": "parse_query",
              "args": {k: v for k, v in vars(q).items() if v and k != "weights"}, "result": ""},
             {"thought": "Поиск и скоринг по базе", "tool": "search_suppliers", "args": {}, "result": f"{len(ranked)} вариантов"}]
    if not ranked:
        return {"answer": "В базе нет подходящих поставщиков. Попробуйте живой поиск по Яндекс.Картам или расширьте регион.",
                "supplier_ids": [], "trace": trace, "mode": "rules"}
    lines = [f"Подобрал {len(ranked)} поставщиков" + (f" для «{db.CATEGORIES[q.category]}»" if q.category else "") +
             (f", город: {q.city}" if q.city else "") + ":"]
    for s in ranked:
        b = _brief(s)
        bits = [f"{b['score']}/100", s["city"]]
        if b["moq"]:
            bits.append(f"мин. заказ {b['moq']}")
        if b["certificates"]:
            bits.append("документы: " + ", ".join(b["certificates"]))
        lines.append(f"• {s['name']} — " + "; ".join(bits) + (f". Риски: {', '.join(b['flags'])}" if b["flags"] else ""))
    lines.append("Следующий шаг: проверьте лидеров по реестру и отправьте запрос КП (кнопки в карточке).")
    return {"answer": "\n".join(lines), "supplier_ids": [s["id"] for s in ranked], "trace": trace, "mode": "rules"}


def run(con, text):
    if not llm.enabled():
        return run_rules(con, text)
    msgs = [{"role": "system", "content": SYSTEM % ", ".join(f'"{k}"' for k in db.CATEGORIES)},
            {"role": "user", "content": text}]
    trace, model, site_calls = [], None, 0
    for step_no in range(MAX_STEPS):
        if step_no == MAX_STEPS - 1:  # последний шаг — только финал, без новых инструментов
            msgs.append({"role": "user", "content": "Шаги закончились. Сейчас верни final по уже собранным данным."})
        try:
            reply, model = llm.chat(msgs, max_tokens=2500)
            step = llm.parse_json(reply)
        except llm.LLMUnavailable as e:
            res = run_rules(con, text)
            res["trace"].insert(0, {"thought": f"LLM недоступна ({e}) — перехожу на правила", "tool": "", "args": {}, "result": ""})
            return res
        except ValueError:
            msgs += [{"role": "assistant", "content": reply},
                     {"role": "user", "content": "Ответ не является JSON. Верни ровно один JSON-объект по протоколу."}]
            continue
        if "final" in step:
            fin = step["final"] or {}
            ids = [int(i) for i in fin.get("supplier_ids", []) if str(i).isdigit()]
            trace.append({"thought": step.get("thought", ""), "tool": "final", "args": {}, "result": ""})
            return {"answer": fin.get("answer", ""), "supplier_ids": ids, "trace": trace, "mode": f"llm:{model}"}
        name, args = step.get("tool"), step.get("args") or {}
        if name == "extract_from_site":
            site_calls += 1
        if name not in TOOLS:
            result = {"error": f"нет инструмента {name}"}
        elif name == "extract_from_site" and site_calls > 2:
            result = {"error": "лимит чтения сайтов исчерпан"}
        else:
            try:
                result = TOOLS[name](con, args)
            except (KeyError, ValueError, TypeError) as e:
                result = {"error": f"неверные аргументы: {e}"}
        trace.append({"thought": step.get("thought", ""), "tool": name, "args": args, "result": _preview(result)})
        msgs += [{"role": "assistant", "content": json.dumps(step, ensure_ascii=False)},
                 {"role": "user", "content": f"Результат {name}: {json.dumps(result, ensure_ascii=False)[:6000]}"}]
    res = run_rules(con, text)
    res["trace"] = trace + [{"thought": "Лимит шагов исчерпан — итог собран правилами", "tool": "", "args": {}, "result": ""}]
    res["mode"] = f"llm:{model}+rules"
    return res
