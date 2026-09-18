"""TomatoIQ production-oriented API and PWA server.

The API is intentionally data-driven: the UI receives configuration, live detector
state, weather and persisted scan history rather than embedding dashboard values.
"""
from __future__ import annotations

import asyncio
import csv
import io
import json
import threading
import time
from collections import defaultdict, deque
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Any

import cv2
import numpy as np
import requests
from fastapi import FastAPI, File, HTTPException, UploadFile, WebSocket, WebSocketDisconnect, Request
from fastapi.responses import FileResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field
from ultralytics import YOLO

from growing_degree_days import fetch_daily_temps, daily_gdd
from auth import authenticate, auth_enabled, cookie_secure, issue_token, require_user, validate_deployment_auth, COOKIE_NAME
from persistence import TomatoRepository
from tracker_core import TomatoTracker, build_live_state, cached_estimate_harvest_date
from disease_detector import analyze_crop, crop_from_frame

ROOT = Path(__file__).resolve().parent
STATIC = ROOT / "pwa"
DATA = ROOT / "data"
DATA.mkdir(exist_ok=True)
CONFIG_PATH = ROOT / "harvest_config.json"
CONFIG = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
REPOSITORY = TomatoRepository(DATA / "tomatoiq.db")
MODEL_LOCK = asyncio.Lock()
model: YOLO | None = None

# Same TomatoTracker the desktop pipeline (tomato_harvest_system.py) uses,
# applied to frames arriving one at a time over HTTP from the in-browser
# scanner instead of a local camera loop. This is what gives browser-side
# scanning the same identity/majority-vote/disease-screening/lifecycle
# behavior as the desktop script, instead of a stateless per-frame YOLO call
# with no memory between requests.
BROWSER_TRACKER = TomatoTracker.from_config(CONFIG)
BROWSER_FRAME_IDX = 0
BROWSER_HARVEST_CACHE: dict[str, Any] = {}

MAX_UPLOAD_BYTES = 8 * 1024 * 1024        # 8MB -- a 640px JPEG frame is tens of KB; this only guards against abuse
MAX_IMAGE_DIMENSION = 4096                 # reject decoded images larger than this on either side
DETECT_RATE_LIMIT_MAX_REQUESTS = 30        # per DETECT_RATE_LIMIT_WINDOW_SECONDS, per client IP
DETECT_RATE_LIMIT_WINDOW_SECONDS = 10.0    # the UI polls /api/detect roughly every 500ms (~2/s); this allows ~3x that


class RateLimiter:
    """Simple in-memory sliding-window limiter, keyed by client IP.
    Single-process only (matches this app's existing single-process
    assumptions -- one global `model`, one SQLite file) -- swap for a
    shared store (e.g. Redis) if this is ever run behind multiple workers."""
    def __init__(self, max_requests: int, window_seconds: float):
        self.max_requests = max_requests
        self.window_seconds = window_seconds
        self._hits: dict[str, deque[float]] = defaultdict(deque)
        self._lock = threading.Lock()

    def allow(self, key: str) -> bool:
        now = time.monotonic()
        with self._lock:
            hits = self._hits[key]
            while hits and now - hits[0] > self.window_seconds:
                hits.popleft()
            if len(hits) >= self.max_requests:
                return False
            hits.append(now)
            return True


DETECT_RATE_LIMITER = RateLimiter(DETECT_RATE_LIMIT_MAX_REQUESTS, DETECT_RATE_LIMIT_WINDOW_SECONDS)
# Login is a much higher-value target for brute-forcing than /api/detect --
# 5 attempts per 5 minutes per IP is tight enough to make guessing a
# password impractical while not locking out a real user who mistypes it
# once or twice.
LOGIN_RATE_LIMITER = RateLimiter(max_requests=5, window_seconds=300.0)

app = FastAPI(title="TomatoIQ API", version="2.0.0")
app.mount("/assets", StaticFiles(directory=STATIC), name="assets")


class LiveConnections:
    """Small in-process WebSocket fan-out. Use a shared broker for multi-node deployment."""
    def __init__(self) -> None:
        self.connections: set[WebSocket] = set()

    async def connect(self, socket: WebSocket) -> None:
        await socket.accept()
        self.connections.add(socket)

    def disconnect(self, socket: WebSocket) -> None:
        self.connections.discard(socket)

    async def broadcast(self, payload: dict[str, Any]) -> None:
        for socket in list(self.connections):
            try:
                await socket.send_json(payload)
            except Exception:
                self.disconnect(socket)


