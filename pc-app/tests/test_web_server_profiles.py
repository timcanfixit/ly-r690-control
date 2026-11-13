"""Regression coverage for the profile-name HTTP routes in web_server.py - 2026-09-08.

A profile named with a literal "/" (e.g. "PS5 HDMI ENCODER / SMALL IC REFLOW" - a real profile
name from this app's own default library) broke EVERY name-addressed route: Flask's default
<name> converter uses a regex that excludes "/", so once the browser's encodeURIComponent-encoded
%2F was decoded back to a literal slash by the WSGI layer (standard behavior, before Flask ever
sees the path), the route simply failed to match at all - a raw 404 from Flask's own error
handler, not this app's JSON error response. Confirmed live: selecting the profile showed "Not
found", and Delete silently did nothing (the DELETE request 404'd, but the JS handler never
checked response.ok before refreshing the list, so it looked like it worked).

Fixed by switching every profile-name route from <name> to <path:name> (Werkzeug's converter for
a path segment that may itself contain slashes) - these tests exist so that fix never quietly
regresses back to a route decorator with a bare <name>.

Note: profile_store.py's own filename SANITIZATION (turning "/" into "-" for the actual file on
disk - see profile_store._safe_filename) was NOT the bug and was already correct; this was purely
an HTTP-routing-layer problem one level up from that.
"""
import profile_store
import web_server
from modbus_client import Profile, CurveSection


class StubBus:
    """web_server.bus is only ever assigned inside main() (never at plain import time) - these
    tests exercise routes that don't touch it, but api_set_profile_metadata's error path and a
    couple of others reference module-level state that expects `bus` to exist by the time any
    route runs, so stub it defensively the same way other route-level tests in this session have."""
    def set_ir_reading_correction(self, value):
        pass


SLASH_NAME = "PS5 HDMI ENCODER / SMALL IC REFLOW"


def _seed_profile(tmp_path, monkeypatch, name=SLASH_NAME):
    monkeypatch.setattr(profile_store, "PROFILES_DIR", tmp_path)
    web_server.bus = StubBus()
    profile_store.save_profile(Profile(name=name, sections=[CurveSection() for _ in range(8)]))
    return web_server.app.test_client()


def test_get_profile_with_slash_in_name(tmp_path, monkeypatch):
    client = _seed_profile(tmp_path, monkeypatch)
    resp = client.get(f"/api/profiles/{SLASH_NAME}")
    assert resp.status_code == 200
    assert resp.get_json()["name"] == SLASH_NAME


def test_delete_profile_with_slash_in_name_actually_deletes(tmp_path, monkeypatch):
    client = _seed_profile(tmp_path, monkeypatch)
    assert SLASH_NAME in profile_store.list_profiles()
    resp = client.delete(f"/api/profiles/{SLASH_NAME}")
    assert resp.status_code == 200
    assert resp.get_json()["ok"] is True
    assert SLASH_NAME not in profile_store.list_profiles()


def test_get_metadata_with_slash_in_name(tmp_path, monkeypatch):
    client = _seed_profile(tmp_path, monkeypatch)
    resp = client.get(f"/api/profiles/{SLASH_NAME}/metadata")
    assert resp.status_code == 200


def test_set_metadata_with_slash_in_name(tmp_path, monkeypatch):
    client = _seed_profile(tmp_path, monkeypatch)
    resp = client.post(f"/api/profiles/{SLASH_NAME}/metadata", json={"notes": "test"})
    assert resp.status_code == 200
    assert resp.get_json()["ok"] is True


def test_get_history_with_slash_in_name(tmp_path, monkeypatch):
    client = _seed_profile(tmp_path, monkeypatch)
    resp = client.get(f"/api/profiles/{SLASH_NAME}/history")
    assert resp.status_code == 200


def test_get_qualification_with_slash_in_name(tmp_path, monkeypatch):
    client = _seed_profile(tmp_path, monkeypatch)
    resp = client.get(f"/api/profiles/{SLASH_NAME}/qualification")
    assert resp.status_code == 200


def test_rename_profile_with_slash_in_name(tmp_path, monkeypatch):
    client = _seed_profile(tmp_path, monkeypatch)
    resp = client.post(f"/api/profiles/{SLASH_NAME}/rename", json={"new_name": "Renamed OK"})
    assert resp.status_code == 200
    assert "Renamed OK" in profile_store.list_profiles()
    assert SLASH_NAME not in profile_store.list_profiles()


def test_restore_version_with_slash_in_name(tmp_path, monkeypatch):
    client = _seed_profile(tmp_path, monkeypatch)
    # Save a second version so there's real history to restore.
    profile_store.save_profile(Profile(name=SLASH_NAME, sections=[CurveSection(upper_temp=99) for _ in range(8)]))
    resp = client.post(f"/api/profiles/{SLASH_NAME}/restore", json={"version": 1})
    assert resp.status_code == 200
