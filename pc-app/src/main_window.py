"""Main application window for the LY-R690 monitor/control app."""
from __future__ import annotations

import csv
import json
import logging
import os
import sys
import threading
import time
from dataclasses import asdict

import pyqtgraph as pg
from PySide6.QtCore import QTimer, Qt, Signal
from PySide6.QtGui import QColor
from PySide6.QtWidgets import (
    QApplication, QMainWindow, QWidget, QVBoxLayout, QHBoxLayout, QGridLayout,
    QPushButton, QLabel, QTableWidget, QTableWidgetItem, QComboBox, QLineEdit,
    QGroupBox, QMessageBox, QInputDialog, QListWidget, QListWidgetItem, QSplitter, QDoubleSpinBox, QSpinBox,
    QCheckBox, QTabWidget, QFileDialog, QDialog, QDialogButtonBox, QTextEdit,
    QAbstractItemView, QScrollArea,
)

from machine_bus import MachineBus
from modbus_client import (
    Profile, CurveSection, CURVE_SECTIONS, ADDR_CURVE_BASE,
    COIL_RUNNING, COIL_KEEP, COIL_VACUUM, COIL_COOLING,
    COIL_SELFTUNE_UPPER, COIL_SELFTUNE_LOWER, COIL_SELFTUNE_IR,
    ADDR_PID_BASE, ADDR_PID_UPPER_P, ADDR_PID_UPPER_I, ADDR_PID_UPPER_D,
    ADDR_PID_LOWER_P, ADDR_PID_LOWER_I, ADDR_PID_LOWER_D,
    ADDR_PID_IR_P, ADDR_PID_IR_I, ADDR_PID_IR_D, PID_SEGMENTS,
    ADDR_VACUUM_TIME, ADDR_FAN_STOP_TEMP, ADDR_FAN_STOP_DELAY, ADDR_ADVANCE_WARNING_TIME,
    ADDR_TEMP_COMP_UPPER, TEMP_COMP_COUNT, to_signed16, to_unsigned16,
)
from alerts import RunMonitor, AlertConfig, Severity
from data_logger import RunLogger, LOGS_DIR, list_runs, check_disk_space, mark_run_golden, mark_run_notes, mark_repair_outcome
from machine_state import MachineState, derive_state, is_control_enabled
from safety_limits import SafetyLimits, RampTracker, check_reading, check_run_duration, check_communication_outage, load_safety_limits, save_safety_limits, parse_safety_limit_updates
from fault_manager import FaultManager, Fault, FaultType, FaultSeverity
from preflight import run_preflight, curve_send_status, profile_curve_regs
from heater_monitor import HeaterMonitor
from profile_validator import validate_profile
from profile_qualification import compute_qualification_status, QUALIFICATION_THRESHOLD_RUNS
from machine_config_snapshot import save_known_good_config, load_known_good_config, compute_drift
from system_health import get_system_health
import automatic_backup
from system_diagnostics import run_diagnostics, overall_status
import config_change_history
from register_viewer import validate_register_read_request, format_register_rows
from profile_simulation import simulate_profile
from port_discovery import list_available_ports, test_connection
from connection_identity import remember_connection, resolve_remembered_port, load_connection_identity
import pid_history
import config_backup
from run_analysis import recommend_for_profile, compute_repair_outcome_stats
import calibration
import notifications
import run_summary_ai
from process_intelligence import compute_zone_deviations
from run_finalization import RunFinalizationTracker, finalize_run, load_summary
from process_limits import parse_process_limits
from self_tune_log import SelfTuneTracker
import maintenance
import process_assistant
import run_recovery
import profile_search
import thermal_model
import profile_store
import profile_import

ROW_LABELS = [
    "Upper Temper", "Upper Rate", "Holding time",
    "Lower Temper", "Lower Rate", "Holding time",
    "IR Temper", "IR Rate", "Holding time",
]
ROW_ATTRS = [
    "upper_temp", "upper_rate", "upper_hold_time",
    "lower_temp", "lower_rate", "lower_hold_time",
    "ir_temp", "ir_rate", "ir_hold_time",
]
PID_ROW_LABELS = ["Upper P", "Upper I", "Upper D", "Lower P", "Lower I", "Lower D"]
PID_ROW_ADDRS = [
    ADDR_PID_UPPER_P, ADDR_PID_UPPER_I, ADDR_PID_UPPER_D,
    ADDR_PID_LOWER_P, ADDR_PID_LOWER_I, ADDR_PID_LOWER_D,
]

POLL_INTERVAL_MS = 500

# Confirmed live 2026-09-09 (user report) - shared by fan/vacuum timing and temperature
# compensation reads since they're the same 10-register active read (see
# MachineBus.read_fan_timing_active's docstring for the full finding and the open question of
# whether a live capture could find a way to trigger this remotely).
FAN_TIMING_TEMP_COMP_NO_RESPONSE_MESSAGE = (
    "No response from the board after repeated attempts. This register range has been observed "
    "to not respond until the touchscreen's Advanced Parameters screen has been opened at least "
    "once since the machine's controller was last powered on - it does not need to stay on that "
    "screen afterward. If you haven't opened that screen yet this power cycle, do that on the "
    "touchscreen once, then try again."
)


