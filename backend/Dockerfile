FROM python:3.12-slim

WORKDIR /tmp/build
COPY . .

WORKDIR /app
RUN apt-get update \
    && apt-get install -y --no-install-recommends ca-certificates \
    && rm -rf /var/lib/apt/lists/*
RUN set -e; \
    if [ -f /tmp/build/backend/requirements.txt ]; then \
      pip install --no-cache-dir -r /tmp/build/backend/requirements.txt; \
      cp /tmp/build/backend/main.py /app/main.py; \
    else \
      pip install --no-cache-dir -r /tmp/build/requirements.txt; \
      cp /tmp/build/main.py /app/main.py; \
    fi; \
    mkdir -p /app/static/js /app/uploads; \
    cp /tmp/build/*.html /app/static/ 2>/dev/null || true; \
    cp /tmp/build/js/*.js /app/static/js/ 2>/dev/null || true; \
    cp /tmp/build/static/*.html /app/static/ 2>/dev/null || true; \
    cp /tmp/build/static/js/*.js /app/static/js/ 2>/dev/null || true; \
    chmod -R a+rX /app/static; \
    chmod 777 /app/uploads

ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONUNBUFFERED=1
ENV HTTP_PORT=8080
ENV STATIC_DIR=/app/static
ENV UPLOAD_DIR=/app/uploads

EXPOSE 8080
USER nobody

CMD ["sh", "-c", "uvicorn main:app --host 0.0.0.0 --port ${HTTP_PORT}"]
