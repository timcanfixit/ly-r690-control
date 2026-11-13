"""A CRITICAL fault must properly END an active run even when the machine's running coil reads
False - so the run is never wrongly flagged INTERRUPTED at the next startup. 2026-09-10.

Bug found live (run_20260910_162151): _check_safety_limits' fault handler gated ALL of the run
teardown (stop logging, write the summary, clear run_recovery's marker) on `reading.running`.
A fault that fired while the running coil read False (coil lag/flicker, or the machine had
already dropped out of running) left the run in limbo: no summary written, marker never cleared,
so the run showed up as "INTERRUPTED" next start with an almost-empty summary sidecar.
"""
import time

import run_recovery
import web_server
from modbus_client import LiveReading
from run_finalization import RunFinalizationTracker
from safety_limits import RampTracker, SafetyLimits


class _Bus:
    def get_temps_timestamp(self):
        return time.monotonic()

    def get_diagnostics(self):
        return {"read_attempts": 0, "read_successes": 0, "read_failures": 0,
                "write_attempts": 0, "write_successes": 0, "write_failures": 0, "retry_count": 0}

    def get_live_reading(self):
        return None

    def write_coil(self, *a, **k):
        return None

    def seconds_since_update(self):
        return 0.0


class _Logger:
    def stop_run(self):
        pass


def _reading_over_limit():
    # running=False, but a probe reading way over any sane ceiling -> a CRITICAL fault fires
    return LiveReading(
        upper_temp=30.0, lower_temp=30.0, infrared_temp=30.0, probe_temp=999.0,
        running=False, running_mirror=False, vacuum=False, cooling=False, keep=False,
    )


def test_fault_with_running_false_still_finalizes_the_run(tmp_path, monkeypatch):
    marker = tmp_path / "active_run.json"
    monkeypatch.setattr(run_recovery, "MARKER_PATH", marker)

    csv_path = tmp_path / "run_test.csv"
    csv_path.write_text(
        "timestamp,elapsed_s,upper_temp,lower_temp,infrared_temp,probe_temp,running,vacuum,cooling,profile_name\n"
        "1.0,0.0,30,30,30,30,False,False,False,Test\n"
        "1.5,0.5,30,30,30,30,False,False,False,Test\n"
    )

    run_recovery.mark_run_started("Test", csv_path.name)
    assert marker.exists()

    monkeypatch.setattr(web_server, "bus", _Bus(), raising=False)
    monkeypatch.setattr(web_server, "logger_", _Logger())
    monkeypatch.setattr(web_server, "safety_limits", SafetyLimits())
    monkeypatch.setattr(web_server, "_ramp_tracker", RampTracker())
    monkeypatch.setattr(web_server, "_run_tracker", RunFinalizationTracker())
    monkeypatch.setattr(web_server, "_current_log_path", csv_path)
    monkeypatch.setattr(web_server, "_logging_active", True)
    monkeypatch.setattr(web_server, "_active_violation_names", set())
    monkeypatch.setattr(web_server, "_prior_reading", None)
    monkeypatch.setattr(web_server.notifications, "notify_async", lambda *a, **k: None)

    web_server._check_safety_limits(_reading_over_limit())

    # The run was closed out cleanly despite running being False the whole time:
    assert not marker.exists(), "run_recovery marker must be cleared - the run had a KNOWN ending (this fault)"
    assert web_server._run_tracker is None, "run tracker must be finalized"
    assert web_server._logging_active is False

    # And the summary is the real finalized one, not a bare {'interrupted': true} stub.
    summary_path = csv_path.with_suffix(".summary.json")
    assert summary_path.exists()
    import json
    summary = json.loads(summary_path.read_text())
    assert "interrupted" not in summary
    assert "events" in summary or "zone_statistics" in summary


def test_fault_with_no_active_run_is_a_safe_noop(tmp_path, monkeypatch):
    marker = tmp_path / "active_run.json"
    monkeypatch.setattr(run_recovery, "MARKER_PATH", marker)
    monkeypatch.setattr(web_server, "bus", _Bus(), raising=False)
    monkeypatch.setattr(web_server, "safety_limits", SafetyLimits())
    monkeypatch.setattr(web_server, "_ramp_tracker", RampTracker())
    monkeypatch.setattr(web_server, "_run_tracker", None)
    monkeypatch.setattr(web_server, "_current_log_path", None)
    monkeypatch.setattr(web_server, "_logging_active", False)
    monkeypatch.setattr(web_server, "_active_violation_names", set())
    monkeypatch.setattr(web_server, "_prior_reading", None)
    monkeypatch.setattr(web_server.notifications, "notify_async", lambda *a, **k: None)

    web_server._check_safety_limits(_reading_over_limit())  # must not raise
    assert not marker.exists()
