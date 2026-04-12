FROM python:3.11-slim

RUN pip install --no-cache-dir prometheus-client==0.20.0 kubernetes==30.1.0

WORKDIR /app
COPY operator/ /app/

ENTRYPOINT ["python", "/app/main.py"]
