"""Точка входа Vercel: Python-рантайм ищет ASGI-объект `app` в этом файле."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.main import app  # noqa: E402,F401
