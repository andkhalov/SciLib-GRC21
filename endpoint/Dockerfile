FROM python:3.11-slim

WORKDIR /app

RUN apt-get update && apt-get install -y --no-install-recommends \
    gcc libpq-dev curl \
    && rm -rf /var/lib/apt/lists/*

COPY services/lean-grag/requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY services/lean-grag/src ./src

ENV PYTHONPATH=/app/src
ENV PYTHONUNBUFFERED=1

EXPOSE 8503

HEALTHCHECK --interval=30s --timeout=10s --retries=3 \
    CMD curl -f http://localhost:8503/health || exit 1

CMD ["python", "src/main.py"]
