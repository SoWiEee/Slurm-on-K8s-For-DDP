FROM python:3.12-slim

WORKDIR /app

RUN apt-get update && apt-get install -y --no-install-recommends \
        curl ca-certificates \
    && rm -rf /var/lib/apt/lists/*

COPY services/weight_tuner/requirements.txt /app/requirements.txt
RUN pip install --no-cache-dir -r requirements.txt

# Service code + sim package (sim_env.py imports sim.loader / sim.runner for
# the default_arm_grid context vector; weight-tuner itself never runs sim).
COPY sim /app/sim
COPY services/weight_tuner /app/services/weight_tuner
COPY services/__init__.py /app/services/__init__.py

ENV PYTHONPATH=/app \
    PORT=8003 \
    LOG_LEVEL=INFO \
    COLLECTOR_INTERVAL_S=300 \
    MIN_JOBS_FOR_UPDATE=5 \
    STATE_DIR=/state \
    SLURMRESTD_URL=http://slurm-restapi.slurm.svc:6820 \
    SLURM_API_VERSION=v0.0.41 \
    SLURM_USER=root \
    BETA_FIXED=0.20 \
    UCB1_C=1.0

EXPOSE 8003

HEALTHCHECK --interval=30s --timeout=3s --start-period=10s --retries=3 \
    CMD curl -fsS http://127.0.0.1:8003/healthz || exit 1

CMD ["python", "-m", "services.weight_tuner.serve", \
     "--port", "8003", "--host", "0.0.0.0"]
