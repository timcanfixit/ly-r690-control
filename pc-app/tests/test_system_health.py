import system_health as sh


def test_unavailable_on_non_linux(monkeypatch):
    monkeypatch.setattr(sh.platform, "system", lambda: "Windows")
    result = sh.get_system_health()
    assert result["available"] is False
    assert "Windows" in result["error"]


def test_available_and_ok_on_healthy_linux_reads(monkeypatch):
    monkeypatch.setattr(sh.platform, "system", lambda: "Linux")
    monkeypatch.setattr(sh, "_read_proc_loadavg", lambda: (0.1, 0.2, 0.3))
    monkeypatch.setattr(sh, "_read_proc_meminfo", lambda: {"MemTotal": 1000000, "MemAvailable": 800000})
    monkeypatch.setattr(sh, "_read_proc_uptime", lambda: 12345.0)
    monkeypatch.setattr(sh.os, "cpu_count", lambda: 4)
    result = sh.get_system_health()
    assert result["available"] is True
    assert result["level"] == "ok"
    assert result["load_avg"] == (0.1, 0.2, 0.3)
    assert result["cpu_count"] == 4
    assert abs(result["mem_used_pct"] - 20.0) < 1e-6
    assert result["uptime_s"] == 12345.0


def test_unavailable_when_proc_reads_fail(monkeypatch):
    monkeypatch.setattr(sh.platform, "system", lambda: "Linux")
    monkeypatch.setattr(sh, "_read_proc_loadavg", lambda: None)
    monkeypatch.setattr(sh, "_read_proc_meminfo", lambda: {"MemTotal": 1, "MemAvailable": 1})
    monkeypatch.setattr(sh, "_read_proc_uptime", lambda: 1.0)
    result = sh.get_system_health()
    assert result["available"] is False


def test_warning_level_on_high_memory_usage(monkeypatch):
    monkeypatch.setattr(sh.platform, "system", lambda: "Linux")
    monkeypatch.setattr(sh, "_read_proc_loadavg", lambda: (0.1, 0.1, 0.1))
    monkeypatch.setattr(sh, "_read_proc_meminfo", lambda: {"MemTotal": 1000000, "MemAvailable": 100000})  # 90% used
    monkeypatch.setattr(sh, "_read_proc_uptime", lambda: 1.0)
    monkeypatch.setattr(sh.os, "cpu_count", lambda: 4)
    result = sh.get_system_health()
    assert result["level"] == "warning"


def test_critical_level_on_very_high_memory_usage(monkeypatch):
    monkeypatch.setattr(sh.platform, "system", lambda: "Linux")
    monkeypatch.setattr(sh, "_read_proc_loadavg", lambda: (0.1, 0.1, 0.1))
    monkeypatch.setattr(sh, "_read_proc_meminfo", lambda: {"MemTotal": 1000000, "MemAvailable": 30000})  # 97% used
    monkeypatch.setattr(sh, "_read_proc_uptime", lambda: 1.0)
    monkeypatch.setattr(sh.os, "cpu_count", lambda: 4)
    result = sh.get_system_health()
    assert result["level"] == "critical"


def test_warning_level_on_high_cpu_load(monkeypatch):
    monkeypatch.setattr(sh.platform, "system", lambda: "Linux")
    monkeypatch.setattr(sh, "_read_proc_loadavg", lambda: (8.0, 1.0, 1.0))  # ratio 2.0 with 4 cores
    monkeypatch.setattr(sh, "_read_proc_meminfo", lambda: {"MemTotal": 1000000, "MemAvailable": 800000})
    monkeypatch.setattr(sh, "_read_proc_uptime", lambda: 1.0)
    monkeypatch.setattr(sh.os, "cpu_count", lambda: 4)
    result = sh.get_system_health()
    assert result["level"] == "warning"


def test_critical_level_on_very_high_cpu_load(monkeypatch):
    monkeypatch.setattr(sh.platform, "system", lambda: "Linux")
    monkeypatch.setattr(sh, "_read_proc_loadavg", lambda: (16.0, 1.0, 1.0))  # ratio 4.0 with 4 cores
    monkeypatch.setattr(sh, "_read_proc_meminfo", lambda: {"MemTotal": 1000000, "MemAvailable": 800000})
    monkeypatch.setattr(sh, "_read_proc_uptime", lambda: 1.0)
    monkeypatch.setattr(sh.os, "cpu_count", lambda: 4)
    result = sh.get_system_health()
    assert result["level"] == "critical"


def test_per_metric_levels_are_independent_2026_09_15(monkeypatch):
    """The System Health tab colors each card by its OWN state, not by the overall (worse-of-two)
    level - high memory alone must not also mark the load card as warning/critical, and vice
    versa."""
    monkeypatch.setattr(sh.platform, "system", lambda: "Linux")
    monkeypatch.setattr(sh, "_read_proc_loadavg", lambda: (0.1, 0.1, 0.1))  # fine
    monkeypatch.setattr(sh, "_read_proc_meminfo", lambda: {"MemTotal": 1000000, "MemAvailable": 30000})  # 97%, critical
    monkeypatch.setattr(sh, "_read_proc_uptime", lambda: 1.0)
    monkeypatch.setattr(sh.os, "cpu_count", lambda: 4)
    result = sh.get_system_health()
    assert result["level"] == "critical"  # overall still reflects the worse of the two
    assert result["load_level"] == "ok"
    assert result["mem_level"] == "critical"


def test_meminfo_missing_keys_treated_as_unavailable(monkeypatch):
    monkeypatch.setattr(sh.platform, "system", lambda: "Linux")
    monkeypatch.setattr(sh, "_read_proc_loadavg", lambda: (0.1, 0.1, 0.1))
    monkeypatch.setattr(sh, "_read_proc_meminfo", lambda: None)
    monkeypatch.setattr(sh, "_read_proc_uptime", lambda: 1.0)
    result = sh.get_system_health()
    assert result["available"] is False
