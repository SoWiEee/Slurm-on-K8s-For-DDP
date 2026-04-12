FROM python:3.11-slim
RUN pip install --no-cache-dir prometheus-client==0.20.0
COPY exporter.py /exporter.py
ENTRYPOINT ["python", "/exporter.py"]
