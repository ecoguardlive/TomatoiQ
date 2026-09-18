"""Auth integration tests.

Fixtures: `client` (auth disabled, the default) and `auth_client` (auth
enabled with a known username/password/secret) live in conftest.py.
"""
from __future__ import annotations


def test_auth_disabled_by_default_never_blocks_requests(client):
    """The default (no env vars set) must behave exactly as before this
    work: no login prompt, no 401s, ever."""
    _, http, _ = client
    resp = http.get("/api/dashboard")
    assert resp.status_code == 200


def test_protected_route_without_credentials_returns_401_not_500(auth_client):
    """This is the actual bug this phase fixes: HTTPException raised inside
    a Starlette @app.middleware("http") function is NOT converted into a
    proper JSON response by FastAPI's normal exception handling. Before the
    fix, this request returned a bare 500 Internal Server Error instead of
    a 401."""
    _, http, _ = auth_client
    resp = http.get("/api/dashboard")
    assert resp.status_code == 401
    assert resp.json()["detail"]


def test_login_endpoint_is_not_itself_protected(auth_client):
    """/api/auth/login is the one route that must stay reachable while
    logged out -- otherwise nobody could ever log in."""
    _, http, _ = auth_client
    resp = http.post("/api/auth/login", json={"username": "farmer", "password": "wrong"})
    assert resp.status_code == 401  # wrong password, but the route itself was reachable


def test_login_with_correct_credentials_sets_cookie_and_returns_token(auth_client):
    _, http, _ = auth_client
    resp = http.post("/api/auth/login", json={"username": "farmer", "password": "correct-horse-battery-staple"})
    assert resp.status_code == 200
    body = resp.json()
    assert body["token_type"] == "bearer"
    assert body["access_token"]
    assert "tomatoiq_session" in resp.cookies


def test_cookie_from_login_authenticates_subsequent_requests(auth_client):
    """This is what makes the PWA's own login flow work: the browser never
    has to manage a bearer token itself, the cookie the login response set
    is sent automatically on every later request."""
    module, http, _ = auth_client
    login = http.post("/api/auth/login", json={"username": "farmer", "password": "correct-horse-battery-staple"})
    assert login.status_code == 200

    # TestClient persists cookies across requests on the same client, just
    # like a real browser -- no manual header wiring needed here.
    resp = http.get("/api/dashboard")
    assert resp.status_code == 200


def test_bearer_token_also_still_works_for_api_clients(auth_client):
    """Scripts/API clients that can't use a cookie jar still work via the
    Authorization header."""
    _, http, _ = auth_client
    login = http.post("/api/auth/login", json={"username": "farmer", "password": "correct-horse-battery-staple"})
    token = login.json()["access_token"]
    http.cookies.clear()  # simulate a client with no cookie jar at all

    resp = http.get("/api/dashboard", headers={"Authorization": f"Bearer {token}"})
    assert resp.status_code == 200


def test_wrong_password_is_rejected(auth_client):
    _, http, _ = auth_client
    resp = http.post("/api/auth/login", json={"username": "farmer", "password": "wrong"})
    assert resp.status_code == 401
    assert "tomatoiq_session" not in resp.cookies


def test_tampered_token_is_rejected(auth_client):
    _, http, _ = auth_client
    login = http.post("/api/auth/login", json={"username": "farmer", "password": "correct-horse-battery-staple"})
    token = login.json()["access_token"]
    tampered = token[:-1] + ("0" if token[-1] != "0" else "1")

    resp = http.get("/api/dashboard", headers={"Authorization": f"Bearer {tampered}"})
    assert resp.status_code == 401


def test_logout_clears_the_session_cookie(auth_client):
    _, http, _ = auth_client
    http.post("/api/auth/login", json={"username": "farmer", "password": "correct-horse-battery-staple"})
    assert http.get("/api/dashboard").status_code == 200

    logout = http.post("/api/auth/logout")
    assert logout.status_code == 200
    assert http.get("/api/dashboard").status_code == 401


def test_login_rate_limit_blocks_repeated_bad_attempts(auth_client):
    """Brute-force protection: LOGIN_RATE_LIMITER allows 5 attempts per
    5 minutes per IP regardless of whether the password was right."""
    module, http, _ = auth_client
    for _ in range(module.LOGIN_RATE_LIMITER.max_requests):
        resp = http.post("/api/auth/login", json={"username": "farmer", "password": "wrong"})
        assert resp.status_code == 401

    blocked = http.post("/api/auth/login", json={"username": "farmer", "password": "wrong"})
    assert blocked.status_code == 429


def test_websocket_rejected_without_auth_when_required(auth_client):
    _, http, _ = auth_client
    try:
        with http.websocket_connect("/api/live"):
            assert False, "expected the handshake to be rejected"
    except Exception:
        pass  # TestClient raises when the server closes during handshake


def test_websocket_accepted_with_session_cookie(auth_client):
    _, http, _ = auth_client
    http.post("/api/auth/login", json={"username": "farmer", "password": "correct-horse-battery-staple"})
    with http.websocket_connect("/api/live") as ws:
        message = ws.receive_json()
        assert message["type"] == "dashboard_update"


def test_websocket_works_normally_when_auth_disabled(client):
    _, http, _ = client
    with http.websocket_connect("/api/live") as ws:
        message = ws.receive_json()
        assert message["type"] == "dashboard_update"


def test_dotenv_file_is_actually_loaded(monkeypatch, tmp_path):
    """Regression test for the real bug behind 'when I log in it doesn't log
    in': only docker-compose's env_file: reads a .env file on its own -- a
    plain `python -m uvicorn` never did, so a .env sitting next to the code
    had zero effect outside Docker until auth.py explicitly loads it."""
    import sys
    monkeypatch.chdir(tmp_path)
    (tmp_path / ".env").write_text(
        "TOMATOIQ_AUTH_REQUIRED=true\n"
        "TOMATOIQ_AUTH_SECRET=from-dotenv-file\n"
        "TOMATOIQ_ADMIN_USERNAME=dotenv-user\n"
        "TOMATOIQ_ADMIN_PASSWORD=dotenv-pass\n"
    )
    # Make sure none of these are already set from the surrounding shell/CI
    # env, so a pass here can only mean the .env file itself was read.
    for key in ("TOMATOIQ_AUTH_REQUIRED", "TOMATOIQ_AUTH_SECRET",
                "TOMATOIQ_ADMIN_USERNAME", "TOMATOIQ_ADMIN_PASSWORD"):
        monkeypatch.delenv(key, raising=False)

    sys.modules.pop("auth", None)
    import auth
    assert auth.auth_enabled() is True
    assert auth.authenticate("dotenv-user", "dotenv-pass") is True
    sys.modules.pop("auth", None)
