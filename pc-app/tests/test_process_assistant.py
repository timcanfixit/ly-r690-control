"""Tests for process_assistant.py (2026-09-07, external feature-gap review). See its own module
docstring for the evidence-tagging model and the hard read-only architectural rule this respects."""
import process_assistant as pa
from profile_search import OllamaConfig


def _config(enabled=True, model="qwen3:latest"):
    return OllamaConfig(host="http://fake-ollama", model=model, enabled=enabled)


def test_disabled_config_returns_error_without_calling_ollama(monkeypatch):
    called = []
    monkeypatch.setattr(pa, "call_ollama_raw", lambda *a, **k: called.append(1) or ("", ""))
    answer, error = pa.ask_process_assistant("why did this run fail?", _config(enabled=False))
    assert answer == ""
    assert "not configured" in error
    assert called == []


def test_empty_question_returns_error_without_calling_ollama(monkeypatch):
    called = []
    monkeypatch.setattr(pa, "call_ollama_raw", lambda *a, **k: called.append(1) or ("", ""))
    answer, error = pa.ask_process_assistant("   ", _config())
    assert answer == ""
    assert "question" in error.lower()
    assert called == []


def test_successful_answer_is_returned_stripped(monkeypatch):
    monkeypatch.setattr(pa, "call_ollama_raw", lambda host, model, prompt: ("  [MEASURED] 3 runs logged.  ", ""))
    monkeypatch.setattr(pa.data_logger, "list_runs", lambda: [])
    monkeypatch.setattr(pa.profile_store, "list_profiles", lambda: [])
    monkeypatch.setattr(pa.maintenance, "compute_maintenance_status", lambda: {
        "runs": {"total_runs": 0, "total_run_hours": 0.0, "total_faults": 0, "total_warnings": 0},
        "calibration": {}, "self_tune": {},
    })
    answer, error = pa.ask_process_assistant("how many runs have I done?", _config())
    assert answer == "[MEASURED] 3 runs logged."
    assert error == ""


def test_ollama_error_is_propagated(monkeypatch):
    monkeypatch.setattr(pa, "call_ollama_raw", lambda host, model, prompt: ("", "Could not reach Ollama at http://fake: connection refused"))
    monkeypatch.setattr(pa.data_logger, "list_runs", lambda: [])
    monkeypatch.setattr(pa.profile_store, "list_profiles", lambda: [])
    monkeypatch.setattr(pa.maintenance, "compute_maintenance_status", lambda: {
        "runs": {"total_runs": 0, "total_run_hours": 0.0, "total_faults": 0, "total_warnings": 0},
        "calibration": {}, "self_tune": {},
    })
    answer, error = pa.ask_process_assistant("anything", _config())
    assert answer == ""
    assert "connection refused" in error


def test_empty_ollama_response_is_an_error_not_a_blank_success(monkeypatch):
    monkeypatch.setattr(pa, "call_ollama_raw", lambda host, model, prompt: ("   ", ""))
    monkeypatch.setattr(pa.data_logger, "list_runs", lambda: [])
    monkeypatch.setattr(pa.profile_store, "list_profiles", lambda: [])
    monkeypatch.setattr(pa.maintenance, "compute_maintenance_status", lambda: {
        "runs": {"total_runs": 0, "total_run_hours": 0.0, "total_faults": 0, "total_warnings": 0},
        "calibration": {}, "self_tune": {},
    })
    answer, error = pa.ask_process_assistant("anything", _config())
    assert answer == ""
    assert "empty" in error.lower()


# ---- context building - the actual data grounding, and the prompt's hard rules

def test_context_includes_real_run_data_and_question(monkeypatch):
    captured = {}
    def fake_call(host, model, prompt):
        captured["prompt"] = prompt
        return "ok", ""
    monkeypatch.setattr(pa, "call_ollama_raw", fake_call)
    monkeypatch.setattr(pa.data_logger, "list_runs", lambda: [
        {"started": "2026-09-01 12:00:00", "profile_name": "PS5 APU", "duration_s": 300.0,
         "peak_upper": 260.0, "peak_lower": 265.0, "peak_ir": 210.0,
         "fault_count": 1, "warning_count": 0, "time_above_wetting_s": 45.0,
         "process_result": "FAIL", "golden": False},
    ])
    monkeypatch.setattr(pa.profile_store, "list_profiles", lambda: [])
    monkeypatch.setattr(pa.maintenance, "compute_maintenance_status", lambda: {
        "runs": {"total_runs": 1, "total_run_hours": 0.08, "total_faults": 1, "total_warnings": 0},
        "calibration": {}, "self_tune": {},
    })
    pa.ask_process_assistant("why did my last PS5 APU run fail?", _config())
    prompt = captured["prompt"]
    assert "PS5 APU" in prompt
    assert "1 fault(s)" in prompt
    assert "result=FAIL" in prompt
    assert "why did my last PS5 APU run fail?" in prompt


