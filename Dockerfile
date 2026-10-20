FROM python:3.11-slim
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY app ./app
COPY data/suppliers.json ./data/suppliers.json
# БД в /tmp: на бесплатных хостингах диск эфемерный, при рестарте база пересоздаётся из стартовой
ENV DB_PATH=/tmp/app.db PYTHONUNBUFFERED=1
EXPOSE 7860
# Render/Koyeb задают порт через $PORT, HF Spaces ждёт 7860
CMD uvicorn app.main:app --host 0.0.0.0 --port ${PORT:-7860}
