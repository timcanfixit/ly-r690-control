"""Coverage for POST /api/websearch/test - 2026-09-09.

Replaces test_web_server_google_search_test.py from earlier the same day: that route tested the
Google Custom Search JSON API, which turned out to be closed to new customers since 2025 (see
profile_search.py's module docstring) - reverted to DuckDuckGo, so there's no api_key/cx to test
anymore, just a query. Lets the operator confirm web search is actually working right now, since
DuckDuckGo's own bot-detection can block this app's requests without warning.
"""
import profile_search
import web_server


def test_test_route_uses_default_query_when_blank(monkeypatch):
    captured = {}

    def fake_search_web(query, max_results=5):
        captured["query"] = query
        return [], ""

    monkeypatch.setattr(profile_search, "search_web", fake_search_web)
    client = web_server.app.test_client()
    resp = client.post("/api/websearch/test", json={})
    assert resp.status_code == 200
    assert captured["query"]  # non-empty default query was used


def test_test_route_uses_given_query(monkeypatch):
    captured = {}

    def fake_search_web(query, max_results=5):
        captured["query"] = query
        return [profile_search.SearchHit(title="Great Guide", url="https://example.com/guide")], ""

    monkeypatch.setattr(profile_search, "search_web", fake_search_web)
    client = web_server.app.test_client()
    resp = client.post("/api/websearch/test", json={"query": "PS5 APU reflow"})
    assert resp.status_code == 200
    assert captured["query"] == "PS5 APU reflow"
    data = resp.get_json()
    assert data["error"] == ""
    assert data["hits"] == [{"title": "Great Guide", "url": "https://example.com/guide"}]


def test_test_route_surfaces_bot_challenge_error(monkeypatch):
    """Regression guard: the route must pass through search_web's real error message (e.g. a
    DuckDuckGo bot-detection challenge) rather than hiding it behind a generic failure."""
    monkeypatch.setattr(
        profile_search, "search_web",
        lambda query, max_results=5: ([], "DuckDuckGo blocked this request as automated traffic (bot-detection challenge) - this app will never attempt to bypass that. Try again later, or find a page yourself and paste its URL in instead."),
    )
    client = web_server.app.test_client()
    resp = client.post("/api/websearch/test", json={"query": "anything"})
    assert resp.status_code == 200
    data = resp.get_json()
    assert data["hits"] == []
    assert "blocked" in data["error"].lower()


def test_test_route_requires_no_credentials(monkeypatch):
    """Unlike the old Google-backed route, there is nothing to configure - an empty JSON body
    (what postJson() in app.js sends for a call with no arguments) must still run a search
    (mocked here to avoid a real network call) rather than 400ing for missing fields."""
    monkeypatch.setattr(profile_search, "search_web", lambda query, max_results=5: ([], ""))
    client = web_server.app.test_client()
    resp = client.post("/api/websearch/test", json={})
    assert resp.status_code == 200
