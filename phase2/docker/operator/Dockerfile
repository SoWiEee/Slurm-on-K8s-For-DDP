FROM python:3.11-slim

ARG KUBECTL_VERSION=v1.30.0

RUN apt-get update \
    && apt-get install -y --no-install-recommends ca-certificates curl bash \
    && rm -rf /var/lib/apt/lists/*

RUN curl -fsSL -o /usr/local/bin/kubectl "https://dl.k8s.io/release/${KUBECTL_VERSION}/bin/linux/amd64/kubectl" \
    && chmod +x /usr/local/bin/kubectl

WORKDIR /app
COPY phase2/operator/main.py /app/main.py

ENTRYPOINT ["python", "/app/main.py"]
