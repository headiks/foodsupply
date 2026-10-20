import os
import tempfile

os.environ["DB_PATH"] = os.path.join(tempfile.mkdtemp(), "test.db")
for k in ("OPENROUTER_API_KEY", "DADATA_API_KEY", "YANDEX_GEOSEARCH_KEY", "YANDEX_MAPS_JS_KEY", "CHECKO_API_KEY"):
    os.environ[k] = ""  # пустое значение, а не pop: иначе подтянется локальный .env и тесты пойдут в живые API

from fastapi.testclient import TestClient  # noqa: E402

from app import agent, db, llm  # noqa: E402
from app.main import app, con  # noqa: E402
from app.scoring import Query, score  # noqa: E402
from app.sources import site  # noqa: E402

client = TestClient(app)


def sup(**kw):
    base = {"id": 1, "name": "Тест", "country": "RU", "city": "Москва", "categories": ["fish"], "products": "лосось охлаждённый",
            "delivery_regions": ["Москва и МО"], "certificates": [], "provenance": {}, "updated_at": "2026-10-20"}
    return base | kw


def test_score_prefers_profile_category_and_city():
    q = Query(category="fish", city="Москва")
    a = score(sup(), q)["total"]
    b = score(sup(categories=["broadline"], city="Новосибирск", delivery_regions=["Новосибирск"]), q)["total"]
    assert a > b


def test_moq_above_order_lowers_score_and_flags():
    q = Query(order_sum=10000)
    ok = score(sup(moq_value=5000, moq_unit="RUB"), q)
    bad = score(sup(moq_value=100000, moq_unit="RUB"), q)
    assert ok["total"] > bad["total"]
    assert any("Мин. заказ выше" in f[1] for f in bad["flags"])


def test_inactive_company_zero_reliability():
    s = score(sup(company={"status": "LIQUIDATED", "registration_date": "2010-01-01"}), Query())
    rel = next(p for p in s["parts"] if p["key"] == "reliability")
    assert rel["value"] == 0 and any(f[0] == "red" for f in s["flags"])


def test_zero_weights_do_not_crash():
    assert score(sup(), Query(weights={k: 0 for k in Query().weights}))["total"] == 0


def test_parse_query_rules():
    q = agent.parse_query("Нужен лосось для суши в СПб, 150 кг, заказ 40 тыс ₽, с Меркурием")
    assert (q.category, q.city, q.country, q.order_kg, q.order_sum, q.need_docs) == ("asian", "Санкт-Петербург", "RU", 150, 40000, True)
    assert agent.parse_query("упаковка Алматы").country == "KZ"
    assert agent.parse_query("упаковка для доставки суши").category == "packaging"


def test_regex_extract():
    pages = [{"url": "https://x.ru/", "text": "Минимальная сумма заказа — 7 000 руб. Бесплатная доставка по Москве от 15 000 ₽. "
                                              "Отсрочка платежа до 14 дней. ИНН 7724400648. Работаем в системе Меркурий, есть декларации соответствия."}]
    f, _ = site.extract_regex(pages)
    assert f["moq_value"] == 7000 and f["moq_unit"] == "RUB" and f["free_delivery_from"] == 15000
    assert f["defer_days"] == 14 and f["tax_id"] == "7724400648"
    assert {"Меркурий", "Декларации ЕАЭС"} <= set(f["certificates"])


def test_ssrf_blocked():
    for url in ("http://127.0.0.1/", "http://localhost:8000/", "file:///etc/passwd", "http://169.254.169.254/"):
        try:
            site.fetch_site(url)
            assert False, url
        except site.FetchError:
            pass


def test_parse_json_from_noisy_reply():
    assert llm.parse_json('Вот ответ:\n```json\n{"a": "x}\\"y", "b": {"c": 1}}\n```') == {"a": 'x}"y', "b": {"c": 1}}


def test_pages_render_and_seed_loaded():
    assert client.get("/health").json()["suppliers"] >= 90
    for url in ("/", "/?category=fish&city=Москва&order_sum=20000", "/results?category=packaging&country=KZ", "/s/1",
                "/compare?ids=1,2,3", "/pipeline", "/live", "/add", "/agent", "/about", "/login", "/export.csv?ids=1,2"):
        r = client.get(url)
        assert r.status_code == 200, url


def test_rules_agent_and_team_flow():
    r = client.post("/agent/ask", data={"text": "овощи для ресторанов в Москве"})
    assert r.status_code == 200 and "Подобрал" in r.text
    client.post("/login", data={"name": "Анна", "next": "/"})
    client.post("/s/1/note", data={"text": "звонила, ждём прайс"})
    client.post("/s/1/status", data={"status": "shortlist"})
    assert db.notes_for(con, 1)[0]["author"] == "Анна"
    assert db.get_supplier(con, 1)["status"]["status"] == "shortlist"
    assert "Гросс" in client.get("/export.csv").text or client.get("/export.csv").status_code == 200


def test_manual_edit_records_provenance():
    client.post("/login", data={"name": "Олег", "next": "/"})
    client.post("/s/2/edit", data={"price_info": "лосось 1650 ₽/кг", "categories": ["fish"]})
    s = db.get_supplier(con, 2)
    assert s["price_info"] == "лосось 1650 ₽/кг" and s["provenance"]["price_info"]["source"] == "manual"


def test_checko_parsing_and_deep_risk(monkeypatch):
    from app.sources import checko
    fake = {
        "company": {"НаимСокр": "ООО Тест", "ИНН": "7700000000", "Статус": {"Наим": "Действует"}, "ДатаРег": "2015-03-01",
                    "ОКВЭД": {"Код": "46.32", "Наим": "Опт мяса"}, "ЮрАдрес": {"АдресРФ": "г. Москва"}, "Руковод": [{"ФИО": "Иванов И.И."}],
                    "СЧР": 42, "Контакты": {"Тел": ["+74950000000"], "Емэйл": ["a@b.ru"]}},
        "finances": {"2023": {"2110": 100_000_000, "2400": -5_000_000}, "2022": {"2110": 80_000_000}},
        "legal-cases": {"ЗапВсего": 3, "ОбщСуммИск": 9_000_000},
        "enforcements": {"ЗапВсего": 2, "ОстЗадолж": 150000},
    }
    monkeypatch.setattr(checko, "_get", lambda method, inn, **kw: fake[method])
    c = checko.company("7700000000")
    assert (c["status"], c["employees"], c["okved"], c["phones"][0]) == ("ACTIVE", 42, "46.32", "+74950000000")
    c["deep"] = checko.deep("7700000000")
    assert c["deep"]["revenue_growth"] == 25 and c["deep"]["enforcements"] == 2 and c["deep"]["enforcement_debt"] == 150000
    clean = score(sup(company={**c, "deep": {}}), Query())
    risky = score(sup(company=c), Query())
    rel = lambda r: next(p["value"] for p in r["parts"] if p["key"] == "reliability")  # noqa: E731
    assert rel(risky) < rel(clean)
    assert any("ФССП" in f[1] for f in risky["flags"]) and any("Убыток" in f[1] for f in risky["flags"])
    assert any("Иски" in f[1] for f in risky["flags"])


def test_yandex_signature_is_urlsafe_hmac():
    from app.sources import yandex
    sig = yandex.sign("/v1/?apikey=k&text=x", "secret")
    assert sig == "VVgxZ6FCWbNek8r66DB-TlJJI5bRzyBuaiHM2TyjqGg="  # сверено с openssl dgst -sha256 -hmac
