"""Яндекс «Поиск по организациям». По условиям API результаты показываются на Яндекс.Карте и не хранятся
массово: в базу попадает только то, что пользователь явно добавил кнопкой."""
import base64
import hashlib
import hmac
from urllib.parse import urlencode

import httpx

from .. import config


class YandexError(Exception):
    pass


def enabled():
    return bool(config.YANDEX_GEOSEARCH_KEY)


def sign(path_and_query, secret):
    return base64.urlsafe_b64encode(hmac.new(secret.encode(), path_and_query.encode(), hashlib.sha256).digest()).decode()


def search(text, results=15):
    if not enabled():
        raise YandexError("YANDEX_GEOSEARCH_KEY не задан")
    q = urlencode({"apikey": config.YANDEX_GEOSEARCH_KEY, "text": text, "lang": "ru_RU", "type": "biz", "results": results})
    url = f"https://search-maps.yandex.ru/v1/?{q}"
    if config.YANDEX_GEOSEARCH_SECRET:  # включена «подпись запросов»: HMAC-SHA256 от пути с параметрами
        url += "&signature=" + sign(f"/v1/?{q}", config.YANDEX_GEOSEARCH_SECRET)
    try:
        r = httpx.get(url, timeout=15)
    except httpx.HTTPError as e:
        raise YandexError(f"сеть: {type(e).__name__}") from e
    if r.status_code != 200:
        raise YandexError(f"HTTP {r.status_code}")
    out = []
    for f in r.json().get("features", []):
        meta = f["properties"].get("CompanyMetaData", {})
        lon, lat = f["geometry"]["coordinates"]
        out.append({
            "yandex_id": meta.get("id"), "name": meta.get("name") or f["properties"].get("name"),
            "address": meta.get("address") or f["properties"].get("description"),
            "website": meta.get("url"), "phone": next((p.get("formatted") for p in meta.get("Phones", [])), None),
            "categories": [c.get("name") for c in meta.get("Categories", [])],
            "hours": (meta.get("Hours") or {}).get("text"), "lat": lat, "lon": lon,
        })
    return out
