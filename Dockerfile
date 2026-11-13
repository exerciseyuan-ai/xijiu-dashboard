FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PORT=8080 \
    FEISHU_SYNC_CACHE_DIR=/tmp/feishu-dashboard \
    FEISHU_SYNC_CONCURRENCY=2 \
    FEISHU_CHUNK_CONCURRENCY=4

WORKDIR /app

RUN useradd --create-home --uid 10001 appuser \
    && mkdir -p /app /tmp/feishu-dashboard \
    && chown -R appuser:appuser /app /tmp/feishu-dashboard

COPY --chown=appuser:appuser feishu_sync_server.py ./
COPY --chown=appuser:appuser xjdashboardV3.3.10.html ./
COPY --chown=appuser:appuser "数据计算口径与来源说明.html" ./
COPY --chown=appuser:appuser vendor ./vendor
COPY --chown=appuser:appuser webfonts ./webfonts

USER appuser

EXPOSE 8080

HEALTHCHECK --interval=30s --timeout=5s --start-period=15s --retries=3 \
    CMD python -c "import os, urllib.request; port=os.environ.get('PORT','8080'); urllib.request.urlopen('http://127.0.0.1:'+port+'/healthz', timeout=3)"

CMD ["python", "feishu_sync_server.py"]
