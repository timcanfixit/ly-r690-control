"""Regression coverage for a real Discord alert (2026-09-13): "Infrared temperature changed
538C/min, exceeding the configured hard maximum of 380C/min" firing right as a run ended, followed
by "Run ended by fault" (the BUG 1 fix from the same week correctly closing the run out - the fault
itself was still a false positive).

IR has very low thermal mass and crashes fast the instant its heater cuts off / cooling engages -
a real, expected cooldown artifact, not a runaway. Two related gaps, both fixed:
1. estimate_current_section() returns None the instant `running` goes False, so the existing
   section-transition ramp reset (which only fires between two non-None indices) never caught the
   run-ending edge - the tracker's baseline was still the last hot in-run sample.
2. COOLING wasn't part of the existing self-tune-style full ramp skip, even though active cooling
   is a deliberate, commanded temperature change across every zone (the opposite of a runaway) and
   can keep driving a fast IR drop for several ticks, not just the one edge tick a reset covers.
"""
import time

import web_server
from modbus_client import LiveReading
from safety_limits import RampTracker, SafetyLimits


class _Bus:
    def __init__(self):
        self._t = 1000.0

    def get_temps_timestamp(self):
        self._t += 10.0  # each call is a genuinely fresh, well-separated sample
        return self._t

    def get_diagnostics(self):
        return {}

    def get_live_reading(self):
        return None

    def write_coil(self, *a, **k):
        from machine_bus import WriteResult
        return WriteResult(True, 1)

    def seconds_since_update(self):
        return 0.0


def _reading(running, cooling=False, upper=200.0, lower=200.0, ir=200.0, probe=50.0, vacuum=False, keep=False):
    return LiveReading(
        upper_temp=upper, lower_temp=lower, infrared_temp=ir, probe_temp=probe,
        running=running, running_mirror=running, vacuum=vacuum, cooling=cooling, keep=keep,
    )


def _setup(monkeypatch):
    monkeypatch.setattr(web_server, "bus", _Bus(), raising=False)
    monkeypatch.setattr(web_server, "safety_limits", SafetyLimits(max_observed_ramp_c_per_min=380.0))
    monkeypatch.setattr(web_server, "_ramp_tracker", RampTracker())
    monkeypatch.setattr(web_server, "_last_ramp_section_index", None)
    monkeypatch.setattr(web_server, "_was_extracting", False)
    monkeypatch.setattr(web_server, "_run_tracker", None)
    monkeypatch.setattr(web_server, "_current_log_path", None)
    monkeypatch.setattr(web_server, "_logging_active", False)
    monkeypatch.setattr(web_server, "_active_violation_names", set())
    monkeypatch.setattr(web_server, "_prior_reading", None)
    monkeypatch.setattr(web_server, "_run_start_time", None)
    monkeypatch.setattr(web_server.notifications, "notify_async", lambda *a, **k: None)
    web_server.fault_manager._active.clear()
    web_server.fault_manager._history.clear()


def _ir_ramp_faults():
    return [f for f in web_server.fault_manager.history if "Infrared" in f.description and "changed" in f.description]


def test_cooling_suppresses_ir_ramp_fault(monkeypatch):
    _setup(monkeypatch)
    web_server._check_safety_limits(_reading(running=True, cooling=True, ir=250.0))
    web_server._check_safety_limits(_reading(running=True, cooling=True, ir=20.0))  # huge, fast IR drop
    assert _ir_ramp_faults() == []


def test_cooling_suppresses_upper_ramp_too_not_just_probe(monkeypatch):
    """The cooling skip must be the FULL self-tune-style skip (all zones) - not the narrower
    probe-only skip used for chip extraction (vacuum/keep). Cooling affects every zone."""
    _setup(monkeypatch)
    web_server._check_safety_limits(_reading(running=True, cooling=True, upper=250.0))
    web_server._check_safety_limits(_reading(running=True, cooling=True, upper=20.0))
    upper_ramp_faults = [f for f in web_server.fault_manager.history if "Upper" in f.description and "changed" in f.description]
    assert upper_ramp_faults == []


def test_run_ending_edge_resets_ramp_baseline_even_before_cooling_is_observed(monkeypatch):
    """Defense in depth for the exact reported incident: the run ends (running True->False) and
    the very next tick already shows a huge IR drop - covered even if, for whatever reason,
    `cooling` hasn't (yet) read True on that same tick."""
    _setup(monkeypatch)
    web_server._check_safety_limits(_reading(running=True, cooling=False, ir=250.0))
    web_server._check_safety_limits(_reading(running=False, cooling=False, ir=20.0))  # run just ended
    assert _ir_ramp_faults() == []


def test_ir_ramp_still_faults_when_actually_running_and_not_cooling(monkeypatch):
    """Control - protection must still work for a genuine runaway: still running, no cooling,
    no extraction, no self-tune, no run-ending edge."""
    _setup(monkeypatch)
    web_server._check_safety_limits(_reading(running=True, cooling=False, ir=50.0))
    web_server._check_safety_limits(_reading(running=True, cooling=False, ir=300.0))
    assert len(_ir_ramp_faults()) == 1


def test_absolute_max_temp_still_enforced_during_cooldown(monkeypatch):
    """skip_full_ramp must only suppress the RATE check - an actually-too-hot zone during cooldown
    still matters and must still fault."""
    _setup(monkeypatch)
    limits = SafetyLimits(max_observed_ramp_c_per_min=380.0, max_upper_temp_c=200.0)
    monkeypatch.setattr(web_server, "safety_limits", limits)
    web_server._check_safety_limits(_reading(running=True, cooling=True, upper=250.0))
    max_temp_faults = [f for f in web_server.fault_manager.history if "exceeds the configured hard maximum" in f.description]
    assert len(max_temp_faults) == 1