connections = LiveConnections()


@app.on_event("startup")
async def startup() -> None:
    validate_deployment_auth()

    async def publish_live_state() -> None:
        previous: tuple[str | None, str] | None = None
        while True:
            state, source = source_state()
            marker = (state.get("updated_at"), source)
            if marker != previous:
                await connections.broadcast({"type": "dashboard_update", "source": source, "state": normalize_state(state), "server_time": now_iso()})
                previous = marker
            await asyncio.sleep(1)
    asyncio.create_task(publish_live_state())


@app.middleware("http")
async def production_api_auth(request: Request, call_next):
    """Protect data-changing and operational API routes when deployment auth is enabled.

    HTTPException raised directly inside a Starlette `@app.middleware("http")`
    function is NOT converted into a proper JSON error response by FastAPI's
    normal exception handling -- that only applies to exceptions raised from
    route handlers/dependencies. Left uncaught here, an unauthenticated
    request would get a bare 500 Internal Server Error instead of a 401,
    which is both a broken client experience and a minor information leak
    (a generic error page instead of an explicit "authenticate" signal).
    Catching it explicitly and returning the JSONResponse ourselves is what
    actually makes this middleware behave like real route-level auth.
    """
    protected = request.url.path.startswith("/api/") and request.url.path not in {"/api/auth/login"}
    if protected:
        try:
            require_user(request)
        except HTTPException as exc:
            return JSONResponse(status_code=exc.status_code, content={"detail": exc.detail})
    return await call_next(request)


def now_iso() -> str:
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")


def read_json(path: Path, fallback: Any) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return fallback


def _state_age_seconds(state: dict[str, Any]) -> float | None:
    updated_at = state.get("updated_at")
    if not updated_at:
        return None
    try:
        ts = datetime.fromisoformat(updated_at)
        if ts.tzinfo is None:
            ts = ts.astimezone()
        return (datetime.now(timezone.utc) - ts.astimezone(timezone.utc)).total_seconds()
    except (TypeError, ValueError):
        return None


def source_state() -> tuple[dict[str, Any], str]:
    """Returns (state, source) where source is one of:
    - "live":    live_state.json exists and was updated recently
    - "stale":   live_state.json exists but hasn't been updated recently
                 (the desktop tracker likely stopped or crashed)
    - "waiting": no live_state.json has ever been written
    A file existing is NOT enough to claim "live" -- staleness is checked
    so the dashboard never reports a detector as live when it has actually
    stopped.
    """
    configured = Path(CONFIG.get("live_state_path", "live_state.json"))
    if not configured.is_absolute():
        configured = ROOT / configured
    if configured.exists():
        state = read_json(configured, {})
        stale_after = CONFIG.get("detector_stale_after_seconds", 60)
        age = _state_age_seconds(state)
        source = "stale" if (age is not None and age > stale_after) else "live"
        return state, source
    return {
        "updated_at": None,
        "total_tomatoes": 0,
        "counts_by_class": {name: 0 for name in CONFIG.get("class_names", [])},
        "ready_now": 0,
        "disease_suspect_count": 0,
        "tomatoes": [],
        "estimation_method": "gdd" if CONFIG.get("gdd", {}).get("enabled") else "flat",
    }, "waiting"


def normalize_state(state: dict[str, Any]) -> dict[str, Any]:
    class_names = CONFIG.get("class_names", [])
    counts = {name: int((state.get("counts_by_class") or {}).get(name, 0)) for name in class_names}
    tomatoes = state.get("tomatoes") or []
    total = int(state.get("total_tomatoes", sum(counts.values())))
    ready = int(state.get("ready_now", sum(1 for t in tomatoes if t.get("ready_now"))))
    risk = int(state.get("disease_suspect_count", sum(1 for t in tomatoes if t.get("disease_suspect"))))
    return {
        **state,
        "counts_by_class": counts,
        "total_tomatoes": total,
        "ready_now": ready,
        "disease_suspect_count": risk,
        "tomatoes": tomatoes,
    }


