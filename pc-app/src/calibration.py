"""Calibration record-keeping - stores (reference instrument reading, machine reading) pairs per
zone and computes the offset between them. Deliberately does NOT auto-apply anything to the
temperature compensation registers (176-179) - those are still an unconfirmed hypothesis (see
modbus_client.ADDR_TEMP_COMP_* and docs/06-modbus-register-map.md's "not independently confirmed"
caveat, repeated in the existing Temperature Compensation UI's own tooltip/confirmation dialog).
Writing a calibration offset into those registers stays a separate, explicit, already-labeled
experimental action the operator takes deliberately through the existing UI - this module only
computes and records what the offset WOULD be, for the operator to act on if they choose.
"""
from __future__ import annotations

import json
import sys
import threading
import time
from dataclasses import dataclass, field, asdict
from pathlib import Path

if getattr(sys, "frozen", False):
    CALIBRATION_PATH = Path(sys.executable).resolve().parent / "calibration_records.json"
else:
    CALIBRATION_PATH = Path(__file__).resolve().parent.parent / "calibration_records.json"

# See pid_history.py's _lock for why - same read-modify-write-a-JSON-file pattern, same race.
_lock = threading.Lock()


@dataclass
class CalibrationPoint:
    zone: str
    reference_c: float
    machine_c: float

    @property
    def offset_c(self) -> float:
        """Reference minus machine - a POSITIVE offset means the machine reads LOW (add this
        much to correct it), matching the sign convention already used by
        modbus_client.ADDR_TEMP_COMP_* (an offset added to the machine's own reading)."""
        return self.reference_c - self.machine_c


@dataclass
class CalibrationRecord:
    timestamp: float = field(default_factory=time.time)
    operator: str = ""
    reference_instrument: str = ""
    points: list[CalibrationPoint] = field(default_factory=list)
    note: str = ""


def _load() -> list[dict]:
    if not CALIBRATION_PATH.exists():
        return []
    try:
        return json.loads(CALIBRATION_PATH.read_text())
    except (json.JSONDecodeError, OSError):
        return []


def add_calibration_record(record: CalibrationRecord) -> None:
    with _lock:
        records = _load()
        records.append({
            "timestamp": record.timestamp,
            "operator": record.operator,
            "reference_instrument": record.reference_instrument,
            "points": [asdict(p) | {"offset_c": p.offset_c} for p in record.points],
            "note": record.note,
        })
        CALIBRATION_PATH.write_text(json.dumps(records, indent=2))


def get_calibration_history() -> list[dict]:
    return _load()
