"""OpenRouter (OpenAI-совместимый API) с перебором бесплатных моделей: free-модели часто упираются в лимиты."""
import json
import re

import httpx

from . import config


class LLMUnavailable(Exception):
    pass


def enabled():
    return bool(config.OPENROUTER_API_KEY)


def chat(messages, temperature=0.2, max_tokens=1500):
    """Возвращает (текст, модель). Падает LLMUnavailable, если ни одна модель не ответила."""
    if not enabled():
        raise LLMUnavailable("OPENROUTER_API_KEY не задан")
    errors = []
    for model in config.OPENROUTER_MODELS:
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
