# Ads Engine – önjáró Google Ads kezelő (Coolify / Docker)
#
# Az állapot a /data kötetben él (SQLite, jelentések, gyorsítótár): a Coolify-ban tartós tárolót kell kötni a /data útvonalra.
# A titkok (GADS_SA_JSON_B64, ANTHROPIC_API_KEY, SMTP_*, UMAMI_*) futásidejű környezeti változók – a képbe soha nem kerülnek.
FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    DATA_DIR=/data \
    ADS_CACHE_DIR=/data/cache \
    TZ=Europe/Budapest \
    PORT=8080

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY VERSION ./VERSION
COPY schema ./schema
COPY config ./config
COPY ads_engine ./ads_engine
COPY docker-entrypoint.sh /usr/local/bin/docker-entrypoint.sh

RUN useradd --uid 10001 --no-create-home --shell /usr/sbin/nologin engine \
    && mkdir -p /data/cache \
    && chown -R 10001:10001 /data \
    && chmod +x /usr/local/bin/docker-entrypoint.sh

VOLUME /data
EXPOSE 8080

# Nem szigorú ellenőrzés: az ütemező-ciklus él-e. (A tartósan hibázó Google-kapcsolat miatt ne induljon újra a konténer: arról levelet küld a motor.)
HEALTHCHECK --interval=60s --timeout=10s --start-period=40s --retries=3 CMD ["python", "-m", "ads_engine", "healthcheck"]

ENTRYPOINT ["docker-entrypoint.sh"]
CMD ["python", "-m", "ads_engine", "serve"]
