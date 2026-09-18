# TomatoIQ — AI-Powered Tomato Harvest Intelligence

TomatoIQ combines YOLO tomato detection, ripeness classification, individual tracking, harvest-date estimation, visual anomaly screening and a responsive PWA dashboard.

## Production-style web experience

The recommended interface is the FastAPI + PWA stack:

```bash
python -m pip install -r requirements.txt
python -m uvicorn pwa_server:app --host 0.0.0.0 --port 8080
```

Open the server URL in a browser. For phone camera access and PWA installation, deploy behind HTTPS.

### Data flow

```text
YOLO / ByteTrack detector
        ↓
live_state.json
        ↓
FastAPI data layer
 ├── live dashboard state
 ├── weather + GDD
 ├── persisted scan history
 └── CSV reporting
        ↓
TomatoIQ responsive PWA
```

The dashboard is intentionally **data-driven**. KPI values, ripeness distribution, harvest forecast, tomato profiles, alerts, health indicators and analytics are derived from the detector state/configuration and persisted history. The UI does not use the mockup's sample numbers as production data.

## Live detector

Run the desktop tracker in another process:

```bash
python tomato_harvest_system.py --source 0
```

Or process a video:

```bash
python tomato_harvest_system.py --source path/to/video.mp4
```

Headless:

```bash
python tomato_harvest_system.py --source video.mp4 --headless --save-video annotated_output.mp4
```

The tracker writes `live_state.json` according to `harvest_config.json`. The web server reads that state without touching the camera/model for dashboard requests.

## Dynamic features

The rebuilt PWA includes:

- Responsive professional dashboard
- Live detector connection state
- Real browser camera scanning through `/api/detect`
- Dynamic KPI cards
- Dynamic ripeness distribution
- Harvest forecast from tracked tomatoes
- Open-Meteo weather data when farm coordinates are configured
- GDD context using configured harvest thresholds
- Data-derived crop health indicator
- Live inspection/harvest recommendations
- Tomato profile list driven by current tracked state
- Alerts derived from current screening results
- Persisted lightweight scan history in `data/scan_history.json`
- 7/30/90-day analytics views
- CSV export at `/api/report`
- Responsive mobile navigation
- PWA service worker and manifest
- Loading, empty and connection-failure states

## Configuration

`harvest_config.json` is the source of truth for operational configuration such as:

- YOLO model path
- trained class names
- detection threshold
- farm coordinates
- GDD model parameters
- disease-screening thresholds
- live state path
- report path

Farm-specific display metadata belongs under the `farm` object in the configuration rather than being embedded in HTML/JavaScript.

## Important model limitations

- The trained model currently recognizes `green`, `half_ripened`, and `fully_ripened`.
- The disease/abnormality screen is a color-based heuristic and is **not** a disease diagnosis.
- Harvest dates are estimates and GDD thresholds should be calibrated for the actual tomato variety and farm.
- Browser live detection is server-side inference: the browser camera sends resized frames to `/api/detect`.
- The desktop tracker maintains identities using ByteTrack; browser frame detection itself does not persist track IDs.

## Production deployment baseline

This remediation replaces JSON scan history with `data/tomatoiq.db` (SQLite WAL
mode), giving concurrent API workers transactional snapshots and durable alert
history. Live tracker state is also atomically published, so readers never see
a partial JSON document. The PWA now receives detector updates over `/api/live`
WebSocket, with a 60-second recovery sync for restrictive proxy networks.

Before exposing the service, copy `.env.example` to `.env` (or your deployment
secret manager) and set `TOMATOIQ_AUTH_REQUIRED=true` with a real
`TOMATOIQ_AUTH_SECRET`/`TOMATOIQ_ADMIN_USERNAME`/`TOMATOIQ_ADMIN_PASSWORD`.
This protects every `/api/*` operational endpoint. `POST /api/auth/login` both
returns a short-lived bearer token (for scripts/API clients) and sets an
HttpOnly, SameSite=Strict session cookie, which is what the PWA itself uses --
the browser sends it automatically on every later request, including the
`/api/live` WebSocket handshake (browsers can't attach a custom
`Authorization` header to a WebSocket, but they do send cookies on it), so no
separate authenticated WebSocket gateway is needed. Terminate TLS at a reverse
proxy as usual; keep `TOMATOIQ_COOKIE_SECURE=true` (the default) so the
session cookie is only ever sent over HTTPS.