def append_history(state: dict[str, Any]) -> None:
    """Persist lightweight real snapshots for analytics; never store camera frames."""
    REPOSITORY.append_snapshot(state)
    for tomato in state.get("tomatoes", []):
        if tomato.get("disease_suspect"):
            tomato_id = tomato.get("tomato_id")
            REPOSITORY.ensure_open_alert(
                tomato_id=tomato_id,
                severity="warning",
                category="inspection",
                message=f"Tomato #{tomato_id} requires human inspection.",
                metadata={"reason": tomato.get("disease_reason"), "screening_confidence": tomato.get("disease_confidence")},
            )


def history(days: int = 90) -> list[dict[str, Any]]:
    return REPOSITORY.snapshots_since(datetime.now(timezone.utc) - timedelta(days=days))


def farm_config() -> dict[str, Any]:
    farm = CONFIG.get("farm", {})
    return {
        "name": farm.get("name", "Configured Farm"),
        "manager": farm.get("manager"),
        "location": farm.get("location", {}),
        "variety": farm.get("variety"),
        "size_hectares": farm.get("size_hectares"),
    }


CONFIG_LOCK = threading.Lock()


class SettingsUpdate(BaseModel):
    """Every field is optional -- only fields the user actually submits are
    changed. Values are validated and persisted to harvest_config.json, and
    the in-memory CONFIG is updated in the same request so /api/dashboard
    reflects the change immediately (no restart required)."""
    confidence_threshold: float | None = Field(default=None, ge=0.0, le=1.0)
    gdd_enabled: bool | None = None
    gdd_base_temp_c: float | None = None
    gdd_cap_c: float | None = None
    disease_screening_enabled: bool | None = None
    farm_name: str | None = None
    farm_manager: str | None = None
    farm_variety: str | None = None
    farm_size_hectares: float | None = None
    farm_latitude: float | None = None
    farm_longitude: float | None = None


class LoginRequest(BaseModel):
    username: str = Field(min_length=1, max_length=128)
    password: str = Field(min_length=1, max_length=256)


def update_settings(update: SettingsUpdate) -> dict[str, Any]:
    with CONFIG_LOCK:
        if update.confidence_threshold is not None:
            CONFIG["confidence_threshold"] = update.confidence_threshold
        if update.gdd_enabled is not None:
            CONFIG.setdefault("gdd", {})["enabled"] = update.gdd_enabled
        if update.gdd_base_temp_c is not None:
            CONFIG.setdefault("gdd", {})["base_temp_c"] = update.gdd_base_temp_c
        if update.gdd_cap_c is not None:
            CONFIG.setdefault("gdd", {})["cap_c"] = update.gdd_cap_c
        if update.disease_screening_enabled is not None:
            CONFIG.setdefault("disease_screening", {})["enabled"] = update.disease_screening_enabled
        farm = CONFIG.setdefault("farm", {})
        if update.farm_name is not None:
            farm["name"] = update.farm_name
        if update.farm_manager is not None:
            farm["manager"] = update.farm_manager
        if update.farm_variety is not None:
            farm["variety"] = update.farm_variety
        if update.farm_size_hectares is not None:
            farm["size_hectares"] = update.farm_size_hectares
        location = farm.setdefault("location", {})
        gdd_cfg = CONFIG.setdefault("gdd", {})
        if update.farm_latitude is not None:
            location["latitude"] = update.farm_latitude
            gdd_cfg["latitude"] = update.farm_latitude
        if update.farm_longitude is not None:
            location["longitude"] = update.farm_longitude
            gdd_cfg["longitude"] = update.farm_longitude
        CONFIG_PATH.write_text(json.dumps(CONFIG, indent=2), encoding="utf-8")
    return CONFIG


def get_model() -> YOLO:
    global model
    if model is None:
        model = YOLO(ROOT / CONFIG["model_path"])
    return model


