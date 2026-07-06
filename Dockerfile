# WhatsApp Web Automation — painel Flask + Playwright
FROM mcr.microsoft.com/playwright/python:v1.49.1-jammy

WORKDIR /app

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    FLASK_HOST=0.0.0.0 \
    FLASK_PORT=5014 \
    FLASK_OPEN_BROWSER=false

COPY requirements.txt .
RUN pip install --upgrade pip && pip install -r requirements.txt

COPY src ./src
COPY config ./config
COPY docker/entrypoint.sh /entrypoint.sh
RUN sed -i 's/\r$//' /entrypoint.sh \
    && chmod +x /entrypoint.sh \
    && mkdir -p /data/profile /data/exports /data/state /data/cache \
    && cp config/targets.example.json config/targets.json

EXPOSE 5014

ENTRYPOINT ["/entrypoint.sh"]
CMD ["python", "src/app.py"]
