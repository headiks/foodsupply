#!/opt/rh/rh-python311/root/usr/bin/python3.11
"""CGI -> uvicorn на 127.0.0.1. На виртуальном хостинге нет [P]-проксирования в .htaccess,
поэтому пересылаем запрос сами (только stdlib: скрипт стартует на каждый запрос, должен быть лёгким).
Unix-сокет не подходит: SELinux запрещает CGI Apache подключаться к сокетам пользователя, TCP на localhost разрешён."""
import http.client
import os
import subprocess
import sys
import time

APP = "/home/a1319063/foodsupply"
PORT = 18731
HOP = {"transfer-encoding", "connection", "keep-alive"}


def forward(method, uri, body, headers):
    c = http.client.HTTPConnection("127.0.0.1", PORT, timeout=300)  # агент с LLM может думать до минуты
    c.request(method, uri, body or None, headers)
    return c.getresponse()


env = os.environ
body = sys.stdin.buffer.read(int(env.get("CONTENT_LENGTH") or 0))
headers = {k[5:].replace("_", "-"): v for k, v in env.items() if k.startswith("HTTP_")}
if env.get("CONTENT_TYPE"):
    headers["CONTENT-TYPE"] = env["CONTENT_TYPE"]
args = (env.get("REQUEST_METHOD", "GET"), env.get("REQUEST_URI") or "/", body, headers)
try:
    r = forward(*args)
except ConnectionRefusedError:
    subprocess.Popen([f"{APP}/deploy/run.sh"], start_new_session=True,
                     stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    for _ in range(40):  # холодный старт uvicorn ~2–5 с
        time.sleep(0.5)
        try:
            r = forward(*args)
            break
        except ConnectionRefusedError:
            continue
    else:
        sys.stdout.write("Status: 503 Service Unavailable\r\nContent-Type: text/plain; charset=utf-8\r\n\r\n"
                         "Сервис запускается, обновите страницу через минуту.")
        sys.exit(0)

out = sys.stdout.buffer
out.write(f"Status: {r.status} {r.reason}\r\n".encode())
for k, v in r.getheaders():
    if k.lower() not in HOP:
        out.write(f"{k}: {v}\r\n".encode("latin-1"))
out.write(b"\r\n")
out.write(r.read())