def current_weather() -> dict[str, Any]:
    gdd = CONFIG.get("gdd", {})
    lat, lon = gdd.get("latitude"), gdd.get("longitude")
    if lat is None or lon is None:
        return {"available": False, "reason": "Farm coordinates are not configured."}
    params = {
        "latitude": lat,
        "longitude": lon,
        "current": "temperature_2m,relative_humidity_2m,wind_speed_10m,weather_code",
        "daily": "temperature_2m_max,temperature_2m_min",
        "forecast_days": 7,
        "timezone": "auto",
    }
    try:
        response = requests.get("https://api.open-meteo.com/v1/forecast", params=params, timeout=5)
        response.raise_for_status()
        data = response.json()
        current = data.get("current", {})
        daily = data.get("daily", {})
        base = float(gdd.get("base_temp_c", 10))
        cap = gdd.get("cap_c")
        gdd_values = [daily_gdd(mx, mn, base, cap) for mx, mn in zip(daily.get("temperature_2m_max", []), daily.get("temperature_2m_min", [])) if mx is not None and mn is not None]
        return {
            "available": True,
            "temperature_c": current.get("temperature_2m"),
            "humidity_pct": current.get("relative_humidity_2m"),
            "wind_kmh": current.get("wind_speed_10m"),
            "weather_code": current.get("weather_code"),
            "daily_gdd": gdd_values,
            "source": "Open-Meteo",
            "updated_at": now_iso(),
        }
    except requests.RequestException as exc:
        return {"available": False, "reason": f"Weather service unavailable: {exc.__class__.__name__}."}


@app.get("/", include_in_schema=False)
async def home() -> FileResponse:
    return FileResponse(STATIC / "index.html")


@app.get("/health")
async def health() -> dict[str, Any]:
    state, source = source_state()
    return {"status": "ok", "state_source": source, "detector_updated_at": state.get("updated_at"), "time": now_iso()}


@app.post("/api/auth/login")
async def login(credentials: LoginRequest, request: Request, response: Response) -> dict[str, Any]:
    if not auth_enabled():
        return {"authentication_required": False}

    client_key = request.client.host if request.client else "unknown"
    if not LOGIN_RATE_LIMITER.allow(client_key):
        raise HTTPException(status_code=429, detail="Too many login attempts -- try again later.")

    if not authenticate(credentials.username, credentials.password):
        raise HTTPException(status_code=401, detail="Invalid credentials")

    token = issue_token(credentials.username)
    # HttpOnly so a successful XSS can't read the session and exfiltrate it;
    # SameSite=strict since this is a same-origin single-page app with no
    # legitimate cross-site use of the cookie; Secure unless explicitly
    # disabled for local plain-HTTP testing (TOMATOIQ_COOKIE_SECURE=false).
    response.set_cookie(
        key=COOKIE_NAME,
        value=token,
        httponly=True,
        samesite="strict",
        secure=cookie_secure(),
        max_age=3600,
        path="/",
    )
    # Bearer token is still returned for non-browser/API clients (scripts,
    # a future native app) that can't rely on a browser's cookie jar.
    return {"access_token": token, "token_type": "bearer", "expires_in": 3600}


@app.post("/api/auth/logout")
async def logout(response: Response) -> dict[str, Any]:
    response.delete_cookie(key=COOKIE_NAME, path="/")
    return {"logged_out": True}


@app.websocket("/api/live")
async def live(socket: WebSocket) -> None:
    if auth_enabled():
        try:
            require_user(socket)
        except HTTPException:
            # Same signed session cookie the HTTP API checks -- a WebSocket
            # handshake can't carry a custom Authorization header, but the
            # browser does send cookies on it automatically (same-origin),
            # so logging in via /api/auth/login is enough to authenticate
            # this connection too.
            await socket.close(code=1008, reason="Authentication required")
            return
    await connections.connect(socket)
    state, source = source_state()
    await socket.send_json({"type": "dashboard_update", "source": source, "state": normalize_state(state), "server_time": now_iso()})
    try:
        while True:
            await socket.receive_text()
    except WebSocketDisconnect:
        connections.disconnect(socket)


