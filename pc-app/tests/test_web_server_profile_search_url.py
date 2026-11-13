"""Coverage for POST /api/profile-search/url - manual-URL mode, 2026-09-09.

Added after confirming live that DuckDuckGo's own bot-detection can block the automatic search
step (/api/profile-search) without warning - this route skips search entirely: the operator
supplies a page they already found themselves, and the app just fetches + extracts from it, same
as it would for one automatic search result. See profile_search.extract_profile_from_url's own
docstring for why this deliberately never falls back to an AI-generated guess on failure.
"""
import profile_search
import web_server
from modbus_client import CurveSection, Profile


def test_requires_board_name():
    client = web_server.app.test_client()
    resp = client.post("/api/profile-search/url", json={"url": "https://example.com/guide"})
    assert resp.status_code == 200
    data = resp.get_json()
    assert data["candidates"] == []
    assert "board_name is required" in data["errors"][0]


def test_requires_url():
    client = web_server.app.test_client()
    resp = client.post("/api/profile-search/url", json={"board_name": "PS5 APU"})
    assert resp.status_code == 200
    data = resp.get_json()
    assert data["candidates"] == []
    assert "url is required" in data["errors"][0]


def test_success_returns_candidate_json(monkeypatch):
    real_candidate = profile_search.ProfileSearchCandidate(
        source_title="https://example.com/guide", source_url="https://example.com/guide",
        profile=Profile(name="PS5 APU", sections=[CurveSection(upper_temp=220)] + [CurveSection()] * 7),
        machine_match=False, quotes_verified=True, notes=["found it"],
    )

    def fake_extract_from_url(board_name, url, config, on_progress=None):
        assert board_name == "PS5 APU"
        assert url == "https://example.com/guide"
        return profile_search.ProfileSearchResult(candidates=[real_candidate], errors=[])

    monkeypatch.setattr(profile_search, "extract_profile_from_url", fake_extract_from_url)
    client = web_server.app.test_client()
    resp = client.post("/api/profile-search/url", json={"board_name": "PS5 APU", "url": "https://example.com/guide"})
    assert resp.status_code == 200
    data = resp.get_json()
    assert len(data["candidates"]) == 1
    assert data["candidates"][0]["source_url"] == "https://example.com/guide"
    assert data["errors"] == []


def test_failure_surfaces_the_real_error(monkeypatch):
    monkeypatch.setattr(
        profile_search, "extract_profile_from_url",
        lambda board_name, url, config, on_progress=None: profile_search.ProfileSearchResult(
            candidates=[], errors=[f"Could not fetch {url}: 404"],
        ),
    )
    client = web_server.app.test_client()
    resp = client.post("/api/profile-search/url", json={"board_name": "PS5 APU", "url": "https://example.com/dead"})
    assert resp.status_code == 200
    data = resp.get_json()
    assert data["candidates"] == []
    assert "Could not fetch" in data["errors"][0]
