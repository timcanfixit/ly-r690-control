import port_discovery as pd


def _port(device, vid="0403", pid="6001", serial_number=""):
    return pd.PortInfo(device=device, description="", manufacturer="", vid=vid, pid=pid, serial_number=serial_number)


def test_finds_port_by_serial_number(monkeypatch):
    ports = [_port("COM3", serial_number="AB123"), _port("COM4", serial_number="CD456")]
    monkeypatch.setattr(pd, "list_available_ports", lambda: ports)
    assert pd.find_port_by_identity(vid="0403", pid="6001", serial_number="CD456") == "COM4"


def test_returns_none_when_serial_number_not_found(monkeypatch):
    ports = [_port("COM3", serial_number="AB123")]
    monkeypatch.setattr(pd, "list_available_ports", lambda: ports)
    assert pd.find_port_by_identity(vid="0403", pid="6001", serial_number="ZZZZZ") is None


def test_falls_back_to_vid_pid_when_no_serial_number_recorded(monkeypatch):
    ports = [_port("COM5", vid="0403", pid="6001", serial_number="")]
    monkeypatch.setattr(pd, "list_available_ports", lambda: ports)
    assert pd.find_port_by_identity(vid="0403", pid="6001", serial_number="") == "COM5"


def test_vid_pid_fallback_does_not_match_different_adapter(monkeypatch):
    ports = [_port("COM5", vid="0403", pid="6015", serial_number="")]
    monkeypatch.setattr(pd, "list_available_ports", lambda: ports)
    assert pd.find_port_by_identity(vid="0403", pid="6001", serial_number="") is None


def test_serial_number_present_never_falls_back_to_vid_pid_on_miss(monkeypatch):
    """A recorded serial_number that no longer matches anything plugged in must not silently
    fall back to a looser VID/PID match - that could pick a DIFFERENT physical adapter."""
    ports = [_port("COM5", vid="0403", pid="6001", serial_number="OTHER_ADAPTER")]
    monkeypatch.setattr(pd, "list_available_ports", lambda: ports)
    assert pd.find_port_by_identity(vid="0403", pid="6001", serial_number="AB123") is None


def test_returns_none_when_nothing_plugged_in(monkeypatch):
    monkeypatch.setattr(pd, "list_available_ports", lambda: [])
    assert pd.find_port_by_identity(vid="0403", pid="6001", serial_number="AB123") is None


def test_returns_none_when_no_identity_info_at_all(monkeypatch):
    ports = [_port("COM3")]
    monkeypatch.setattr(pd, "list_available_ports", lambda: ports)
    assert pd.find_port_by_identity(vid="", pid="", serial_number="") is None