class MainWindow(QMainWindow):
    # (callback, result) - lets a background write thread hand its result back to the GUI
    # thread safely; Qt auto-queues signal emits from a non-GUI thread onto the receiver's
    # thread, which a raw callback invoked from that thread would not do.
    _write_done = Signal(object, object)
    # (callback, chunk_index, total_chunks, ok) - see write_registers()'s on_chunk_done. Lets a
    # chunked send that's taking a long time under bus contention (confirmed live 2026-08-19: 2-3
    # minutes for a badly-contended curve send) show real progress instead of a static "Sending..."
    # with zero feedback the whole time.
    _write_progress = Signal(object, int, int, bool)

    def __init__(self, com_port: str = "COM4"):
        super().__init__()
        self.setWindowTitle("LY-R690 Monitor & Control")
        # 1300x950 was sized for the app's original, much smaller feature set - by the time Stage
        # 1-24 work finished, the Alerts panel alone (Alert Thresholds + Safety Limits + Webhook
        # Notifications + the alerts list) needed noticeably more room than that left it, and was
        # visibly cramped (confirmed via a live screenshot 2026-08-22). Bumped up, and see
        # _scrollable() for the other half of the fix - content that still doesn't fit scrolls
        # instead of being force-squeezed below its own sizeHint.
        self.resize(1550, 1020)

        self.bus = MachineBus(com_port)
        self.monitor = RunMonitor(AlertConfig())
        self.logger = RunLogger()
        self._logging_active = False
        self._start_time = time.monotonic()
        self._plot_data = {"t": [], "upper": [], "lower": [], "ir": [], "probe": []}
        self._last_curve_count = 0
        self._section_marker_lines: list = []
        self._last_shown_section: int | None = None
        # Buttons with a write currently in flight - see _mark_busy/_mark_idle and
        # _apply_control_gating for why this exists: control gating needs to be able to
        # RE-enable a button once the machine state allows it again (e.g. Start after Cooling
        # turns back off), but must never fight an in-flight write's own disable/re-enable of
        # that same button (confirmed live 2026-08-23 - Start toggled by Cooling's gating stayed
        # disabled forever afterward, since nothing else was allowed to re-enable it).
        self._busy_buttons: set = set()

        # ---- safety foundation (state machine / preflight / hard limits / fault manager) ----
        self.safety_limits = load_safety_limits()
        # A fresh MachineBus always starts at ir_reading_correction_c=0.0 - push the configured
        # value in now, and again after every reconnect (see _connect()), since bus.get_live_reading()
        # is the one place this correction actually gets applied.
        self.bus.set_ir_reading_correction(self.safety_limits.ir_reading_correction_c)
        self.fault_manager = FaultManager()
        self.heater_monitor = HeaterMonitor()
        self._current_state = MachineState.DISCONNECTED
        self._preflight_active = False
        self._prior_reading = None
        self._prior_reading_time = None
        self._run_start_time = None
        self._active_violation_names: set[str] = set()
        self._was_extracting = False  # VACUUM/KEEP engaged during a run last tick - see _check_safety_limits
        # Separate from _prior_reading/_prior_reading_time (which track every poll tick, used
        # for run-start-time bookkeeping) - this tracks only genuinely fresh temperature
        # samples, for the ramp-rate check specifically. See safety_limits.RampTracker's
        # docstring for why conflating "time since last poll tick" with "time since temps last
        # actually changed" produced a real false-positive auto-stop (reported live: 1641C/min
        # for a normal ~14C change).
        self._ramp_tracker = RampTracker()
        # Tracks estimate_current_section()'s last value so _check_safety_limits can detect a
        # transition and reset _ramp_tracker right at that boundary - see RampTracker's docstring
        # ("Third, related fix") for why.
        self._last_ramp_section_index: int | None = None
        # _check_run_ended_externally's own state (2026-09-14, real gap reported live: neither a
        # profile completing on its own nor the touchscreen's own physical Stop button were ever
        # detected/notified) - deliberately separate from _last_ramp_section_index above, which
        # gets reset to None at every section transition and can't be used to remember "how far did
        # we get" at the moment running drops. See web_server.py's identical fields.
        self._run_end_watch_was_running = False
        self._last_active_section_index: int | None = None
        self._last_active_section_total: int | None = None
        # Snapshot of self._run_start_time taken every tick while still running=True - see
        # web_server.py's _last_active_run_start_time comment for the real bug this avoids:
        # _check_safety_limits (called earlier in the same poll tick) already clears
        # self._run_start_time to None the instant reading.running goes False, so
        # _check_run_ended_externally reading it directly on the drop tick would always compute
        # duration_s=0. Reset to None once consumed.
        self._last_active_run_start_time: float | None = None
        # Disk-space guard (2026-09-07, external feature-gap review) - see data_logger.
        # check_disk_space()'s own docstring. Throttled (not checked every poll tick, a syscall
        # for a slow-changing metric) and only raises a fault ON A LEVEL CHANGE (see
        # _check_disk_space below) - fault_manager.raise_fault() has no built-in dedup, so calling
        # it every tick while space stays low would flood the fault list with duplicates.
        self._last_disk_level: str | None = None
        self._last_disk_check_time: float = 0.0
        # Configuration drift detection (2026-09-08, external feature-gap review, second pass) -
        # see machine_config_snapshot.py. Same throttle/dedup pattern as the disk-space guard
        # above, but using PASSIVE observation rather than an active read every tick.
        self._last_config_drift_state: bool | None = None
        self._last_config_drift_check_time: float = 0.0
        # Interrupted-run live-state confirmation (2026-09-08, external feature-gap review,
        # second pass) - set by _check_for_interrupted_run(), consumed once by
        # _confirm_interrupted_run_live_state() the first time a live reading arrives.
        self._interrupted_run_pending_confirmation = None
        # System health guard (2026-09-08, external feature-gap review, second pass) - see
        # system_health.py. Same throttle/dedup pattern as the disk-space guard above.
        self._last_system_health_level: str | None = None
        self._last_system_health_check_time: float = 0.0
        # Automatic backups (2026-09-08, external feature-gap review, second pass) - see
        # automatic_backup.py's module docstring. The check itself (is_backup_due) is a directory
        # listing, so this throttle exists purely to avoid doing that every single poll tick, not
        # to dedup faults (there's no fault here at all - a routine daily backup isn't a problem).
        self._last_auto_backup_check_time: float = 0.0
        # Self-tune session logging (2026-09-07, external feature-gap review, the maintenance-
        # tracking item) - see self_tune_log.py's module docstring for why a real self-tune
        # session previously left no trace anywhere once it ended. One tracker for the app's
        # whole lifetime, not per-run - self-tune isn't tied to a profile run at all.
        self._self_tune_tracker = SelfTuneTracker()
        self.webhook_config = notifications.load_webhook_config()
        self.ollama_config = profile_search.load_ollama_config()
        self._current_log_path = None
        self._run_tracker: RunFinalizationTracker | None = None
        self._run_process_limits = None
        # The exact 72 curve registers of the last Send-to-machine that succeeded THIS session -
        # used only to remind the operator, before Start, if the editor's curve was never sent
        # from here (see preflight.curve_send_status - this is provenance, not a claim about what's
        # actually on the board, which this machine can't reliably read back).
        self._curve_last_sent_regs: list[int] | None = None

        self._write_done.connect(lambda callback, result: callback(result))
        self._write_progress.connect(lambda callback, idx, total, ok: callback(idx, total, ok))

        self._build_ui()
        self._connect(com_port, show_error=True)
        self._check_for_interrupted_run()

        self.timer = QTimer(self)
        self.timer.timeout.connect(self._poll)
        self.timer.start(POLL_INTERVAL_MS)

    # ---------------- UI construction ----------------

    def _build_ui(self) -> None:
        central = QWidget()
        self.setCentralWidget(central)
        root = QVBoxLayout(central)

        root.addLayout(self._build_connection_bar())
        root.addWidget(self._build_status_bar())
        root.addWidget(self._build_process_dashboard())

        splitter = QSplitter(Qt.Vertical)
        splitter.addWidget(self._build_graph())
        splitter.addWidget(self._build_bottom_panel())
        splitter.setSizes([360, 660])
        root.addWidget(splitter)

    def _build_connection_bar(self) -> QHBoxLayout:
        """COM port was previously only settable via a launch argument (main(): `sys.argv[1]`),
        with no way to change it - or retry after a failed connection - without closing and
        relaunching the whole app. Not workable for a packaged --windowed exe (no console to pass
        an argument through even if the user knew to). Added 2026-08-19."""
        layout = QHBoxLayout()
        layout.addWidget(QLabel("COM port:"))
        # Editable combo, not a plain dropdown - port_discovery.list_available_ports() is a
        # convenience over guessing blindly, not a guarantee the right port always shows up (a
        # port already held open elsewhere, or an adapter using a driver that doesn't enumerate
        # descriptively, still needs to be typeable by hand).
        self.com_port_combo = QComboBox()
        self.com_port_combo.setEditable(True)
        self.com_port_combo.setFixedWidth(90)
        self.com_port_combo.setEditText(self.bus.com_port)
        layout.addWidget(self.com_port_combo)
        self.com_port_edit = self.com_port_combo.lineEdit()
        btn_refresh_ports = QPushButton("Refresh ports")
        btn_refresh_ports.clicked.connect(self._on_refresh_ports)
        layout.addWidget(btn_refresh_ports)
        btn_test_port = QPushButton("Test")
        btn_test_port.setToolTip("Opens the selected port and asks the LY-R690 for a real temperature reading, without disturbing the app's current connection.")
        btn_test_port.clicked.connect(self._on_test_port)
        layout.addWidget(btn_test_port)
        self.btn_reconnect = QPushButton("Reconnect")
        self.btn_reconnect.clicked.connect(self._on_reconnect_clicked)
        layout.addWidget(self.btn_reconnect)
        self.lbl_connection = QLabel("Not connected")
        self.lbl_connection.setStyleSheet("color: #d55; font-weight: bold;")
        layout.addWidget(self.lbl_connection)
        self._on_refresh_ports()

        layout.addSpacing(24)
        layout.addWidget(QLabel("Machine state:"))
        self.lbl_machine_state = QLabel(MachineState.DISCONNECTED.value.upper())
        self.lbl_machine_state.setStyleSheet("font-weight: bold; padding: 2px 8px; border-radius: 4px; background: #444; color: #aaa;")
        layout.addWidget(self.lbl_machine_state)
        self.btn_ack_fault = QPushButton("ACKNOWLEDGE FAULT")
        self.btn_ack_fault.setStyleSheet("background: #a33; color: white; font-weight: bold;")
        self.btn_ack_fault.clicked.connect(self._on_acknowledge_fault)
        self.btn_ack_fault.setVisible(False)
        layout.addWidget(self.btn_ack_fault)

        layout.addStretch()
        return layout

    def _build_status_bar(self) -> QWidget:
        box = QGroupBox("Status")
        layout = QHBoxLayout(box)

        self.lbl_upper = QLabel("Upper: --")
        self.lbl_lower = QLabel("Lower: --")
        self.lbl_ir = QLabel("IR: --")
        self.lbl_probe = QLabel("Probe: --")
        self.lbl_running = QLabel("RUNNING")
        self.lbl_vacuum = QLabel("VACUUM")
        self.lbl_cooling = QLabel("COOLING")
        self.lbl_keep = QLabel("KEEP")
        for lbl in (self.lbl_running, self.lbl_vacuum, self.lbl_cooling, self.lbl_keep):
            lbl.setStyleSheet("background: #444; color: #aaa; padding: 4px 10px; border-radius: 4px;")
        self.lbl_keep.setToolTip(
            "Inferred from source strings (coil address 173 / tag M174), not yet independently "
            "confirmed with a live test - see docs/06-modbus-register-map.md."
        )

        for w in (self.lbl_upper, self.lbl_lower, self.lbl_ir, self.lbl_probe):
            layout.addWidget(w)
        layout.addStretch()
        layout.addWidget(self.lbl_running)
        layout.addWidget(self.lbl_vacuum)
        layout.addWidget(self.lbl_cooling)
        layout.addWidget(self.lbl_keep)

        self.btn_start = QPushButton("START")
        self.btn_start.setStyleSheet("background: #2a6; color: white; font-weight: bold; padding: 6px 16px;")
        self.btn_start.clicked.connect(self._on_start_clicked)
        self.btn_stop = QPushButton("STOP")
        self.btn_stop.setStyleSheet("background: #c33; color: white; font-weight: bold; padding: 6px 16px;")
        self.btn_stop.clicked.connect(self._on_stop_clicked)
        self.btn_keep = QPushButton("KEEP")
        self.btn_keep.setStyleSheet("background: #b8860b; color: white; font-weight: bold; padding: 6px 16px;")
        self.btn_keep.setToolTip("Hold the current profile step at its current temperatures. Only meaningful while running.")
        self.btn_keep.clicked.connect(self._on_keep_clicked)
        self.btn_vacuum = QPushButton("VACUUM")
        self.btn_vacuum.setStyleSheet("background: #38c; color: white; font-weight: bold; padding: 6px 16px;")
        self.btn_vacuum.clicked.connect(self._on_vacuum_clicked)
        self.btn_cooling = QPushButton("COOLING")
        self.btn_cooling.setStyleSheet("background: #38c; color: white; font-weight: bold; padding: 6px 16px;")
        self.btn_cooling.clicked.connect(self._on_cooling_clicked)
        layout.addWidget(self.btn_start)
        layout.addWidget(self.btn_stop)
        layout.addWidget(self.btn_keep)
        layout.addWidget(self.btn_vacuum)
        layout.addWidget(self.btn_cooling)

        return box

    def _build_process_dashboard(self) -> QWidget:
        box = QGroupBox("Process Status")
        box.setToolTip(
            "A live composite of already-known state - per-zone deviation from the nearest "
            "target in the loaded profile (not a claim about which section 'should' be running "
            "now - see alerts.py's module docstring for why), elapsed run time, and active fault/"
            "warning counts. Nothing here is a new machine reading."
        )
        layout = QHBoxLayout(box)
        self.lbl_process_dashboard = QLabel("Not running.")
        self.lbl_process_dashboard.setWordWrap(True)
        layout.addWidget(self.lbl_process_dashboard)
        return box

    def _update_process_dashboard(self, reading) -> None:
        if reading.running and self._run_start_time is not None:
            elapsed = time.monotonic() - self._run_start_time
            status = f"Running - elapsed {int(elapsed // 60)}m {int(elapsed % 60)}s"
        else:
            status = "Not running"

        dev_parts = []
        for d in compute_zone_deviations(reading, self.monitor.active_profile):
            if d.deviation_c is None:
                dev_parts.append(f"{d.zone} {d.actual_c:.1f}C (no target in profile)")
            else:
                dev_parts.append(f"{d.zone} {d.actual_c:.1f}C ({d.deviation_c:.1f}C from nearest target {d.nearest_target_c:.0f}C)")

        active = self.fault_manager.active_faults
        if active:
            fault_summary = f"{len(active)} active: " + "; ".join(f.description for f in active[:3])
        else:
            fault_summary = "No active faults"

        self.lbl_process_dashboard.setText(f"{status}  |  {'  '.join(dev_parts)}  |  {fault_summary}")

    def _build_graph(self) -> QWidget:
        pg.setConfigOption("background", "#1e1e1e")
        pg.setConfigOption("foreground", "#ccc")
        self.plot = pg.PlotWidget(title="Live Temperatures")
        self.plot.setLabel("left", "Temperature", units="C")
        self.plot.setLabel("bottom", "Time", units="s")
        self.plot.addLegend()
        self.plot.showGrid(x=True, y=True, alpha=0.3)

        self.curve_upper = self.plot.plot(pen=pg.mkPen("#e74c3c", width=2), name="Upper")
        self.curve_lower = self.plot.plot(pen=pg.mkPen("#3498db", width=2), name="Lower")
        self.curve_ir = self.plot.plot(pen=pg.mkPen("#f39c12", width=2), name="Infrared")
        self.curve_probe = self.plot.plot(pen=pg.mkPen("#2ecc71", width=2), name="Probe")

        self.wetting_line = pg.InfiniteLine(
            pos=217.0, angle=0, pen=pg.mkPen("#e91e63", width=1, style=Qt.DashLine),
            label="Wetting ({value:.0f}C)", labelOpts={"position": 0.05},
        )
        self.plot.addItem(self.wetting_line)

        # Target envelope (2026-09-07, external feature-gap review): a shaded band around the
        # CURRENTLY ESTIMATED section's own target for each zone, +/- TARGET_ENVELOPE_TOLERANCE_C
        # - lets you see "in/near/out of window" at a glance instead of eyeballing a bare number
        # against a moving line. Tied to estimate_current_section() (see _update_section_indicator,
        # the same estimate the section-boundary markers already use) - an ESTIMATE, not a claim
        # of ground truth, same caveat as everywhere else this estimate is used. Hidden (zero-size,
        # not shown) until a profile is actually active. Only Upper/Lower/IR get a band - Probe has
        # no profile-defined target at all, just a live reading.
        self.target_envelope_upper = pg.LinearRegionItem(orientation="horizontal", movable=False, brush=pg.mkBrush(231, 76, 60, 40), pen=pg.mkPen(None))
        self.target_envelope_lower = pg.LinearRegionItem(orientation="horizontal", movable=False, brush=pg.mkBrush(52, 152, 219, 40), pen=pg.mkPen(None))
        self.target_envelope_ir = pg.LinearRegionItem(orientation="horizontal", movable=False, brush=pg.mkBrush(243, 156, 18, 40), pen=pg.mkPen(None))
        for region in (self.target_envelope_upper, self.target_envelope_lower, self.target_envelope_ir):
            region.setVisible(False)
            region.setZValue(-10)  # behind the temperature lines, not obscuring them
            self.plot.addItem(region)

        return self.plot

    TARGET_ENVELOPE_TOLERANCE_C = 5.0

    def _reset_section_markers(self) -> None:
        """Clears the estimated-section boundary lines drawn on the live graph and the title's
        section readout - called whenever monitoring restarts against a (possibly different)
        profile, so stale markers from a previous run/profile never linger. Also hides the
        target-envelope bands (see _build_graph) until the first real section estimate comes in
        against whatever profile is active now."""
        for line in self._section_marker_lines:
            self.plot.removeItem(line)
        self._section_marker_lines = []
        self._last_shown_section = None
        self.plot.setTitle("Live Temperatures")
        for region in (self.target_envelope_upper, self.target_envelope_lower, self.target_envelope_ir):
            region.setVisible(False)

    def _update_section_indicator(self, reading, t: float) -> None:
        """Estimated only - see alerts.py's RunMonitor.estimate_current_section() for the
        technique and why it's safe with respect to the curve profile's unconfirmed "rate" field.
        Updates the graph title with the current estimate, drops a vertical marker line at the
        moment the estimate advances to a new section (building a visual timeline of section
        boundaries directly on the live graph), and moves the target-envelope bands (see
        _build_graph) to that section's own Upper/Lower/IR targets."""
        section_index = self.monitor.estimate_current_section(reading)
        profile = self.monitor.active_profile
        envelope_by_zone = {
            "upper_temp": self.target_envelope_upper, "lower_temp": self.target_envelope_lower, "ir_temp": self.target_envelope_ir,
        }
        if section_index is None or profile is None:
            self.plot.setTitle("Live Temperatures")
            for region in envelope_by_zone.values():
                region.setVisible(False)
            return

        total = len(profile.sections)
        self.plot.setTitle(f"Live Temperatures — Section {section_index + 1} of {total} (estimated)")

        section = profile.sections[section_index]
        for attr, region in envelope_by_zone.items():
            target = getattr(section, attr)
            if target > 0:  # a 0 target means "not controlled this section", not a real 0C target
                region.setRegion([target - self.TARGET_ENVELOPE_TOLERANCE_C, target + self.TARGET_ENVELOPE_TOLERANCE_C])
                region.setVisible(True)
            else:
                region.setVisible(False)

        if section_index != self._last_shown_section:
            self._last_shown_section = section_index
            line = pg.InfiniteLine(
                pos=t, angle=90, pen=pg.mkPen("#888", width=1, style=Qt.DashLine),
                label=f"Sec {section_index + 1}", labelOpts={"position": 0.95, "color": "#aaa"},
            )
            self.plot.addItem(line)
            self._section_marker_lines.append(line)

    def _scrollable(self, widget: QWidget, min_width: int = 0) -> QScrollArea:
        """Wraps `widget` in a vertically-scrolling area so a panel that's grown taller than the
        window (the Alerts panel now has Alert Thresholds + Safety Limits + Webhook Notifications
        + the alerts list all stacked - confirmed live 2026-08-22 that this was overflowing its
        given space and getting squeezed/overlapping instead of just scrolling) always stays
        readable regardless of window size, rather than every row being force-shrunk below its
        own sizeHint."""
        if min_width:
            widget.setMinimumWidth(min_width)
        area = QScrollArea()
        area.setWidgetResizable(True)
        area.setWidget(widget)
        return area

    def _build_bottom_panel(self) -> QWidget:
        tabs = QTabWidget()
        main_tab = QWidget()
        main_layout = QHBoxLayout(main_tab)
        main_layout.addWidget(self._build_profile_editor(), stretch=3)
        main_layout.addWidget(self._scrollable(self._build_alerts_panel(), min_width=380), stretch=2)
        tabs.addTab(main_tab, "Profile && Alerts")
        tabs.addTab(self._scrollable(self._build_advanced_panel()), "Advanced Parameters")
        tabs.addTab(self._build_run_history_panel(), "Run History")
        tabs.addTab(self._build_maintenance_panel(), "Maintenance")
        tabs.addTab(self._build_process_assistant_panel(), "AI Assistant")
        return tabs

    def _build_run_history_panel(self) -> QWidget:
        """Every run has always been logged to a CSV under pc-app/logs/ (see data_logger.py),
        but nothing surfaced that history back into either app's UI before now - the only way to
        see it was digging through the folder manually. Read-only, never touches the machine."""
        box = QWidget()
        layout = QVBoxLayout(box)

        toolbar = QHBoxLayout()
        btn_refresh = QPushButton("Refresh")
        btn_refresh.clicked.connect(self._refresh_run_history)
        btn_open_folder = QPushButton("Open logs folder")
        btn_open_folder.clicked.connect(lambda: os.startfile(LOGS_DIR) if LOGS_DIR.exists() else None)
        self.btn_open_run_csv = QPushButton("Open selected CSV")
        self.btn_open_run_csv.clicked.connect(self._on_open_selected_run_csv)
        self.btn_open_run_csv.setEnabled(False)
        self.btn_compare_runs = QPushButton("Compare selected")
        self.btn_compare_runs.setToolTip("Select 2 or more runs (Ctrl+click) to see their stats side by side.")
        self.btn_compare_runs.clicked.connect(self._on_compare_runs)
        self.btn_compare_runs.setEnabled(False)
        self.btn_view_run_details = QPushButton("View details")
        self.btn_view_run_details.setToolTip(
            "Shows this run's full event timeline (faults/warnings with real timestamps) and the "
            "exact configuration snapshot (safety limits, alert thresholds, the profile actually "
            "sent) captured when the run started - see run_finalization.py. Select exactly one run."
        )
        self.btn_view_run_details.clicked.connect(self._on_view_run_details)
        self.btn_view_run_details.setEnabled(False)
        toolbar.addWidget(btn_refresh)
        toolbar.addWidget(btn_open_folder)
        toolbar.addWidget(self.btn_open_run_csv)
        toolbar.addWidget(self.btn_compare_runs)
        toolbar.addWidget(self.btn_view_run_details)
        toolbar.addStretch()
        layout.addLayout(toolbar)

        # 12 columns - was left at 10 (a real bug: too few for setItem/setCellWidget calls below,
        # which use fixed positional column indices) when "Result" was added without bumping this;
        # caught and fixed 2026-09-07 while adding the "Golden" column alongside it.
        self.run_history_table = QTableWidget(0, 12)
        self.run_history_table.setHorizontalHeaderLabels(
            ["Started", "Profile", "Duration (s)", "Peak Upper (C)", "Peak Lower (C)", "Peak IR (C)",
             "Time to Peak Upper (s)", "Faults", "Warnings", "Rows", "Result", "Golden"]
        )
        self.run_history_table.setEditTriggers(QTableWidget.NoEditTriggers)
        self.run_history_table.setSelectionBehavior(QTableWidget.SelectRows)
        self.run_history_table.setSelectionMode(QAbstractItemView.ExtendedSelection)
        self.run_history_table.itemSelectionChanged.connect(self._on_run_history_selection_changed)
        self.run_history_table.horizontalHeader().setStretchLastSection(True)
        layout.addWidget(self.run_history_table)

        thermal_box = QGroupBox("Thermal Model (Stage 24)")
        thermal_box.setToolTip(
            "A real per-zone linear fit trained only on this machine's own PASS runs (see "
            "thermal_model.py) - predicts the actual peak temperature and time-to-peak a "
            "profile's commanded target will produce. Every prediction reports its own "
            "R-squared and sample count. Use the web app's Profile Editor for the "
            "'Predict actual response' button - not yet wired into this desktop tab."
        )
        thermal_layout = QVBoxLayout(thermal_box)
        self.lbl_thermal_model_status = QLabel("")
        self.lbl_thermal_model_status.setWordWrap(True)
        thermal_layout.addWidget(self.lbl_thermal_model_status)
        layout.addWidget(thermal_box)

        self._run_history: list[dict] = []
        self._refresh_run_history()
        return box

    def _on_toggle_run_golden(self, filename: str, golden: bool) -> None:
        try:
            mark_run_golden(filename, golden)
        except ValueError as exc:
            QMessageBox.critical(self, "Error", str(exc))
            return
        self._refresh_run_history()

    def _refresh_run_history(self) -> None:
        self._run_history = list_runs()
        self.run_history_table.setRowCount(len(self._run_history))
        for row, run in enumerate(self._run_history):
            profile_display = f"{run['profile_name']} [INTERRUPTED]" if run.get("interrupted") else run["profile_name"]
            values = [
                run["started"], profile_display, f"{run['duration_s']:.0f}",
                f"{run['peak_upper']:.1f}", f"{run['peak_lower']:.1f}", f"{run['peak_ir']:.1f}",
                f"{run['time_to_peak_upper_s']:.0f}",
                "-" if run.get("fault_count") is None else str(run["fault_count"]),
                "-" if run.get("warning_count") is None else str(run["warning_count"]),
                str(run["row_count"]),
                # See run_finalization.classify_process_result()'s docstring for exactly what
                # this is (and deliberately isn't - no target-window metallurgical judgment).
                run.get("process_result") or "-",
            ]
            for col, value in enumerate(values):
                self.run_history_table.setItem(row, col, QTableWidgetItem(value))
            # Golden-run marking (2026-09-07) - see run_finalization.set_run_golden()'s docstring
            # for why this reuses the existing "compare selected runs" mechanism rather than
            # building a separate comparison path. A real button widget (not a QTableWidgetItem),
            # since it needs its own click handler per row.
            golden_btn = QPushButton("★" if run.get("golden") else "☆")  # filled/outline star
            golden_btn.setToolTip("Golden run - click to unmark" if run.get("golden") else "Mark as golden run")
            golden_btn.setFlat(True)
            golden_btn.setStyleSheet("font-size: 14px; color: #d4af37;")
            golden_btn.clicked.connect(lambda checked=False, filename=run["filename"], currently_golden=run.get("golden", False): self._on_toggle_run_golden(filename, not currently_golden))
            self.run_history_table.setCellWidget(row, 11, golden_btn)
        self.btn_open_run_csv.setEnabled(False)
        self.btn_compare_runs.setEnabled(False)
        self.btn_view_run_details.setEnabled(False)
        self.lbl_thermal_model_status.setText(thermal_model.get_model_status().message)

    def _build_maintenance_panel(self) -> QWidget:
        """2026-09-07, external feature-gap review - see maintenance.py's own module docstring
        for why everything shown here is built from data already logged (run history, calibration
        records, self-tune sessions), never a fabricated hardware-wear estimate."""
        box = QWidget()
        layout = QVBoxLayout(box)

        toolbar = QHBoxLayout()
        btn_refresh = QPushButton("Refresh")
        btn_refresh.clicked.connect(self._refresh_maintenance_status)
        toolbar.addWidget(btn_refresh)
        toolbar.addWidget(QLabel("Calibration due every (days):"))
        self.maint_calibration_interval_spin = QDoubleSpinBox()
        self.maint_calibration_interval_spin.setRange(1.0, 3650.0)
        toolbar.addWidget(self.maint_calibration_interval_spin)
        toolbar.addWidget(QLabel("Self-tune review every (days):"))
        self.maint_selftune_interval_spin = QDoubleSpinBox()
        self.maint_selftune_interval_spin.setRange(1.0, 3650.0)
        toolbar.addWidget(self.maint_selftune_interval_spin)
        btn_save_intervals = QPushButton("Save intervals")
        btn_save_intervals.clicked.connect(self._on_save_maintenance_config)
        toolbar.addWidget(btn_save_intervals)
        toolbar.addStretch()
        layout.addLayout(toolbar)

        # System self-test / Run Diagnostics (2026-09-08, external feature-gap review, second
        # pass) - see system_diagnostics.py's module docstring for why this is broader than (and
        # doesn't replace) the Start-gating preflight check.
        layout.addWidget(QLabel("<b>System Diagnostics</b>"))
        btn_run_diagnostics = QPushButton("Run Diagnostics")
        btn_run_diagnostics.clicked.connect(self._on_run_diagnostics)
        layout.addWidget(btn_run_diagnostics)
        self.lbl_diagnostics_overall = QLabel("")
        layout.addWidget(self.lbl_diagnostics_overall)
        self.diagnostics_table = QTableWidget(0, 3)
        self.diagnostics_table.setHorizontalHeaderLabels(["Check", "Status", "Detail"])
        self.diagnostics_table.setEditTriggers(QTableWidget.NoEditTriggers)
        layout.addWidget(self.diagnostics_table)

        self.lbl_maint_run_summary = QLabel("")
        self.lbl_maint_run_summary.setWordWrap(True)
        layout.addWidget(self.lbl_maint_run_summary)

        layout.addWidget(QLabel("<b>Calibration</b>"))
        self.maint_calibration_table = QTableWidget(0, 4)
        self.maint_calibration_table.setHorizontalHeaderLabels(["Zone", "Last Calibrated", "Days Since", "Status"])
        self.maint_calibration_table.setEditTriggers(QTableWidget.NoEditTriggers)
        layout.addWidget(self.maint_calibration_table)

        layout.addWidget(QLabel("<b>Self-Tune (PID Autotune)</b>"))
        self.maint_selftune_table = QTableWidget(0, 5)
        self.maint_selftune_table.setHorizontalHeaderLabels(["Zone", "Last Self-Tuned", "Days Since", "Sessions", "Status"])
        self.maint_selftune_table.setEditTriggers(QTableWidget.NoEditTriggers)
        layout.addWidget(self.maint_selftune_table)

        # Known-good machine configuration + drift detection (2026-09-08, external feature-gap
        # review, second pass) - see machine_config_snapshot.py's module docstring for why this
        # only covers PID/fan-timing/temp-compensation (not the curve, which has its own versioned
        # history already).
        layout.addWidget(QLabel("<b>Machine Configuration (PID / Fan Timing / Temp Compensation)</b>"))
        config_note = QLabel(
            "Save the machine's current PID gains, fan/vacuum timing, and temperature compensation "
            "as a known-good reference. This app then watches for the touchscreen (or anything "
            "else) changing them without going through here - it never auto-corrects anything, "
            "only reports what changed."
        )
        config_note.setWordWrap(True)
        layout.addWidget(config_note)
        self.lbl_known_good_config = QLabel("")
        layout.addWidget(self.lbl_known_good_config)
        config_toolbar = QHBoxLayout()
        btn_save_known_good = QPushButton("Save current as known-good")
        btn_save_known_good.clicked.connect(self._on_save_known_good_config)
        config_toolbar.addWidget(btn_save_known_good)
        btn_check_config = QPushButton("Check now")
        btn_check_config.clicked.connect(self._on_check_config_drift)
        config_toolbar.addWidget(btn_check_config)
        config_toolbar.addStretch()
        layout.addLayout(config_toolbar)
        self._refresh_known_good_config_label()

        # System health (2026-09-08, external feature-gap review, second pass) - see
        # system_health.py's module docstring for why this is Linux-only (stdlib /proc reads, no
        # psutil dependency) and honestly reports "not available" rather than faking a number on
        # this app's occasional Windows dev-PC runs.
        layout.addWidget(QLabel("<b>Host System Health</b>"))
        self.lbl_system_health = QLabel("")
        self.lbl_system_health.setWordWrap(True)
        layout.addWidget(self.lbl_system_health)
        self._refresh_system_health_label()

        # Automatic backups (2026-09-08, external feature-gap review, second pass) - see
        # automatic_backup.py's module docstring. Restoring reuses the exact same
        # config_backup.restore_backup() + confirmation flow as the existing manual Import.
        layout.addWidget(QLabel("<b>Automatic Backups</b>"))
        self.lbl_auto_backup_status = QLabel("")
        self.lbl_auto_backup_status.setWordWrap(True)
        layout.addWidget(self.lbl_auto_backup_status)
        btn_restore_auto_backup = QPushButton("Restore from automatic backup...")
        btn_restore_auto_backup.clicked.connect(self._on_restore_automatic_backup)
        layout.addWidget(btn_restore_auto_backup)
        self._refresh_auto_backup_status_label()

        # Repair outcome summary (2026-09-08 audit finding) - compute_repair_outcome_stats() and
        # its use in the web app's Maintenance tab already existed, but nothing in this desktop
        # app ever called it either - a "wired up but not active" gap found during a full-
        # codebase audit. See run_analysis.py's own docstring for what this deliberately is (and
        # isn't) - a tally, not a repair-shop CRM.
        layout.addWidget(QLabel("<b>Repair Outcome Summary</b>"))
        repair_stats_row = QHBoxLayout()
        repair_stats_row.addWidget(QLabel("Profile:"))
        self.repair_stats_profile_combo = QComboBox()
        self.repair_stats_profile_combo.addItem("All profiles", userData=None)
        self.repair_stats_profile_combo.currentIndexChanged.connect(self._refresh_repair_outcome_stats)
        repair_stats_row.addWidget(self.repair_stats_profile_combo)
        btn_refresh_repair_stats = QPushButton("Refresh")
        btn_refresh_repair_stats.clicked.connect(self._refresh_repair_outcome_stats)
        repair_stats_row.addWidget(btn_refresh_repair_stats)
        repair_stats_row.addStretch()
        layout.addLayout(repair_stats_row)
        self.lbl_repair_stats = QLabel("")
        self.lbl_repair_stats.setWordWrap(True)
        layout.addWidget(self.lbl_repair_stats)
        self._refresh_repair_outcome_stats()

        self._refresh_maintenance_status()
        return box

    _MAINT_STATUS_COLORS = {"ok": "#2a6", "due_soon": "#b8860b", "overdue": "#a33", "never": "#555"}
    _MAINT_STATUS_LABELS = {"ok": "OK", "due_soon": "Due soon", "overdue": "OVERDUE", "never": "Never"}

    def _maint_status_item(self, status: str) -> QTableWidgetItem:
        item = QTableWidgetItem(self._MAINT_STATUS_LABELS.get(status, status))
        item.setBackground(QColor(self._MAINT_STATUS_COLORS.get(status, "#444")))
        item.setForeground(QColor("#fff"))
        return item

    @staticmethod
    def _format_maint_timestamp(ts) -> str:
        return "Never" if ts is None else time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(ts))

    @staticmethod
    def _format_maint_days_since(days) -> str:
        return "-" if days is None else f"{days:.1f} days ago"

    def _populate_repair_stats_profile_combo(self) -> None:
        current = self.repair_stats_profile_combo.currentData()
        self.repair_stats_profile_combo.blockSignals(True)
        self.repair_stats_profile_combo.clear()
        self.repair_stats_profile_combo.addItem("All profiles", userData=None)
        for name in profile_store.list_profiles():
            self.repair_stats_profile_combo.addItem(name, userData=name)
        idx = self.repair_stats_profile_combo.findData(current)
        self.repair_stats_profile_combo.setCurrentIndex(idx if idx >= 0 else 0)
        self.repair_stats_profile_combo.blockSignals(False)

    def _refresh_repair_outcome_stats(self) -> None:
        profile_name = self.repair_stats_profile_combo.currentData()
        stats = compute_repair_outcome_stats(profile_name)
        c = stats["counts"]
        rate = "n/a (no outcomes recorded yet)" if stats["success_rate"] is None else f"{stats['success_rate'] * 100:.0f}%"
        self.lbl_repair_stats.setText(
            f"{stats['total_runs']} run(s) total | Success: {c['Success']}, Partial: {c['Partial']}, "
            f"Failed: {c['Failed']}, Unknown: {c['Unknown']}, not recorded: {c['not_recorded']} | "
            f"Success rate (of recorded outcomes): {rate}"
        )

    def _refresh_maintenance_status(self) -> None:
        self._refresh_system_health_label()
        self._refresh_auto_backup_status_label()
        self._populate_repair_stats_profile_combo()
        self._refresh_repair_outcome_stats()
        status = maintenance.compute_maintenance_status()
        self.maint_calibration_interval_spin.blockSignals(True)
        self.maint_calibration_interval_spin.setValue(status["config"]["calibration_interval_days"])
        self.maint_calibration_interval_spin.blockSignals(False)
        self.maint_selftune_interval_spin.blockSignals(True)
        self.maint_selftune_interval_spin.setValue(status["config"]["self_tune_review_interval_days"])
        self.maint_selftune_interval_spin.blockSignals(False)

        runs = status["runs"]
        self.lbl_maint_run_summary.setText(
            f"{runs['total_runs']} total run(s), {runs['total_run_hours']:.1f} total logged hours, "
            f"{runs['total_faults']} fault(s), {runs['total_warnings']} warning(s) "
            f"({runs['runs_with_finalization_data']} of {runs['total_runs']} run(s) have full data)."
        )

        self.maint_calibration_table.setRowCount(len(status["calibration"]))
        for row, (zone, info) in enumerate(status["calibration"].items()):
            self.maint_calibration_table.setItem(row, 0, QTableWidgetItem(zone.capitalize()))
            self.maint_calibration_table.setItem(row, 1, QTableWidgetItem(self._format_maint_timestamp(info["last_calibrated_at"])))
            self.maint_calibration_table.setItem(row, 2, QTableWidgetItem(self._format_maint_days_since(info["days_since"])))
            self.maint_calibration_table.setItem(row, 3, self._maint_status_item(info["status"]))

        self.maint_selftune_table.setRowCount(len(status["self_tune"]))
        for row, (zone, info) in enumerate(status["self_tune"].items()):
            self.maint_selftune_table.setItem(row, 0, QTableWidgetItem(zone.capitalize()))
            self.maint_selftune_table.setItem(row, 1, QTableWidgetItem(self._format_maint_timestamp(info["last_self_tuned_at"])))
            self.maint_selftune_table.setItem(row, 2, QTableWidgetItem(self._format_maint_days_since(info["days_since"])))
            self.maint_selftune_table.setItem(row, 3, QTableWidgetItem(str(info["session_count"])))
            self.maint_selftune_table.setItem(row, 4, self._maint_status_item(info["status"]))

    def _on_save_maintenance_config(self) -> None:
        config = maintenance.MaintenanceConfig(
            calibration_interval_days=self.maint_calibration_interval_spin.value(),
            self_tune_review_interval_days=self.maint_selftune_interval_spin.value(),
        )
        maintenance.save_maintenance_config(config)
        self._refresh_maintenance_status()

    _DIAGNOSTICS_STATUS_COLORS = {"ok": "#2a6", "warning": "#b8860b", "critical": "#a33", "info": "#555"}

    def _on_run_diagnostics(self) -> None:
        """System self-test / Run Diagnostics - see system_diagnostics.py's module docstring.
        Involves real active reads (PID + fan-timing/temp-comp), same as the config-drift
        'Check now' button, so this runs on a background thread via _run_write."""
        def do_read():
            pid_regs = self.bus.read_pid_active()
            fan_timing_regs = self.bus.read_fan_timing_active()
            return pid_regs, fan_timing_regs

        def done(result):
            self._mark_idle(btn)
            btn.setText(original_text)
            pid_regs, fan_timing_regs = result
            temp_comp_regs = self.bus.get_latest_temp_comp() if fan_timing_regs is not None else None

            known_good = load_known_good_config()
            config_drift = None
            if known_good is not None and pid_regs is not None and fan_timing_regs is not None:
                config_drift = compute_drift(known_good, pid_regs, fan_timing_regs, temp_comp_regs or [])

            identity = load_connection_identity()
            remembered_adapter = None
            if identity is not None:
                remembered_adapter = {"currently_resolves_to": resolve_remembered_port()}

            checks = run_diagnostics(
                connected=self.bus.is_connected,
                live_reading=self.bus.get_live_reading(),
                disk_status=check_disk_space(),
                system_health_status=get_system_health(),
                maintenance_status=maintenance.compute_maintenance_status(),
                automatic_backups=automatic_backup.list_automatic_backups(),
                remembered_adapter=remembered_adapter,
                config_drift=config_drift,
                pid_regs=pid_regs,
                fan_timing_regs=fan_timing_regs,
            )
            overall = overall_status(checks)
            self.lbl_diagnostics_overall.setText(f"Overall: {overall.upper()}")
            self.lbl_diagnostics_overall.setStyleSheet(
                f"font-weight: bold; color: {self._DIAGNOSTICS_STATUS_COLORS.get(overall, '#ccc')};"
            )
            self.diagnostics_table.setRowCount(len(checks))
            for row, check in enumerate(checks):
                self.diagnostics_table.setItem(row, 0, QTableWidgetItem(check.name))
                status_item = QTableWidgetItem(check.status.upper())
                status_item.setBackground(QColor(self._DIAGNOSTICS_STATUS_COLORS.get(check.status, "#444")))
                status_item.setForeground(QColor("#fff"))
                self.diagnostics_table.setItem(row, 1, status_item)
                self.diagnostics_table.setItem(row, 2, QTableWidgetItem(check.detail))

        btn = self.sender()
        original_text = btn.text()
        self._mark_busy(btn)
        btn.setText("Running...")
        self._run_write(do_read, done)

    def _refresh_system_health_label(self) -> None:
        status = get_system_health()
        if not status["available"]:
            self.lbl_system_health.setText(f"Not available: {status['error']}")
            return
        self.lbl_system_health.setText(
            f"Load average: {status['load_avg'][0]:.2f}, {status['load_avg'][1]:.2f}, "
            f"{status['load_avg'][2]:.2f} ({status['cpu_count']} core(s)) | "
            f"Memory: {status['mem_used_pct']:.1f}% used ({status['mem_available_mb']:.0f} MB "
            f"available of {status['mem_total_mb']:.0f} MB) | "
            f"Uptime: {status['uptime_s'] / 3600:.1f} hours | Status: {status['level'].upper()}"
        )

    def _refresh_auto_backup_status_label(self) -> None:
        backups = automatic_backup.list_automatic_backups()
        if not backups:
            self.lbl_auto_backup_status.setText("No automatic backups yet - the first one is created within an hour of this app running.")
            return
        newest = backups[0]
        when = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(newest["created_at"])) if newest["created_at"] else "unknown time"
        self.lbl_auto_backup_status.setText(f"Last automatic backup: {when} ({len(backups)} kept, {newest['profile_count']} profile(s)).")

    def _on_restore_automatic_backup(self) -> None:
        backups = automatic_backup.list_automatic_backups()
        if not backups:
            QMessageBox.information(self, "No Backups", "No automatic backups exist yet.")
            return
        labels = [
            f"{time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(b['created_at'])) if b['created_at'] else 'unknown'} "
            f"({b['profile_count']} profile(s))"
            for b in backups
        ]
        label, ok = QInputDialog.getItem(self, "Restore Automatic Backup", "Choose a backup to restore:", labels, 0, False)
        if not ok:
            return
        chosen = backups[labels.index(label)]
        try:
            backup = json.loads((automatic_backup.BACKUPS_DIR / chosen["filename"]).read_text())
        except (json.JSONDecodeError, OSError) as exc:
            QMessageBox.critical(self, "Restore Failed", f"Could not read {chosen['filename']}: {exc}")
            return
        reply = QMessageBox.question(
            self, "Confirm Restore",
            f"This will restore {len(backup.get('profiles', []))} profile(s) into your local "
            f"library (each as a new version, existing history preserved), and overwrite the "
            f"current safety limits and alert thresholds. Continue?",
            QMessageBox.Yes | QMessageBox.No, QMessageBox.No,
        )
        if reply != QMessageBox.Yes:
            return
        try:
            summary = config_backup.restore_backup(backup, self.safety_limits, self.monitor.config)
        except ValueError as exc:
            QMessageBox.critical(self, "Restore Failed", str(exc))
            return
        QMessageBox.information(
            self, "Restored",
            f"Restored {len(summary['profiles_restored'])} profile(s), safety limits, and alert thresholds.",
        )

    def _refresh_known_good_config_label(self) -> None:
        snapshot = load_known_good_config()
        if snapshot is None:
            self.lbl_known_good_config.setText("No known-good configuration saved yet.")
        else:
            when = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(snapshot.captured_at))
            self.lbl_known_good_config.setText(f"Known-good configuration saved: {when}")

    def _on_save_known_good_config(self) -> None:
        """Actively reads PID + fan-timing/temp-compensation and saves them as the new known-good
        reference - an explicit operator action, not something done silently."""
        def done(result):
            self._mark_idle(btn)
            btn.setText(original_text)
            pid, fan_timing = result
            if pid is None or fan_timing is None:
                QMessageBox.critical(self, "Failed", "Could not read PID and/or fan-timing/compensation from the board.")
                return
            temp_comp = self.bus.get_latest_temp_comp()
            save_known_good_config(pid, fan_timing, temp_comp)
            self._refresh_known_good_config_label()
            self._last_config_drift_state = None  # force the next periodic check to re-evaluate
            QMessageBox.information(self, "Saved", "Current machine configuration saved as known-good.")

        def do_read():
            pid = self.bus.read_pid_active()
            fan_timing = self.bus.read_fan_timing_active()
            return pid, fan_timing

        btn = self.sender()
        original_text = btn.text()
        self._mark_busy(btn)
        btn.setText("Reading...")
        self._run_write(do_read, done)

    def _on_check_config_drift(self) -> None:
        """On-demand drift check via a real active read - unlike the periodic passive-only
        background check, this gives an immediate, reliable answer regardless of what the
        touchscreen has recently shown."""
        known_good = load_known_good_config()
        if known_good is None:
            QMessageBox.information(self, "No Reference Saved", "No known-good configuration saved yet - use 'Save current as known-good' first.")
            return

        def done(result):
            self._mark_idle(btn)
            btn.setText(original_text)
            pid, fan_timing = result
            if pid is None or fan_timing is None:
                QMessageBox.critical(self, "Failed", "Could not read PID and/or fan-timing/compensation from the board.")
                return
            temp_comp = self.bus.get_latest_temp_comp()
            drift = compute_drift(known_good, pid, fan_timing, temp_comp)
            if not drift["has_drift"]:
                QMessageBox.information(self, "No Drift", "Machine configuration matches the saved known-good reference.")
                return
            all_diffs = drift["pid_diffs"] + drift["fan_timing_diffs"] + drift["temp_comp_diffs"]
            QMessageBox.warning(
                self, "Configuration Drift Detected",
                "The machine's current configuration differs from the saved known-good reference:\n\n"
                + "\n".join(f"- {d}" for d in all_diffs),
            )

        def do_read():
            pid = self.bus.read_pid_active()
            fan_timing = self.bus.read_fan_timing_active()
            return pid, fan_timing

        btn = self.sender()
        original_text = btn.text()
        self._mark_busy(btn)
        btn.setText("Checking...")
        self._run_write(do_read, done)

    def _build_process_assistant_panel(self) -> QWidget:
        """AI Process Assistant (2026-09-07, external feature-gap review) - see
        process_assistant.py's own module docstring for the evidence-tagging model
        ([MEASURED]/[HISTORICAL]/[INFERENCE]) and the hard read-only architectural rule this
        respects: this can only ask a question about already-logged data, never act on anything."""
        box = QWidget()
        layout = QVBoxLayout(box)
        intro_label = QLabel(
            "Answers questions using ONLY this machine's own logged data (run history, saved "
            "profile metadata, maintenance status) - never general BGA rework knowledge, and "
            "never controls the machine. Every claim is tagged [MEASURED] (a direct fact from the "
            "data), [HISTORICAL] (computed/derived from it), or [INFERENCE] (a pattern/guess, not "
            "a fact) - uses the same Ollama connection configured for Profile Search above."
        )
        intro_label.setWordWrap(True)
        layout.addWidget(intro_label)

        row = QHBoxLayout()
        self.assistant_question_edit = QLineEdit()
        self.assistant_question_edit.setPlaceholderText("e.g. Why did my last PS5 APU run fail? Which profiles are still Draft?")
        self.assistant_question_edit.returnPressed.connect(self._on_ask_process_assistant)
        row.addWidget(self.assistant_question_edit, stretch=1)
        self.btn_ask_assistant = QPushButton("Ask")
        self.btn_ask_assistant.clicked.connect(self._on_ask_process_assistant)
        row.addWidget(self.btn_ask_assistant)
        layout.addLayout(row)

        self.assistant_answer_text = QTextEdit()
        self.assistant_answer_text.setReadOnly(True)
        layout.addWidget(self.assistant_answer_text)
        return box

    def _on_ask_process_assistant(self) -> None:
        question = self.assistant_question_edit.text().strip()
        if not question:
            return

        def done(result):
            self._mark_idle(self.btn_ask_assistant)
            self.btn_ask_assistant.setText("Ask")
            answer, error = result
            if error:
                self.assistant_answer_text.setPlainText(f"Error: {error}")
            else:
                self.assistant_answer_text.setPlainText(answer)

        ollama_config = self.ollama_config
        self._mark_busy(self.btn_ask_assistant)
        self.btn_ask_assistant.setText("Thinking...")
        self.assistant_answer_text.setPlainText("")
        self._run_write(lambda: process_assistant.ask_process_assistant(question, ollama_config), done)

    def _on_run_history_selection_changed(self) -> None:
        rows = {item.row() for item in self.run_history_table.selectedItems()}
        self.btn_open_run_csv.setEnabled(len(rows) >= 1)
        self.btn_compare_runs.setEnabled(len(rows) >= 2)
        self.btn_view_run_details.setEnabled(len(rows) == 1)

    def _on_compare_runs(self) -> None:
        rows = sorted({item.row() for item in self.run_history_table.selectedItems()})
        if len(rows) < 2:
            return
        runs = [self._run_history[r] for r in rows]
        lines = []
        for run in runs:
            line = (
                f"{run['started']} ({run['profile_name']}): Peak Upper {run['peak_upper']:.1f}C "
                f"(at {run['time_to_peak_upper_s']:.0f}s), Lower {run['peak_lower']:.1f}C, "
                f"IR {run['peak_ir']:.1f}C, Probe {run.get('peak_probe', 0.0):.1f}C, "
                f"Duration {run['duration_s']:.0f}s"
            )
            # Fault/warning counts and comm/cooling stats only exist for runs finalized after
            # Run Finalization was added (see run_finalization.py) - None means "not recorded",
            # never fabricated as 0 for an older run.
            if run.get("fault_count") is not None:
                line += f"\n  Faults: {run['fault_count']}, Warnings: {run['warning_count']}"
            if run.get("time_above_wetting_s") is not None:
                line += f", Time above wetting: {run['time_above_wetting_s']:.0f}s"
            if run.get("cooling_rate_c_per_min") is not None:
                line += f", Cooling rate: {run['cooling_rate_c_per_min']:.1f}C/min"
            if run.get("process_result") is not None:
                # See run_finalization.classify_process_result()'s own docstring for exactly what
                # this is (and isn't) - a real fault-based FAIL / wetting-reached PASS / ambiguous
                # INCOMPLETE, not a target-window metallurgical judgment.
                line += f", Process result: {run['process_result']}"
            lines.append(line)
        QMessageBox.information(self, "Run Comparison", "RUN COMPARISON\n\n" + "\n\n".join(lines))

    def _on_open_selected_run_csv(self) -> None:
        rows = {item.row() for item in self.run_history_table.selectedItems()}
        if not rows:
            return
        run = self._run_history[min(rows)]
        os.startfile(LOGS_DIR / run["filename"])

    def _on_view_run_details(self) -> None:
        """Shows the run's event timeline and config snapshot (2026-09-07, external feature-gap
        review) - both were being computed and saved into every run's .summary.json all along
        (see run_finalization.RunFinalizationTracker.record_event()/config_snapshot), but nothing
        ever surfaced them in the UI, making them dead weight from the operator's own perspective.
        Reads the sidecar directly (no HTTP round-trip needed - this is the desktop app, already
        running on the same filesystem the logs live on)."""
        rows = {item.row() for item in self.run_history_table.selectedItems()}
        if len(rows) != 1:
            return
        run = self._run_history[next(iter(rows))]
        summary = load_summary(LOGS_DIR / run["filename"])
        if summary is None:
            QMessageBox.information(
                self, "No Details Available",
                f"'{run['filename']}' has no saved summary (predates Run Finalization, or the "
                f"run never finalized cleanly) - no event timeline or config snapshot to show.",
            )
            return

        dialog = QDialog(self)
        dialog.setWindowTitle(f"Run Details — {run['started']} ({run['profile_name']})")
        dialog.resize(700, 550)
        layout = QVBoxLayout(dialog)

        tabs = QTabWidget()
        layout.addWidget(tabs)

        # Operator run notes (2026-09-07, external feature-gap review, second pass) - free-text,
        # attachable to any historical run. Saved immediately on its own button, independent of
        # the dialog's own Close button, so a note isn't lost if the dialog is dismissed some other
        # way.
        notes_edit = QTextEdit()
        notes_edit.setPlainText(summary.get("notes", ""))
        notes_edit.setPlaceholderText("e.g. Cold joint on U4, reflowed a second time at higher IR.")
        notes_tab = QWidget()
        notes_layout = QVBoxLayout(notes_tab)
        notes_layout.addWidget(QLabel("Notes:"))
        notes_layout.addWidget(notes_edit)

        # Repair outcome tracking (2026-09-07, external feature-gap review, second pass) - see
        # run_finalization.set_repair_outcome()'s docstring for why this stays deliberately
        # minimal (one status + one free-text reason, no repair-shop CRM).
        notes_layout.addWidget(QLabel("Repair outcome:"))
        outcome_combo = QComboBox()
        outcome_combo.addItems(["", "Success", "Partial", "Failed", "Unknown"])
        outcome_combo.setCurrentText(summary.get("repair_outcome", ""))
        notes_layout.addWidget(outcome_combo)
        notes_layout.addWidget(QLabel("Failure reason (if any):"))
        reason_edit = QTextEdit(summary.get("repair_failure_reason", ""))
        reason_edit.setMaximumHeight(60)
        notes_layout.addWidget(reason_edit)

        btn_save_notes = QPushButton("Save")

        def _save_notes():
            mark_run_notes(run["filename"], notes_edit.toPlainText())
            mark_repair_outcome(run["filename"], outcome_combo.currentText(), reason_edit.toPlainText())
            run["notes"] = notes_edit.toPlainText()
            run["repair_outcome"] = outcome_combo.currentText()
            run["repair_failure_reason"] = reason_edit.toPlainText()
            QMessageBox.information(self, "Saved", "Notes and repair outcome saved.")

        btn_save_notes.clicked.connect(_save_notes)
        notes_layout.addWidget(btn_save_notes)
        tabs.addTab(notes_tab, "Notes / Repair Outcome")

        # Process result (2026-09-07, external feature-gap review, second pass) - shows WHY a run
        # got its PASS/FAIL/INCOMPLETE, not just the bare label already in the table. See
        # run_finalization.finalize_run()'s docstring for "process_result_basis"/
        # "process_limit_violations".
        result_text = QTextEdit()
        result_text.setReadOnly(True)
        result_text.setFontFamily("Consolas, monospace")
        basis = summary.get("process_result_basis")
        lines = [f"Result: {summary.get('process_result', '-')}"]
        if basis == "process_limits":
            lines.append("Basis: this profile's own configured process limits (see its metadata).")
            violations = summary.get("process_limit_violations") or []
            if violations:
                lines.append("\nViolations:")
                lines.extend(f"  - {v}" for v in violations)
            else:
                lines.append("\nNo violations - within every configured limit.")
        elif basis == "conservative":
            lines.append(
                "Basis: conservative fallback (no process limits configured for this profile) - "
                "FAIL only means a fault occurred; INCOMPLETE means no fault but wetting was never "
                "reached, which is deliberately ambiguous (see run_finalization.classify_process_"
                "result's docstring). Add process limits in this profile's metadata for a real "
                "pass/fail window."
            )
        else:
            lines.append("(This run predates process-result classification.)")
        result_text.setPlainText("\n".join(lines))
        tabs.addTab(result_text, "Process Result")

        events_text = QTextEdit()
        events_text.setReadOnly(True)
        events_text.setFontFamily("Consolas, monospace")
        events = summary.get("events") or []
        if events:
            events_text.setPlainText("\n".join(f"[{e['elapsed_s']:>8.1f}s]  {e['description']}" for e in events))
        else:
            events_text.setPlainText("No events recorded for this run (predates the event timeline feature).")
        tabs.addTab(events_text, "Event Timeline")

        config_text = QTextEdit()
        config_text.setReadOnly(True)
        config_text.setFontFamily("Consolas, monospace")
        config_snapshot = summary.get("config_snapshot")
        if config_snapshot:
            config_text.setPlainText(json.dumps(config_snapshot, indent=2))
        else:
            config_text.setPlainText("No configuration snapshot recorded for this run (predates the config-snapshot feature).")
        tabs.addTab(config_text, "Configuration Snapshot")

        btn_close = QPushButton("Close")
        btn_close.clicked.connect(dialog.accept)
        layout.addWidget(btn_close)
        dialog.exec()

    def _build_profile_editor(self) -> QWidget:
        box = QGroupBox("Profile Editor")
        layout = QVBoxLayout(box)

        # Split across two rows, not one - 12 buttons plus the profile selector in a single
        # QHBoxLayout has no way to wrap, so past a certain window width Qt just shrinks every
        # widget below its own sizeHint, truncating button text (confirmed live 2026-08-22 - a
        # user screenshot showed "Send to machine" and "Recommendations..." both cut off mid-
        # word). Grouped by what each row is for: local file operations on row 1, everything that
        # either touches the physical machine or is a less-frequent action on row 2.
        toolbar = QHBoxLayout()
        toolbar2 = QHBoxLayout()
        self.profile_combo = QComboBox()
        self.profile_combo.setEditable(True)
        self._refresh_profile_list()
        toolbar.addWidget(QLabel("Profile:"))
        toolbar.addWidget(self.profile_combo, stretch=1)

        btn_load = QPushButton("Load (local)")
        btn_load.clicked.connect(self._on_load_local)
        btn_save = QPushButton("Save (local)")
        btn_save.clicked.connect(self._on_save_local)
        btn_rename = QPushButton("Rename...")
        btn_rename.clicked.connect(self._on_rename_local)
        btn_delete = QPushButton("Delete")
        btn_delete.setStyleSheet("color: #d55;")
        btn_delete.clicked.connect(self._on_delete_local)
        self.btn_send_machine = QPushButton("Send to machine")
        self.btn_send_machine.setStyleSheet("background: #c60; color: white;")
        self.btn_send_machine.clicked.connect(self._on_send_to_machine)
        btn_mirror = QPushButton("Mirror as active profile")
        btn_mirror.setToolTip(
            "Tell live monitoring this is the profile actually running on the machine right "
            "now, without sending anything or requiring Start to have been pressed from this "
            "app - use this when the machine was started from its own touchscreen or physical "
            "button instead."
        )
        btn_mirror.clicked.connect(self._on_mirror_active_profile)
        btn_import = QPushButton("Import file...")
        btn_import.setToolTip("Import one or more profiles from a .csv or .xlsx file - see profile_import.py for the expected layout(s).")
        btn_import.clicked.connect(self._on_import_file)
        btn_export = QPushButton("Export all...")
        btn_export.setToolTip("Export every saved local profile to a single .csv or .xlsx file - the reverse of Import, for backup or sharing.")
        btn_export.clicked.connect(self._on_export_all)
        btn_metadata = QPushButton("Metadata...")
        btn_metadata.setToolTip("Documentation beyond the raw curve data - process type, solder, notes. Saved immediately, versioned like any other save.")
        btn_metadata.clicked.connect(self._on_edit_metadata)
        btn_history = QPushButton("History...")
        btn_history.setToolTip("Every save is kept, not overwritten - view and restore past versions of this profile.")
        btn_history.clicked.connect(self._on_view_history)
        btn_recommend = QPushButton("Recommendations...")
        btn_recommend.setToolTip("Analyzes past logged runs of this profile (by name) for a consistent overshoot/undershoot pattern. Never modifies the profile automatically.")
        btn_recommend.clicked.connect(self._on_view_recommendations)
        btn_search_online = QPushButton("Search online...")
        btn_search_online.setToolTip(
            "Searches the web for a real published profile for a board you name, using a local "
            "Ollama model only to extract numbers from real page text - never to invent one. "
            "Loads a candidate into this table for review, never auto-saved or auto-sent. "
            "Requires Ollama to be configured below."
        )
        btn_search_online.clicked.connect(self._on_search_profile_online)
        for b in (btn_load, btn_save, btn_rename, btn_delete):
            toolbar.addWidget(b)
        toolbar.addStretch()
        for b in (self.btn_send_machine, btn_mirror, btn_import, btn_export, btn_metadata, btn_history, btn_recommend, btn_search_online):
            toolbar2.addWidget(b)
        toolbar2.addStretch()
        layout.addLayout(toolbar)
        layout.addLayout(toolbar2)

        # The machine only emits curve data on certain touchscreen screen-load events, not
        # continuously - this makes that visible instead of leaving the user guessing.
        status_row = QHBoxLayout()
        self.lbl_curve_status = QLabel("Curve data: none observed yet")
        self.lbl_curve_status.setStyleSheet("color: #aaa; font-style: italic;")
        status_row.addWidget(self.lbl_curve_status)
        status_row.addStretch()
        self.chk_autoload = QCheckBox("Auto-load new curve data into this table")
        self.chk_autoload.setChecked(True)
        self.chk_autoload.setToolTip(
            "When the machine sends fresh curve data (triggered by navigating to the curve "
            "editor screen on the touchscreen), load it into this table automatically - so you "
            "don't have to catch the timing by hand."
        )
        status_row.addWidget(self.chk_autoload)
        layout.addLayout(status_row)

        self.table = QTableWidget(len(ROW_LABELS), CURVE_SECTIONS)
        self.table.setVerticalHeaderLabels(ROW_LABELS)
        self.table.setHorizontalHeaderLabels([f"Section {i+1}" for i in range(CURVE_SECTIONS)])
        self.table.itemChanged.connect(lambda _item: self._update_profile_chart())
        layout.addWidget(self.table)

        # Graphical view of the table above, kept in sync automatically (populate and manual
        # edits both route through here - see _populate_table's blockSignals + explicit call,
        # and the itemChanged connection just above). Plots the profile's own TARGET values per
        # section, not a physical prediction of actual temperature - see profile_simulation.py's
        # module docstring for why (no real thermal model, and the profile's own "rate" field has
        # never-confirmed real-world units so ramp shape between sections can't be drawn either,
        # hence the flat per-section markers connected by straight lines rather than true steps).
        self.profile_plot = pg.PlotWidget()
        self.profile_plot.setBackground("#1e1e1e")
        self.profile_plot.setLabel("left", "Target", units="C")
        self.profile_plot.setLabel("bottom", "Section")
        self.profile_plot.showGrid(x=True, y=True, alpha=0.3)
        self.profile_plot.addLegend()
        self.profile_plot.setMaximumHeight(220)
        # Distinct line style + marker shape per zone, not just color - many real profiles
        # (confirmed live 2026-08-22 against imported profiles) set Upper and Lower to the exact
        # same target at every section, which made the solid same-shape Upper line completely
        # invisible underneath an identical Lower line drawn on top of it. Different dash
        # patterns and marker shapes stay visually distinguishable even when the underlying
        # values are pixel-identical.
        self.profile_curve_upper = self.profile_plot.plot(pen=pg.mkPen("#e74c3c", width=2, style=Qt.SolidLine), symbol="o", symbolSize=6, name="Upper")
        self.profile_curve_lower = self.profile_plot.plot(pen=pg.mkPen("#3498db", width=2, style=Qt.DashLine), symbol="s", symbolSize=6, name="Lower")
        self.profile_curve_ir = self.profile_plot.plot(pen=pg.mkPen("#f39c12", width=2, style=Qt.DotLine), symbol="t", symbolSize=8, name="Infrared")
        self.profile_wetting_line = pg.InfiniteLine(angle=0, pen=pg.mkPen("#e91e63", style=Qt.DashLine))
        self.profile_plot.addItem(self.profile_wetting_line)
        layout.addWidget(self.profile_plot)

        self.lbl_profile_sim = QLabel()
        self.lbl_profile_sim.setStyleSheet("color: #9ab; font-size: 0.85rem;")
        self.lbl_profile_sim.setWordWrap(True)
        layout.addWidget(self.lbl_profile_sim)

        self.current_profile = Profile(name="Untitled")
        self._populate_table(self.current_profile)

        return box

    def _build_alerts_panel(self) -> QWidget:
        box = QGroupBox("Alerts")
        layout = QVBoxLayout(box)

        self.lbl_active_profile = QLabel("Monitoring against: none")
        self.lbl_active_profile.setStyleSheet("color: #aaa; font-style: italic;")
        layout.addWidget(self.lbl_active_profile)

        backup_row = QHBoxLayout()
        btn_export_backup = QPushButton("Export backup...")
        btn_export_backup.setToolTip("Bundles safety limits, alert thresholds, every saved local profile, and PID send history into one file.")
        btn_export_backup.clicked.connect(self._on_export_backup)
        btn_import_backup = QPushButton("Import backup...")
        btn_import_backup.clicked.connect(self._on_import_backup)
        backup_row.addWidget(btn_export_backup)
        backup_row.addWidget(btn_import_backup)
        backup_row.addStretch()
        layout.addLayout(backup_row)

        wetting_row = QHBoxLayout()
        wetting_row.addWidget(QLabel("Wetting temp (C):"))
        self.wetting_spin = QDoubleSpinBox()
        self.wetting_spin.setRange(100.0, 300.0)
        self.wetting_spin.setValue(217.0)
        self.wetting_spin.valueChanged.connect(self._on_wetting_changed)
        wetting_row.addWidget(self.wetting_spin)
        wetting_row.addStretch()
        layout.addLayout(wetting_row)

        # The other alert thresholds (alerts.AlertConfig) were previously hardcoded defaults with
        # no way to adjust them from either UI - only wetting temp was exposed. Added 2026-08-19.
        thresholds_box = QGroupBox("Alert thresholds")
        thresholds_grid = QGridLayout(thresholds_box)
        default_config = self.monitor.config
        self._alert_threshold_spins: dict[str, QDoubleSpinBox] = {}
        for row, (label, attr, lo, hi, step) in enumerate([
            ("Stuck sensor time (s):", "stuck_sensor_seconds", 5.0, 120.0, 1.0),
            ("Stuck sensor epsilon (C):", "stuck_sensor_epsilon", 0.01, 1.0, 0.01),
            ("Max jump per reading (C):", "max_jump_per_reading", 1.0, 100.0, 1.0),
            ("Section deviation (C):", "section_deviation_c", 1.0, 100.0, 1.0),
        ]):
            spin = QDoubleSpinBox()
            spin.setRange(lo, hi)
            spin.setSingleStep(step)
            spin.setDecimals(2 if step < 1 else 0)
            spin.setValue(getattr(default_config, attr))
            spin.valueChanged.connect(lambda value, a=attr: setattr(self.monitor.config, a, value))
            self._alert_threshold_spins[attr] = spin
            thresholds_grid.addWidget(QLabel(label), row, 0)
            thresholds_grid.addWidget(spin, row, 1)
        layout.addWidget(thresholds_box)

        # Deliberately separate from "Alert thresholds" above - these are enforcement-grade
        # (violating one stops the run automatically, see _check_safety_limits), not advisory.
        # See safety_limits.py for why ramp-rate/duration are computed from observed data rather
        # than the profile's own unit-ambiguous "rate" field.
        safety_box = QGroupBox("Safety limits (hard - auto-stops the run)")
        safety_box.setStyleSheet("QGroupBox { border: 1px solid #a33; border-radius: 4px; margin-top: 8px; } QGroupBox::title { color: #e88; }")
        safety_grid = QGridLayout(safety_box)
        self._safety_limit_spins: dict[str, tuple[QDoubleSpinBox, bool]] = {}
        for row, (label, attr, lo, hi, step) in enumerate([
            ("Max Upper temp (C):", "max_upper_temp_c", 50.0, 400.0, 5.0),
            ("Max Lower temp (C):", "max_lower_temp_c", 50.0, 400.0, 5.0),
            ("Max IR temp (C):", "max_ir_temp_c", 50.0, 400.0, 5.0),
            ("Max Probe temp (C):", "max_probe_temp_c", 50.0, 400.0, 5.0),
            ("Max observed ramp (C/min):", "max_observed_ramp_c_per_min", 10.0, 500.0, 10.0),
            ("Min ramp sample interval (s):", "min_ramp_sample_interval_s", 1.0, 60.0, 1.0),
            ("Max run duration (min):", "max_run_duration_s", 5.0, 240.0, 5.0),
            ("Max comms outage (s):", "max_communication_outage_s", 5.0, 300.0, 5.0),
        ]):
            spin = QDoubleSpinBox()
            spin.setRange(lo, hi)
            spin.setSingleStep(step)
            spin.setDecimals(0)
            is_seconds_as_minutes = attr in ("max_run_duration_s",)
            spin.setValue(getattr(self.safety_limits, attr) / (60.0 if is_seconds_as_minutes else 1.0))
            # editingFinished (fires once, on focus-out/Enter), not valueChanged (fires on every
            # keystroke/spin-arrow-click) - confirming per-digit-typed would be an unusable nag.
            # See _on_safety_limit_edited for the confirm-or-revert logic this triggers.
            spin.editingFinished.connect(
                lambda s=spin, a=attr, mins=is_seconds_as_minutes: self._on_safety_limit_edited(s, a, mins)
            )
            self._safety_limit_spins[attr] = (spin, is_seconds_as_minutes)
            label_widget = QLabel(label)
            if attr == "max_communication_outage_s":
                tip = "How long a run may continue with no fresh reading before it's treated as unsafe to keep trusting - only enforced while a run is active."
                label_widget.setToolTip(tip)
                spin.setToolTip(tip)
            elif attr == "min_ramp_sample_interval_s":
                tip = (
                    "Minimum real time between two readings before the ramp-rate check trusts the "
                    "result - a rate computed over less time than this is dominated by ordinary "
                    "sensor/read noise, not a real trend (confirmed live 2026-09: a few degrees of "
                    "normal per-tick jitter, extrapolated from a ~1-2s gap to a per-minute figure, "
                    "looked like a runaway ramp and kept false-triggering even as the ramp limit "
                    "itself was raised). Raise this if you still see false ramp-rate stops; lower "
                    "it only if a genuinely fast, dangerous ramp needs to be caught faster than "
                    "this allows."
                )
                label_widget.setToolTip(tip)
                spin.setToolTip(tip)
            safety_grid.addWidget(label_widget, row, 0)
            safety_grid.addWidget(spin, row, 1)
        layout.addWidget(safety_box)
        # Pre-change snapshots + undo (2026-09-08, external feature-gap review, second pass) -
        # see config_change_history.py's module docstring.
        btn_safety_limits_history = QPushButton("Safety limits history / undo...")
        btn_safety_limits_history.setToolTip("Every safety-limit change is logged here, with a real before/after diff - restoring re-applies a past set of values, itself recorded as a new change.")
        btn_safety_limits_history.clicked.connect(self._on_view_safety_limits_history)
        layout.addWidget(btn_safety_limits_history)

        layout.addWidget(self._build_webhook_box())
        layout.addWidget(self._build_web_search_box())
        layout.addWidget(self._build_profile_search_box())

        self.alerts_list = QListWidget()
        layout.addWidget(self.alerts_list)

        return box

    def _on_safety_limit_edited(self, spin: QDoubleSpinBox, attr: str, is_minutes: bool) -> None:
        """Safety limits are enforcement-grade (a wrong value either fails to catch a real problem
        or stops a perfectly good run) - the roadmap explicitly calls for confirmation before
        changing one, unlike the advisory Alert Thresholds above."""
        new_value = spin.value() * (60.0 if is_minutes else 1.0)
        old_value = getattr(self.safety_limits, attr)
        if new_value == old_value:
            return
        reply = QMessageBox.warning(
            self, "Confirm Safety Limit Change",
            f"Change {attr.replace('_', ' ')} from {old_value:.1f} to {new_value:.1f}?\n\n"
            f"This changes when the software automatically stops a run for safety.",
            QMessageBox.Yes | QMessageBox.No, QMessageBox.No,
        )
        if reply == QMessageBox.Yes:
            old_values = asdict(self.safety_limits)
            setattr(self.safety_limits, attr, new_value)
            save_safety_limits(self.safety_limits)
            # Pre-change snapshot (2026-09-08, external feature-gap review, second pass) - see
            # config_change_history.py's module docstring for why this exists alongside the daily
            # automatic backup.
            config_change_history.record_config_change(old_values, asdict(self.safety_limits))
        else:
            spin.blockSignals(True)
            spin.setValue(old_value / (60.0 if is_minutes else 1.0))
            spin.blockSignals(False)

    def _build_web_search_box(self) -> QWidget:
        """Web search backend for Profile Search below - DuckDuckGo's html results page, 2026-09-09
        (reverted back here after briefly switching to the Google Custom Search JSON API and then
        confirming live that Google closed that API to new customers back in 2025 - see
        profile_search.py's module docstring for the full history). Keyless, needs no operator
        setup - the only thing worth a box here is a way to test it's actually working right now,
        since DuckDuckGo's own bot-detection can and does block this app's requests without
        warning (also confirmed live the same day)."""
        box = QGroupBox("Web Search (DuckDuckGo)")
        box.setToolTip(
            "Used by Profile Search below to find candidate pages online - no setup needed, this "
            "is a plain DuckDuckGo web search. Every query also runs a second time scoped to a "
            "curated list of known-good repair-forum and rework-equipment sites (badcaps.net, "
            "eevblog.com, reddit.com, ifixit.com, youtube.com, metcal.com, atlaspcb.com, "
            "pcbmay.com, chipquik.com, jbctools.com) and merges both - this can only find MORE "
            "candidates than a plain search, never fewer.\n\n"
            "Real risk confirmed live 2026-09-09: DuckDuckGo's own bot-detection can block this "
            "app's requests without warning (an \"anomaly\" challenge instead of real results) - "
            "this app will never attempt to bypass that. Use Test search below to check whether "
            "it's currently working."
        )
        layout = QVBoxLayout(box)

        query_row = QHBoxLayout()
        query_row.addWidget(QLabel("Test query:"))
        self.web_search_test_query_edit = QLineEdit()
        self.web_search_test_query_edit.setPlaceholderText("BGA rework reflow profile")
        query_row.addWidget(self.web_search_test_query_edit)
        layout.addLayout(query_row)

        btn_test_search = QPushButton("Test search")
        btn_test_search.setToolTip(
            "Runs one real query against DuckDuckGo right now (general web + the curated site "
            "list above) so you can confirm it's actually working, since DuckDuckGo's own "
            "bot-detection can block this app's requests without warning."
        )
        btn_test_search.clicked.connect(self._on_test_web_search)
        layout.addWidget(btn_test_search)

        return box

    def _on_test_web_search(self) -> None:
        """Runs one real DuckDuckGo query (profile_search.search_web) so the operator can confirm
        web search is actually working right now, independent of the full Profile Search flow -
        DuckDuckGo's bot-detection can block this app's requests without warning (confirmed live
        2026-09-09), so this is a genuinely useful thing to be able to check on its own."""
        query = self.web_search_test_query_edit.text().strip() or "BGA rework reflow profile"
        hits, error = profile_search.search_web(query, max_results=5)
        if error:
            QMessageBox.critical(self, "Test search failed", error)
            return
        if not hits:
            QMessageBox.information(self, "Test search", "Connected OK, but 0 results for that query - try a broader query.")
            return
        lines = "\n".join(f"- {h.title}\n  {h.url}" for h in hits)
        QMessageBox.information(self, "Test search", f"Found {len(hits)} result(s):\n\n{lines}")

    def _build_profile_search_box(self) -> QWidget:
        box = QGroupBox("Profile Search (Ollama)")
        box.setToolTip(
            "Searches the web for a real published reflow/rework profile for a board you name, "
            "then uses a local Ollama model to pull numbers out of whatever real page text is "
            "found - it never asks the model to invent a profile from its own knowledge. Every "
            "result is tagged with its source page and never auto-loaded, auto-saved, or "
            "auto-sent - you review it in the Profile Editor like any other profile."
        )
        layout = QVBoxLayout(box)

        host_row = QHBoxLayout()
        host_row.addWidget(QLabel("Ollama host:"))
        self.ollama_host_edit = QLineEdit(self.ollama_config.host)
        self.ollama_host_edit.setPlaceholderText("http://192.168.1.50:41434")
        host_row.addWidget(self.ollama_host_edit)
        layout.addLayout(host_row)

        model_row = QHBoxLayout()
        model_row.addWidget(QLabel("Model:"))
        self.ollama_model_combo = QComboBox()
        self.ollama_model_combo.setEditable(True)
        if self.ollama_config.model:
            self.ollama_model_combo.addItem(self.ollama_config.model)
            self.ollama_model_combo.setCurrentText(self.ollama_config.model)
        model_row.addWidget(self.ollama_model_combo, stretch=1)
        btn_refresh_models = QPushButton("Refresh models")
        btn_refresh_models.clicked.connect(self._on_refresh_ollama_models)
        model_row.addWidget(btn_refresh_models)
        layout.addLayout(model_row)

        self.ollama_enabled_check = QCheckBox("Enabled")
        self.ollama_enabled_check.setChecked(self.ollama_config.enabled)
        layout.addWidget(self.ollama_enabled_check)

        btn_save_ollama = QPushButton("Save")
        btn_save_ollama.clicked.connect(self._on_save_ollama_config)
        layout.addWidget(btn_save_ollama)

        return box

    def _on_refresh_ollama_models(self) -> None:
        host = self.ollama_host_edit.text().strip()
        if not host:
            QMessageBox.warning(self, "Refresh models", "Enter an Ollama host URL first.")
            return
        models, error = profile_search.list_ollama_models(host)
        if error:
            QMessageBox.critical(self, "Refresh models", error)
            return
        current = self.ollama_model_combo.currentText()
        self.ollama_model_combo.clear()
        self.ollama_model_combo.addItems(models)
        if current in models:
            self.ollama_model_combo.setCurrentText(current)
        QMessageBox.information(self, "Refresh models", f"Found {len(models)} model(s).")

    def _on_save_ollama_config(self) -> None:
        self.ollama_config = profile_search.OllamaConfig(
            host=self.ollama_host_edit.text().strip(),
            model=self.ollama_model_combo.currentText().strip(),
            enabled=self.ollama_enabled_check.isChecked(),
        )
        profile_search.save_ollama_config(self.ollama_config)
        QMessageBox.information(self, "Saved", "Ollama settings saved.")

    def _build_webhook_box(self) -> QWidget:
        box = QGroupBox("Webhook Notifications")
        box.setToolTip(
            "Posts a message to a webhook URL you provide (Discord, Slack, or any custom relay) "
            "on run started/completed/failed and critical faults. No credentials are ever "
            "entered or stored here beyond the URL itself."
        )
        layout = QVBoxLayout(box)

        url_row = QHBoxLayout()
        url_row.addWidget(QLabel("Webhook URL:"))
        self.webhook_url_edit = QLineEdit(self.webhook_config.url)
        url_row.addWidget(self.webhook_url_edit)
        layout.addLayout(url_row)

        self.webhook_enabled_check = QCheckBox("Enabled")
        self.webhook_enabled_check.setChecked(self.webhook_config.enabled)
        layout.addWidget(self.webhook_enabled_check)

        self._webhook_event_checks: dict[str, QCheckBox] = {}
        events_row = QHBoxLayout()
        for event_type, label in [
            ("run_started", "Run started"), ("run_completed", "Run completed"),
            ("run_failed", "Run failed"), ("critical_fault", "Critical fault"),
        ]:
            chk = QCheckBox(label)
            chk.setChecked(self.webhook_config.events.get(event_type, True))
            self._webhook_event_checks[event_type] = chk
            events_row.addWidget(chk)
        layout.addLayout(events_row)

        btn_row = QHBoxLayout()
        btn_save = QPushButton("Save")
        btn_save.clicked.connect(self._on_webhook_save)
        btn_test = QPushButton("Test webhook")
        btn_test.clicked.connect(self._on_webhook_test)
        btn_row.addWidget(btn_save)
        btn_row.addWidget(btn_test)
        btn_row.addStretch()
        layout.addLayout(btn_row)

        return box

    def _read_webhook_form(self) -> notifications.WebhookConfig:
        return notifications.WebhookConfig(
            url=self.webhook_url_edit.text().strip(),
            enabled=self.webhook_enabled_check.isChecked(),
            events={name: chk.isChecked() for name, chk in self._webhook_event_checks.items()},
        )

    def _on_webhook_save(self) -> None:
        self.webhook_config = self._read_webhook_form()
        notifications.save_webhook_config(self.webhook_config)
        QMessageBox.information(self, "Saved", "Webhook settings saved.")

    def _on_webhook_test(self) -> None:
        config = self._read_webhook_form()
        # Test bypasses the per-event enabled checkboxes (a user testing a brand-new webhook
        # hasn't necessarily ticked any event box yet) but still respects the Enabled toggle and
        # requires a URL - those aren't a "which event" question, they're "is this on at all".
        if not config.url:
            QMessageBox.warning(self, "Test webhook", "Enter a webhook URL first.")
            return
        test_config = notifications.WebhookConfig(url=config.url, enabled=True, events={"test": True})
        ok, detail = notifications.send_notification(test_config, "test", "LY-R690 Control: test notification.")
        if ok:
            QMessageBox.information(self, "Test webhook", f"Success: {detail}")
        else:
            QMessageBox.critical(self, "Test webhook", f"Failed: {detail}")

    def _build_advanced_panel(self) -> QWidget:
        # Three columns instead of one long vertical stack - the PID table is naturally wide but
        # short (7 rows), and the other sections are each short too, so stacking everything
        # vertically needed far more height than the splitter gave this tab, forcing a scroll to
        # see everything at once. Side by side fits within one screen instead. Fan Timing and
        # Temperature Compensation share a column since they're the same read/poll on the wire
        # (registers 172-181 in one request) and sit next to each other on the real screen too.
        panel = QWidget()
        layout = QHBoxLayout(panel)
        layout.addWidget(self._build_pid_box(), stretch=3)

        middle_col = QWidget()
        middle_layout = QVBoxLayout(middle_col)
        middle_layout.setContentsMargins(0, 0, 0, 0)
        middle_layout.addWidget(self._build_fan_timing_box())
        middle_layout.addWidget(self._build_temp_comp_box())
        middle_layout.addWidget(self._build_ir_reading_correction_box())
        middle_layout.addStretch()
        layout.addWidget(middle_col, stretch=2)

        right_col = QWidget()
        right_layout = QVBoxLayout(right_col)
        right_layout.setContentsMargins(0, 0, 0, 0)
        right_layout.addWidget(self._build_selftune_box())
        right_layout.addWidget(self._build_comm_diagnostics_box())
        right_layout.addWidget(self._build_register_viewer_box())
        right_layout.addStretch()
        layout.addWidget(right_col, stretch=2)

        return panel

    def _build_pid_box(self) -> QWidget:
        pid_box = QGroupBox("PID Gains")
        pid_layout = QVBoxLayout(pid_box)
        pid_toolbar = QHBoxLayout()
        self.btn_pid_read = QPushButton("Read from machine")
        self.btn_pid_read.clicked.connect(self._on_pid_read)
        self.btn_pid_send = QPushButton("Send to machine")
        self.btn_pid_send.setStyleSheet("background: #c60; color: white;")
        self.btn_pid_send.clicked.connect(self._on_pid_send)
        btn_pid_history = QPushButton("History...")
        btn_pid_history.setToolTip("Every PID gain sent to the machine is logged here (reads aren't, since they don't change anything).")
        btn_pid_history.clicked.connect(self._on_view_pid_history)
        # Save as known-good (2026-09-08) - same action as the Maintenance tab's "Save current as
        # known-good" button (see _on_save_known_good_config), just also reachable right here,
        # since this is where an operator naturally is after pulling PID values from the machine.
        # Actively re-reads PID + fan-timing/temp-comp itself (doesn't just reuse whatever's
        # currently shown in this table) - same as the Maintenance tab's button.
        btn_pid_save_known_good = QPushButton("Save as known-good config")
        btn_pid_save_known_good.setToolTip(
            "Saves the machine's CURRENT PID gains, fan/vacuum timing, and temperature "
            "compensation (re-read fresh from the board, not just what's shown in this table) as "
            "the known-good reference used for configuration drift detection (see the "
            "Maintenance tab)."
        )
        btn_pid_save_known_good.clicked.connect(self._on_save_known_good_config)
        pid_toolbar.addWidget(self.btn_pid_read)
        pid_toolbar.addWidget(self.btn_pid_send)
        pid_toolbar.addWidget(btn_pid_history)
        pid_toolbar.addWidget(btn_pid_save_known_good)
        pid_toolbar.addStretch()
        pid_layout.addLayout(pid_toolbar)
        self.lbl_pid_status = QLabel("PID data: none observed yet")
        self.lbl_pid_status.setStyleSheet("color: #aaa; font-style: italic;")
        pid_layout.addWidget(self.lbl_pid_status)

        self.pid_table = QTableWidget(len(PID_ROW_LABELS), PID_SEGMENTS)
        self.pid_table.setVerticalHeaderLabels(PID_ROW_LABELS)
        self.pid_table.setHorizontalHeaderLabels([f"Segment {i+1}" for i in range(PID_SEGMENTS)])
        self.pid_table.verticalHeader().setDefaultSectionSize(24)
        self.pid_table.setMinimumHeight(24 * (len(PID_ROW_LABELS) + 1) + 30)
        for row in range(len(PID_ROW_LABELS)):
            for col in range(PID_SEGMENTS):
                self.pid_table.setItem(row, col, QTableWidgetItem("0"))
        pid_layout.addWidget(self.pid_table)

        ir_row = QHBoxLayout()
        ir_row.addWidget(QLabel("IR P:"))
        self.pid_ir_p = QLineEdit("0")
        ir_row.addWidget(self.pid_ir_p)
        ir_row.addWidget(QLabel("IR I:"))
        self.pid_ir_i = QLineEdit("0")
        ir_row.addWidget(self.pid_ir_i)
        ir_row.addWidget(QLabel("IR D:"))
        self.pid_ir_d = QLineEdit("0")
        ir_row.addWidget(self.pid_ir_d)
        pid_layout.addLayout(ir_row)
        return pid_box

    def _build_fan_timing_box(self) -> QWidget:
        fan_box = QGroupBox("Fan / Vacuum Timing")
        fan_layout = QVBoxLayout(fan_box)
        fan_toolbar = QHBoxLayout()
        self.btn_fan_read = QPushButton("Read from machine")
        self.btn_fan_read.clicked.connect(self._on_fan_read)
        self.btn_fan_send = QPushButton("Send to machine")
        self.btn_fan_send.setStyleSheet("background: #c60; color: white;")
        self.btn_fan_send.clicked.connect(self._on_fan_send)
        fan_toolbar.addWidget(self.btn_fan_read)
        fan_toolbar.addWidget(self.btn_fan_send)
        fan_toolbar.addStretch()
        fan_layout.addLayout(fan_toolbar)
        self.lbl_fan_status = QLabel("Fan timing data: none observed yet")
        self.lbl_fan_status.setStyleSheet("color: #aaa; font-style: italic;")
        self.lbl_fan_status.setWordWrap(True)
        fan_layout.addWidget(self.lbl_fan_status)

        fan_fields = QGridLayout()
        fields = [
            ("Vacuum time (s):", "fan_vacuum_time"),
            ("Fan stop temp (C):", "fan_stop_temp"),
            ("Fan stop delay (s):", "fan_stop_delay"),
            ("Advance warning (s):", "fan_advance_warning"),
        ]
        for i, (label, attr) in enumerate(fields):
            fan_fields.addWidget(QLabel(label), i, 0)
            edit = QLineEdit("0")
            setattr(self, attr, edit)
            fan_fields.addWidget(edit, i, 1)
        fan_layout.addLayout(fan_fields)
        return fan_box

    def _build_temp_comp_box(self) -> QWidget:
        # Upper (176) and Lower (177) CONFIRMED live 2026-09-07 - see modbus_client.ADDR_TEMP_COMP_*
        # for the actual before/after capture, no cap found up to double-digit test values. IR
        # (178) is ALSO confirmed real and writable (a live edit of 5.5 held steady, and directly
        # shifted the live IR reading by the same amount when toggled 0/10 - confirmed feeding the
        # actual reading, not cosmetic) - but it's hard-capped at +10C: 10 holds, 11+ reverts to 0
        # within seconds every time. Initial suspicion was IR being fought by its own
        # auto-calibration routine or being the wrong register entirely - both ruled out live by
        # this same testing. Probe (179) remains unconfirmed. Since the observed live-IR-vs-
        # profile-target gap runs ~20-24C, larger than IR's +10C ceiling here, see
        # _build_ir_reading_correction_box() for the software-side top-up that covers the rest.
        box = QGroupBox("Temperature Compensation")
        box.setToolTip(
            "Registers 176-179. Upper (176) and Lower (177) are CONFIRMED, no cap found. IR (178) "
            "is CONFIRMED real and working - directly shifts the live IR reading - but hard-capped "
            "at +10C by the machine itself (11+ reverts to 0 within seconds, confirmed live, "
            "repeatedly). Set IR here to 10 for the maximum the hardware allows, then use the IR "
            "Reading Correction box below for the remainder. Probe (179) remains unconfirmed."
        )
        layout = QVBoxLayout(box)
        toolbar = QHBoxLayout()
        self.btn_tempcomp_read = QPushButton("Read from machine")
        self.btn_tempcomp_read.clicked.connect(self._on_temp_comp_read)
        self.btn_tempcomp_send = QPushButton("Send to machine")
        self.btn_tempcomp_send.setStyleSheet("background: #c60; color: white;")
        self.btn_tempcomp_send.clicked.connect(self._on_temp_comp_send)
        toolbar.addWidget(self.btn_tempcomp_read)
        toolbar.addWidget(self.btn_tempcomp_send)
        toolbar.addStretch()
        layout.addLayout(toolbar)

        fields = QGridLayout()
        for i, (label, attr) in enumerate([
            ("Upper (C):", "tempcomp_upper"), ("Lower (C):", "tempcomp_lower"),
            ("IR (C):", "tempcomp_ir"), ("Probe (C):", "tempcomp_probe"),
        ]):
            fields.addWidget(QLabel(label), i, 0)
            edit = QLineEdit("0.0")
            setattr(self, attr, edit)
            fields.addWidget(edit, i, 1)
        layout.addLayout(fields)

        btn_calibration = QPushButton("Calibration wizard...")
        btn_calibration.setToolTip(
            "Records (reference instrument, machine reading) pairs and computes the offset - "
            "informational only, does NOT automatically write to the compensation fields above."
        )
        btn_calibration.clicked.connect(self._on_open_calibration_wizard)
        layout.addWidget(btn_calibration)
        return box

    def _build_ir_reading_correction_box(self) -> QWidget:
        """Software-side top-up for the part of the confirmed live-IR-vs-profile-target gap that
        the hardware Temperature Compensation register can't reach (it's hard-capped at +10C - see
        _build_temp_comp_box()). Applied in MachineBus.get_live_reading(), the one place every
        consumer of a live reading (display, logging, alerts, safety checks) reads through - not
        written to any machine register, purely a correction this app applies to what it reads."""
        box = QGroupBox("IR Reading Correction (software)")
        box.setToolTip(
            "Added to every live IR reading before this app displays, logs, or safety-checks it - "
            "never written to the machine. Set the hardware Temperature Compensation IR field "
            "above to its max (+10C) FIRST, then use this for whatever's left of your observed "
            "gap between live IR and the profile's own IR targets (confirmed live 2026-09-07: "
            "roughly 20-24C total before any correction, so with +10C from hardware this defaults "
            "to an estimated remainder - tune it to what you actually observe on a fresh run, not "
            "this default)."
        )
        layout = QHBoxLayout(box)
        layout.addWidget(QLabel("Correction (C):"))
        self.ir_correction_spin = QDoubleSpinBox()
        self.ir_correction_spin.setRange(-50.0, 50.0)
        self.ir_correction_spin.setSingleStep(1.0)
        self.ir_correction_spin.setDecimals(1)
        self.ir_correction_spin.setValue(self.safety_limits.ir_reading_correction_c)
        self.ir_correction_spin.valueChanged.connect(self._on_ir_reading_correction_changed)
        layout.addWidget(self.ir_correction_spin)
        layout.addStretch()
        return box

    def _on_ir_reading_correction_changed(self, value: float) -> None:
        old_values = asdict(self.safety_limits)
        self.safety_limits.ir_reading_correction_c = value
        self.bus.set_ir_reading_correction(value)
        save_safety_limits(self.safety_limits)
        config_change_history.record_config_change(old_values, asdict(self.safety_limits))

    def _build_selftune_box(self) -> QWidget:
        tune_box = QGroupBox("Self-Tuning (PID Autotune)")
        tune_layout = QVBoxLayout(tune_box)
        warn_lbl = QLabel(
            "Starts a real autotune routine that perturbs the actual heating elements to "
            "characterize their response. This is a genuine physical action, not a settings change."
        )
        warn_lbl.setStyleSheet("color: #ec5; font-size: 11px;")
        warn_lbl.setWordWrap(True)
        tune_layout.addWidget(warn_lbl)
        for label, zone, coil in (
            ("Upper self-tune", "Upper", COIL_SELFTUNE_UPPER),
            ("Lower self-tune", "Lower", COIL_SELFTUNE_LOWER),
            ("IR self-tune", "IR", COIL_SELFTUNE_IR),
        ):
            btn = QPushButton(label)
            btn.setStyleSheet("background: #c60; color: white;")
            btn.clicked.connect(lambda checked=False, z=zone, c=coil: self._on_selftune_clicked(z, c))
            tune_layout.addWidget(btn)
        return tune_box

    def _build_comm_diagnostics_box(self) -> QWidget:
        box = QGroupBox("Communication Diagnostics")
        box.setToolTip(
            "Read/write attempt, success, failure, and retry counts observed on the bus since "
            "the app started (or since last reset) - purely observational, does not change how "
            "reads or writes are attempted or retried."
        )
        layout = QVBoxLayout(box)
        self.lbl_comm_diag = QLabel("No data yet.")
        self.lbl_comm_diag.setWordWrap(True)
        layout.addWidget(self.lbl_comm_diag)
        btn_reset = QPushButton("Reset counters")
        btn_reset.clicked.connect(self._on_reset_comm_diagnostics)
        layout.addWidget(btn_reset)
        return box

    def _update_comm_diagnostics(self) -> None:
        diag = self.bus.get_diagnostics()
        read_total = diag["read_attempts"]
        write_total = diag["write_attempts"]
        read_rate = (diag["read_successes"] / read_total * 100) if read_total else 0.0
        write_rate = (diag["write_successes"] / write_total * 100) if write_total else 0.0
        avg_rt = diag["avg_response_time_s"]
        avg_rt_text = f"{avg_rt * 1000:.0f} ms" if avg_rt is not None else "n/a"
        self.lbl_comm_diag.setText(
            f"Reads: {diag['read_successes']}/{read_total} ok ({read_rate:.0f}%)\n"
            f"Writes: {diag['write_successes']}/{write_total} ok ({write_rate:.0f}%)\n"
            f"Retries: {diag['retry_count']}\n"
            f"Avg response time: {avg_rt_text} (last {diag['response_time_sample_count']} sample(s))"
        )

    def _on_reset_comm_diagnostics(self) -> None:
        self.bus.reset_diagnostics()
        self._update_comm_diagnostics()

    def _build_register_viewer_box(self) -> QWidget:
        """Raw register/coil viewer - 2026-09-08, external feature-gap review, second pass. See
        register_viewer.py's module docstring for why this is deliberately READ-ONLY: no write
        path exists here at all."""
        box = QGroupBox("Register / Coil Viewer")
        box.setToolTip(
            "Read-only. For looking at raw wire-level data directly - registers/coils this app "
            "doesn't already have a named, understood reading for. Never writes anything."
        )
        layout = QVBoxLayout(box)
        row = QHBoxLayout()
        self.reg_viewer_func_combo = QComboBox()
        self.reg_viewer_func_combo.addItem("Holding Registers (FC3)", userData=3)
        self.reg_viewer_func_combo.addItem("Coils (FC1)", userData=1)
        row.addWidget(self.reg_viewer_func_combo)
        row.addWidget(QLabel("Start:"))
        self.reg_viewer_start_spin = QSpinBox()
        self.reg_viewer_start_spin.setRange(0, 9999)
        row.addWidget(self.reg_viewer_start_spin)
        row.addWidget(QLabel("Count:"))
        self.reg_viewer_count_spin = QSpinBox()
        self.reg_viewer_count_spin.setRange(1, 125)
        self.reg_viewer_count_spin.setValue(10)
        row.addWidget(self.reg_viewer_count_spin)
        btn_read = QPushButton("Read")
        btn_read.clicked.connect(self._on_read_registers)
        row.addWidget(btn_read)
        layout.addLayout(row)
        self.lbl_reg_viewer_status = QLabel("")
        layout.addWidget(self.lbl_reg_viewer_status)
        self.reg_viewer_table = QTableWidget(0, 3)
        self.reg_viewer_table.setHorizontalHeaderLabels(["Address", "Value", "Hex"])
        self.reg_viewer_table.setEditTriggers(QTableWidget.NoEditTriggers)
        self.reg_viewer_table.setMaximumHeight(200)
        layout.addWidget(self.reg_viewer_table)
        return box

    def _on_read_registers(self) -> None:
        func_code = self.reg_viewer_func_combo.currentData()
        start_address = self.reg_viewer_start_spin.value()
        count = self.reg_viewer_count_spin.value()
        error = validate_register_read_request(func_code, start_address, count)
        if error is not None:
            self.lbl_reg_viewer_status.setText(error)
            return

        def do_read():
            on_progress = self._read_progress_reporter(btn, "registers")
            return self.bus.read_raw_active(func_code, start_address, count, on_progress=on_progress)

        def done(values):
            self._mark_idle(btn)
            btn.setText(original_text)
            self.reg_viewer_table.setRowCount(0)
            if values is None:
                self.lbl_reg_viewer_status.setText("No response from the board after repeated attempts.")
                return
            rows = format_register_rows(func_code, start_address, values)
            self.lbl_reg_viewer_status.setText(f"{len(rows)} value(s) read.")
            self.reg_viewer_table.setRowCount(len(rows))
            for r, row in enumerate(rows):
                self.reg_viewer_table.setItem(r, 0, QTableWidgetItem(str(row["address"])))
                self.reg_viewer_table.setItem(r, 1, QTableWidgetItem(str(row["value"])))
                self.reg_viewer_table.setItem(r, 2, QTableWidgetItem(row.get("hex", "")))

        btn = self.sender()
        original_text = btn.text()
        self._mark_busy(btn)
        btn.setText("Reading...")
        self._run_write(do_read, done)

    # ---------------- PID / fan-timing table <-> value conversion ----------------

    def _populate_pid_table(self, regs: list[int]) -> None:
        for row, base_addr in enumerate(PID_ROW_ADDRS):
            for seg in range(PID_SEGMENTS):
                value = regs[base_addr - ADDR_PID_BASE + seg]
                self.pid_table.setItem(row, seg, QTableWidgetItem(str(value)))
        self.pid_ir_p.setText(str(regs[ADDR_PID_IR_P - ADDR_PID_BASE]))
        self.pid_ir_i.setText(str(regs[ADDR_PID_IR_I - ADDR_PID_BASE]))
        self.pid_ir_d.setText(str(regs[ADDR_PID_IR_D - ADDR_PID_BASE]))

    def _read_pid_table(self) -> list[int]:
        regs = [0] * (ADDR_PID_IR_D - ADDR_PID_BASE + 1)
        for row, base_addr in enumerate(PID_ROW_ADDRS):
            for seg in range(PID_SEGMENTS):
                item = self.pid_table.item(row, seg)
                try:
                    value = int(float(item.text())) if item else 0
                except ValueError:
                    value = 0
                regs[base_addr - ADDR_PID_BASE + seg] = value

        def parse(edit: QLineEdit) -> int:
            try:
                return int(float(edit.text()))
            except ValueError:
                return 0

        regs[ADDR_PID_IR_P - ADDR_PID_BASE] = parse(self.pid_ir_p)
        regs[ADDR_PID_IR_I - ADDR_PID_BASE] = parse(self.pid_ir_i)
        regs[ADDR_PID_IR_D - ADDR_PID_BASE] = parse(self.pid_ir_d)
        return regs

    def _populate_fan_timing(self, regs: list[int]) -> None:
        self.fan_vacuum_time.setText(str(regs[0]))
        self.fan_stop_temp.setText(str(regs[1] / 10.0))
        self.fan_stop_delay.setText(str(regs[2]))
        self.fan_advance_warning.setText(str(regs[3]))

    def _read_fan_timing(self) -> list[int]:
        def parse_int(edit: QLineEdit) -> int:
            try:
                return int(float(edit.text()))
            except ValueError:
                return 0

        def parse_float(edit: QLineEdit) -> float:
            try:
                return float(edit.text())
            except ValueError:
                return 0.0

        return [
            parse_int(self.fan_vacuum_time),
            round(parse_float(self.fan_stop_temp) * 10),
            parse_int(self.fan_stop_delay),
            parse_int(self.fan_advance_warning),
        ]

    def _populate_temp_comp(self, regs: list[int]) -> None:
        edits = [self.tempcomp_upper, self.tempcomp_lower, self.tempcomp_ir, self.tempcomp_probe]
        for edit, raw in zip(edits, regs):
            edit.setText(str(to_signed16(raw) / 10.0))

    def _read_temp_comp(self) -> list[int]:
        edits = [self.tempcomp_upper, self.tempcomp_lower, self.tempcomp_ir, self.tempcomp_probe]
        values = []
        for edit in edits:
            try:
                display = float(edit.text())
            except ValueError:
                display = 0.0
            values.append(to_unsigned16(round(display * 10)))
        return values

    # ---------------- profile table <-> Profile conversion ----------------

    def _populate_table(self, profile: Profile) -> None:
        # Curve/profile registers (100-171) are RAW integers, not the x10 fixed-point scale used
        # elsewhere in this app (PID, temp compensation, live readings) - confirmed ground truth
        # in docs/06-modbus-register-map.md ("Curve / profile data" Verification: a live touchscreen
        # edit of 130->180 landed at address 100 as literally 180, not 1800). TEMP_ROWS x10 scaling
        # here was wrong: sending e.g. 1200 instead of 120 for a target temperature almost
        # certainly failed a sanity check on the board and got silently reset to 0 - confirmed live
        # 2026-08-19, all three Temper rows (the only rows this scaling touched) came back as 0 on
        # the touchscreen's Current Curve screen after a "Sent and confirmed" write, while every
        # other row (Rate, Holding time - never scaled) landed correctly.
        self.table.blockSignals(True)
        for row, attr in enumerate(ROW_ATTRS):
            for col in range(CURVE_SECTIONS):
                raw = getattr(profile.sections[col], attr)
                item = QTableWidgetItem(str(raw))
                self.table.setItem(row, col, item)
        self.table.blockSignals(False)
        self._update_profile_chart()

    def _read_table(self) -> Profile:
        profile = Profile(name=self.profile_combo.currentText() or "Untitled")
        for row, attr in enumerate(ROW_ATTRS):
            for col in range(CURVE_SECTIONS):
                item = self.table.item(row, col)
                try:
                    value = float(item.text()) if item else 0
                except ValueError:
                    value = 0
                setattr(profile.sections[col], attr, int(round(value)))
        return profile

    def _update_profile_chart(self) -> None:
        """Redraws the graphical curve view + simulation summary from the table's CURRENT
        contents - called after every programmatic populate (_populate_table) and every manual
        cell edit (table.itemChanged), so the two never drift apart. Guards on hasattr since this
        can fire (via _populate_table's initial call) before the chart widgets and wetting_spin
        exist yet - see _build_profile_editor's construction order."""
        if not hasattr(self, "profile_plot"):
            return
        profile = self._read_table()
        xs = list(range(1, CURVE_SECTIONS + 1))
        self.profile_curve_upper.setData(xs, [s.upper_temp for s in profile.sections])
        self.profile_curve_lower.setData(xs, [s.lower_temp for s in profile.sections])
        self.profile_curve_ir.setData(xs, [s.ir_temp for s in profile.sections])
        wetting_temp = self.wetting_spin.value() if hasattr(self, "wetting_spin") else 217.0
        self.profile_wetting_line.setValue(wetting_temp)

        sim = simulate_profile(profile, wetting_temp)
        mins, secs = divmod(sim.total_hold_time_s, 60)
        wet_mins, wet_secs = divmod(sim.time_above_wetting_s, 60)
        self.lbl_profile_sim.setText(
            f"Simulation (target values only, not a physical prediction - see profile_simulation.py): "
            f"total hold time {mins:.0f}:{secs:02.0f} (excludes ramp time between sections) | "
            f"peak Upper {sim.peak_upper_c:.0f}C, Lower {sim.peak_lower_c:.0f}C, IR {sim.peak_ir_c:.0f}C | "
            f"time with Upper target >= wetting ({wetting_temp:.0f}C): {wet_mins:.0f}:{wet_secs:02.0f}"
        )

    # ---------------- actions ----------------

    def _connect(self, com_port: str, show_error: bool = False) -> None:
        """Starts self.bus and updates the connection status label either way. Shared by the
        initial startup attempt and _on_reconnect_clicked - as a packaged --windowed exe there is
        no console to show a failure's traceback on, so it must be surfaced in the GUI or a bad
        COM port just looks like the app silently doing nothing."""
        try:
            self.bus.start()
        except Exception as exc:
            self.lbl_connection.setText(f"Not connected ({com_port})")
            self.lbl_connection.setStyleSheet("color: #d55; font-weight: bold;")
            if show_error:
                QMessageBox.critical(
                    self, "Connection Failed",
                    f"Could not open serial port '{com_port}':\n{exc}\n\n"
                    "Check that the RS485 adapter is connected and the COM port is correct, "
                    "then try Reconnect."
                )
            return
        self.lbl_connection.setText(f"Connected: {com_port}")
        self.lbl_connection.setStyleSheet("color: #6a6; font-weight: bold;")
        # USB adapter persistent identity (2026-09-08, external feature-gap review, second pass) -
        # see connection_identity.py's module docstring. A no-op if this port's hardware identity
        # can't be found (e.g. a virtual/test port with no real USB descriptor).
        for p in list_available_ports():
            if p.device == com_port:
                remember_connection(p)
                break

    def _on_reconnect_clicked(self) -> None:
        """Lets the user pick a different COM port, or retry the same one after fixing a
        physical connection, without closing and relaunching the whole app. A fresh MachineBus
        is required (not just changing the port on the existing one) - it owns per-connection
        state (the open serial handle, background threads) that isn't safe to reuse across a
        port change. self.bus.stop() is safe to call even if the previous connection attempt
        never actually succeeded."""
        new_port = self.com_port_edit.text().strip() or "COM4"
        self.bus.stop()
        self.bus = MachineBus(new_port)
        self.bus.set_ir_reading_correction(self.safety_limits.ir_reading_correction_c)
        self._connect(new_port, show_error=True)

    def _on_refresh_ports(self) -> None:
        # Remembered-adapter tooltip (2026-09-08, external feature-gap review, second pass) - see
        # connection_identity.py's module docstring. Purely informational here (the desktop app's
        # port picker is already manual/visible, unlike the headless Pi's auto-resolve-on-startup)
        # so a hover tooltip is enough, not a dedicated UI row.
        identity = load_connection_identity()
        if identity is None:
            self.com_port_combo.setToolTip("No remembered adapter yet - one is recorded automatically on first successful connection.")
        else:
            resolved = resolve_remembered_port()
            self.com_port_combo.setToolTip(
                f"Remembered adapter: {identity.description or 'unknown device'} "
                f"(serial {identity.serial_number or 'none'}), last used on {identity.last_known_port}. "
                f"Currently resolves to: {resolved or 'not found - check it is plugged in'}."
            )
        current_text = self.com_port_combo.currentText()
        ports = list_available_ports()
        self.com_port_combo.blockSignals(True)
        self.com_port_combo.clear()
        for p in ports:
            label = f"{p.device} - {p.description}" if p.description else p.device
            self.com_port_combo.addItem(label, userData=p.device)
        self.com_port_combo.setEditText(current_text)
        self.com_port_combo.blockSignals(False)

    def _on_test_port(self) -> None:
        """Uses the real MachineBus/active-read path (test_connection), not a bare "did the port
        open" check - a port opening successfully only proves a serial device exists there, not
        that it's this machine. Runs on a background thread since it can take a few seconds
        (same reasoning as _run_write) and never touches self.bus, so it can't disturb whatever
        connection the app already has."""
        port = self.com_port_edit.text().strip()
        if not port:
            return
        sender = self.sender()
        sender.setEnabled(False)
        original_text = sender.text()
        sender.setText("Testing...")

        def done(result):
            sender.setEnabled(True)
            sender.setText(original_text)
            ok, detail = result
            (QMessageBox.information if ok else QMessageBox.warning)(self, "Port Test", detail)

        self._run_write(lambda: test_connection(port), done)

    def _run_write(self, fn, on_done) -> None:
        """Runs a blocking MachineBus write (fn) on a background thread instead of the GUI
        thread. These can legitimately take a long time - a coil write retries up to 20 times,
        each waiting on a multi-second quiet-gap timeout, and a curve send does that per chunk
        across 9 chunks. Running that directly in a button's clicked handler froze the whole
        window for the duration; confirmed live 2026-08-19 - a slow Vacuum write made the app
        look hung and it was force-closed as unresponsive, leaving no way to tell whether the
        write had actually succeeded. on_done(result) runs back on the GUI thread."""
        def worker():
            try:
                result = fn()
            except Exception as exc:
                # Same reasoning as the bus.start() guard above: no console exists to catch this
                # in a packaged --windowed exe, so an uncaught exception here would previously
                # have left the triggering button silently disabled forever with zero feedback.
                # Review finding #12 (2026-11-12): Python deletes `exc` when this handler ends, so the old
                # lambda raised NameError when the GUI thread finally ran it - the error box never showed and
                # on_done never ran (button stuck busy). Format the message NOW, and always run on_done.
                message = f"Machine communication failed unexpectedly:\n{type(exc).__name__}: {exc}"

                def _report_failure(_, message=message):
                    try:
                        QMessageBox.critical(self, "Error", message)
                    finally:
                        try:
                            from machine_bus import WriteResult as _WR
                            on_done(_WR(ok=False, attempts=0, detail=message, independently_verified=False))
                        except Exception:
                            logging.getLogger(__name__).exception("on_done after a failed write also failed")

                self._write_done.emit(_report_failure, None)
                return
            self._write_done.emit(on_done, result)
        threading.Thread(target=worker, daemon=True).start()

    def _chunk_progress_reporter(self, button: QPushButton, label: str):
        """Returns an on_progress(confirmed_count, total, pass_num) callback for
        write_registers() that updates `button`'s text live as chunks get confirmed - see
        _write_progress and _send_chunked_registers' multi-pass retry. Called from the
        background write thread; marshaled to the GUI thread the same way _run_write's result
        is."""
        def on_progress(confirmed_count: int, total: int, pass_num: int) -> None:
            self._write_progress.emit(
                lambda c, t, p: button.setText(f"Sending {label}... {c}/{t} confirmed" + (f" (pass {p})" if p > 1 else "")),
                confirmed_count, total, pass_num,
            )
        return on_progress

    def _read_progress_reporter(self, button: QPushButton, label: str):
        """Returns an on_progress(attempt, max_attempts) callback for active reads that updates
        `button`'s text live - same GUI-thread marshaling as _chunk_progress_reporter, just for
        read attempts instead of write chunks. Without this a slow read under real bus contention
        (confirmed live 2026-08-22: PID/fan-timing reads sometimes taking tens of seconds) showed
        a static "Reading..." the whole time, indistinguishable from a genuine hang."""
        def on_progress(attempt: int, max_attempts: int) -> None:
            self._write_progress.emit(
                lambda a, m, _unused: button.setText(f"Reading {label}... (attempt {a}/{m})"),
                attempt, max_attempts, False,
            )
        return on_progress

    def _coil_progress_reporter(self, button: QPushButton, label: str):
        """Returns an on_progress(attempt, max_attempts, pass_num) callback for write_coil() that
        updates `button`'s text live - same GUI-thread marshaling as _chunk_progress_reporter/
        _read_progress_reporter. A heavily-contended coil write (Start/Stop/Vacuum/Cooling) can
        legitimately take 20-40+ seconds across multiple retry passes (confirmed live
        2026-08-22), which looked identical to a hang with a static button label the whole time."""
        def on_progress(attempt: int, max_attempts: int, pass_num: int) -> None:
            self._write_progress.emit(
                lambda a, m, p: button.setText(f"{label}... ({a}/{m}" + (f", pass {p})" if p > 1 else ")")),
                attempt, max_attempts, pass_num,
            )
        return on_progress

    def _refresh_profile_list(self) -> None:
        self.profile_combo.clear()
        self.profile_combo.addItems(profile_store.list_profiles())

    def _on_load_local(self) -> None:
        name = self.profile_combo.currentText()
        if not name:
            return
        try:
            profile = profile_store.load_profile(name)
        except FileNotFoundError:
            QMessageBox.warning(self, "Not found", f"No local profile named '{name}'.")
            return
        self.current_profile = profile
        self._populate_table(profile)

    def _on_save_local(self) -> None:
        profile = self._read_table()
        try:
            path = profile_store.save_profile(profile)
        except ValueError as exc:
            # 2026-09-08 audit finding - see profile_store.save_profile()'s own docstring: a
            # genuine filename collision with a DIFFERENT profile is now a real, surfaced error
            # instead of silently corrupting the other profile.
            QMessageBox.critical(self, "Save Failed", str(exc))
            return
        self._refresh_profile_list()
        QMessageBox.information(self, "Saved", f"Saved to {path} (version {profile_store.get_profile_version(profile.name)})")

    def _on_edit_metadata(self) -> None:
        """Recipe metadata (2026-09-07, external feature-gap review, extending the pre-existing
        process_type/solder/notes fields): board/component identify WHAT this profile is for,
        probe_placement documents WHERE the reference thermocouple goes (useful even for a single
        operator returning to a profile months later, not just a multi-technician shop), and
        status is a soft Draft/Tested/Approved reminder - see profile_validator.validate_profile's
        own docstring for why it's a non-blocking warning, never an error, and never fabricated
        for a profile that predates this field."""
        name = self.profile_combo.currentText()
        if not name:
            return
        existing = profile_store.get_profile_metadata(name)

        dialog = QDialog(self)
        dialog.setWindowTitle(f"Metadata - {name}")
        dialog.resize(420, 480)
        layout = QVBoxLayout(dialog)
        layout.addWidget(QLabel("Board/device:"))
        board_edit = QLineEdit(existing.get("board", ""))
        board_edit.setPlaceholderText("e.g. PS5, Xbox Series X, Switch")
        layout.addWidget(board_edit)
        layout.addWidget(QLabel("Component:"))
        component_edit = QLineEdit(existing.get("component", ""))
        component_edit.setPlaceholderText("e.g. APU, Southbridge, HDMI encoder")
        layout.addWidget(component_edit)
        layout.addWidget(QLabel("Process type:"))
        process_combo = QComboBox()
        process_combo.addItems(["", "Reflow", "Reball", "Rework", "Other"])
        process_combo.setCurrentText(existing.get("process_type", ""))
        layout.addWidget(process_combo)
        layout.addWidget(QLabel("Solder:"))
        solder_edit = QLineEdit(existing.get("solder", ""))
        layout.addWidget(solder_edit)
        layout.addWidget(QLabel("Probe placement (where the reference thermocouple goes):"))
        probe_placement_edit = QTextEdit(existing.get("probe_placement", ""))
        probe_placement_edit.setMaximumHeight(60)
        probe_placement_edit.setPlaceholderText("e.g. APU edge, Kapton + thermal compound, ~5mm from package")
        layout.addWidget(probe_placement_edit)
        layout.addWidget(QLabel("Status:"))
        status_combo = QComboBox()
        status_combo.addItems(["", "Draft", "Tested", "Approved"])
        status_combo.setToolTip(
            "Purely informational - 'Draft' shows a non-blocking reminder before sending/starting "
            "this profile, asking whether it's actually been validated. Update it yourself once "
            "you're confident; nothing here is enforced or auto-changed."
        )
        status_combo.setCurrentText(existing.get("status", ""))
        layout.addWidget(status_combo)
        # Profile qualification (2026-09-08, external feature-gap review, second pass) - purely
        # informational, same as Status itself - see profile_qualification.py.
        qualification = compute_qualification_status(name)
        qual_label = QLabel(
            (
                f"Qualified: {qualification.thermal_pass_count} thermal PASS run(s), "
                f"{qualification.repair_success_count} logged repair Success outcome(s)."
            ) if qualification.qualified else (
                f"Not yet qualified: {qualification.thermal_pass_count} thermal PASS run(s), "
                f"{qualification.repair_success_count} logged repair Success outcome(s) "
                f"(recommend >= {QUALIFICATION_THRESHOLD_RUNS} of either before marking Approved)."
            )
        )
        qual_label.setWordWrap(True)
        qual_label.setStyleSheet("color: #8ab; font-size: 11px;")
        layout.addWidget(qual_label)
        layout.addWidget(QLabel("Notes:"))
        notes_edit = QTextEdit(existing.get("notes", ""))
        layout.addWidget(notes_edit)

        # Process limits (2026-09-07, external feature-gap review, second pass) - see
        # process_limits.py's module docstring for why this is deliberately separate from
        # safety_limits.py. Every field optional/blank = "not specified", never a fabricated 0.
        existing_limits = existing.get("process_limits", {}) or {}
        limits_label = QLabel("Process limits (optional quality window - leave blank for none):")
        limits_label.setWordWrap(True)
        layout.addWidget(limits_label)
        limits_grid = QGridLayout()
        min_tal_edit = QLineEdit(self._format_optional(existing_limits.get("min_time_above_wetting_s")))
        min_tal_edit.setPlaceholderText("e.g. 45")
        limits_grid.addWidget(QLabel("Min time above wetting (s):"), 0, 0)
        limits_grid.addWidget(min_tal_edit, 0, 1)
        max_tal_edit = QLineEdit(self._format_optional(existing_limits.get("max_time_above_wetting_s")))
        max_tal_edit.setPlaceholderText("e.g. 90")
        limits_grid.addWidget(QLabel("Max time above wetting (s):"), 1, 0)
        limits_grid.addWidget(max_tal_edit, 1, 1)
        max_upper_edit = QLineEdit(self._format_optional(existing_limits.get("max_peak_upper_c")))
        max_upper_edit.setPlaceholderText("e.g. 235")
        limits_grid.addWidget(QLabel("Max Upper peak (C):"), 2, 0)
        limits_grid.addWidget(max_upper_edit, 2, 1)
        max_lower_edit = QLineEdit(self._format_optional(existing_limits.get("max_peak_lower_c")))
        limits_grid.addWidget(QLabel("Max Lower peak (C):"), 3, 0)
        limits_grid.addWidget(max_lower_edit, 3, 1)
        max_ir_edit = QLineEdit(self._format_optional(existing_limits.get("max_peak_ir_c")))
        limits_grid.addWidget(QLabel("Max Infrared peak (C):"), 4, 0)
        limits_grid.addWidget(max_ir_edit, 4, 1)
        layout.addLayout(limits_grid)

        buttons = QDialogButtonBox(QDialogButtonBox.Save | QDialogButtonBox.Cancel)
        buttons.accepted.connect(dialog.accept)
        buttons.rejected.connect(dialog.reject)
        layout.addWidget(buttons)

        if dialog.exec() == QDialog.Accepted:
            metadata = {
                "board": board_edit.text(),
                "component": component_edit.text(),
                "process_type": process_combo.currentText(),
                "solder": solder_edit.text(),
                "probe_placement": probe_placement_edit.toPlainText(),
                "status": status_combo.currentText(),
                "notes": notes_edit.toPlainText(),
                "process_limits": {
                    "min_time_above_wetting_s": self._parse_optional_float(min_tal_edit.text()),
                    "max_time_above_wetting_s": self._parse_optional_float(max_tal_edit.text()),
                    "max_peak_upper_c": self._parse_optional_float(max_upper_edit.text()),
                    "max_peak_lower_c": self._parse_optional_float(max_lower_edit.text()),
                    "max_peak_ir_c": self._parse_optional_float(max_ir_edit.text()),
                },
            }
            try:
                profile = profile_store.load_profile(name)
            except FileNotFoundError:
                profile = self._read_table()
            try:
                profile_store.save_profile(profile, metadata=metadata)
            except ValueError as exc:
                QMessageBox.critical(self, "Save Failed", str(exc))
                return
            QMessageBox.information(self, "Saved", "Metadata saved.")

    @staticmethod
    def _format_optional(value) -> str:
        return "" if value is None else str(value)

    @staticmethod
    def _parse_optional_float(text: str):
        text = text.strip()
        if not text:
            return None
        try:
            return float(text)
        except ValueError:
            return None

    def _on_view_history(self) -> None:
        name = self.profile_combo.currentText()
        if not name:
            return
        history = profile_store.get_profile_history(name)
        if not history:
            QMessageBox.information(self, "No history", f"No prior versions saved for '{name}' yet - every save from now on will be kept.")
            return

        dialog = QDialog(self)
        dialog.setWindowTitle(f"History - {name}")
        dialog.resize(400, 300)
        layout = QVBoxLayout(dialog)
        current_version = profile_store.get_profile_version(name)
        layout.addWidget(QLabel(f"Current version: {current_version}"))
        history_list = QListWidget()
        for entry in reversed(history):
            saved_at = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(entry["saved_at"]))
            item_text = f"Version {entry['version']} - saved {saved_at}"
            history_list.addItem(item_text)
            history_list.item(history_list.count() - 1).setData(Qt.UserRole, entry["version"])
        layout.addWidget(history_list)
        btn_restore = QPushButton("Restore selected version")
        layout.addWidget(btn_restore)

        def on_restore():
            item = history_list.currentItem()
            if item is None:
                return
            version = item.data(Qt.UserRole)
            reply = QMessageBox.question(
                dialog, "Confirm restore",
                f"Restore version {version} of '{name}'? This loads it into the table and saves "
                f"it as a new current version - it does not delete any history.",
                QMessageBox.Yes | QMessageBox.No, QMessageBox.No,
            )
            if reply != QMessageBox.Yes:
                return
            restored = profile_store.restore_profile_version(name, version)
            self._populate_table(restored)
            dialog.accept()

        btn_restore.clicked.connect(on_restore)
        dialog.exec()

    def _on_view_recommendations(self) -> None:
        profile = self._read_table()
        rec = recommend_for_profile(profile)
        if rec is None:
            QMessageBox.information(
                self, "Not enough data",
                f"Need at least 3 logged runs of a profile named '{profile.name}' to look for a "
                f"pattern (this is a real-run-count check, not the profile's own edit history).",
            )
            return
        QMessageBox.information(
            self, "Profile Recommendation",
            f"PROFILE ANALYSIS - '{rec.profile_name}'\n\n"
            f"Based on {rec.run_count} logged run(s).\n\n"
            f"Average peak Upper: {rec.avg_peak_upper:.1f}C\n"
            f"Target Upper: {rec.target_peak_upper:.0f}C\n\n"
            f"{rec.suggestion}",
        )

    def _on_search_profile_online(self) -> None:
        """Searches the web for a real published profile for a board and uses Ollama only to
        EXTRACT numbers from real page text - never to invent one from its own knowledge (see
        profile_search.py's module docstring). Never auto-saves or auto-sends anything - a
        selected candidate only populates the Profile Editor table, exactly like loading any
        other profile, so it goes through the same validate/preflight/confirm gates before it
        could ever reach the physical machine."""
        if not self.ollama_config.enabled or not self.ollama_config.model:
            QMessageBox.warning(
                self, "Profile Search not configured",
                "Set up an Ollama host and model, and check Enabled, in the 'Profile Search "
                "(Ollama)' box below (Profile & Alerts tab) first.",
            )
            return
        board_name, ok = QInputDialog.getText(self, "Search for Profile Online", "Board / component name:")
        if not ok or not board_name.strip():
            return
        board_name = board_name.strip()

        dialog = QDialog(self)
        dialog.setWindowTitle(f"Searching for '{board_name}'...")
        dialog.resize(600, 420)
        layout = QVBoxLayout(dialog)
        warn = QLabel(
            "Results are pulled from real webpages by a local AI model - they are NOT verified "
            "against this machine or any authoritative source. If nothing real is found, the AI "
            "will offer its own generated estimate instead, clearly marked - review every value "
            "in the table and chart before Save or Send, especially an AI estimate."
        )
        warn.setWordWrap(True)
        warn.setStyleSheet("color: #ec5;")
        layout.addWidget(warn)
        status_label = QLabel("Starting search...")
        status_label.setWordWrap(True)
        layout.addWidget(status_label)
        results_list = QListWidget()
        results_list.setToolTip("Double-click a result to review and optionally load it into the Profile Editor table.")
        layout.addWidget(results_list)
        btn_row = QHBoxLayout()
        btn_paste_url = QPushButton("Paste URL...")
        btn_paste_url.setToolTip(
            "Skip the automatic search - if you already found a candidate page yourself (in a "
            "real browser), paste its URL here and this reads and extracts from that page "
            "directly. No search API, no bot-detection risk, works even if DuckDuckGo above is "
            "currently blocking automatic search (added 2026-09-09 for exactly that reason)."
        )
        btn_row.addWidget(btn_paste_url)
        btn_close = QPushButton("Close")
        btn_close.clicked.connect(dialog.reject)
        btn_row.addWidget(btn_close)
        layout.addLayout(btn_row)

        def on_progress(message: str) -> None:
            self._write_progress.emit(lambda a, m, _unused: status_label.setText(message), 0, 0, False)

        def on_item_double_clicked(item):
            candidate = item.data(Qt.UserRole)
            if candidate is None:
                return
            if candidate.ai_generated:
                source_line = "Source: none - AI-generated estimate"
                note = (
                    "\n\nThis was NOT found on any real webpage - it is the AI model's own guess, "
                    "generated because no real documented profile could be found online. Treat "
                    "every value as an unverified starting point only."
                )
            else:
                source_line = f"Source: {candidate.source_url}"
                note = (
                    "\n\nThe model's cited source quotes could NOT be verified against the actual "
                    "webpage text - this candidate may be paraphrased or partly invented. Treat it "
                    "with extra caution." if not candidate.quotes_verified else ""
                )
            notes_lines = [n for n in (candidate.notes or []) if n and n.strip()]
            notes_block = ("\n\n" + "\n".join(notes_lines)) if notes_lines else ""
            # Real excerpts that informed an AI estimate (2026-11-12) - verbatim page text, shown so
            # the operator can judge them rather than trust the estimate blindly.
            excerpt_lines = profile_search.evidence_lines(candidate)
            if excerpt_lines:
                notes_block += "\n\nReal excerpts it was informed by:\n" + "\n".join(excerpt_lines)
            reply = QMessageBox.warning(
                self, "Load Search Result",
                f"Load this into the Profile Editor table for review?\n\n"
                f"{source_line}\n\n"
                f"Nothing is saved or sent automatically.{note}{notes_block}",
                QMessageBox.Yes | QMessageBox.No, QMessageBox.No,
            )
            if reply == QMessageBox.Yes:
                # Bug found live 2026-09-07: this used to populate the table's curve numbers
                # without ever syncing profile_combo's text - _read_table() names a run from
                # profile_combo.currentText() (see its own comment), so a search result loaded
                # this way and started without the operator manually retyping its name into the
                # combo got logged/reported everywhere as "Untitled", even though the real named
                # profile's curve was what actually ran. Confirmed against a real run log
                # (run_20260906_180554.csv: profile_name "Untitled" for the whole file, despite
                # the operator having loaded and run "Xbox One X SMC (U2) Reflow").
                self.profile_combo.setCurrentText(candidate.profile.name)
                self._populate_table(candidate.profile)
                dialog.accept()

        results_list.itemDoubleClicked.connect(on_item_double_clicked)

        def done(result):
            btn_close.setText("Close")
            status_label.setText(
                f"Done. {len(result.candidates)} candidate(s) found"
                + (f", {len(result.errors)} page(s) skipped (see below)." if result.errors else ".")
            )
            for candidate in result.candidates:
                if candidate.ai_generated:
                    bits = ["AI ESTIMATE (no real source - model's own guess)"]
                else:
                    bits = [candidate.source_title or candidate.source_url]
                    if candidate.machine_match:
                        bits.append(f"[mentions {profile_search.MACHINE_NAME}]")
                    if not candidate.quotes_verified:
                        bits.append("[UNVERIFIED - review carefully]")
                item = QListWidgetItem(" - ".join(bits))
                item.setData(Qt.UserRole, candidate)
                if candidate.ai_generated:
                    item.setForeground(QColor("#e74c3c"))
                notes_lines = [n for n in (candidate.notes or []) if n and n.strip()]
                notes_lines += profile_search.evidence_lines(candidate)
                if notes_lines:
                    item.setToolTip("\n".join(notes_lines))
                results_list.addItem(item)
            for err in result.errors:
                item = QListWidgetItem(f"(skipped) {err}")
                item.setForeground(QColor("#888"))
                results_list.addItem(item)
            if not result.candidates and not result.errors:
                # Reached only by an automatic search that genuinely found nothing to even
                # complain about - a Paste URL failure always has a specific error above instead,
                # so this generic line is never shown alongside a misleading "board name" hint.
                results_list.addItem("No candidate profiles found - try a more specific or differently-worded board name.")

        ollama_config = self.ollama_config

        def run_search():
            return profile_search.search_profile_online(board_name, ollama_config, on_progress=on_progress)

        def on_paste_url_clicked():
            url, ok = QInputDialog.getText(dialog, "Paste URL", "Page URL to read and extract from:")
            if not ok or not url.strip():
                return
            btn_paste_url.setEnabled(False)
            status_label.setText("Reading pasted URL...")

            def paste_url_done(result):
                btn_paste_url.setEnabled(True)
                done(result)

            self._run_write(
                lambda: profile_search.extract_profile_from_url(board_name, url.strip(), ollama_config, on_progress=on_progress),
                paste_url_done,
            )

        btn_paste_url.clicked.connect(on_paste_url_clicked)

        self._run_write(run_search, done)
        dialog.exec()

    def _on_rename_local(self) -> None:
        old_name = self.profile_combo.currentText()
        if not old_name:
            return
        new_name, ok = QInputDialog.getText(self, "Rename profile", "New name:", text=old_name)
        if not ok or not new_name.strip() or new_name == old_name:
            return
        try:
            profile_store.rename_profile(old_name, new_name.strip())
        except FileNotFoundError:
            QMessageBox.warning(self, "Not found", f"No local profile named '{old_name}'.")
            return
        except ValueError as exc:
            QMessageBox.critical(self, "Rename Failed", str(exc))
            return
        self._refresh_profile_list()
        self.profile_combo.setCurrentText(new_name.strip())

    def _on_delete_local(self) -> None:
        name = self.profile_combo.currentText()
        if not name:
            return
        reply = QMessageBox.question(
            self, "Confirm delete",
            f"Delete the saved local profile '{name}'? This only removes it from this app's "
            f"local storage - it does not affect anything currently loaded on the machine.",
            QMessageBox.Yes | QMessageBox.No, QMessageBox.No,
        )
        if reply != QMessageBox.Yes:
            return
        profile_store.delete_profile(name)
        self._refresh_profile_list()

    def _on_send_to_machine(self) -> None:
        profile = self._read_table()

        validation = validate_profile(
            profile, self.safety_limits, metadata=profile_store.get_profile_metadata(profile.name),
            qualification=compute_qualification_status(profile.name),
        )
        if validation.blocks_send:
            QMessageBox.critical(
                self, "Profile Validation Failed",
                "PROFILE VALIDATION\n\nERRORS (sending blocked):\n" + "\n".join(f"- {e}" for e in validation.errors),
            )
            return
        if validation.warnings:
            reply = QMessageBox.warning(
                self, "Profile Validation Warnings",
                "PROFILE VALIDATION\n\nWARNINGS:\n" + "\n".join(f"- {w}" for w in validation.warnings) + "\n\nSend anyway?",
                QMessageBox.Yes | QMessageBox.No, QMessageBox.No,
            )
            if reply != QMessageBox.Yes:
                return

        reply = QMessageBox.question(
            self, "Confirm send to machine",
            f"This will overwrite the curve currently loaded on the machine's control board "
            f"with '{profile.name}'. This is a real write to the physical machine. Continue?",
            QMessageBox.Yes | QMessageBox.No, QMessageBox.No,
        )
        if reply != QMessageBox.Yes:
            return
        regs = []
        for attr in ROW_ATTRS:
            for section in profile.sections:
                regs.append(getattr(section, attr))

        def do_send_and_verify():
            result = self.bus.write_registers(ADDR_CURVE_BASE, regs, on_progress=on_progress)
            if not result.ok:
                return result, None
            # Read-back verification (2026-09-07, external feature-gap review): "independently_
            # verified" on the write result above only means the board's own write-command ACK
            # was confirmed - a real, already-confirmed-live gap, since ACK just means "the board
            # accepted the write", not "the value actually stuck" (the IR temp-comp register this
            # same session ACK-confirmed a write that reverted moments later). This reads the
            # curve back and compares it against what was actually sent - None if the opportunistic
            # read never fires in time (not itself a failure, see done() below), True/False if a
            # comparison was actually possible.
            readback = self.bus.read_curve_active()
            if readback is None:
                return result, None
            readback_regs = [getattr(section, attr) for attr in ROW_ATTRS for section in readback.sections]
            return result, (readback_regs == regs)

        def done(outcome):
            result, verified_match = outcome
            self._mark_idle(self.btn_send_machine)
            self.btn_send_machine.setText("Send to machine")
            if result.ok:
                # Record what was sent so the pre-Start reminder (see _on_start_clicked) can tell
                # whether the editor's current curve has actually left this app. ACK-confirmed is
                # enough here - "unsent" is about provenance, not about whether the board stored it
                # (the read-back check above already handles that separately).
                self._curve_last_sent_regs = list(regs)
            if not result.ok:
                QMessageBox.critical(self, "Failed", f"Could not send the curve: {result.detail}")
            elif verified_match is True:
                QMessageBox.information(self, "Profile Verified", f"Curve sent and read back exactly as written (attempt {result.attempts}).")
            elif verified_match is False:
                QMessageBox.critical(
                    self, "Read-Back Mismatch",
                    f"The board ACK-confirmed the write, but reading the curve back afterward does "
                    f"NOT match what was sent. The board accepted the write command but did not "
                    f"actually store the values you sent - do not trust this profile until you "
                    f"verify it directly on the touchscreen's Current Curve screen.",
                )
            else:
                QMessageBox.information(
                    self, "Sent (unconfirmed)",
                    f"The curve was transmitted and ACK-confirmed ({result.detail}).\n\n"
                    f"A read-back comparison couldn't be completed - the machine's confirming data "
                    f"read is unpredictable in timing. Please glance at the curve editor screen on "
                    f"the touchscreen to confirm the new values are showing.",
                )

        self._mark_busy(self.btn_send_machine)
        self.btn_send_machine.setText("Sending...")
        on_progress = self._chunk_progress_reporter(self.btn_send_machine, "curve")
        self._run_write(do_send_and_verify, done)

    def _check_for_interrupted_run(self) -> None:
        """Called once at startup, before any new run can begin - see run_recovery.py's module
        docstring. At this point the bus has only just started connecting, so this can't yet know
        whether the physical machine is still actually running - see
        _confirm_interrupted_run_live_state below for the follow-up once a live reading arrives."""
        interrupted = run_recovery.check_for_interrupted_run()
        if interrupted is None:
            return
        started = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(interrupted.started_at))
        QMessageBox.warning(
            self, "Possible Interrupted Run Detected",
            f"POSSIBLE INTERRUPTED RUN DETECTED\n\n"
            f"Profile: {interrupted.profile_name}\n"
            f"Started: {started}\n"
            f"Log file: {interrupted.log_filename}\n\n"
            f"This app closed (or crashed) without a recorded Stop while this run was active. "
            f"Checking the machine's live state now that it's reconnected...\n\n"
            f"The run's history entry has been marked INTERRUPTED, not completed.",
        )
        run_recovery.acknowledge_interrupted_run(interrupted.log_filename)
        # 2026-09-08, external feature-gap review, second pass - deferred to the first live
        # reading (see _poll) so the operator gets a REAL, current answer instead of just "check it
        # yourself", the moment one becomes available.
        self._interrupted_run_pending_confirmation = interrupted

    def _confirm_interrupted_run_live_state(self, reading) -> None:
        """One-shot follow-up to _check_for_interrupted_run() above - fires the first time a live
        reading arrives after an interrupted run was detected, giving a definitive answer instead
        of leaving the operator to go check the machine themselves."""
        interrupted = self._interrupted_run_pending_confirmation
        self._interrupted_run_pending_confirmation = None
        if reading.running:
            QMessageBox.warning(
                self, "Interrupted Run - Machine Still Running",
                f"The machine's Running state is currently ON - it appears to still be actively "
                f"executing the interrupted profile ('{interrupted.profile_name}'). If this "
                f"wasn't intended, stop it now.",
            )
        else:
            QMessageBox.information(
                self, "Interrupted Run - Machine Not Running",
                f"The machine's Running state is currently OFF - it does not appear to still be "
                f"actively running the interrupted profile ('{interrupted.profile_name}').",
            )

    def _on_start_clicked(self) -> None:
        profile = self._read_table()

        self._preflight_active = True
        self._update_machine_state(self.bus.get_live_reading())
        result = run_preflight(self.bus, profile, self.safety_limits)
        self._preflight_active = False
        self._update_machine_state(self.bus.get_live_reading())

        if not result.passed:
            lines = [f"{'PASS' if c.passed else 'FAIL'}  {c.name}: {c.detail}" for c in result.checks]
            QMessageBox.critical(
                self, "Preflight Failed",
                "PREFLIGHT FAILED\n\n" + "\n".join(lines) + "\n\nSTART BLOCKED",
            )
            return

        # Pre-Start "did you actually send this?" reminder. The machine runs whatever curve is on
        # its control board; this app can't reliably read that back (see curve_send_status), so all
        # it can honestly do is check whether Send-to-machine was used for this exact curve since
        # it was loaded/edited here, and make the operator confirm if not.
        status = curve_send_status(profile_curve_regs(profile), self._curve_last_sent_regs)
        if status == "empty":
            reply = QMessageBox.warning(
                self, "No profile loaded",
                "The profile editor is empty - every curve value is 0.\n\nStarting now would run "
                "whatever curve is already on the machine's control board, with nothing loaded "
                "here to monitor against. Load a profile and send it to the machine first.\n\n"
                "Start anyway?",
                QMessageBox.Yes | QMessageBox.No, QMessageBox.No,
            )
            if reply != QMessageBox.Yes:
                return
        elif status == "unsent":
            box = QMessageBox(self)
            box.setIcon(QMessageBox.Warning)
            box.setWindowTitle("Profile not sent to the machine")
            box.setText(
                f"'{profile.name}' is loaded in the editor, but it has NOT been sent to the "
                f"machine from this app since you loaded it or last changed a value.\n\n"
                f"The machine will heat to whatever curve is currently on its control board - "
                f"which may be a different profile entirely. This app cannot reliably read the "
                f"machine's curve back to check for you.\n\n"
                f"Send it to the machine now, or confirm it's already loaded there."
            )
            send_btn = box.addButton("Send to machine first", QMessageBox.AcceptRole)
            proceed_btn = box.addButton("Already on the machine - Start", QMessageBox.DestructiveRole)
            box.addButton("Cancel", QMessageBox.RejectRole)
            box.setDefaultButton(send_btn)
            box.exec()
            clicked = box.clickedButton()
            if clicked is send_btn:
                self._on_send_to_machine()
                return  # operator verifies the send landed, then clicks Start again
            if clicked is not proceed_btn:
                return

        reply = QMessageBox.warning(
            self, "Confirm Start",
            "Preflight checks passed.\n\nThis will start an actual heating cycle on the physical machine. Continue?",
            QMessageBox.Yes | QMessageBox.No, QMessageBox.No,
        )
        if reply != QMessageBox.Yes:
            return
        self.monitor.reset(profile=profile)
        self.heater_monitor.reset()
        self._reset_section_markers()
        self.lbl_active_profile.setText(f"Monitoring against: {profile.name}")
        self.lbl_active_profile.setStyleSheet("color: #6a6; font-style: italic;")
        self._current_log_path = self.logger.start_run(profile.name)
        run_recovery.mark_run_started(profile.name, self._current_log_path.name)
        # config_snapshot (2026-09-07, external feature-gap review): what actually governed this
        # specific run, captured now rather than reconstructed later from today's settings, which
        # could easily have changed by the time anyone looks back at this run's history.
        self._run_tracker = RunFinalizationTracker(
            comm_snapshot_start=self.bus.get_diagnostics(),
            config_snapshot={
                "safety_limits": asdict(self.safety_limits),
                "alert_config": asdict(self.monitor.config),
                "profile": {"name": profile.name, "sections": [asdict(s) for s in profile.sections]},
            },
        )
        # process_limits (2026-09-07, external feature-gap review, second pass): looked up now,
        # from whatever's saved for THIS profile at Start time, for the same reason config_snapshot
        # is captured now rather than at Stop - a profile's process_limits could be edited mid-run.
        self._run_process_limits = parse_process_limits(profile_store.get_profile_metadata(profile.name))
        self._logging_active = True
        self._start_time = time.monotonic()
        self._plot_data = {"t": [], "upper": [], "lower": [], "ir": [], "probe": []}

        start_label = self.btn_start.text()

        def done(result):
            self._mark_idle(self.btn_start)
            self.btn_start.setText(start_label)
            if not result.ok:
                QMessageBox.critical(self, "Failed", f"Could not confirm Start was applied: {result.detail}")
                self._logging_active = False
                self.logger.stop_run()
            else:
                notifications.notify_async(self.webhook_config, "run_started", f"Run started: {profile.name}")

        self._mark_busy(self.btn_start)
        on_progress = self._coil_progress_reporter(self.btn_start, "Starting")
        self._run_write(lambda: self.bus.write_coil(COIL_RUNNING, True, on_progress=on_progress), done)

    def _on_stop_clicked(self) -> None:
        prior = self.bus.get_live_reading()
        was_running = prior.running if prior else False
        profile_name = self.monitor.active_profile.name if self.monitor.active_profile else "unknown"

        if was_running:
            reply = QMessageBox.warning(
                self, "Confirm Stop",
                f"Stop the active run ({profile_name})?\n\nThis is a real write to the physical machine.",
                QMessageBox.Yes | QMessageBox.No, QMessageBox.No,
            )
            if reply != QMessageBox.Yes:
                return

        stop_label = self.btn_stop.text()
        duration_s = (time.monotonic() - self._run_start_time) if self._run_start_time is not None else 0.0

        def done(result):
            self._mark_idle(self.btn_stop)
            self.btn_stop.setText(stop_label)
            if not result.ok:
                # Review finding #1 (2026-11-12): an unacknowledged Stop means the machine may still be
                # heating - do NOT close the log / clear the recovery marker as if the run were over.
                QMessageBox.critical(
                    self, "STOP NOT CONFIRMED",
                    f"The machine did not acknowledge Stop and may still be running.\nThe run is still being recorded. "
                    f"Stop it at the machine, or press Stop again.\n\n{result.detail}",
                )
                return
            self._logging_active = False
            self.logger.stop_run()
            summary = self._finalize_current_run()
            if was_running:
                # Enriched completion notification (2026-09-14, explicit request) - real computed
                # stats always included; the AI sentence is best-effort on top (see run_summary_ai).
                if summary is not None:
                    # Review finding #10: the Ollama call can take up to 120 s - never on the GUI thread.
                    def _notify(summary=summary):
                        try:
                            ai_text = run_summary_ai.generate_run_summary_text(profile_name, "stopped from this app", summary, duration_s, self.ollama_config)
                        except Exception:
                            ai_text = None
                        notifications.notify_async(
                            self.webhook_config, "run_completed",
                            run_summary_ai.format_run_summary_notification(f"Run stopped: {profile_name}", summary, duration_s, ai_text))
                    threading.Thread(target=_notify, name="run-summary", daemon=True).start()
                else:
                    notifications.notify_async(self.webhook_config, "run_completed", f"Run stopped: {profile_name}")

        self._mark_busy(self.btn_stop)
        on_progress = self._coil_progress_reporter(self.btn_stop, "Stopping")
        self._run_write(lambda: self.bus.write_coil(COIL_RUNNING, False, on_progress=on_progress), done)

    def _finalize_current_run(self) -> dict | None:
        """Computes and saves the just-finished run's summary sidecar (see run_finalization.py) -
        called once, right after the log file is actually closed, so the CSV read here sees every
        row. A no-op (returns None) if no run was actually tracked (e.g. Stop pressed while
        already idle). Returns the computed summary dict so a caller can build a real completion
        notification from it - see run_summary_ai.py."""
        if self._current_log_path is None or self._run_tracker is None:
            return None
        summary = None
        try:
            with open(self._current_log_path, newline="") as f:
                rows = list(csv.DictReader(f))
            summary = finalize_run(
                self._current_log_path, self._run_tracker, self.bus.get_diagnostics(),
                rows, self.monitor.config.wetting_temp_c,
                process_limits=self._run_process_limits,
            )
        except OSError:
            pass
        run_recovery.mark_run_ended()
        self._current_log_path = None
        self._run_tracker = None
        self._run_process_limits = None
        return summary

    def _on_keep_clicked(self) -> None:
        current = self.bus.get_live_reading()
        new_state = not (current.keep if current else False)

        def done(result):
            self._mark_idle(self.btn_keep)
            if not result.ok:
                QMessageBox.critical(self, "Failed", f"Could not confirm KEEP was applied: {result.detail}")

        self._mark_busy(self.btn_keep)
        self._run_write(lambda: self.bus.write_coil(COIL_KEEP, new_state), done)

    def _on_vacuum_clicked(self) -> None:
        current = self.bus.get_live_reading()
        new_state = not (current.vacuum if current else False)
        vacuum_label = self.btn_vacuum.text()

        def done(result):
            self._mark_idle(self.btn_vacuum)
            self.btn_vacuum.setText(vacuum_label)
            if not result.ok:
                QMessageBox.critical(self, "Failed", f"Could not confirm VACUUM was applied: {result.detail}")

        self._mark_busy(self.btn_vacuum)
        on_progress = self._coil_progress_reporter(self.btn_vacuum, "Vacuum")
        self._run_write(lambda: self.bus.write_coil(COIL_VACUUM, new_state, on_progress=on_progress), done)

    def _on_cooling_clicked(self) -> None:
        current = self.bus.get_live_reading()
        new_state = not (current.cooling if current else False)
        cooling_label = self.btn_cooling.text()

        def done(result):
            self._mark_idle(self.btn_cooling)
            self.btn_cooling.setText(cooling_label)
            if not result.ok:
                QMessageBox.critical(self, "Failed", f"Could not confirm COOLING was applied: {result.detail}")

        self._mark_busy(self.btn_cooling)
        on_progress = self._coil_progress_reporter(self.btn_cooling, "Cooling")
        self._run_write(lambda: self.bus.write_coil(COIL_COOLING, new_state, on_progress=on_progress), done)

    def _on_mirror_active_profile(self) -> None:
        profile = self._read_table()
        self.monitor.reset(profile=profile)
        self.heater_monitor.reset()
        self._reset_section_markers()
        self.lbl_active_profile.setText(f"Monitoring against: {profile.name}")
        self.lbl_active_profile.setStyleSheet("color: #6a6; font-style: italic;")

    def _on_wetting_changed(self, value: float) -> None:
        self.monitor.config.wetting_temp_c = value
        self.wetting_line.setValue(value)
        self._update_profile_chart()

    def _on_pid_read(self) -> None:
        """Actively requests PID gains directly from the board - see MachineBus "active reads"."""
        def done(regs):
            self.btn_pid_read.setEnabled(True)
            self.btn_pid_read.setText("Read from machine")
            if regs is None:
                QMessageBox.warning(self, "No response", "No response from the board after repeated attempts.")
                return
            self._populate_pid_table(regs)

        self.btn_pid_read.setEnabled(False)
        self.btn_pid_read.setText("Reading...")
        on_progress = self._read_progress_reporter(self.btn_pid_read, "PID")
        self._run_write(lambda: self.bus.read_pid_active(on_progress=on_progress), done)

    def _on_pid_send(self) -> None:
        reply = QMessageBox.question(
            self, "Confirm send",
            "This will overwrite the PID gains currently loaded on the machine's control "
            "board. This is a real write to the physical machine. Continue?",
            QMessageBox.Yes | QMessageBox.No, QMessageBox.No,
        )
        if reply != QMessageBox.Yes:
            return
        regs = self._read_pid_table()
        old_regs = self.bus.get_latest_pid()

        def done(result):
            self._mark_idle(self.btn_pid_send)
            self.btn_pid_send.setText("Send to machine")
            if result.ok:
                pid_history.record_pid_change(old_regs, regs)
            if result.ok and result.independently_verified:
                QMessageBox.information(self, "Sent", f"PID gains sent and confirmed (attempt {result.attempts}).")
            elif result.ok:
                QMessageBox.information(self, "Sent (unconfirmed)", result.detail)
            else:
                QMessageBox.critical(self, "Failed", f"Could not send PID gains: {result.detail}")

        self._mark_busy(self.btn_pid_send)
        self.btn_pid_send.setText("Sending...")
        on_progress = self._chunk_progress_reporter(self.btn_pid_send, "PID")
        self._run_write(lambda: self.bus.write_registers(ADDR_PID_BASE, regs, on_progress=on_progress), done)

    def _on_view_safety_limits_history(self) -> None:
        """Safety limits history / undo - 2026-09-08, external feature-gap review, second pass.
        Same structure as _on_view_pid_history() below, adapted for named-field dicts instead of a
        flat register list - see config_change_history.py's module docstring for why this exists
        alongside the daily automatic backup (a per-field undo, not a whole-app restore)."""
        history = config_change_history.get_config_change_history()
        if not history:
            QMessageBox.information(self, "No history", "No safety-limit changes recorded yet - every future change will be logged here.")
            return
        ordered = list(reversed(history))  # newest first

        dialog = QDialog(self)
        dialog.setWindowTitle("Safety Limits History")
        dialog.resize(600, 450)
        layout = QVBoxLayout(dialog)
        history_list = QListWidget()
        for entry in ordered:
            when = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(entry["timestamp"]))
            diffs = config_change_history.diff_config_values(entry.get("old_values"), entry["new_values"])
            if entry.get("old_values") is None:
                summary = "(no prior value to compare against)"
            elif not diffs:
                summary = "no values actually changed"
            else:
                summary = f"{len(diffs)} field(s) changed"
            note = f" [{entry['note']}]" if entry.get("note") else ""
            history_list.addItem(f"{when} - {summary}{note}")
        layout.addWidget(history_list)

        diff_text = QTextEdit()
        diff_text.setReadOnly(True)
        diff_text.setFontFamily("Consolas, monospace")
        layout.addWidget(diff_text)

        def show_diff(row: int) -> None:
            if row < 0:
                return
            diffs = config_change_history.diff_config_values(ordered[row].get("old_values"), ordered[row]["new_values"])
            diff_text.setPlainText("\n".join(diffs) if diffs else "(no field-level differences to show)")

        history_list.currentRowChanged.connect(show_diff)
        history_list.setCurrentRow(0)

        btn_restore = QPushButton("Restore this version")

        def do_restore() -> None:
            row = history_list.currentRow()
            if row < 0:
                return
            entry = ordered[row]
            when = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(entry["timestamp"]))
            reply = QMessageBox.question(
                self, "Confirm Restore",
                f"This will apply the safety limits from {when}, overwriting whatever is "
                f"currently configured. Continue?",
                QMessageBox.Yes | QMessageBox.No, QMessageBox.No,
            )
            if reply != QMessageBox.Yes:
                return
            old_values = asdict(self.safety_limits)
            try:
                updates = parse_safety_limit_updates(entry["new_values"])
            except ValueError as exc:  # audit C07: validate everything before applying anything
                QMessageBox.critical(self, "Cannot restore", f"That history entry is invalid and was not restored: {exc}")
                return
            for field_name, value in updates.items():
                setattr(self.safety_limits, field_name, value)
            self.bus.set_ir_reading_correction(self.safety_limits.ir_reading_correction_c)
            save_safety_limits(self.safety_limits)
            config_change_history.record_config_change(old_values, asdict(self.safety_limits), note=f"Restored from {when}")
            # Reflect the restored values in the spinboxes without re-triggering their own
            # editingFinished confirm/record path.
            for attr, (spin, is_minutes) in self._safety_limit_spins.items():
                spin.blockSignals(True)
                spin.setValue(getattr(self.safety_limits, attr) / (60.0 if is_minutes else 1.0))
                spin.blockSignals(False)
            self.ir_correction_spin.blockSignals(True)
            self.ir_correction_spin.setValue(self.safety_limits.ir_reading_correction_c)
            self.ir_correction_spin.blockSignals(False)
            QMessageBox.information(self, "Restored", f"Safety limits from {when} applied.")
            dialog.accept()

        btn_restore.clicked.connect(do_restore)
        layout.addWidget(btn_restore)

        buttons = QDialogButtonBox(QDialogButtonBox.Close)
        buttons.rejected.connect(dialog.reject)
        buttons.accepted.connect(dialog.accept)
        layout.addWidget(buttons)
        dialog.exec()

    def _on_view_pid_history(self) -> None:
        """PID Send History - extended 2026-09-08 (external feature-gap review, second pass) with
        a real per-register before/after diff (was just a bare "N register(s) changed" count) and
        a "Restore this version" button that re-sends a past entry's values - itself recorded as a
        new history entry (see pid_history.record_pid_change), never a silent rewrite of history,
        matching profile_store.restore_profile_version()'s identical convention."""
        history = pid_history.get_pid_history()
        if not history:
            QMessageBox.information(self, "No history", "No PID sends recorded yet - every future send will be logged here.")
            return
        ordered = list(reversed(history))  # newest first

        dialog = QDialog(self)
        dialog.setWindowTitle("PID Send History")
        dialog.resize(600, 450)
        layout = QVBoxLayout(dialog)
        history_list = QListWidget()
        for entry in ordered:
            when = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(entry["timestamp"]))
            diffs = pid_history.diff_pid_values(entry.get("old_values"), entry["new_values"])
            if entry.get("old_values") is None:
                summary = "(no prior read to compare against)"
            elif not diffs:
                summary = "no values actually changed"
            else:
                summary = f"{len(diffs)} register(s) changed"
            note = f" [{entry['note']}]" if entry.get("note") else ""
            history_list.addItem(f"{when} - {summary}{note}")
        layout.addWidget(history_list)

        diff_text = QTextEdit()
        diff_text.setReadOnly(True)
        diff_text.setFontFamily("Consolas, monospace")
        layout.addWidget(diff_text)

        def show_diff(row: int) -> None:
            if row < 0:
                return
            diffs = pid_history.diff_pid_values(ordered[row].get("old_values"), ordered[row]["new_values"])
            diff_text.setPlainText("\n".join(diffs) if diffs else "(no register-level differences to show)")

        history_list.currentRowChanged.connect(show_diff)
        history_list.setCurrentRow(0)

        btn_restore = QPushButton("Restore this version")

        def do_restore() -> None:
            row = history_list.currentRow()
            if row < 0:
                return
            entry = ordered[row]
            when = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(entry["timestamp"]))
            reply = QMessageBox.question(
                self, "Confirm Restore",
                f"This will send the PID values from {when} back to the machine, overwriting "
                f"whatever is currently loaded. This is a real write to the physical machine. Continue?",
                QMessageBox.Yes | QMessageBox.No, QMessageBox.No,
            )
            if reply != QMessageBox.Yes:
                return
            restore_values = entry["new_values"]
            old_regs = self.bus.get_latest_pid()

            def done(result):
                self._mark_idle(btn_restore)
                btn_restore.setText("Restore this version")
                if result.ok:
                    pid_history.record_pid_change(old_regs, restore_values, note=f"Restored from {when}")
                    QMessageBox.information(self, "Restored", f"PID values from {when} sent to the machine.")
                    dialog.accept()
                else:
                    QMessageBox.critical(self, "Failed", f"Could not restore PID values: {result.detail}")

            self._mark_busy(btn_restore)
            btn_restore.setText("Restoring...")
            self._run_write(lambda: self.bus.write_registers(ADDR_PID_BASE, restore_values), done)

        btn_restore.clicked.connect(do_restore)
        layout.addWidget(btn_restore)

        buttons = QDialogButtonBox(QDialogButtonBox.Close)
        buttons.rejected.connect(dialog.reject)
        buttons.accepted.connect(dialog.accept)
        layout.addWidget(buttons)
        dialog.exec()

    def _on_fan_read(self) -> None:
        """Actively requests fan/vacuum timing directly from the board - see MachineBus
        "active reads"."""
        def done(regs):
            self.btn_fan_read.setEnabled(True)
            self.btn_fan_read.setText("Read from machine")
            if regs is None:
                QMessageBox.warning(self, "No response", FAN_TIMING_TEMP_COMP_NO_RESPONSE_MESSAGE)
                return
            self._populate_fan_timing(regs)

        self.btn_fan_read.setEnabled(False)
        self.btn_fan_read.setText("Reading...")
        on_progress = self._read_progress_reporter(self.btn_fan_read, "fan timing")
        self._run_write(lambda: self.bus.read_fan_timing_active(on_progress=on_progress), done)

    def _on_fan_send(self) -> None:
        reply = QMessageBox.question(
            self, "Confirm send",
            "This will overwrite the fan/vacuum timing settings on the machine's control "
            "board. This is a real write to the physical machine. Continue?",
            QMessageBox.Yes | QMessageBox.No, QMessageBox.No,
        )
        if reply != QMessageBox.Yes:
            return
        regs = self._read_fan_timing()

        def done(result):
            self.btn_fan_send.setEnabled(True)
            self.btn_fan_send.setText("Send to machine")
            if result.ok and result.independently_verified:
                QMessageBox.information(self, "Sent", f"Fan timing sent and confirmed (attempt {result.attempts}).")
            elif result.ok:
                QMessageBox.information(self, "Sent (unconfirmed)", result.detail)
            else:
                QMessageBox.critical(self, "Failed", f"Could not send fan timing: {result.detail}")

        self.btn_fan_send.setEnabled(False)
        self.btn_fan_send.setText("Sending...")
        self._run_write(lambda: self.bus.write_registers(ADDR_VACUUM_TIME, regs), done)

    def _on_temp_comp_read(self) -> None:
        """See MachineBus "active reads" - same underlying request as fan timing (same 10-
        register range on the wire), just returns the other slice of it."""
        def done(regs):
            self.btn_tempcomp_read.setEnabled(True)
            self.btn_tempcomp_read.setText("Read from machine")
            if regs is None:
                QMessageBox.warning(self, "No response", FAN_TIMING_TEMP_COMP_NO_RESPONSE_MESSAGE)
                return
            self._populate_temp_comp(regs)

        self.btn_tempcomp_read.setEnabled(False)
        self.btn_tempcomp_read.setText("Reading...")
        self._run_write(self.bus.read_temp_comp_active, done)

    def _on_temp_comp_send(self) -> None:
        reply = QMessageBox.question(
            self, "Confirm send",
            "This will overwrite the temperature compensation offsets on the machine's control "
            "board. Upper (176) and Lower (177) are confirmed live - this should genuinely take "
            "effect. IR (178) is also confirmed real and working, but hard-capped at +10C by the "
            "machine itself - anything above that (11+) reverts to 0 within seconds regardless of "
            "source. Verify all values against the touchscreen's own Advanced Parameters screen "
            "after sending. This is a real write to the physical machine. Continue?",
            QMessageBox.Yes | QMessageBox.No, QMessageBox.No,
        )
        if reply != QMessageBox.Yes:
            return
        regs = self._read_temp_comp()

        def done(result):
            self.btn_tempcomp_send.setEnabled(True)
            self.btn_tempcomp_send.setText("Send to machine")
            if result.ok and result.independently_verified:
                QMessageBox.information(self, "Sent", f"Temperature compensation sent and confirmed (attempt {result.attempts}).")
            elif result.ok:
                QMessageBox.information(self, "Sent (unconfirmed)", result.detail)
            else:
                QMessageBox.critical(self, "Failed", f"Could not send temperature compensation: {result.detail}")

        self.btn_tempcomp_send.setEnabled(False)
        self.btn_tempcomp_send.setText("Sending...")
        self._run_write(lambda: self.bus.write_registers(ADDR_TEMP_COMP_UPPER, regs), done)

    def _on_open_calibration_wizard(self) -> None:
        dialog = QDialog(self)
        dialog.setWindowTitle("Calibration Wizard")
        dialog.resize(480, 380)
        layout = QVBoxLayout(dialog)
        layout.addWidget(QLabel(
            "Records (reference instrument, machine reading) pairs and computes the offset.\n"
            "Informational only - does NOT write anything to the machine's temperature "
            "compensation registers automatically (see the Temperature Compensation box above "
            "if you want to act on a computed offset - that write stays a separate, explicit "
            "action, same as it always has been)."
        ))

        top_row = QHBoxLayout()
        top_row.addWidget(QLabel("Operator:"))
        operator_edit = QLineEdit()
        top_row.addWidget(operator_edit)
        top_row.addWidget(QLabel("Reference instrument:"))
        instrument_edit = QLineEdit()
        top_row.addWidget(instrument_edit)
        layout.addLayout(top_row)

        zones = ["upper", "lower", "ir", "probe"]
        reading = self.bus.get_live_reading()
        live_by_zone = {}
        if reading is not None:
            live_by_zone = {"upper": reading.upper_temp, "lower": reading.lower_temp, "ir": reading.infrared_temp, "probe": reading.probe_temp}

        table = QTableWidget(len(zones), 3)
        table.setHorizontalHeaderLabels(["Reference (C)", "Machine reading (C)", "Offset (C)"])
        table.setVerticalHeaderLabels([z.capitalize() for z in zones])
        for row, zone in enumerate(zones):
            table.setItem(row, 0, QTableWidgetItem(""))
            machine_val = live_by_zone.get(zone, 0.0)
            table.setItem(row, 1, QTableWidgetItem(f"{machine_val:.1f}" if reading else ""))
            table.setItem(row, 2, QTableWidgetItem(""))
        table.setToolTip("Machine reading is pre-filled from the current live reading if connected - overwrite it with whatever the machine actually showed at calibration time if different.")
        layout.addWidget(table)

        def recompute_offsets():
            for row in range(table.rowCount()):
                try:
                    ref = float(table.item(row, 0).text())
                    machine = float(table.item(row, 1).text())
                except (ValueError, AttributeError):
                    table.item(row, 2).setText("")
                    continue
                table.item(row, 2).setText(f"{ref - machine:.1f}")
        table.itemChanged.connect(lambda _item: recompute_offsets())

        layout.addWidget(QLabel("Notes:"))
        notes_edit = QTextEdit()
        notes_edit.setMaximumHeight(60)
        layout.addWidget(notes_edit)

        button_row = QHBoxLayout()
        btn_save = QPushButton("Save record")
        btn_history = QPushButton("View history...")
        button_row.addWidget(btn_save)
        button_row.addWidget(btn_history)
        layout.addLayout(button_row)

        def on_save():
            points = []
            for row, zone in enumerate(zones):
                try:
                    ref = float(table.item(row, 0).text())
                    machine = float(table.item(row, 1).text())
                except (ValueError, AttributeError):
                    continue
                points.append(calibration.CalibrationPoint(zone=zone, reference_c=ref, machine_c=machine))
            if not points:
                QMessageBox.warning(dialog, "Nothing to save", "Enter at least one reference/machine reading pair.")
                return
            record = calibration.CalibrationRecord(
                operator=operator_edit.text(), reference_instrument=instrument_edit.text(),
                points=points, note=notes_edit.toPlainText(),
            )
            calibration.add_calibration_record(record)
            QMessageBox.information(dialog, "Saved", f"Saved {len(points)} calibration point(s).")
            dialog.accept()

        def on_view_history():
            history = calibration.get_calibration_history()
            if not history:
                QMessageBox.information(dialog, "No history", "No calibration records saved yet.")
                return
            lines = []
            for entry in reversed(history):
                when = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(entry["timestamp"]))
                point_lines = "; ".join(f"{p['zone']}: ref {p['reference_c']}C, machine {p['machine_c']}C, offset {p['offset_c']:+.1f}C" for p in entry["points"])
                lines.append(f"{when} ({entry.get('operator') or 'unknown operator'}, {entry.get('reference_instrument') or 'unknown instrument'})\n  {point_lines}")
            QMessageBox.information(dialog, "Calibration History", "\n\n".join(lines))

        btn_save.clicked.connect(on_save)
        btn_history.clicked.connect(on_view_history)
        dialog.exec()

    def _on_selftune_clicked(self, zone_label: str, coil: int) -> None:
        reply = QMessageBox.warning(
            self, "Confirm self-tuning",
            f"This starts a REAL self-tuning (PID autotune) routine on the {zone_label} zone. "
            f"It will perturb the actual heating elements to characterize their response - "
            f"this is a real physical action on the machine, not a settings change. Continue?",
            QMessageBox.Yes | QMessageBox.No, QMessageBox.No,
        )
        if reply != QMessageBox.Yes:
            return

        def done(result):
            if not result.ok:
                QMessageBox.critical(self, "Failed", f"Could not confirm {zone_label} self-tuning was triggered: {result.detail}")

        self._run_write(lambda: self.bus.write_coil(coil, True), done)

    def _on_import_file(self) -> None:
        path, _ = QFileDialog.getOpenFileName(self, "Import profiles", "", "Profile files (*.csv *.xlsx)")
        if not path:
            return
        data = open(path, "rb").read()
        if path.lower().endswith(".xlsx"):
            result = profile_import.parse_xlsx(data)
        elif path.lower().endswith(".csv"):
            result = profile_import.parse_csv(data)
        else:
            QMessageBox.warning(self, "Unsupported file", f"Expected a .csv or .xlsx file: {path}")
            return

        saved_names = []
        save_errors = []
        for profile in result.profiles:
            try:
                profile_store.save_profile(profile)
                saved_names.append(profile.name)
            except (OSError, ValueError) as exc:
                # A single bad profile name (e.g. containing filesystem-illegal characters, or one
                # that collides with an existing profile after sanitization - see
                # profile_store.save_profile()'s own docstring, added 2026-09-08) must never abort
                # the rest of the batch or crash silently - confirmed live 2026-08-19, an uncaught
                # exception here repeatedly killed the import with no dialog shown at all, and
                # eventually segfaulted the app.
                save_errors.append(f"Could not save '{profile.name}': {exc}")
        self._refresh_profile_list()

        lines = []
        if saved_names:
            lines.append(f"Imported {len(saved_names)} profile(s): {', '.join(saved_names)}")
        else:
            lines.append("No profiles were imported.")
        lines += [f"- {w}" for w in result.warnings]
        lines += [f"- {e}" for e in save_errors]
        if saved_names:
            QMessageBox.information(self, "Import complete", "\n".join(lines))
        else:
            QMessageBox.warning(self, "Import failed", "\n".join(lines))

    def _on_export_all(self) -> None:
        names = profile_store.list_profiles()
        if not names:
            QMessageBox.information(self, "Nothing to export", "No saved local profiles to export.")
            return
        path, _ = QFileDialog.getSaveFileName(self, "Export all profiles", "profiles.xlsx", "Excel (*.xlsx);;CSV (*.csv)")
        if not path:
            return
        profiles = [profile_store.load_profile(name) for name in names]
        data = profile_import.export_xlsx(profiles) if path.lower().endswith(".xlsx") else profile_import.export_csv(profiles)
        with open(path, "wb") as f:
            f.write(data)
        QMessageBox.information(self, "Exported", f"Exported {len(profiles)} profile(s) to {path}")

    def _on_export_backup(self) -> None:
        backup = config_backup.create_backup(self.safety_limits, self.monitor.config)
        path, _ = QFileDialog.getSaveFileName(self, "Export configuration backup", "ly-r690-backup.json", "JSON (*.json)")
        if not path:
            return
        with open(path, "w") as f:
            json.dump(backup, f, indent=2)
        QMessageBox.information(self, "Exported", f"Backed up {len(backup['profiles'])} profile(s), safety limits, and alert thresholds to {path}")

    def _on_import_backup(self) -> None:
        path, _ = QFileDialog.getOpenFileName(self, "Import configuration backup", "", "JSON (*.json)")
        if not path:
            return
        try:
            with open(path) as f:
                backup = json.load(f)
        except (json.JSONDecodeError, OSError) as exc:
            QMessageBox.critical(self, "Import failed", f"Could not read {path}: {exc}")
            return
        reply = QMessageBox.question(
            self, "Confirm restore",
            f"This will restore {len(backup.get('profiles', []))} profile(s) into your local "
            f"library (each as a new version, existing history preserved), and overwrite the "
            f"current safety limits and alert thresholds. Continue?",
            QMessageBox.Yes | QMessageBox.No, QMessageBox.No,
        )
        if reply != QMessageBox.Yes:
            return
        try:
            summary = config_backup.restore_backup(backup, self.safety_limits, self.monitor.config)
        except ValueError as exc:
            QMessageBox.critical(self, "Import failed", str(exc))
            return
        self.wetting_spin.setValue(self.monitor.config.wetting_temp_c)
        for attr, spin in self._alert_threshold_spins.items():
            spin.blockSignals(True)
            spin.setValue(getattr(self.monitor.config, attr))
            spin.blockSignals(False)
        for attr, (spin, is_minutes) in self._safety_limit_spins.items():
            spin.blockSignals(True)
            spin.setValue(getattr(self.safety_limits, attr) / (60.0 if is_minutes else 1.0))
            spin.blockSignals(False)
        self.ir_correction_spin.blockSignals(True)
        self.ir_correction_spin.setValue(self.safety_limits.ir_reading_correction_c)
        self.ir_correction_spin.blockSignals(False)
        self.bus.set_ir_reading_correction(self.safety_limits.ir_reading_correction_c)
        # Otherwise a restore only lasts until the next restart, same gap this whole persistence
        # mechanism exists to close - see safety_limits.save_safety_limits().
        save_safety_limits(self.safety_limits)
        self._refresh_profile_list()
        message = f"Restored {len(summary['profiles_restored'])} profile(s), safety limits, and alert thresholds."
        # profiles_skipped (2026-09-08 audit finding) - see config_backup.restore_backup()'s own
        # docstring: a profile whose name collides with an existing, DIFFERENT profile after
        # filename sanitization is skipped rather than silently corrupting the other one.
        skipped = summary.get("profiles_skipped") or []
        if skipped:
            message += "\n\nSkipped (name conflicts with an existing profile):\n" + "\n".join(
                f"- {s['name']}: {s['reason']}" for s in skipped
            )
        QMessageBox.information(self, "Restored", message)

    # ---------------- polling loop ----------------

    def _poll(self) -> None:
        self._update_curve_status()
        self._update_freshness_label(self.lbl_pid_status, "PID data", *self.bus.get_pid_status())
        self._update_freshness_label(self.lbl_fan_status, "Fan timing data", *self.bus.get_fan_timing_status())
        self._update_comm_diagnostics()
        self._check_disk_space()
        self._check_config_drift()
        self._check_system_health()
        self._check_automatic_backup()

        reading = self.bus.get_live_reading()
        self._update_machine_state(reading)

        if reading is None:
            return

        if self._interrupted_run_pending_confirmation is not None:
            self._confirm_interrupted_run_live_state(reading)

        self._check_safety_limits(reading)
        self._check_heater_response(reading)
        self._check_run_ended_externally(reading)
        self._update_process_dashboard(reading)
        self._self_tune_tracker.observe(reading.self_tune_upper, reading.self_tune_lower, reading.self_tune_ir)

        self.lbl_upper.setText(f"Upper: {reading.upper_temp:.1f}C")
        self.lbl_lower.setText(f"Lower: {reading.lower_temp:.1f}C")
        self.lbl_ir.setText(f"IR: {reading.infrared_temp:.1f}C")
        self.lbl_probe.setText(f"Probe: {reading.probe_temp:.1f}C")
        self._set_indicator(self.lbl_running, reading.running, "#2a6")
        self._set_indicator(self.lbl_vacuum, reading.vacuum, "#38c")
        self._set_indicator(self.lbl_cooling, reading.cooling, "#38c")
        self._set_indicator(self.lbl_keep, reading.keep, "#b8860b")

        t = time.monotonic() - self._start_time
        self._plot_data["t"].append(t)
        self._plot_data["upper"].append(reading.upper_temp)
        self._plot_data["lower"].append(reading.lower_temp)
        self._plot_data["ir"].append(reading.infrared_temp)
        self._plot_data["probe"].append(reading.probe_temp)
        self.curve_upper.setData(self._plot_data["t"], self._plot_data["upper"])
        self.curve_lower.setData(self._plot_data["t"], self._plot_data["lower"])
        self.curve_ir.setData(self._plot_data["t"], self._plot_data["ir"])
        self.curve_probe.setData(self._plot_data["t"], self._plot_data["probe"])

        if self._logging_active:
            self.logger.log(reading)

        for alert in self.monitor.check(reading):
            self._show_alert(alert)
        self._update_section_indicator(reading, t)

    # ---------------- safety foundation ----------------

    _STATE_COLORS = {
        MachineState.DISCONNECTED: "#444",
        MachineState.CONNECTING: "#666",
        MachineState.IDLE: "#444",
        MachineState.PREFLIGHT: "#b8860b",
        MachineState.RUNNING: "#2a6",
        MachineState.PAUSED: "#b8860b",
        MachineState.COOLING: "#38c",
        MachineState.FAULT: "#a33",
        # Real heating action (PID autotune), separate from a profile run - distinct purple so it
        # doesn't read as either "safe/idle" (grey) or "actively running a profile" (green). See
        # machine_state.py's MachineState.SELF_TUNING comment for the incident this fixes
        # (confirmed live 2026-09-07: this used to be silently shown as idle).
        MachineState.SELF_TUNING: "#86b",
        # Caution orange - not as alarming as FAULT's red (this isn't a confirmed problem, just
        # untrustworthy data), but deliberately distinct from IDLE's neutral grey, since "we don't
        # know what the machine is doing" is not the same fact as "it's safely idle". See
        # machine_state.py's MachineState.UNKNOWN comment for what this actually means.
        MachineState.UNKNOWN: "#c70",
    }

    def _update_machine_state(self, reading) -> None:
        temps_ts = self.bus.get_temps_timestamp()
        state = derive_state(
            reading, self.bus.is_connected,
            active_fault=self.fault_manager.has_unacknowledged_critical(),
            preflight_active=self._preflight_active,
            seconds_since_temps_update=(time.monotonic() - temps_ts) if temps_ts > 0.0 else None,
        )
        self._current_state = state
        color = self._STATE_COLORS.get(state, "#444")
        text_color = "#aaa" if color == "#444" else "#fff"
        self.lbl_machine_state.setText(state.value.upper())
        self.lbl_machine_state.setStyleSheet(
            f"font-weight: bold; padding: 2px 8px; border-radius: 4px; background: {color}; color: {text_color};"
        )
        self.btn_ack_fault.setVisible(state == MachineState.FAULT)
        self._apply_control_gating(state)

    def _mark_busy(self, btn) -> None:
        """Use instead of btn.setEnabled(False) for any of the run-state buttons gated by
        _apply_control_gating (start/stop/keep/vacuum/cooling) - marks the button as having a
        write in flight so gating knows not to touch it (see _apply_control_gating)."""
        self._busy_buttons.add(btn)
        btn.setEnabled(False)

    def _mark_idle(self, btn) -> None:
        """Pair of _mark_busy - call from the write's own done() callback, exactly where
        btn.setEnabled(True) used to be called directly."""
        self._busy_buttons.discard(btn)
        btn.setEnabled(True)

    def _apply_control_gating(self, state: MachineState) -> None:
        """Disables a control the current state doesn't allow, and RE-enables one the state now
        does allow - but only if that button has no write in flight (see _mark_busy/_mark_idle).
        Without the busy check, gating and the busy-disable-during-a-write mechanism would race
        each other (confirmed as a real risk for a simpler case earlier this session: coil-toggle
        staleness). Without the re-enable half, a button disabled by an unrelated transition (e.g.
        Start disabled while Cooling was on) stayed disabled forever once that transition reversed,
        since nothing else was left to turn it back on - confirmed live 2026-08-23."""
        for name, btn in (
            ("start", self.btn_start), ("stop", self.btn_stop), ("keep", self.btn_keep),
            ("vacuum", self.btn_vacuum), ("cooling", self.btn_cooling),
            ("reconnect", self.btn_reconnect),
        ):
            if btn in self._busy_buttons:
                continue
            btn.setEnabled(is_control_enabled(state, name))
        if self.btn_send_machine not in self._busy_buttons:
            self.btn_send_machine.setEnabled(is_control_enabled(state, "send_profile"))
        if self.btn_pid_send not in self._busy_buttons:
            self.btn_pid_send.setEnabled(is_control_enabled(state, "send_pid"))

    def _check_safety_limits(self, reading) -> None:
        """The ramp-rate check goes through self._ramp_tracker (safety_limits.RampTracker)
        instead of this method's own poll cadence - see that class's docstring for the three
        related fixes this went through live in 2026-09: stale-reading false positives, noisy
        short-interval false positives, and a real-but-INTENDED section-boundary target jump
        being mistaken for a runaway. This method's own contribution to that third fix: detect a
        section transition via self.monitor.estimate_current_section() (observed-data only, no
        profile "rate" field involved) and reset the ramp baseline right at that boundary, so the
        check never spans across a legitimate commanded jump.

        Fourth related case, confirmed live 2026-09-07: a real IR self-tune (PID autotune,
        COIL_SELFTUNE_*) produced legitimate large/fast IR ramps with `running` False the whole
        time (self-tune isn't a profile run, so estimate_current_section()'s section-transition
        mechanism can't see it either). While any self-tune coil is active, keep resetting the
        ramp baseline every tick (same effect as never establishing one) and pass
        skip_ramp_check=True - this also means the first tick AFTER self-tune ends starts a fresh
        baseline instead of comparing against a stale pre/during-self-tune reading, avoiding a
        false positive on that transition too. Absolute max-temperature checks are NOT skipped -
        see check_reading()'s docstring for why."""
        now = time.monotonic()
        was_running = self._prior_reading.running if self._prior_reading else False
        if reading.running and not was_running:
            self._run_start_time = now
        if not reading.running:
            self._run_start_time = None
        if was_running and not reading.running:
            # Run just ended (2026-09-13, real Discord alert: "Infrared temperature changed
            # 538C/min" right as a run finished). IR has very low thermal mass and craters fast
            # the instant its heater cuts off / cooling engages - a real, expected cooldown
            # artifact, not a runaway, but with no reset here the ramp tracker's last reading is
            # still the final hot in-run sample, and the very next (now-cooling) tick reads as a
            # huge ramp. estimate_current_section() returns None the instant running goes False,
            # so the section-transition reset below never catches this - it only fires between two
            # non-None section indices. Reset explicitly on this edge too.
            self._ramp_tracker.reset()
            self._last_ramp_section_index = None

        # Chip extraction phase (2026-09-10, run_20260910_165928): once the operator engages
        # VACUUM (chip pickup) or KEEP to lift the chip at cycle end, the probe thermocouple is
        # riding the part being removed and its reading is no longer a temperature - skip the
        # probe ramp check while that's happening, and reset the whole ramp baseline on the way
        # out so the first post-extraction tick doesn't compare across the disturbance. See
        # check_reading's skip_probe_ramp_check docstring.
        extracting = reading.running and (reading.vacuum or reading.keep)
        if self._was_extracting and not extracting:
            self._ramp_tracker.reset()
        self._was_extracting = extracting

        # skip_full_ramp also covers COOLING (2026-09-13, same real incident as above): active
        # cooling is a deliberate, commanded temperature change across every zone, not a runaway -
        # the opposite failure mode, if anything - and the same fast-IR-cooldown effect can keep
        # tripping the ramp check tick after tick for as long as cooling stays engaged, not just on
        # the one edge tick the reset above covers. Absolute max-temperature checks are NOT
        # skipped by any of this - see check_reading()'s docstring for why.
        self_tuning = reading.self_tune_upper or reading.self_tune_lower or reading.self_tune_ir
        skip_full_ramp = self_tuning or reading.cooling
        if skip_full_ramp:
            self._ramp_tracker.reset()
            self._last_ramp_section_index = None
            ramp_prior_reading, seconds_since_prior = None, None
        else:
            current_section = self.monitor.estimate_current_section(reading)
            if current_section is not None and self._last_ramp_section_index is not None and current_section != self._last_ramp_section_index:
                self._ramp_tracker.reset()
            self._last_ramp_section_index = current_section

            ramp_prior_reading, seconds_since_prior = self._ramp_tracker.observe(
                reading, self.bus.get_temps_timestamp(), min_interval_s=self.safety_limits.min_ramp_sample_interval_s,
            )
        violations = check_reading(
            self.safety_limits, reading, ramp_prior_reading, seconds_since_prior,
            skip_ramp_check=skip_full_ramp, skip_probe_ramp_check=extracting,
        )

        if reading.running and self._run_start_time is not None:
            duration_violation = check_run_duration(self.safety_limits, now - self._run_start_time)
            if duration_violation:
                violations.append(duration_violation)
            outage_violation = check_communication_outage(self.safety_limits, self.bus.seconds_since_update())
            if outage_violation:
                violations.append(outage_violation)

        current_names = {v.limit_name for v in violations}
        new_violations = [v for v in violations if v.limit_name not in self._active_violation_names]
        self._active_violation_names = current_names

        for v in new_violations:
            fault = self.fault_manager.raise_fault(Fault(
                fault_type=FaultType.SAFETY_LIMIT_EXCEEDED,
                severity=FaultSeverity.CRITICAL,
                description=v.description,
                reading_snapshot={
                    "upper": reading.upper_temp, "lower": reading.lower_temp,
                    "ir": reading.infrared_temp, "probe": reading.probe_temp,
                },
                profile_name=self.profile_combo.currentText() if hasattr(self, "profile_combo") else "",
            ))
            self._on_fault_raised(fault)

        self._prior_reading = reading
        self._prior_reading_time = now

    def _check_heater_response(self, reading) -> None:
        """Pattern-based, not a hard numeric limit - see heater_monitor.py's module docstring for
        why these are WARNING faults (shown, don't auto-stop) rather than CRITICAL."""
        for issue in self.heater_monitor.update(reading, self.monitor.active_profile, time.monotonic()):
            fault_type = {
                "not_responding": FaultType.HEATER_NOT_RESPONDING,
                "overshoot": FaultType.HEATER_OVERSHOOT,
                "stuck_on": FaultType.HEATER_STUCK_ON,
            }[issue.kind]
            fault = self.fault_manager.raise_fault(Fault(
                fault_type=fault_type, severity=FaultSeverity.WARNING,
                description=issue.description,
                profile_name=self.profile_combo.currentText() if hasattr(self, "profile_combo") else "",
            ))
            self._on_fault_raised(fault)

    def _check_run_ended_externally(self, reading) -> None:
        """Catches a run ending by anything OTHER than this app's own Stop button or a fault this
        same tick already handled - a real gap reported live 2026-09-14: neither a profile
        completing on its own (the controller's own curve-sequencer reaches the end and the
        running coil drops by itself) nor the touchscreen's own physical Stop button were ever
        detected. Runs every tick regardless of whether a run is active; it's a no-op unless it
        actually observes the running->not-running edge with a run still open. Mirrors
        web_server.py's function of the same name exactly.

        Can't perfectly distinguish "the profile finished on its own" from "someone pressed Stop
        on the touchscreen" - both look identical on this bus (the running coil just goes False) -
        so it makes an honest best-effort call from the last section estimate seen before the
        drop: reaching the profile's own final section reads as "reached the end of the profile";
        anything earlier (or no estimate at all) reads as "ended early", never claiming more than
        what was observed."""
        was_running = self._run_end_watch_was_running
        self._run_end_watch_was_running = reading.running
        if reading.running:
            section_index = self.monitor.estimate_current_section(reading)
            if section_index is not None:
                self._last_active_section_index = section_index
                self._last_active_section_total = len(self.monitor.active_profile.sections) if self.monitor.active_profile else None
            if self._run_start_time is not None:
                self._last_active_run_start_time = self._run_start_time
            return
        if not (was_running and (self._run_tracker is not None or self._logging_active)):
            return  # no edge this tick, or already closed out by the fault path above

        section_index, section_total = self._last_active_section_index, self._last_active_section_total
        self._last_active_section_index = None
        self._last_active_section_total = None
        profile_name = self.monitor.active_profile.name if self.monitor.active_profile else "unknown"
        run_start_time, self._last_active_run_start_time = self._last_active_run_start_time, None
        duration_s = (time.monotonic() - run_start_time) if run_start_time is not None else 0.0

        self._logging_active = False
        self.logger.stop_run()
        summary = self._finalize_current_run()
        if summary is None:
            return
        if section_index is not None and section_total is not None and section_index >= section_total - 1:
            outcome = "reached the end of the profile"
        elif section_index is not None and section_total is not None:
            outcome = f"ended early - last matched section {section_index + 1} of {section_total} (verify this was intentional)"
        else:
            outcome = "ended (not stopped from this app)"
        # Review finding #10: the Ollama request can take ~2 minutes - never on the GUI thread.
        def _notify():
            try:
                ai_text = run_summary_ai.generate_run_summary_text(profile_name, outcome, summary, duration_s, self.ollama_config)
            except Exception:
                ai_text = None
            notifications.notify_async(
                self.webhook_config, "run_completed",
                run_summary_ai.format_run_summary_notification(f"Run {outcome}: {profile_name}", summary, duration_s, ai_text))
        threading.Thread(target=_notify, name="run-summary", daemon=True).start()

    _DISK_CHECK_INTERVAL_S = 60.0

    def _check_disk_space(self) -> None:
        """Throttled - see this method's self._last_disk_check_time comment in __init__ for why.
        Only raises a fault on an actual LEVEL CHANGE (ok/warning/critical/unknown), in either
        direction - fault_manager.raise_fault() has no dedup, and disk space doesn't change fast
        enough to need per-tick alerting anyway."""
        now = time.monotonic()
        if now - self._last_disk_check_time < self._DISK_CHECK_INTERVAL_S:
            return
        self._last_disk_check_time = now
        status = check_disk_space()
        level = status["level"]
        if level == self._last_disk_level:
            return
        self._last_disk_level = level
        if level == "ok":
            return  # recovered from warning/critical, or the very first check landed on ok - no fault needed
        if level == "unknown":
            description = f"Could not check disk space for run logs: {status.get('error', 'unknown error')}."
        else:
            description = (
                f"Disk space for run logs is {level} - {status['free_pct']:.1f}% free "
                f"({status['free_bytes'] / (1024**3):.2f} GB of {status['total_bytes'] / (1024**3):.2f} GB)."
            )
        fault = self.fault_manager.raise_fault(Fault(
            fault_type=FaultType.DISK_SPACE_LOW, severity=FaultSeverity.WARNING, description=description,
        ))
        self._on_fault_raised(fault)

    _CONFIG_DRIFT_CHECK_INTERVAL_S = 60.0

    def _check_config_drift(self) -> None:
        """Throttled - same pattern as _check_disk_space() above. No-op if no known-good config
        has ever been saved, or if the passive listener hasn't observed all three register groups
        yet - see machine_config_snapshot.py's module docstring for why this uses passive
        observation rather than an active read every tick."""
        now = time.monotonic()
        if now - self._last_config_drift_check_time < self._CONFIG_DRIFT_CHECK_INTERVAL_S:
            return
        self._last_config_drift_check_time = now

        known_good = load_known_good_config()
        if known_good is None:
            return
        pid = self.bus.get_latest_pid()
        fan_timing = self.bus.get_latest_fan_timing()
        temp_comp = self.bus.get_latest_temp_comp()
        if pid is None or fan_timing is None or temp_comp is None:
            return

        drift = compute_drift(known_good, pid, fan_timing, temp_comp)
        state = drift["has_drift"]
        if state == self._last_config_drift_state:
            return
        self._last_config_drift_state = state
        if not state:
            return  # recovered, or re-saved as known-good since the last check
        all_diffs = drift["pid_diffs"] + drift["fan_timing_diffs"] + drift["temp_comp_diffs"]
        description = "Machine configuration has drifted from the saved known-good reference: " + "; ".join(all_diffs)
        fault = self.fault_manager.raise_fault(Fault(
            fault_type=FaultType.CONFIG_DRIFT_DETECTED, severity=FaultSeverity.WARNING, description=description,
        ))
        self._on_fault_raised(fault)

    _SYSTEM_HEALTH_CHECK_INTERVAL_S = 60.0

    def _check_system_health(self) -> None:
        """Throttled - same pattern as _check_disk_space() above. No-op (never faults) when
        system_health.py reports unavailable (e.g. this app's occasional Windows dev-PC runs) -
        that's a permanent, expected condition there, not a transient problem."""
        now = time.monotonic()
        if now - self._last_system_health_check_time < self._SYSTEM_HEALTH_CHECK_INTERVAL_S:
            return
        self._last_system_health_check_time = now
        status = get_system_health()
        if not status["available"]:
            return
        level = status["level"]
        if level == self._last_system_health_level:
            return
        self._last_system_health_level = level
        if level == "ok":
            return
        description = (
            f"Host system resources are {level} - load average {status['load_avg'][0]:.2f} "
            f"({status['cpu_count']} core(s)), memory {status['mem_used_pct']:.1f}% used."
        )
        fault = self.fault_manager.raise_fault(Fault(
            fault_type=FaultType.SYSTEM_HEALTH_DEGRADED, severity=FaultSeverity.WARNING, description=description,
        ))
        self._on_fault_raised(fault)

    _AUTO_BACKUP_CHECK_INTERVAL_S = 3600.0

    def _check_automatic_backup(self) -> None:
        """Throttled purely to avoid a directory listing every poll tick - see
        automatic_backup.py's module docstring. No fault/dialog on success; a routine daily backup
        isn't something the operator needs interrupted for."""
        now = time.monotonic()
        if now - self._last_auto_backup_check_time < self._AUTO_BACKUP_CHECK_INTERVAL_S:
            return
        self._last_auto_backup_check_time = now
        try:
            automatic_backup.run_automatic_backup_if_due(self.safety_limits, self.monitor.config)
        except OSError:
            pass  # a failed automatic backup shouldn't take down the app - manual Export still works

    def _on_fault_raised(self, fault: Fault) -> None:
        self.alerts_list.addItem(f"[FAULT] {fault.description}")
        self.alerts_list.scrollToBottom()
        if self._run_tracker is not None:
            if fault.severity in (FaultSeverity.CRITICAL, FaultSeverity.EMERGENCY):
                self._run_tracker.record_fault()
            else:
                self._run_tracker.record_warning()
            self._run_tracker.record_event(f"[{fault.severity.value.upper()}] {fault.description}")
        if fault.severity in (FaultSeverity.CRITICAL, FaultSeverity.EMERGENCY):
            notifications.notify_async(self.webhook_config, "critical_fault", fault.description)
            reading = self.bus.get_live_reading()
            running_now = reading is not None and reading.running
            had_active_run = self._logging_active or self._run_tracker is not None

            def end_run_bookkeeping():
                # A critical fault is a KNOWN run ending - close the run out (write its summary,
                # clear run_recovery's marker) so it's never wrongly flagged INTERRUPTED at the
                # next startup. Bug found live 2026-09-10 (run_20260910_162151): this was gated on
                # reading.running, so a fault that fired while the running coil read False (coil
                # lag/flicker, or the machine had already dropped out of running) left the run in
                # limbo - marker never cleared, summary never written, "INTERRUPTED" next start.
                if self._logging_active or self._run_tracker is not None:
                    self._logging_active = False
                    self.logger.stop_run()
                    self._finalize_current_run()

            if running_now:
                # Review finding #1: do not claim success before the board has acknowledged the stop.
                fault.auto_action_taken = "Stop command sent and cooling requested - watch for a 'STOP NOT CONFIRMED' warning."
                notifications.notify_async(self.webhook_config, "run_failed", f"Run auto-stop requested: {fault.description}")

                def _after_stop(result):
                    if result is not None and result.ok:
                        end_run_bookkeeping()
                    else:
                        notifications.notify_async(self.webhook_config, "run_failed",
                                                   f"STOP NOT CONFIRMED after: {fault.description} - CHECK THE MACHINE NOW")
                        QMessageBox.critical(
                            self, "STOP NOT CONFIRMED",
                            "The machine did not acknowledge the automatic stop and may STILL BE HEATING.\n"
                            "Stop it at the machine now. The run is still being recorded.")
                self._run_write(lambda: self.bus.write_coil(COIL_RUNNING, False), _after_stop)
                # Roadmap's own emergency-handling sequence (Section 37) calls for enabling
                # cooling "if appropriate" after an auto-stop - heating was just forcibly cut, so
                # letting the zones sit at temperature with no active cooling is worse than
                # engaging it. Only if cooling isn't already on - never force it off (only-disable/
                # only-add pattern, same reasoning as _apply_control_gating).
                if not reading.cooling:
                    self._run_write(lambda: self.bus.write_coil(COIL_COOLING, True), lambda result: None)
            elif had_active_run:
                fault.auto_action_taken = "Run ended by this fault (the machine was already not in a running state)."
                notifications.notify_async(self.webhook_config, "run_failed", f"Run ended by fault: {fault.description}")
                end_run_bookkeeping()
            QMessageBox.critical(
                self, "SAFETY FAULT",
                f"{fault.description}\n\n{fault.auto_action_taken}\n\n"
                f"Acknowledge this fault (button in the top bar) before starting again.",
            )

    def _on_acknowledge_fault(self) -> None:
        self.fault_manager.acknowledge_all()
        self._active_violation_names.clear()

    def _update_curve_status(self) -> None:
        count, age = self.bus.get_curve_status()
        if count == 0:
            self.lbl_curve_status.setText(
                "Curve data: none observed yet - navigate to the curve editor screen on the "
                "touchscreen to make it send one"
            )
            self.lbl_curve_status.setStyleSheet("color: #c93; font-style: italic;")
            return

        if count > self._last_curve_count:
            self._last_curve_count = count
            if self.chk_autoload.isChecked():
                curve = self.bus.get_latest_curve()
                if curve is not None:
                    curve.name = self.profile_combo.currentText() or "FromMachine"
                    self.current_profile = curve
                    self._populate_table(curve)

        self.lbl_curve_status.setText(f"Curve data: {count} received, last {age:.0f}s ago")
        self.lbl_curve_status.setStyleSheet("color: #6a6; font-style: italic;")

    def _update_freshness_label(self, label: QLabel, name: str, count: int, age: float) -> None:
        if count == 0:
            label.setText(f"{name}: none observed yet - navigate to the Advanced Parameters screen on the touchscreen to make it send one")
            label.setStyleSheet("color: #c93; font-style: italic;")
            return
        label.setText(f"{name}: {count} received, last {age:.0f}s ago")
        label.setStyleSheet("color: #6a6; font-style: italic;")

    def _set_indicator(self, label: QLabel, active: bool, color: str) -> None:
        if active:
            label.setStyleSheet(f"background: {color}; color: white; padding: 4px 10px; border-radius: 4px; font-weight: bold;")
        else:
            label.setStyleSheet("background: #444; color: #aaa; padding: 4px 10px; border-radius: 4px;")

    def _show_alert(self, alert) -> None:
        prefix = {Severity.INFO: "[i]", Severity.WARNING: "[!]", Severity.CRITICAL: "[!!!]"}[alert.severity]
        self.alerts_list.addItem(f"{prefix} {alert.message}")
        self.alerts_list.scrollToBottom()
        if self._run_tracker is not None:
            self._run_tracker.record_warning()

    def closeEvent(self, event) -> None:
        self.bus.stop()
        super().closeEvent(event)


def main() -> None:
    if len(sys.argv) > 1:
        com_port = sys.argv[1]  # explicit override always wins - see web_server.main()'s identical logic
    else:
        # USB adapter persistent identity (2026-09-08, external feature-gap review, second pass) -
        # see connection_identity.py's module docstring. Falls back to the old hardcoded default
        # when nothing's been remembered yet, or the remembered adapter isn't currently plugged in.
        com_port = resolve_remembered_port() or "COM4"
    app = QApplication(sys.argv)
    window = MainWindow(com_port)
    window.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
