# Build this checkout; no credentials or local runtime data enter the image.
FROM python:3.13-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    TZ=Asia/Taipei \
    MALLOC_CONF="background_thread:true,dirty_decay_ms:5000,muzzy_decay_ms:5000"

# ld.so's preload file uses the discovered path on both amd64 and arm64.
RUN apt-get update && \
    apt-get install -y --no-install-recommends tzdata libjemalloc2 && \
    find /usr/lib -name libjemalloc.so.2 -print -quit > /etc/ld.so.preload && \
    test -s /etc/ld.so.preload && \
    ln -sf /usr/share/zoneinfo/Asia/Taipei /etc/localtime && \
    echo "Asia/Taipei" > /etc/timezone && \
    rm -rf /var/lib/apt/lists/* && \
    groupadd --gid 10001 agy2api && \
    useradd --uid 10001 --gid 10001 --no-create-home --shell /usr/sbin/nologin agy2api

WORKDIR /app
COPY requirements.lock ./
RUN pip install --require-hashes -r requirements.lock
COPY . .
RUN mkdir -p /app/creds /app/data/request_logs && \
    chown -R 10001:10001 /app/creds /app/data && \
    chmod 700 /app/creds /app/data/request_logs
USER 10001:10001
EXPOSE 7861
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
    CMD python -c "import os, urllib.request; urllib.request.urlopen('http://127.0.0.1:' + os.environ.get('PORT', '7861') + '/health/ready', timeout=3)"
CMD ["python", "web.py"]