```bash
python -m pip install -r requirements.txt
python -m pytest -q
python -m uvicorn pwa_server:app --host 127.0.0.1 --port 8080
```

### Running with Docker

```bash
cp .env.example .env        # then edit in real secrets
docker compose up --build
```

This starts the FastAPI web service (dashboard + browser-based live scanning)
on `http://localhost:8000`, with `./data` mounted for the SQLite database so
it survives container restarts. It deliberately does **not** containerize
`tomato_harvest_system.py` (the desktop camera script) -- that needs a
physical camera device attached to whatever host runs it, which doesn't
containerize meaningfully across Linux/Mac/Windows hosts. Run it directly on
that host instead; see the comments in `docker-compose.yml` for wiring its
output into the containerized dashboard via a shared `live_state.json`.

`requirements-docker.txt` is a deliberately narrower dependency set than
`requirements.txt` for the container image (headless OpenCV, no Streamlit) --
see the comments in that file for why.

### Deploying to Render (free)

`render.yaml` is a Render Blueprint -- in the Render dashboard, **New >
Blueprint**, point it at this GitHub repo, and it provisions the service from
that file automatically. You'll be prompted for `TOMATOIQ_AUTH_SECRET`,
`TOMATOIQ_ADMIN_USERNAME`, and `TOMATOIQ_ADMIN_PASSWORD` (not committed to the
repo). Render builds the same `Dockerfile` used above and gives you a real
HTTPS URL (`https://tomatoiq.onrender.com` or similar) with no separate TLS
setup -- Render terminates TLS at its own edge, so `Caddyfile`/the `caddy`
service in `docker-compose.yml` are for the self-hosted-VPS path only and
aren't used on Render.

**Be aware of two real free-tier limitations before pointing other people at
it:**
- **The SQLite database does not survive.** Render's free web services have
  no persistent disk -- local filesystem changes (including `data/tomatoiq.db`
  and `live_state.json`) are wiped on every restart, redeploy, *and* every
  time the service spins down from inactivity (see below). Scan history and
  saved settings will periodically reset to defaults. If that's a problem,
  the fix is a persistent disk (Render paid plan) or moving `persistence.py`
  to Render's managed Postgres -- neither is set up here, since it's a real
  change worth making deliberately rather than defaulting into.
- **It sleeps after 15 minutes of no traffic** and takes 30-60 seconds to
  wake back up on the next request -- the first visitor after a quiet period
  will see a slow initial load, not a broken app.

Neither of these is a bug in what's built here -- they're the actual, current
shape of Render's free tier. Fine for a demo/portfolio deployment; worth
knowing about before treating it as a dependable service for other people.

### CI

`.github/workflows/ci.yml` runs on every push/PR: a fast test job (stubs out
`ultralytics`/`torch` so it doesn't need a multi-gigabyte ML install just to
check request validation and auth logic -- see `conftest.py`), followed by a
job that builds the real Docker image with the real dependencies and boots it
against the real model to confirm `/health` actually comes up.

For multi-node deployments, put PostgreSQL and a shared pub/sub broker behind
the repository and WebSocket fan-out interfaces. SQLite WAL is production-safe
for a single host, but not a distributed database.

### Disease model release gate

The current `best.pt` contains only ripeness classes. The color/texture module
therefore remains a *human-inspection screen*, not a diagnosis. Do not market
it as disease classification. A true production disease feature requires a
versioned, labeled disease dataset, held-out farm validation by lighting and
variety, calibrated confidence thresholds, and monitoring for false positives
and drift. The application safely surfaces the model limitation instead of
fabricating clinical confidence.

## Legacy Streamlit applications

`dashboard.py` and `mobile_app.py` remain in the repository for compatibility with the original project. The FastAPI/PWA interface is the preferred production-style experience.
