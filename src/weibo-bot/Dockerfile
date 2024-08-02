FROM python:3.11.5-slim-bookworm

RUN set -eux; \
    apt-get update; \
    apt-get install -y --no-install-recommends \
        chromium \
        chromium-driver \
    ; \
    rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY . .

RUN set -eux; \
    pip install --no-cache-dir -r requirements.txt

ENV TZ="Asia/Shanghai"

ENTRYPOINT ["python", "bot.py", "--real"]
