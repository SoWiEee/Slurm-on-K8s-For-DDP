FROM python:3.12-slim

WORKDIR /app

# OS deps for lightgbm wheel + healthz curl
RUN apt-get update && apt-get install -y --no-install-recommends \
        libgomp1 curl ca-certificates \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt /app/requirements.txt
RUN pip install --no-cache-dir -r requirements.txt

# Service code (importable as `runtime_predictor` package)
COPY __init__.py features.py train.py app.py /app/runtime_predictor/

ENV PYTHONPATH=/app \
    MODEL_PATH=/models/predictor.pkl \
    LOG_LEVEL=INFO

EXPOSE 8080

HEALTHCHECK --interval=30s --timeout=3s --start-period=10s --retries=3 \
    CMD curl -fsS http://127.0.0.1:8080/healthz || exit 1

CMD ["uvicorn", "runtime_predictor.app:app", "--host", "0.0.0.0", "--port", "8080"]
