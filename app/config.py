import os
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

# Локальный .env (в git/Docker не попадает). Уже заданные переменные окружения важнее файла.
_env = ROOT / ".env"
if _env.exists():
    for line in _env.read_text(encoding="utf-8").splitlines():
        k, sep, v = line.partition("=")
        if sep and k.strip() and not k.lstrip().startswith("#"):
            os.environ.setdefault(k.strip(), v.split(" #")[0].strip())
# На Vercel файловая система только для чтения, кроме /tmp
DB_PATH = os.getenv("DB_PATH") or ("/tmp/app.db" if os.getenv("VERCEL") else str(ROOT / "data" / "app.db"))
SEED_PATH = ROOT / "data" / "suppliers.json"

OPENROUTER_API_KEY = os.getenv("OPENROUTER_API_KEY", "")
# Бесплатные модели OpenRouter меняются — список задаётся env, перебирается по очереди при ошибке/лимите.
OPENROUTER_MODELS = [m.strip() for m in os.getenv(
    "OPENROUTER_MODELS",
    "minimax/minimax-m3.1-flash:free,nvidia/nemotron-3-super-120b-a12b:free,google/gemma-4-31b-it:free",
).split(",") if m.strip()]

GIGACHAT_AUTH_KEY = os.getenv("GIGACHAT_AUTH_KEY", "")  # ключ авторизации (Basic) из кабинета developers.sber.ru
GIGACHAT_SCOPE = os.getenv("GIGACHAT_SCOPE", "GIGACHAT_API_PERS")
GIGACHAT_MODEL = os.getenv("GIGACHAT_MODEL", "GigaChat")

DADATA_API_KEY = os.getenv("DADATA_API_KEY", "")
YANDEX_GEOSEARCH_KEY = os.getenv("YANDEX_GEOSEARCH_KEY", "")
YANDEX_GEOSEARCH_SECRET = os.getenv("YANDEX_GEOSEARCH_SECRET", "")  # секрет «подписи запросов», если она включена
YANDEX_MAPS_JS_KEY = os.getenv("YANDEX_MAPS_JS_KEY", "")
CHECKO_API_KEY = os.getenv("CHECKO_API_KEY", "")

STALE_DAYS = 180  # данные старше — флаг «устарело»
