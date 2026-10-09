# KIMS WhatsApp acknowledgement service
#   docker compose build                                   runtime image (target: runtime)
#   docker build --target runtime -t kims-wa-service .    same, without compose
#   docker build --target test .                           runs the test suite inside the image

FROM python:3.13-slim-bookworm AS runtime

# WeasyPrint needs Pango/HarfBuzz; DejaVu Sans Mono is the slip font
RUN apt-get update \
 && apt-get install -y --no-install-recommends \
      libpango-1.0-0 libpangoft2-1.0-0 libharfbuzz0b libharfbuzz-subset0 fonts-dejavu-core \
 && rm -rf /var/lib/apt/lists/*

RUN useradd --system --uid 10001 --home-dir /app --shell /usr/sbin/nologin app
WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app/ ./app/
COPY templates/ ./templates/
COPY table_mapping.yaml whatsapp_templates.yaml ./

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    HEARTBEAT_FILE=/tmp/heartbeat \
    XDG_CACHE_HOME=/tmp/cache

USER app

# Healthy = the worker completed a cycle in the last 2 minutes
HEALTHCHECK --interval=30s --timeout=5s --start-period=60s --retries=3 \
  CMD test -f /tmp/heartbeat && test $(( $(date +%s) - $(stat -c %Y /tmp/heartbeat) )) -lt 120 || exit 1

# Extra arguments go to the CLI: `setup`, `--once`, `--dry-run --job 12`
ENTRYPOINT ["python", "-m", "app.main"]
CMD []


FROM runtime AS test
USER root
COPY requirements-dev.txt .
RUN pip install --no-cache-dir -r requirements-dev.txt
COPY tests/ ./tests/
USER app
RUN python -m pytest -q -p no:cacheprovider
