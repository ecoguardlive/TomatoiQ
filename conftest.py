"""Shared fixtures for pwa_server.py tests.

ultralytics (and the torch install it pulls in) is stubbed out here on
purpose: these tests exercise request handling, auth, and tracking logic,
not YOLO inference itself, and requiring a multi-gigabyte torch install
just to validate a 401 response is exactly the kind of CI friction that
keeps a test suite from actually being run on every commit.
"""
from __future__ import annotations

import os
import shutil
import sys
import types
from unittest.mock import MagicMock

import numpy as np
import pytest


def make_fake_result(boxes_data):
    """boxes_data: list of (x1, y1, x2, y2, track_id_or_None, cls_idx, conf)"""
    result = MagicMock()
    if not boxes_data:
        result.boxes = None
        return result

    xyxy = np.array([[b[0], b[1], b[2], b[3]] for b in boxes_data], dtype=float)
    ids = [b[4] for b in boxes_data]
    has_ids = all(i is not None for i in ids)
    cls = np.array([b[5] for b in boxes_data], dtype=int)
    conf = np.array([b[6] for b in boxes_data], dtype=float)

    boxes = MagicMock()
    boxes.__len__.return_value = len(boxes_data)
    boxes.xyxy.cpu.return_value.numpy.return_value = xyxy
    boxes.cls.cpu.return_value.numpy.return_value = cls
    boxes.conf.cpu.return_value.numpy.return_value = conf
    if has_ids:
        boxes.id.cpu.return_value.numpy.return_value = np.array(ids, dtype=int)
    else:
        boxes.id = None
    result.boxes = boxes
    return result


def _load_pwa_server(monkeypatch, tmp_path, env=None):
    fake_ultralytics = types.ModuleType("ultralytics")
    fake_yolo_instance = MagicMock()
    fake_yolo_instance.track.return_value = [make_fake_result([])]

    class FakeYOLO:
        def __new__(cls, *a, **kw):
            return fake_yolo_instance

    fake_ultralytics.YOLO = FakeYOLO
    sys.modules["ultralytics"] = fake_ultralytics

    monkeypatch.chdir(tmp_path)
    project_root = os.path.dirname(os.path.abspath(__file__))
    for name in ("harvest_config.json", "best.pt"):
        src = os.path.join(project_root, name)
        if os.path.exists(src):
            shutil.copy(src, tmp_path / name)
    (tmp_path / "pwa").mkdir(exist_ok=True)

    for key, value in (env or {}).items():
        monkeypatch.setenv(key, value)

    sys.modules.pop("pwa_server", None)
    sys.modules.pop("auth", None)  # auth.py reads env vars at import time
    import pwa_server
    from fastapi.testclient import TestClient

    pwa_server.model = fake_yolo_instance
    pwa_server.get_model = lambda: fake_yolo_instance
    return pwa_server, TestClient(pwa_server.app), fake_yolo_instance


@pytest.fixture()
def client(monkeypatch, tmp_path):
    """Auth disabled (the default) -- matches normal/dev use."""
    module, http, yolo = _load_pwa_server(monkeypatch, tmp_path)
    yield module, http, yolo
    sys.modules.pop("pwa_server", None)
    sys.modules.pop("auth", None)
    sys.modules.pop("ultralytics", None)


@pytest.fixture()
def auth_client(monkeypatch, tmp_path):
    """Auth enabled, with a known admin username/password/secret."""
    module, http, yolo = _load_pwa_server(monkeypatch, tmp_path, env={
        "TOMATOIQ_AUTH_REQUIRED": "true",
        "TOMATOIQ_AUTH_SECRET": "test-secret-key",
        "TOMATOIQ_ADMIN_USERNAME": "farmer",
        "TOMATOIQ_ADMIN_PASSWORD": "correct-horse-battery-staple",
        "TOMATOIQ_COOKIE_SECURE": "false",  # TestClient talks plain HTTP
    })
    yield module, http, yolo
    sys.modules.pop("pwa_server", None)
    sys.modules.pop("auth", None)
    sys.modules.pop("ultralytics", None)
