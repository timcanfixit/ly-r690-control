"""Tests for ollama_client.py (2026-09-07) - factored out of profile_search.py so
process_assistant.py could reuse the same tested request/error-handling logic. call_ollama_json/
extract_json_object are already exercised indirectly through profile_search.py's own 46 tests
(which monkeypatch them under their original private names) - this file covers call_ollama_raw
specifically, the one function process_assistant.py needed that profile_search.py never used."""
import json

import ollama_client as oc


class _FakeResp:
    def __init__(self, body: dict):
        self._body = body

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def read(self):
        return json.dumps(self._body).encode("utf-8")


def test_call_ollama_raw_returns_plain_text_no_json_parsing(monkeypatch):
    monkeypatch.setattr(oc.urllib.request, "urlopen", lambda req, timeout=5.0: _FakeResp({"response": "This is a plain-text answer, not JSON."}))
    text, error = oc.call_ollama_raw("http://fake", "model", "prompt")
    assert error == ""
    assert text == "This is a plain-text answer, not JSON."


def test_call_ollama_raw_does_not_request_json_format(monkeypatch):
    captured = {}

    def fake_urlopen(req, timeout=5.0):
        captured["body"] = json.loads(req.data.decode("utf-8"))
        return _FakeResp({"response": "ok"})

    monkeypatch.setattr(oc.urllib.request, "urlopen", fake_urlopen)
    oc.call_ollama_raw("http://fake", "model", "prompt")
    assert "format" not in captured["body"]


def test_call_ollama_raw_reports_connection_failure(monkeypatch):
    import urllib.error

    def fake_urlopen(req, timeout=5.0):
        raise urllib.error.URLError("connection refused")

    monkeypatch.setattr(oc.urllib.request, "urlopen", fake_urlopen)
    text, error = oc.call_ollama_raw("http://fake", "model", "prompt")
    assert text == ""
    assert "connection refused" in error


def test_call_ollama_raw_handles_malformed_envelope(monkeypatch):
    class BadResp:
        def __enter__(self): return self
        def __exit__(self, *a): return False
        def read(self): return b"not json at all"

    monkeypatch.setattr(oc.urllib.request, "urlopen", lambda req, timeout=5.0: BadResp())
    text, error = oc.call_ollama_raw("http://fake", "model", "prompt")
    assert text == ""
    assert error != ""


def test_call_ollama_raw_missing_response_key_returns_empty_string(monkeypatch):
    """No "response" key at all - an honest empty string, not a crash."""
    monkeypatch.setattr(oc.urllib.request, "urlopen", lambda req, timeout=5.0: _FakeResp({}))
    text, error = oc.call_ollama_raw("http://fake", "model", "prompt")
    assert text == ""
    assert error == ""


def test_call_ollama_raw_caps_num_ctx(monkeypatch):
    """Confirmed live 2026-09-13: qwen3:14b's own default context (40960, per `ollama ps`) forced
    a needless partial GPU/CPU split even on a 16GB card that fits the model comfortably at a sane
    context - see _OLLAMA_NUM_CTX's docstring. Every request must cap it explicitly rather than
    trust whatever a given model's own Modelfile happens to default to."""
    captured = {}

    def fake_urlopen(req, timeout=5.0):
        captured["body"] = json.loads(req.data.decode("utf-8"))
        return _FakeResp({"response": "ok"})

    monkeypatch.setattr(oc.urllib.request, "urlopen", fake_urlopen)
    oc.call_ollama_raw("http://fake", "model", "prompt")
    assert captured["body"]["options"]["num_ctx"] == oc._OLLAMA_NUM_CTX


def test_call_ollama_json_caps_num_ctx(monkeypatch):
    captured = {}

    def fake_urlopen(req, timeout=5.0):
        captured["body"] = json.loads(req.data.decode("utf-8"))
        return _FakeResp({"response": "{}"})

    monkeypatch.setattr(oc.urllib.request, "urlopen", fake_urlopen)
    oc.call_ollama_json("http://fake", "model", "prompt")
    assert captured["body"]["options"]["num_ctx"] == oc._OLLAMA_NUM_CTX
    # also confirmed with the non-JSON-grammar path (use_json_format=False), the other real
    # call shape this module supports
    oc.call_ollama_json("http://fake", "model", "prompt", use_json_format=False)
    assert captured["body"]["options"]["num_ctx"] == oc._OLLAMA_NUM_CTX


def test_extract_json_object_still_works_from_this_module_directly():
    """Same function profile_search.py re-exports under _extract_json_object - covered here too
    since process_assistant.py could theoretically use it directly in the future."""
    assert oc.extract_json_object('prefix text {"a": 1} suffix') == {"a": 1}
    assert oc.extract_json_object("no object here") is None
