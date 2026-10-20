# Деплой на виртуальный хостинг (Sprintbox / SpaceWeb и похожие)

На таком хостинге нет Docker и systemd, а `.htaccess` не умеет проксировать (`[P]`). Схема:

```
браузер → Apache → .htaccess → proxy.cgi ──unix-сокет──▶ uvicorn (FastAPI)
                                   └─ если сокета нет — запускает run.sh
cron (раз в минуту) → run.sh — поднимает uvicorn после перезагрузки сервера
```

- Код лежит в `~/foodsupply`, вне `public_html`, поэтому `.env` недоступен из веба.
- База `~/foodsupply/data/app.db` постоянная: заметки и статусы сохраняются.
- uvicorn слушает unix-сокет, а не порт: на общем хостинге порты могут пересечься, а сокет доступен только владельцу.

Установка и обновление (из корня проекта на своей машине):
```bash
git archive HEAD | ssh USER@HOST "mkdir -p ~/foodsupply && tar -x -C ~/foodsupply"
scp .env USER@HOST:~/foodsupply/.env
ssh USER@HOST "bash ~/foodsupply/deploy/install.sh"
```
В `proxy.cgi`, `run.sh` и `install.sh` путь `/home/a1319063`, домен и путь к Python замените на свои.

**Ограничение:** OpenRouter, OpenAI, Groq и большинство зарубежных LLM-API блокируют российские IP. На сервере в РФ нужен российский провайдер (GigaChat, YandexGPT) — без LLM сервис работает в режиме правил.
