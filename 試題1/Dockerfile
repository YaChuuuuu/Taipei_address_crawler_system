FROM python:3.11-slim

RUN apt-get update && DEBIAN_FRONTEND=noninteractive apt-get install -y \
    tzdata \
    chromium \
    chromium-driver \
    fonts-noto-cjk \
    libglib2.0-0 \
    libnss3 \
    libfontconfig1 \
    wget \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY 試題1/requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY 試題3/log_utils.py .
COPY 試題1/crawler.py .

EXPOSE 8001

CMD ["python", "crawler.py"]
