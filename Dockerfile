FROM python:3.12-slim
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1
ARG http_proxy=
ARG https_proxy=
ARG HTTP_PROXY=
ARG HTTPS_PROXY=
RUN apt-get update && apt-get install -y --no-install-recommends \
    chromium \
    chromium-driver \
    fonts-liberation \
    libnss3 \
    libatk-bridge2.0-0 \
    libatk1.0-0 \
    libgtk-3-0 \
    libdrm2 \
    libxkbcommon0 \
    libxcomposite1 \
    libxdamage1 \
    libxfixes3 \
    libxrandr2 \
    libgbm1 \
    libasound2 \
    xclip \
    xdotool \
    xvfb \
    && rm -rf /var/lib/apt/lists/*

RUN mkdir -p /dev/shm && chmod 1777 /dev/shm

ENV DISPLAY=:99
RUN printf '#!/bin/sh\nexec /usr/bin/chromedriver --allowed-ips="" --allowed-origins="*" "$@"\n' \
    > /usr/local/bin/chromedriver \
    && chmod +x /usr/local/bin/chromedriver
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY . .
EXPOSE ${PORT:-5000}
CMD ["bash", "start.sh"]
