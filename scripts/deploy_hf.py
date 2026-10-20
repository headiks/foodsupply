"""Деплой на Hugging Face Spaces: создаёт Docker-Space, кладёт ключи из .env в Secrets, загружает код.

    pip install huggingface_hub
    hf auth login                      # токен с правом write: https://huggingface.co/settings/tokens
    python scripts/deploy_hf.py <user>/foodsupply

Ключи берутся из .env и уходят только в Secrets Space — в код и git они не попадают.
"""
import sys
from pathlib import Path

from huggingface_hub import HfApi

ROOT = Path(__file__).resolve().parent.parent
KEYS = ("OPENROUTER_API_KEY", "OPENROUTER_MODELS", "GIGACHAT_AUTH_KEY", "GIGACHAT_SCOPE", "DADATA_API_KEY", "YANDEX_GEOSEARCH_KEY", "YANDEX_GEOSEARCH_SECRET", "YANDEX_MAPS_JS_KEY", "CHECKO_API_KEY")


def read_env():
    env = {}
    for line in (ROOT / ".env").read_text(encoding="utf-8").splitlines():
        k, sep, v = line.partition("=")
        if sep and not k.lstrip().startswith("#"):
            env[k.strip()] = v.split(" #")[0].strip()
    return env


def main():
    if len(sys.argv) != 2 or "/" not in sys.argv[1]:
        sys.exit("Использование: python scripts/deploy_hf.py <hf-user>/<space-name>")
    repo = sys.argv[1]
    api = HfApi()
    api.create_repo(repo, repo_type="space", space_sdk="docker", exist_ok=True)
    env = read_env()
    for k in KEYS:
        if env.get(k):
            api.add_space_secret(repo, k, env[k])
            print("secret:", k)
    api.upload_folder(folder_path=str(ROOT), repo_id=repo, repo_type="space", commit_message="deploy",
                      ignore_patterns=[".env", ".git/*", ".claude/*", "data/app.db", "**/__pycache__/*", ".pytest_cache/*"])
    print(f"Готово: https://huggingface.co/spaces/{repo}  (сборка ~2–3 мин)")
    print(f"Приложение: https://{repo.replace('/', '-').replace('_', '-').lower()}.hf.space")


if __name__ == "__main__":
    main()
