FROM python:3.12-slim

WORKDIR /app

RUN apt-get update && apt-get install -y --no-install-recommends \
        curl ca-certificates libgomp1 \
    && rm -rf /var/lib/apt/lists/*

COPY services/rl_scheduler/requirements.txt /app/requirements.txt
RUN pip install --no-cache-dir -r requirements.txt

# Service code — copy sim + service packages so serve.py's `from sim...`
# imports resolve. The sim package is tiny and pure-python.
COPY sim /app/sim
COPY services/rl_scheduler /app/services/rl_scheduler
COPY services/__init__.py /app/services/__init__.py

# Bake the DSAC checkpoint into the image at /models so the container can
# come up without external storage. Override at runtime by mounting a PVC at
# /models if a different policy is wanted.
COPY runs/eval_mlp_20260514-210824/train/dsac.pt /models/dsac.pt

ENV PYTHONPATH=/app \
    LOG_LEVEL=INFO \
    SHADOW_MODE=true \
    SNAPSHOT_TTL_S=30 \
    PRIORITY_BOOST=1000

EXPOSE 8002

HEALTHCHECK --interval=30s --timeout=3s --start-period=10s --retries=3 \
    CMD curl -fsS http://127.0.0.1:8002/healthz || exit 1

CMD ["python", "-m", "services.rl_scheduler.serve", \
     "--policy-dir", "/models", "--port", "8002", "--host", "0.0.0.0"]