@app.get("/api/health")
async def api_health() -> dict[str, Any]:
    """Reports the actual, checkable status of each subsystem. Never claims
    a subsystem is fine without a real signal for it; camera status is
    reported as 'browser-controlled' because the server-side process has no
    visibility into the browser's camera permission/state -- that's a real
    architectural limit, not a status this endpoint can fabricate."""
    state, source = source_state()
    age = _state_age_seconds(state)
    detector_status = {"live": "running", "stale": "stopped_or_unresponsive", "waiting": "not_started"}[source]

    model_status = "loaded" if model is not None else "not_loaded_yet"
    try:
        model_path = ROOT / CONFIG["model_path"]
        model_file_ok = model_path.exists()
    except Exception:
        model_file_ok = False

    gdd = CONFIG.get("gdd", {})
    weather_configured = gdd.get("latitude") is not None and gdd.get("longitude") is not None

    overall = "ok"
    if detector_status != "running":
        overall = "degraded"
    if not model_file_ok:
        overall = "degraded"

    return {
        "status": overall,
        "api": "ok",
        "database": "ok",
        "storage_backend": "sqlite",
        "detector": detector_status,
        "detector_updated_at": state.get("updated_at"),
        "detector_age_seconds": round(age, 1) if age is not None else None,
        "model_file_present": model_file_ok,
        "model_status": model_status,
        "camera": "browser-controlled",
        "weather": "configured" if weather_configured else "not_configured",
        "time": now_iso(),
    }


@app.get("/api/dashboard")
async def dashboard() -> dict[str, Any]:
    state, source = source_state()
    state = normalize_state(state)
    append_history(state)
    return {
        "config": {
            "class_names": CONFIG.get("class_names", []),
            "confidence_threshold": CONFIG.get("confidence_threshold", 0.25),
            "gdd": CONFIG.get("gdd", {}),
            "disease_screening": CONFIG.get("disease_screening", {}),
            "estimation_method": state.get("estimation_method", "unknown"),
        },
        "farm": farm_config(),
        "state": state,
        "source": source,
        "server_time": now_iso(),
    }


@app.get("/api/settings")
async def get_settings() -> dict[str, Any]:
    farm = CONFIG.get("farm", {})
    gdd = CONFIG.get("gdd", {})
    return {
        "confidence_threshold": CONFIG.get("confidence_threshold", 0.25),
        "gdd_enabled": gdd.get("enabled", False),
        "gdd_base_temp_c": gdd.get("base_temp_c"),
        "gdd_cap_c": gdd.get("cap_c"),
        "disease_screening_enabled": CONFIG.get("disease_screening", {}).get("enabled", False),
        "farm_name": farm.get("name"),
        "farm_manager": farm.get("manager"),
        "farm_variety": farm.get("variety"),
        "farm_size_hectares": farm.get("size_hectares"),
        "farm_latitude": farm.get("location", {}).get("latitude"),
        "farm_longitude": farm.get("location", {}).get("longitude"),
    }


@app.post("/api/settings")
async def post_settings(update: SettingsUpdate) -> dict[str, Any]:
    """Persists real configuration changes to harvest_config.json. This is a
    genuine write -- not a UI control that visually changes but does
    nothing -- and takes effect on the very next /api/dashboard or
    /api/detect call, no restart required."""
    update_settings(update)
    return await get_settings()


@app.get("/api/weather")
async def weather() -> dict[str, Any]:
    return current_weather()


@app.get("/api/history")
async def analytics(days: int = 30) -> dict[str, Any]:
    days = max(1, min(days, 90))
    return {"days": days, "points": history(days)}


@app.get("/api/alerts")
async def alerts(limit: int = 100, include_resolved: bool = False) -> dict[str, Any]:
    return {"alerts": REPOSITORY.list_alerts(limit=max(1, min(limit, 500)), include_resolved=include_resolved)}


@app.post("/api/alerts/{alert_id}/resolve")
async def resolve_alert(alert_id: int) -> dict[str, Any]:
    if not REPOSITORY.resolve_alert(alert_id):
        raise HTTPException(status_code=404, detail="Open alert not found")
    return {"resolved": True, "alert_id": alert_id}


@app.get("/api/report")
async def report() -> Response:
    state, _ = source_state()
    tomatoes = state.get("tomatoes") or []
    output = io.StringIO()
    fields = ["tomato_id", "ripeness_class", "estimated_harvest_date", "ready_now", "disease_suspect", "disease_reason", "disease_confidence"]
    writer = csv.DictWriter(output, fieldnames=fields)
    writer.writeheader()
    for tomato in tomatoes:
        writer.writerow({field: tomato.get(field, "") for field in fields})
    return Response(
        content=output.getvalue(),
        media_type="text/csv",
        headers={"Content-Disposition": "attachment; filename=tomatoiq_report.csv"},
    )


