# Runs pwa_server.py (the FastAPI web service: dashboard, browser-based live
# scanning via /api/detect, settings). Does NOT run tomato_harvest_system.py,
# the desktop camera script -- that needs a physical camera device attached
# to whatever host runs it, which doesn't containerize meaningfully the same
# way across Linux/Mac/Windows Docker hosts. Run it directly on that host
# instead (see README) and point TOMATOIQ's live_state_path at a location
# this container's `live_state` volume also mounts, if you want the
# dashboard to pick up its output.

FROM python:3.11-slim AS base

# libgl1/libglib2.0-0: required by opencv-python-headless's image codecs even
# though nothing here opens a display. curl: used by the HEALTHCHECK below.
RUN apt-get update && apt-get install -y --no-install-recommends \
        libgl1 \
        libglib2.0-0 \
        curl \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements-docker.txt .
RUN pip install --no-cache-dir -r requirements-docker.txt

COPY pwa_server.py auth.py persistence.py tracker_core.py disease_detector.py growing_degree_days.py harvest_config.json best.pt ./
COPY pwa ./pwa

# Runs as a non-root user -- there's no reason this process needs root, and
# not running as root is one of the cheapest real security wins available.
RUN useradd --create-home --uid 1000 tomatoiq \
    && mkdir -p /app/data \
    && chown -R tomatoiq:tomatoiq /app
USER tomatoiq

ENV PYTHONUNBUFFERED=1
# Render (and most container hosting platforms) inject their own PORT env
# var at runtime and require the container to bind to it -- 8000 is only
# the local/docker-compose default when PORT isn't set.
ENV PORT=8000
EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
    CMD curl -f "http://localhost:${PORT}/health" || exit 1

# Shell form (not the usual JSON-array exec form) so ${PORT} actually gets
# substituted at container start -- exec form CMD does not expand env vars.
CMD uvicorn pwa_server:app --host 0.0.0.0 --port ${PORT}
