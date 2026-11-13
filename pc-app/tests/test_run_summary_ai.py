"""Tests for run_summary_ai.py (2026-09-14) - the completion notification's AI-phrased summary.
Strictly extractive: the model must only ever be given real, already-computed numbers and asked to
phrase them, never asked to invent anything - same rule as profile_search.py/process_assistant.py.
"""
import json

import ollama_client as oc
import run_summary_ai as rsa
from profile_search import OllamaConfig


def _summary(fault_count=0, warning_count=0, process_result="PASS", time_above_wetting_s=90.0):
    return {
        "fault_count": fault_count,
        "warning_count": warning_count,
        "process_result": process_result,
        "time_above_wetting_s": time_above_wetting_s,
        "zone_statistics": {
            "upper": {"min_c": 30.0, "max_c": 277.6, "avg_c": 190.0, "time_to_peak_s": 507.4},
            "lower": {"min_c": 30.2, "max_c": 281.3, "avg_c": 196.9, "time_to_peak_s": 506.9},
            "infrared": {"min_c": 39.2, "max_c": 196.5, "avg_c": 136.8, "time_to_peak_s": 504.9},
            "probe": {"min_c": 30.2, "max_c": 253.2, "avg_c": 166.9, "time_to_peak_s": 531.5},
        },
    }


def test_generate_run_summary_text_empty_when_ollama_not_configured():
    config = OllamaConfig(enabled=False)
    assert rsa.generate_run_summary_text("Test", "completed", _summary(), 553.0, config) == ""


def test_generate_run_summary_text_empty_when_no_model_set():
    config = OllamaConfig(host="http://fake", model="", enabled=True)
    assert rsa.generate_run_summary_text("Test", "completed", _summary(), 553.0, config) == ""


def test_generate_run_summary_text_uses_only_real_numbers_in_the_prompt(monkeypatch):
    """The model must be given the real computed stats, not asked to guess - and the prompt must
    explicitly forbid inventing numbers not listed."""
    captured = {}

    def fake_urlopen(req, timeout=5.0):
        body = json.loads(req.data.decode("utf-8"))
        captured["prompt"] = body["prompt"]

        class R:
            def __enter__(self): return self
            def __exit__(self, *a): return False
            def read(self): return json.dumps({"response": "It ran fine, hit 281C peak, no faults."}).encode("utf-8")
        return R()

    monkeypatch.setattr(oc.urllib.request, "urlopen", fake_urlopen)
    config = OllamaConfig(host="http://fake", model="qwen3:14b", enabled=True)
    text = rsa.generate_run_summary_text("PS5 APU REFLOW", "completed", _summary(fault_count=0), 553.0, config)
    assert text == "It ran fine, hit 281C peak, no faults."
    assert "do not estimate, calculate, guess, or add any number not explicitly listed" in captured["prompt"]
    assert "277.6" in captured["prompt"]  # real upper peak
    assert "281.3" in captured["prompt"]  # real lower peak
    assert "PS5 APU REFLOW" in captured["prompt"]


def test_generate_run_summary_text_empty_on_ollama_error(monkeypatch):
    import urllib.error

    def fake_urlopen(req, timeout=5.0):
        raise urllib.error.URLError("connection refused")

    monkeypatch.setattr(oc.urllib.request, "urlopen", fake_urlopen)
    config = OllamaConfig(host="http://fake", model="qwen3:14b", enabled=True)
    assert rsa.generate_run_summary_text("Test", "completed", _summary(), 553.0, config) == ""


def test_format_run_summary_notification_includes_header_and_real_peaks_without_ai_text():
    text = rsa.format_run_summary_notification("Run completed: Test Profile", _summary(), 553.0)
    assert "Run completed: Test Profile" in text
    assert "Upper 277.6C" in text
    assert "Lower 281.3C" in text
    assert "Probe 253.2C" in text
    assert "Duration: 553s" in text
    assert "Faults: 0" in text


def test_format_run_summary_notification_appends_ai_text_when_given():
    text = rsa.format_run_summary_notification("Run completed: Test", _summary(), 100.0, ai_text="A real ai sentence.")
    assert text.endswith("A real ai sentence.")


def test_format_run_summary_notification_handles_missing_zone_data_gracefully():
    text = rsa.format_run_summary_notification("Run completed: Test", {"fault_count": 0, "warning_count": 0}, 10.0)
    assert "unknown" in text
