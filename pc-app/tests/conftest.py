"""Makes pc-app/src importable as plain top-level modules (machine_state, safety_limits, etc.)
without needing an installed package - matches how main_window.py/web_server.py already import
their shared modules."""
import sys
from pathlib import Path

SRC_DIR = Path(__file__).resolve().parent.parent / "src"
if str(SRC_DIR) not in sys.path:
    sys.path.insert(0, str(SRC_DIR))


import pytest


@pytest.fixture(autouse=True)
def _reset_web_server_command_gate():
    """web_server keeps module-level command-gate / protective-stop flags (2026-11-12 review fixes); a test
    that sets one must not leak it into the next."""
    yield
    ws = sys.modules.get("web_server")
    if ws is not None:
        ws._restart_pending = False
        ws._inflight_ops = 0
        ws._protective_stop_pending = False
        ws._protective_fault = None
        ws._step_failures.clear()
