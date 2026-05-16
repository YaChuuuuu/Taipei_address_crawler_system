FROM python:3.11-slim

RUN apt-get update && DEBIAN_FRONTEND=noninteractive apt-get install -y \
    tzdata \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY 試題2/requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY 試題3/log_utils.py .
COPY 試題2/api.py .

CMD ["uvicorn", "api:app", "--host", "0.0.0.0", "--port", "8000"]
