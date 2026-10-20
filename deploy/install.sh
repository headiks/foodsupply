#!/bin/bash
# Ставит зависимости, cron и публикует сайт. Повторный запуск безопасен (так же обновляется код).
set -e
APP=/home/a1319063/foodsupply
WEB=/home/a1319063/domains/a1319063.xsph.ru/public_html
PY=/opt/rh/rh-python311/root/usr/bin/python3.11
cd $APP
chmod 600 .env
[ -d .venv ] || $PY -m venv .venv
.venv/bin/pip install -q --upgrade pip
.venv/bin/pip install -q -r requirements.txt
chmod 755 deploy/run.sh deploy/proxy.cgi
# cron: поднять сервис после перезагрузки сервера или падения
( crontab -l 2>/dev/null | grep -v foodsupply/deploy/run.sh; echo "* * * * * $APP/deploy/run.sh" ) | crontab -
# публикация: заглушку хостинга убираем в бэкап, ставим прокси
[ -f $WEB/index.php ] && mv -n $WEB/index.php $WEB/index.php.bak
rm -rf $WEB/fs-test
cp deploy/proxy.cgi $WEB/proxy.cgi && chmod 755 $WEB/proxy.cgi
cp deploy/htaccess $WEB/.htaccess
# статику отдаёт сам веб-сервер хостинга (фронтовый nginx перехватывает .css/.js по расширению)
rm -rf $WEB/static && cp -r app/static $WEB/static
# перезапуск приложения с новым кодом
pkill -u "$(whoami)" -f "[u]vicorn app.main:app" || true
sleep 1
deploy/run.sh
for i in $(seq 1 30); do curl -s -o /dev/null http://127.0.0.1:18731/health && break; sleep 0.5; done
curl -s http://127.0.0.1:18731/health && echo " <- health OK"
rm -f $WEB/dbg.cgi
