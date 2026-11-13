"""Access control (review finding #13). Test-client requests default to 127.0.0.1, which is deliberately
trusted (deploy script / local shell); these tests simulate remote clients explicitly."""
import json

import pytest

import web_auth
import web_server as w

REMOTE = {"REMOTE_ADDR": "10.0.0.50"}
TOKEN = "t" * 32


@pytest.fixture
def client(monkeypatch):
    monkeypatch.delenv("LY_R690_AUTH", raising=False)
    monkeypatch.setattr(w, "_access_token", TOKEN)
    monkeypatch.setattr(w.app, "secret_key", "s" * 64)
    web_auth.reset_state_for_tests()
    return w.app.test_client()


def test_remote_api_without_credentials_is_401(client):
    for method, path in (("get", "/api/state"), ("post", "/api/stop"), ("post", "/api/admin/restart"), ("get", "/events")):
        r = getattr(client, method)(path, environ_base=REMOTE)
        assert r.status_code == 401, path


def test_remote_page_redirects_to_login(client):
    r = client.get("/", environ_base=REMOTE, headers={"Accept": "text/html"})
    assert r.status_code == 302 and r.headers["Location"].endswith("/login")


def test_login_with_right_token_grants_a_session(client):
    r = client.post("/login", data={"token": TOKEN}, environ_base=REMOTE)
    assert r.status_code == 302
    assert client.get("/api/diagnostics", environ_base=REMOTE).status_code != 401


def test_login_with_wrong_token_is_rejected_and_rate_limited(client):
    for _ in range(web_auth.MAX_FAILURES):
        assert client.post("/login", data={"token": "wrong"}, environ_base=REMOTE).status_code == 401
    # now locked out - even the RIGHT token is refused until the lockout ends
    r = client.post("/login", data={"token": TOKEN}, environ_base=REMOTE)
    assert r.status_code == 401 and b"Too many" in r.data
    assert client.get("/api/state", environ_base=REMOTE).status_code == 401


def test_bearer_token_works_for_scripts(client):
    r = client.get("/api/diagnostics", environ_base=REMOTE, headers={"Authorization": f"Bearer {TOKEN}"})
    assert r.status_code != 401
    r = client.get("/api/diagnostics", environ_base=REMOTE, headers={"Authorization": "Bearer nope"})
    assert r.status_code == 401


def test_loopback_is_trusted_but_not_when_proxied(client):
    assert client.get("/api/diagnostics").status_code != 401
    r = client.get("/api/diagnostics", headers={"X-Forwarded-For": "8.8.8.8"})
    assert r.status_code == 401


def test_uninitialised_auth_fails_closed_for_remote(monkeypatch):
    monkeypatch.delenv("LY_R690_AUTH", raising=False)
    monkeypatch.setattr(w, "_access_token", None)
    assert w.app.test_client().get("/api/state", environ_base=REMOTE).status_code == 401


def test_auth_can_be_disabled_explicitly(monkeypatch):
    monkeypatch.setenv("LY_R690_AUTH", "off")
    assert w.app.test_client().get("/api/diagnostics", environ_base=REMOTE).status_code != 401


def test_credentials_file_is_created_private_and_reused(tmp_path, monkeypatch):
    monkeypatch.setattr(web_auth, "AUTH_CONFIG_PATH", tmp_path / "auth_config.json")
    token, secret, created = web_auth.load_or_create_credentials()
    assert created and len(token) >= 24 and len(secret) >= 32
    again = web_auth.load_or_create_credentials()
    assert again == (token, secret, False)
    (tmp_path / "auth_config.json").write_text("{broken")
    assert web_auth.load_or_create_credentials()[2] is True
