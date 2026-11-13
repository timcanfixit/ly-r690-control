from fault_manager import FaultManager, Fault, FaultType, FaultSeverity, _MAX_HISTORY


def _fault(desc="x", severity=FaultSeverity.WARNING, timestamp=None):
    kwargs = dict(fault_type=FaultType.SENSOR_INVALID, severity=severity, description=desc)
    if timestamp is not None:
        kwargs["timestamp"] = timestamp
    return Fault(**kwargs)


def test_raise_and_acknowledge():
    fm = FaultManager()
    f = fm.raise_fault(_fault())
    assert f in fm.active_faults
    fm.acknowledge(f)
    assert f not in fm.active_faults
    assert f.acknowledged is True


def test_acknowledge_all():
    fm = FaultManager()
    fm.raise_fault(_fault("a"))
    fm.raise_fault(_fault("b"))
    fm.acknowledge_all()
    assert fm.active_faults == []


def test_has_unacknowledged_critical():
    fm = FaultManager()
    assert fm.has_unacknowledged_critical() is False
    fm.raise_fault(_fault("crit", severity=FaultSeverity.CRITICAL))
    assert fm.has_unacknowledged_critical() is True


def test_history_bounded_to_prevent_unbounded_memory_growth():
    fm = FaultManager()
    for i in range(_MAX_HISTORY + 100):
        fm.raise_fault(_fault(f"fault {i}"))
        fm.acknowledge_all()
    assert len(fm.history) == _MAX_HISTORY


def test_acknowledge_uses_identity_not_value_equality():
    """Two structurally-identical Fault objects (same type/severity/description/timestamp) must
    remain independently trackable - acknowledging one must not silently also acknowledge the
    other. Regression test for the Fault dataclass previously using value equality."""
    f1 = _fault("same text", timestamp=1.0)
    f2 = _fault("same text", timestamp=1.0)
    fm = FaultManager()
    fm.raise_fault(f1)
    fm.raise_fault(f2)
    fm.acknowledge(f1)
    assert f1.acknowledged is True
    assert f2.acknowledged is False
    assert fm.active_faults == [f2]
