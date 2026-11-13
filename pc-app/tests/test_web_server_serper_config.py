"""Search provider settings (2026-11-12): the Serper API key is write-only through the web API - it is
stored owner-only and never returned, only whether one exists and its last 4 characters."""
import json

import pytest

import profile_search
import web_server

KEY = "abcdef0123456789abcdef0123456789abcd"  # 36 alphanumeric characters


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setattr(profile_search, "SERPER_CONFIG_PATH", tmp_path / "serper_config.json")
    return web_server.app.test_client()


def test_status_when_nothing_is_stored(client):
    body = client.get("/api/serper-config").get_json()
    assert body == {"configured": False, "enabled": False, "key_hint": ""}


def test_saving_a_key_stores_it_and_never_returns_it(client):
    resp = client.post("/api/serper-config", json={"api_key": KEY, "enabled": True})
    assert resp.status_code == 200
    body = resp.get_json()
    assert body["ok"] and body["configured"] and body["enabled"] and body["key_hint"] == "...abcd"
    assert KEY not in json.dumps(body) and KEY not in client.get("/api/serper-config").get_data(as_text=True)
    assert profile_search.load_serper_config() == profile_search.SerperConfig(api_key=KEY, enabled=True)


def test_a_blank_key_keeps_the_stored_one_so_the_page_can_toggle_enabled_without_the_key(client):
    client.post("/api/serper-config", json={"api_key": KEY, "enabled": True})
    body = client.post("/api/serper-config", json={"api_key": "", "enabled": False}).get_json()
    assert body["ok"] and body["configured"] and body["enabled"] is False
    assert profile_search.load_serper_config().api_key == KEY


def test_enabling_without_any_key_is_refused(client):
    resp = client.post("/api/serper-config", json={"api_key": "", "enabled": True})
    assert resp.status_code == 400 and "key" in resp.get_json()["error"].lower()
    assert not profile_search.SERPER_CONFIG_PATH.exists()


@pytest.mark.parametrize("bad", ["short", "has spaces in it that is long enough xx", "<script>alert(1)</script>aaaaaaaa"])
def test_a_malformed_key_is_rejected_and_not_stored(client, bad):
    resp = client.post("/api/serper-config", json={"api_key": bad, "enabled": True})
    assert resp.status_code == 400
    assert not profile_search.SERPER_CONFIG_PATH.exists()


def test_the_websearch_test_reports_which_provider_answered(client, monkeypatch):
    def fake_search(query, max_results=5):
        profile_search.last_search_provider = "Serper (Google results)"
        return [profile_search.SearchHit("T", "https://x/")], ""

    monkeypatch.setattr(profile_search, "search_web", fake_search)
    body = client.post("/api/websearch/test", json={"query": "q"}).get_json()
    assert body["provider"] == "Serper (Google results)" and body["hits"][0]["url"] == "https://x/"


def test_search_web_records_the_provider_that_answered(monkeypatch):
    monkeypatch.setattr(profile_search, "load_serper_config", lambda: profile_search.SerperConfig(api_key=KEY, enabled=True))
    monkeypatch.setattr(profile_search, "_serper_query", lambda q, n, k: ([profile_search.SearchHit("S", "https://s/")], ""))
    profile_search.search_web("x")
    assert profile_search.last_search_provider == "Serper (Google results)"
    monkeypatch.setattr(profile_search, "_serper_query", lambda q, n, k: ([], "Serper search failed: HTTP 402"))
    monkeypatch.setattr(profile_search, "_search_web_ddg", lambda q, n=5: ([profile_search.SearchHit("D", "https://d/")], ""))
    profile_search.search_web("x")
    assert profile_search.last_search_provider == "DuckDuckGo"
