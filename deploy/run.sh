#!/bin/bash
# Поднимает uvicorn, если он не запущен. Вызывается cron раз в минуту и proxy.cgi при падении.
# Блокировку держит сам uvicorn (наследует fd 9): пока он жив — повторный запуск сразу выходит.
cd /home/a1319063/foodsupply || exit 1
exec 9>deploy/run.lock
flock -n 9 || exit 0
rm -f app.sock
setsid .venv/bin/uvicorn app.main:app --uds app.sock --workers 1 >> deploy/uvicorn.log 2>&1 < /dev/null &