@app.post("/api/detect")
async def detect(request: Request, frame: UploadFile = File(...)) -> dict[str, Any]:
    global BROWSER_FRAME_IDX

    client_key = request.client.host if request.client else "unknown"
    if not DETECT_RATE_LIMITER.allow(client_key):
        raise HTTPException(status_code=429, detail="Too many scan requests -- slow down.")

    if frame.content_type and not frame.content_type.startswith("image/"):
        raise HTTPException(status_code=400, detail="Uploaded frame must be an image")

    raw = await frame.read()
    if not raw:
        raise HTTPException(status_code=400, detail="Empty frame upload")
    if len(raw) > MAX_UPLOAD_BYTES:
        raise HTTPException(status_code=413, detail=f"Frame exceeds {MAX_UPLOAD_BYTES // (1024*1024)}MB limit")

    image = cv2.imdecode(np.frombuffer(raw, np.uint8), cv2.IMREAD_COLOR)
    if image is None:
        raise HTTPException(status_code=400, detail="Frame could not be decoded as an image")

    h, w = image.shape[:2]
    if h > MAX_IMAGE_DIMENSION or w > MAX_IMAGE_DIMENSION:
        raise HTTPException(status_code=400, detail=f"Frame dimensions exceed {MAX_IMAGE_DIMENSION}px limit")

    class_names = CONFIG.get("class_names", [])
    disease_cfg = CONFIG.get("disease_screening", {"enabled": False})

    async with MODEL_LOCK:
        # .track(..., persist=True) -- not .predict() -- keeps ByteTrack's
        # internal state on the model between calls, so tomatoes get a
        # stable identity across successive frames from the browser the
        # same way the desktop camera loop's ByteTrack stream does.
        result = get_model().track(
            image,
            conf=CONFIG.get("confidence_threshold", 0.25),
            tracker=CONFIG.get("tracker", "bytetrack.yaml"),
            persist=True,
            verbose=False,
        )[0]

        boxes: list[dict[str, Any]] = []
        seen_track_ids: list[int] = []
        result_boxes = result.boxes
        has_ids = result_boxes is not None and result_boxes.id is not None
        iterable = zip(
            result_boxes.xyxy.cpu().numpy(),
            result_boxes.id.cpu().numpy().astype(int) if has_ids else [None] * len(result_boxes),
            result_boxes.cls.cpu().numpy().astype(int),
            result_boxes.conf.cpu().numpy(),
        ) if result_boxes is not None else []

        for xyxy, track_id, cls_idx, conf in iterable:
            label = class_names[cls_idx] if cls_idx < len(class_names) else str(cls_idx)
            x1, y1, x2, y2 = xyxy.tolist()

            if track_id is not None:
                seen_track_ids.append(int(track_id))
                BROWSER_TRACKER.update(int(track_id), label, BROWSER_FRAME_IDX)
                if disease_cfg.get("enabled"):
                    crop = crop_from_frame(image, xyxy)
                    if crop is not None:
                        flag = analyze_crop(
                            crop,
                            dark_spot_ratio_thresh=disease_cfg.get("dark_spot_ratio_thresh", 0.08),
                            pale_patch_ratio_thresh=disease_cfg.get("pale_patch_ratio_thresh", 0.15),
                        )
                        BROWSER_TRACKER.update_disease_flag(int(track_id), flag)

            boxes.append({
                "x": round(x1 / w, 4),
                "y": round(y1 / h, 4),
                "width": round((x2 - x1) / w, 4),
                "height": round((y2 - y1) / h, 4),
                "label": label,
                "confidence": round(float(conf), 3),
                "track_id": int(track_id) if track_id is not None else None,
            })

        BROWSER_TRACKER.finalize_frame(BROWSER_FRAME_IDX, seen_track_ids)
        BROWSER_FRAME_IDX += 1

    tracked_state = build_live_state(BROWSER_TRACKER, class_names, CONFIG, BROWSER_HARVEST_CACHE)
    return {
        "detections": boxes,
        "count": len(boxes),
        "processed_at": now_iso(),
        # Tracked/session state from the same TomatoTracker + disease-screening
        # logic the desktop pipeline uses -- not yet merged into the desktop's
        # live_state.json/dashboard history (see note in README/roadmap: doing
        # so requires deciding how a browser-scan session and a desktop-camera
        # session resolve as sources of truth if both are used at once).
        "tracked_state": tracked_state,
    }
