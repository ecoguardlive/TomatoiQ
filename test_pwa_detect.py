"""Tests for /api/detect: input validation, rate limiting, and the
browser-scan path being wired through the same TomatoTracker + disease
screening logic the desktop pipeline uses.

Fixtures (client, auth_client) live in conftest.py.
"""
from __future__ import annotations

import numpy as np

from conftest import make_fake_result

_make_fake_result = make_fake_result  # local alias, keeps existing call sites below unchanged


def _jpeg_bytes():
    import cv2
    image = np.zeros((32, 32, 3), dtype=np.uint8)
    ok, buf = cv2.imencode(".jpg", image)
    assert ok
    return buf.tobytes()


def test_rejects_non_image_content_type(client):
    _, http, _ = client
    files = {"frame": ("frame.txt", b"not an image", "text/plain")}
    resp = http.post("/api/detect", files=files)
    assert resp.status_code == 400


def test_rejects_empty_upload(client):
    _, http, _ = client
    files = {"frame": ("frame.jpg", b"", "image/jpeg")}
    resp = http.post("/api/detect", files=files)
    assert resp.status_code == 400


def test_rejects_oversized_upload(client):
    module, http, _ = client
    oversized = b"\xff" * (module.MAX_UPLOAD_BYTES + 1)
    files = {"frame": ("frame.jpg", oversized, "image/jpeg")}
    resp = http.post("/api/detect", files=files)
    assert resp.status_code == 413


def test_rejects_undecodable_image_bytes(client):
    _, http, _ = client
    files = {"frame": ("frame.jpg", b"garbage-not-a-real-jpeg", "image/jpeg")}
    resp = http.post("/api/detect", files=files)
    assert resp.status_code == 400


def test_accepts_valid_frame_with_no_detections(client):
    _, http, yolo = client
    yolo.track.return_value = [_make_fake_result([])]
    files = {"frame": ("frame.jpg", _jpeg_bytes(), "image/jpeg")}
    resp = http.post("/api/detect", files=files)
    assert resp.status_code == 200
    body = resp.json()
    assert body["count"] == 0
    assert body["detections"] == []
    assert body["tracked_state"]["total_tomatoes"] == 0


def test_tracked_state_accumulates_across_requests_by_track_id(client):
    """The core unification fix: the same track_id seen across separate HTTP
    requests must be counted as ONE tomato, not one-per-request -- this is
    only possible because /api/detect now runs through a persistent
    TomatoTracker instead of a stateless model.predict() per request.

    harvest_config.json's tracking.confirm_after_observations=2 applies here
    too (browser and desktop share the same config-driven lifecycle), so a
    track_id only starts counting on its *second* sighting -- this asserts
    that shared behavior rather than assuming 1-shot confirmation.
    """
    _, http, yolo = client
    frame = _jpeg_bytes()

    # track_id 7, first sighting -- not yet confirmed
    yolo.track.return_value = [_make_fake_result([(1, 1, 10, 10, 7, 0, 0.9)])]
    resp1 = http.post("/api/detect", files={"frame": ("f.jpg", frame, "image/jpeg")})
    assert resp1.status_code == 200
    assert resp1.json()["tracked_state"]["total_tomatoes"] == 0

    # track_id 7, second sighting (separate request) -- now confirmed, counts as 1
    resp2 = http.post("/api/detect", files={"frame": ("f.jpg", frame, "image/jpeg")})
    assert resp2.status_code == 200
    assert resp2.json()["tracked_state"]["total_tomatoes"] == 1

    # track_id 7 again (still 1) plus a brand-new track_id 8's first sighting
    # (not yet confirmed) -- total must stay 1, not jump to 2
    yolo.track.return_value = [_make_fake_result([(2, 2, 11, 11, 7, 0, 0.9), (20, 20, 30, 30, 8, 2, 0.8)])]
    resp3 = http.post("/api/detect", files={"frame": ("f.jpg", frame, "image/jpeg")})
    assert resp3.json()["tracked_state"]["total_tomatoes"] == 1

    # track_id 8's second sighting -- now it confirms too, total becomes 2
    resp4 = http.post("/api/detect", files={"frame": ("f.jpg", frame, "image/jpeg")})
    assert resp4.json()["tracked_state"]["total_tomatoes"] == 2


def test_active_browser_scan_becomes_the_dashboard_live_source(client):
    """This is the actual bug reported from a real deployment: a cloud host
    with no physical camera can only ever get live data from the browser
    scanner, but /api/dashboard's source_state() used to only look at
    live_state.json (the desktop script's output) -- so the dashboard was
    permanently stuck on "NO DETECTOR" / "waiting" no matter how much
    browser scanning was happening. /api/detect must make itself the live
    source when there's no fresher desktop file."""
    module, http, yolo = client

    before = http.get("/api/dashboard").json()
    assert before["source"] == "waiting"

    yolo.track.return_value = [make_fake_result([(1, 1, 10, 10, 7, 0, 0.9)])]
    frame = _jpeg_bytes()
    http.post("/api/detect", files={"frame": ("f.jpg", frame, "image/jpeg")})
    http.post("/api/detect", files={"frame": ("f.jpg", frame, "image/jpeg")})  # 2nd sighting -> confirms

    after = http.get("/api/dashboard").json()
    assert after["source"] == "live"
    assert after["state"]["total_tomatoes"] == 1


def test_browser_live_source_goes_stale_after_the_configured_window(client, monkeypatch):
    module, http, yolo = client
    yolo.track.return_value = [make_fake_result([(1, 1, 10, 10, 7, 0, 0.9)])]
    frame = _jpeg_bytes()
    http.post("/api/detect", files={"frame": ("f.jpg", frame, "image/jpeg")})

    # Backdate the browser state's timestamp instead of sleeping in the test.
    stale_cutoff = module.CONFIG.get("detector_stale_after_seconds", 60)
    old_timestamp = (module.datetime.now() - module.timedelta(seconds=stale_cutoff + 5)).isoformat(timespec="seconds")
    module.BROWSER_LIVE_STATE["updated_at"] = old_timestamp

    resp = http.get("/api/dashboard").json()
    assert resp["source"] == "stale"


def test_rate_limit_blocks_after_threshold(client):
    module, http, yolo = client
    yolo.track.return_value = [_make_fake_result([])]
    files = {"frame": ("frame.jpg", _jpeg_bytes(), "image/jpeg")}

    for _ in range(module.DETECT_RATE_LIMIT_MAX_REQUESTS):
        resp = http.post("/api/detect", files=files)
        assert resp.status_code == 200

    blocked = http.post("/api/detect", files=files)
    assert blocked.status_code == 429
