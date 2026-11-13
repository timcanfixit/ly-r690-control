import connection_identity as ci
from port_discovery import PortInfo


def _port_info(device="COM4", vid="0403", pid="6001", serial_number="AB123", description="FTDI USB Serial"):
    return PortInfo(device=device, description=description, manufacturer="FTDI", vid=vid, pid=pid, serial_number=serial_number)


def test_save_and_load_round_trip(tmp_path, monkeypatch):
    monkeypatch.setattr(ci, "CONNECTION_IDENTITY_PATH", tmp_path / "connection_identity.json")
    identity = ci.ConnectionIdentity(vid="0403", pid="6001", serial_number="AB123", description="FTDI", last_known_port="COM4")
    ci.save_connection_identity(identity)
    loaded = ci.load_connection_identity()
    assert loaded == identity


def test_load_returns_none_when_no_file_exists(tmp_path, monkeypatch):
    monkeypatch.setattr(ci, "CONNECTION_IDENTITY_PATH", tmp_path / "missing.json")
    assert ci.load_connection_identity() is None


def test_load_returns_none_on_corrupt_json(tmp_path, monkeypatch):
    path = tmp_path / "connection_identity.json"
    path.write_text("not json")
    monkeypatch.setattr(ci, "CONNECTION_IDENTITY_PATH", path)
    assert ci.load_connection_identity() is None


def test_remember_connection_saves_a_real_identity(tmp_path, monkeypatch):
    monkeypatch.setattr(ci, "CONNECTION_IDENTITY_PATH", tmp_path / "connection_identity.json")
    ci.remember_connection(_port_info())
    loaded = ci.load_connection_identity()
    assert loaded.vid == "0403"
    assert loaded.serial_number == "AB123"
    assert loaded.last_known_port == "COM4"


def test_remember_connection_skips_ports_with_no_hardware_identity(tmp_path, monkeypatch):
    """A synthetic/virtual port with no VID and no serial number must never overwrite a
    previously-recorded good identity."""
    monkeypatch.setattr(ci, "CONNECTION_IDENTITY_PATH", tmp_path / "connection_identity.json")
    ci.remember_connection(_port_info())
    ci.remember_connection(_port_info(device="COM9", vid="", pid="", serial_number=""))
    loaded = ci.load_connection_identity()
    assert loaded.last_known_port == "COM4"  # unchanged - the blank one was never saved


def test_resolve_remembered_port_returns_none_when_nothing_remembered(tmp_path, monkeypatch):
    monkeypatch.setattr(ci, "CONNECTION_IDENTITY_PATH", tmp_path / "missing.json")
    assert ci.resolve_remembered_port() is None


def test_resolve_remembered_port_uses_find_port_by_identity(tmp_path, monkeypatch):
    monkeypatch.setattr(ci, "CONNECTION_IDENTITY_PATH", tmp_path / "connection_identity.json")
    ci.remember_connection(_port_info(device="COM4"))
    import port_discovery
    monkeypatch.setattr(port_discovery, "find_port_by_identity", lambda vid, pid, serial_number: "COM7")
    assert ci.resolve_remembered_port() == "COM7"