def test_context_includes_profile_metadata(monkeypatch):
    captured = {}
    def fake_call(host, model, prompt):
        captured["prompt"] = prompt
        return "ok", ""
    monkeypatch.setattr(pa, "call_ollama_raw", fake_call)
    monkeypatch.setattr(pa.data_logger, "list_runs", lambda: [])
    monkeypatch.setattr(pa.profile_store, "list_profiles", lambda: ["PS5 APU Reflow"])
    monkeypatch.setattr(pa.profile_store, "get_profile_metadata", lambda name: {"board": "PS5", "component": "APU", "status": "Approved"})
    monkeypatch.setattr(pa.maintenance, "compute_maintenance_status", lambda: {
        "runs": {"total_runs": 0, "total_run_hours": 0.0, "total_faults": 0, "total_warnings": 0},
        "calibration": {}, "self_tune": {},
    })
    pa.ask_process_assistant("what profiles do I have for PS5?", _config())
    assert "board=PS5" in captured["prompt"]
    assert "status=Approved" in captured["prompt"]


def test_context_includes_maintenance_status(monkeypatch):
    captured = {}
    def fake_call(host, model, prompt):
        captured["prompt"] = prompt
        return "ok", ""
    monkeypatch.setattr(pa, "call_ollama_raw", fake_call)
    monkeypatch.setattr(pa.data_logger, "list_runs", lambda: [])
    monkeypatch.setattr(pa.profile_store, "list_profiles", lambda: [])
    monkeypatch.setattr(pa.maintenance, "compute_maintenance_status", lambda: {
        "runs": {"total_runs": 5, "total_run_hours": 2.5, "total_faults": 3, "total_warnings": 1},
        "calibration": {"ir": {"days_since": 45.0, "status": "ok"}},
        "self_tune": {"ir": {"days_since": None, "status": "never", "session_count": 0}},
    })
    pa.ask_process_assistant("is my IR calibration overdue?", _config())
    assert "45 days ago" in captured["prompt"]
    assert "status=ok" in captured["prompt"]
    assert "never" in captured["prompt"]


def test_prompt_instructs_evidence_tagging_and_no_control_capability(monkeypatch):
    """The two hard rules this whole module exists to enforce - not just data grounding, but the
    evidence tags and the explicit "I cannot act" instruction."""
    captured = {}
    def fake_call(host, model, prompt):
        captured["prompt"] = prompt
        return "ok", ""
    monkeypatch.setattr(pa, "call_ollama_raw", fake_call)
    monkeypatch.setattr(pa.data_logger, "list_runs", lambda: [])
    monkeypatch.setattr(pa.profile_store, "list_profiles", lambda: [])
    monkeypatch.setattr(pa.maintenance, "compute_maintenance_status", lambda: {
        "runs": {"total_runs": 0, "total_run_hours": 0.0, "total_faults": 0, "total_warnings": 0},
        "calibration": {}, "self_tune": {},
    })
    pa.ask_process_assistant("start a run for me", _config())
    prompt = captured["prompt"]
    assert "[MEASURED]" in prompt
    assert "[HISTORICAL]" in prompt
    assert "[INFERENCE]" in prompt
    assert "cannot control this machine" in prompt


def test_no_runs_or_profiles_yet_produces_honest_placeholder_not_a_crash(monkeypatch):
    captured = {}
    def fake_call(host, model, prompt):
        captured["prompt"] = prompt
        return "ok", ""
    monkeypatch.setattr(pa, "call_ollama_raw", fake_call)
    monkeypatch.setattr(pa.data_logger, "list_runs", lambda: [])
    monkeypatch.setattr(pa.profile_store, "list_profiles", lambda: [])
    monkeypatch.setattr(pa.maintenance, "compute_maintenance_status", lambda: {
        "runs": {"total_runs": 0, "total_run_hours": 0.0, "total_faults": 0, "total_warnings": 0},
        "calibration": {}, "self_tune": {},
    })
    pa.ask_process_assistant("anything", _config())
    assert "no runs logged yet" in captured["prompt"]
    assert "no profiles saved yet" in captured["prompt"]
