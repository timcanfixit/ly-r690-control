"""Regression coverage for a 2026-09-08 audit finding: web_server._poll_loop() previously had NO
exception handling at all. A single unhandled exception on any one tick - confirmed real via a
check-then-use race on the module-level `_run_tracker` global (the poll-loop thread checked
"if _run_tracker is not None" and then called a method on it, while /api/start and /api/stop -
Flask request-handler threads, since the dev server runs threaded=True - could reassign
_run_tracker to None or a new object in between) - would silently kill the ENTIRE background
monitoring thread forever: on a headless Pi, the Flask server stays nominally "up" but alerting,
safety-limit checks, run logging, and the SSE state clients poll all freeze permanently, with
nothing in the UI ever indicating this happened.

Fixed two ways: (1) every _run_tracker check-then-use site now captures the global into a local
variable ONCE before checking, closing the specific race; (2) _poll_loop() now wraps each tick
(_run_poll_tick) in try/except Exception, logging and continuing rather than dying - defense in
depth against this whole CLASS of bug, not just this one instance.
"""
import threading

import web_server


class _StopTestLoop(BaseException):
    """Deliberately NOT an Exception subclass - must NOT be caught by _poll_loop's
    `except Exception:`, so it can escape and end the infinite `while True:` for the test."""


def test_run_tracker_toctou_is_fixed_by_local_capture(monkeypatch):
    """Directly simulates the exact interleaving that used to crash the poll loop: another thread
    (as /api/stop's _finalize_current_run would, from a Flask request handler) reassigns
    _run_tracker to None in the MIDDLE of a single check-and-record sequence - specifically,
    between the first call (record_warning) and the second (record_event). Before the fix, both
    calls re-read the global "_run_tracker" directly, so the second call would crash with
    "'NoneType' object has no attribute 'record_event'". With the fix (capturing _run_tracker into
    a local ONCE before either call), both calls use the same already-resolved reference, so the
    reassignment happening in between has no effect on this in-flight check."""
    calls = []

    class FakeTracker:
        def record_warning(self):
            calls.append("warning")
            # Simulates another thread's /api/stop clearing the global between this call and the
            # next one on the SAME captured reference.
            web_server._run_tracker = None

        def record_event(self, description):
            calls.append(("event", description))

        def record_fault(self):
            calls.append("fault")

    monkeypatch.setattr(web_server, "_run_tracker", FakeTracker())
    monkeypatch.setattr(web_server, "_last_disk_check_time", 0.0)
    monkeypatch.setattr(web_server, "_last_disk_level", "ok")
    monkeypatch.setattr(
        web_server, "check_disk_space",
        lambda: {"level": "warning", "free_pct": 5.0, "free_bytes": 0, "total_bytes": 0},
    )

    web_server._check_disk_space()  # must not raise AttributeError on the None the fake set mid-call

    assert calls[0] == "warning"
    assert calls[1][0] == "event"
    assert web_server._run_tracker is None  # confirms the simulated reassignment really did happen


def test_poll_loop_survives_an_exception_in_a_single_tick(monkeypatch):
    """The core fix: _run_poll_tick raising must not kill _poll_loop - the exception is caught,
    logged, and the loop moves on to the next tick instead of the whole daemon thread dying."""
    tick_calls = []

    def fake_tick(start_time):
        tick_calls.append(1)
        if len(tick_calls) == 1:
            raise RuntimeError("simulated failure on the first tick - must not be fatal")
        raise _StopTestLoop()  # ends the infinite loop for this test once we've proven survival

    monkeypatch.setattr(web_server, "_run_poll_tick", fake_tick)
    monkeypatch.setattr(web_server.time, "sleep", lambda s: None)

    try:
        web_server._poll_loop()
        assert False, "expected _StopTestLoop to propagate and end the loop"
    except _StopTestLoop:
        pass

    # Two ticks ran: the first raised RuntimeError (caught internally) and the loop still went on
    # to attempt a second tick, proving the first exception did not terminate _poll_loop itself.
    assert len(tick_calls) == 2


def test_poll_loop_logs_the_exception_instead_of_swallowing_silently(monkeypatch, caplog):
    calls = []

    def fake_tick(start_time):
        calls.append(1)
        if len(calls) == 1:
            raise RuntimeError("boom")
        raise _StopTestLoop()

    monkeypatch.setattr(web_server, "_run_poll_tick", fake_tick)
    monkeypatch.setattr(web_server.time, "sleep", lambda s: None)

    import logging
    caplog.set_level(logging.ERROR, logger="web_server")
    try:
        web_server._poll_loop()
    except _StopTestLoop:
        pass

    assert any("Unhandled exception in poll loop tick" in r.message for r in caplog.records)
