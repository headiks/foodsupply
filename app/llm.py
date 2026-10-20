"""LLM-каскад: OpenRouter (перебор бесплатных моделей — они часто упираются в лимиты) -> GigaChat.
GigaChat нужен для серверов в РФ: OpenRouter, OpenAI, Groq и др. блокируют российские IP (HTTP 403)."""
import json
import re
import ssl
import time
import uuid
from pathlib import Path

import httpx

from . import config

GIGA_OAUTH = "https://ngw.devices.sberbank.ru:9443/api/v2/oauth"
GIGA_CHAT = "https://gigachat.devices.sberbank.ru/api/v1/chat/completions"
# Сертификаты Сбера выпущены НУЦ Минцифры — его корня нет в стандартных хранилищах; проверку TLS не отключаем.
_GIGA_TLS = ssl.create_default_context(cafile=str(Path(__file__).parent / "certs" / "russian_trusted_root_ca.pem"))
_giga = {"token": None, "exp_ms": 0}


class LLMUnavailable(Exception):
    pass


def enabled():
    return bool(config.OPENROUTER_API_KEY or config.GIGACHAT_AUTH_KEY)


def _giga_token():
    """Токен живёт 30 минут — кэшируем и обновляем за минуту до истечения."""
    if _giga["token"] and time.time() * 1000 < _giga["exp_ms"] - 60_000:
        return _giga["token"]
    r = httpx.post(GIGA_OAUTH, data={"scope": config.GIGACHAT_SCOPE}, verify=_GIGA_TLS, timeout=20,
                   headers={"Authorization": f"Basic {config.GIGACHAT_AUTH_KEY}", "RqUID": str(uuid.uuid4()),
                            "Accept": "application/json"})
    r.raise_for_status()
    d = r.json()
    _giga.update(token=d["access_token"], exp_ms=d["expires_at"])
    return _giga["token"]


def _gigachat(messages, temperature, max_tokens):
    r = httpx.post(GIGA_CHAT, verify=_GIGA_TLS, timeout=120, headers={"Authorization": f"Bearer {_giga_token()}"},
                   json={"model": config.GIGACHAT_MODEL, "messages": messages,
                         "temperature": max(temperature, 0.01), "max_tokens": max_tokens})  # 0 GigaChat не принимает
    r.raise_for_status()
    d = r.json()
    return (d["choices"][0]["message"].get("content") or "").strip(), d.get("model", config.GIGACHAT_MODEL)


def chat(messages, temperature=0.2, max_tokens=1500):
    """Возвращает (текст, модель). Падает LLMUnavailable, если ни один провайдер не ответил."""
    if not enabled():
        raise LLMUnavailable("Не задан ни OPENROUTER_API_KEY, ни GIGACHAT_AUTH_KEY")
    errors = []
    for model in config.OPENROUTER_MODELS if config.OPENROUTER_API_KEY else []:
        try:
            r = httpx.post(
                "https://openrouter.ai/api/v1/chat/completions",
                headers={"Authorization": f"Bearer {config.OPENROUTER_API_KEY}",
                         "HTTP-Referer": "https://huggingface.co/spaces", "X-Title": "Food Supplier Finder"},
                json={"model": model, "messages": messages, "temperature": temperature, "max_tokens": max_tokens,
                      # «думающие» free-модели иначе тратят весь лимит на рассуждения и возвращают пустой content
                      "reasoning": {"effort": "low", "exclude": True}},
                timeout=60,
            )
            if r.status_code != 200:
                errors.append(f"{model}: HTTP {r.status_code}")
                continue
            text = (r.json()["choices"][0]["message"].get("content") or "").strip()
            if text:
                return text, model
            errors.append(f"{model}: пустой ответ")
        except (httpx.HTTPError, KeyError, ValueError) as e:
            errors.append(f"{model}: {type(e).__name__}")
    if config.GIGACHAT_AUTH_KEY:
        try:
            text, model = _gigachat(messages, temperature, max_tokens)
            if text:
                return text, f"gigachat:{model}"
            errors.append("GigaChat: пустой ответ")
        except (httpx.HTTPError, KeyError, ValueError) as e:
            errors.append(f"GigaChat: {type(e).__name__}")
    raise LLMUnavailable("; ".join(errors))


def parse_json(text):
    """Модели любят оборачивать JSON в ```json ...``` или добавлять текст — достаём первый объект."""
    text = re.sub(r"<think>.*?</think>", "", text, flags=re.S)
    start = text.find("{")
    if start < 0:
        raise ValueError("JSON не найден в ответе модели")
    depth, in_str, esc = 0, False, False
    for i, ch in enumerate(text[start:], start):
        if in_str:
            if esc:
                esc = False
            elif ch == "\\":
                esc = True
            elif ch == '"':
                in_str = False
            continue
        if ch == '"':
            in_str = True
        elif ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                return json.loads(text[start:i + 1])
    raise ValueError("Незакрытый JSON в ответе модели")
