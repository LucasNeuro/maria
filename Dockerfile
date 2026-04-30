FROM python:3.11-slim

WORKDIR /app

RUN apt-get update && apt-get install -y --no-install-recommends \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY maria_os.py tools_maria.py uazapi_webhook.py run.py pyproject.toml ./
COPY specs ./specs

ENV PYTHONUNBUFFERED=1
# Render (e outros PaaS) injetam PORT; localmente usa 8000
ENV PORT=8000
EXPOSE 8000

CMD ["sh", "-c", "uvicorn maria_os:app --host 0.0.0.0 --port ${PORT}"]
