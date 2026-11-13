// LY-R690 web control panel. Mirrors pc-app/src/main_window.py's behavior, talking to
// web_server.py's JSON/SSE API instead of the Qt widgets talking directly to MachineBus.

const ROW_LABELS = [
  "Upper Temper", "Upper Rate", "Holding time",
  "Lower Temper", "Lower Rate", "Holding time",
  "IR Temper", "IR Rate", "Holding time",
];
const ROW_ATTRS = [
  "upper_temp", "upper_rate", "upper_hold_time",
  "lower_temp", "lower_rate", "lower_hold_time",
  "ir_temp", "ir_rate", "ir_hold_time",
];
const SECTIONS = 8;
const WETTING_DEFAULT = 217.0;
const MAX_GRAPH_POINTS = 600; // ~5 minutes at 500ms/point

const PID_SEGMENTS = 5;
const PID_ROWS = [
  ["Upper P", "upper", "p"], ["Upper I", "upper", "i"], ["Upper D", "upper", "d"],
  ["Lower P", "lower", "p"], ["Lower I", "lower", "i"], ["Lower D", "lower", "d"],
];

let lastCurveCount = 0;
let wettingTemp = WETTING_DEFAULT;
let latestReading = null;
let interruptedRunNoticeShown = false;
const plotData = { t: [], upper: [], lower: [], ir: [], probe: [] };
// Best-effort ESTIMATE, not a machine-reported fact - see alerts.py's RunMonitor.
// estimate_current_section() docstring. sectionMarkers holds {t, index} for each point where the
// estimate changed, so drawGraph() can render a timeline of section boundaries on the live graph.
let lastShownSectionIndex = null;
let currentSectionLabel = "";
let sectionMarkers = [];
// Target envelope (2026-09-07, external feature-gap review) - the current estimated section's
// own Upper/Lower/IR targets (see web_server.py's SSE payload), shaded +/- TARGET_ENVELOPE_
// TOLERANCE_C on the graph - see drawGraph()'s envelope block. null when no section is active.
let currentSectionTargets = null;
const TARGET_ENVELOPE_TOLERANCE_C = 5.0;

// ---------------- profile table ----------------

function buildTable() {
  const tbody = document.querySelector("#profile-table tbody");
  tbody.innerHTML = "";
  for (let row = 0; row < ROW_LABELS.length; row++) {
    const tr = document.createElement("tr");
    const th = document.createElement("td");
    th.textContent = ROW_LABELS[row];
    tr.appendChild(th);
    for (let col = 0; col < SECTIONS; col++) {
      const td = document.createElement("td");
      const input = document.createElement("input");
      input.type = "number";
      input.step = "1";
      input.dataset.row = row;
      input.dataset.col = col;
      input.value = "0";
      input.addEventListener("input", drawProfileChart);
      td.appendChild(input);
      tr.appendChild(td);
    }
    tbody.appendChild(tr);
  }
}

function readTable() {
  // Curve/profile registers (100-171) are RAW integers, not the x10 fixed-point scale used
  // elsewhere (PID, temp compensation, live readings) - confirmed ground truth in
  // docs/06-modbus-register-map.md (a live touchscreen edit of 130->180 landed at address 100 as
  // literally 180, not 1800). The old TEMP_ROWS x10 scaling here was wrong: sending e.g. 1200
  // instead of 120 for a target temperature got silently reset to 0 on the board - confirmed live
  // 2026-08-19 via the desktop app, same bug, same fix applies here.
  const sections = [];
  for (let col = 0; col < SECTIONS; col++) sections.push({});
  for (const input of document.querySelectorAll("#profile-table input")) {
    const row = parseInt(input.dataset.row, 10);
    const col = parseInt(input.dataset.col, 10);
    let value = parseFloat(input.value);
    if (isNaN(value)) value = 0;
    sections[col][ROW_ATTRS[row]] = Math.round(value);
  }
  return { name: document.getElementById("profile-name").value || "Untitled", sections };
}

function loadProfileByName(name) {
  fetch(`/api/profiles/${encodeURIComponent(name)}`).then((r) => {
    if (!r.ok) { alert("Not found: No local profile named '" + name + "'."); return null; }
    return r.json();
  }).then((profile) => { if (profile) populateTable(profile); });
}

function populateTable(profile) {
  for (const input of document.querySelectorAll("#profile-table input")) {
    const row = parseInt(input.dataset.row, 10);
    const col = parseInt(input.dataset.col, 10);
    input.value = profile.sections[col][ROW_ATTRS[row]] || 0;
  }
  if (profile.name) {
    document.getElementById("profile-name").value = profile.name;
    const select = document.getElementById("profile-select");
    select.value = Array.from(select.options).some((o) => o.value === profile.name) ? profile.name : "";
  }
  drawProfileChart();
}

// ---------------- graphical profile view + simulation ----------------
// Plots the profile's own TARGET values per section, not a physical prediction of actual
// temperature - see profile_simulation.py's module docstring (mirrored here) for why: no real
// thermal model, and the profile's own "rate" field has never-confirmed real-world units so ramp
// shape between sections can't be drawn either, hence flat per-section markers connected by
// straight lines rather than true steps.

function simulateProfile(profile, wettingTempC) {
  const sections = profile.sections;
  const totalHoldTime = sections.reduce((sum, s) => sum + Math.max(s.upper_hold_time, s.lower_hold_time, s.ir_hold_time), 0);
  const peakUpper = Math.max(...sections.map((s) => s.upper_temp));
  const peakLower = Math.max(...sections.map((s) => s.lower_temp));
  const peakIr = Math.max(...sections.map((s) => s.ir_temp));
  const timeAboveWetting = sections
    .filter((s) => s.upper_temp >= wettingTempC)
    .reduce((sum, s) => sum + Math.max(s.upper_hold_time, s.lower_hold_time, s.ir_hold_time), 0);
  return { totalHoldTime, peakUpper, peakLower, peakIr, timeAboveWetting };
}

function fmtMinSec(totalSeconds) {
  const m = Math.floor(totalSeconds / 60);
  const s = Math.round(totalSeconds % 60);
  return `${m}:${String(s).padStart(2, "0")}`;
}

function drawMarker(ctx, x, y, shape) {
  // ctx.fillStyle/strokeStyle are already set by the caller for this series' color.
  ctx.beginPath();
  if (shape === "square") {
    const r = 3.5;
    ctx.rect(x - r, y - r, r * 2, r * 2);
  } else if (shape === "triangle") {
    const r = 4.5;
    ctx.moveTo(x, y - r);
    ctx.lineTo(x + r, y + r);
    ctx.lineTo(x - r, y + r);
    ctx.closePath();
  } else {
    ctx.arc(x, y, 3, 0, 2 * Math.PI);
  }
  ctx.fill();
}

function drawProfileChart() {
  const profile = readTable();
  const canvas = document.getElementById("profile-graph");
  const ctx = canvas.getContext("2d");
  const w = canvas.width, h = canvas.height;
  ctx.clearRect(0, 0, w, h);

  const upperVals = profile.sections.map((s) => s.upper_temp);
  const lowerVals = profile.sections.map((s) => s.lower_temp);
  const irVals = profile.sections.map((s) => s.ir_temp);
  const allVals = [...upperVals, ...lowerVals, ...irVals, wettingTemp];
  const minY = Math.min(0, ...allVals) - 5;
  const maxY = Math.max(...allVals) + 5;
  const padL = 40, padB = 20, padT = 10, padR = 10;
  const plotW = w - padL - padR, plotH = h - padT - padB;
  const n = profile.sections.length;

  const xOf = (i) => padL + (n <= 1 ? plotW / 2 : (i / (n - 1)) * plotW);
  const yOf = (v) => padT + plotH - ((v - minY) / Math.max(1e-6, maxY - minY)) * plotH;

  ctx.strokeStyle = "#333";
  ctx.fillStyle = "#888";
  ctx.font = "11px sans-serif";
  ctx.lineWidth = 1;
  for (let i = 0; i <= 4; i++) {
    const v = minY + ((maxY - minY) * i) / 4;
    const y = yOf(v);
    ctx.beginPath();
    ctx.moveTo(padL, y);
    ctx.lineTo(w - padR, y);
    ctx.stroke();
    ctx.fillText(v.toFixed(0), 4, y + 4);
  }
  for (let i = 0; i < n; i++) {
    ctx.fillText(`S${i + 1}`, xOf(i) - 8, h - 4);
  }

  ctx.strokeStyle = "#e91e63";
  ctx.setLineDash([5, 4]);
  ctx.beginPath();
  ctx.moveTo(padL, yOf(wettingTemp));
  ctx.lineTo(w - padR, yOf(wettingTemp));
  ctx.stroke();
  ctx.setLineDash([]);

  // Distinct dash pattern + marker shape per zone, not just color - many real profiles
  // (confirmed live 2026-08-22 against imported profiles) set Upper and Lower to the exact same
  // target at every section, which made the solid Upper line completely invisible underneath an
  // identical Lower line drawn on top of it. Different dash patterns and marker shapes stay
  // visually distinguishable even when the underlying values are pixel-identical.
  const SERIES = [
    { vals: upperVals, color: "#e74c3c", dash: [], marker: "circle" },
    { vals: lowerVals, color: "#3498db", dash: [7, 4], marker: "square" },
    { vals: irVals, color: "#f39c12", dash: [2, 3], marker: "triangle" },
  ];
  for (const { vals, color, dash, marker } of SERIES) {
    ctx.strokeStyle = color;
    ctx.fillStyle = color;
    ctx.lineWidth = 2;
    ctx.setLineDash(dash);
    ctx.beginPath();
    vals.forEach((v, i) => {
      const x = xOf(i), y = yOf(v);
      if (i === 0) ctx.moveTo(x, y); else ctx.lineTo(x, y);
    });
    ctx.stroke();
    ctx.setLineDash([]);
    vals.forEach((v, i) => drawMarker(ctx, xOf(i), yOf(v), marker));
  }

  const sim = simulateProfile(profile, wettingTemp);
  document.getElementById("profile-sim").textContent =
    `Simulation (target values only, not a physical prediction): total hold time ${fmtMinSec(sim.totalHoldTime)} ` +
    `(excludes ramp time between sections) | peak Upper ${sim.peakUpper.toFixed(0)}C, Lower ${sim.peakLower.toFixed(0)}C, ` +
    `IR ${sim.peakIr.toFixed(0)}C | time with Upper target >= wetting (${wettingTemp.toFixed(0)}C): ${fmtMinSec(sim.timeAboveWetting)}`;
}

// ---------------- PID table ----------------

function buildPidTable() {
  const tbody = document.querySelector("#pid-table tbody");
  tbody.innerHTML = "";
  for (let row = 0; row < PID_ROWS.length; row++) {
    const [label, zone, gain] = PID_ROWS[row];
    const tr = document.createElement("tr");
    const th = document.createElement("td");
    th.textContent = label;
    tr.appendChild(th);
    for (let seg = 0; seg < PID_SEGMENTS; seg++) {
      const td = document.createElement("td");
      const input = document.createElement("input");
      input.type = "number";
      input.dataset.zone = zone;
      input.dataset.gain = gain;
      input.dataset.seg = seg;
      input.value = "0";
      td.appendChild(input);
      tr.appendChild(td);
    }
    tbody.appendChild(tr);
  }
}

function readPid() {
  const pid = {
    upper: { p: [0, 0, 0, 0, 0], i: [0, 0, 0, 0, 0], d: [0, 0, 0, 0, 0] },
    lower: { p: [0, 0, 0, 0, 0], i: [0, 0, 0, 0, 0], d: [0, 0, 0, 0, 0] },
  };
  for (const input of document.querySelectorAll("#pid-table input")) {
    const zone = input.dataset.zone, gain = input.dataset.gain, seg = parseInt(input.dataset.seg, 10);
    pid[zone][gain][seg] = Math.round(parseFloat(input.value) || 0);
  }
  pid.ir = {
    p: Math.round(parseFloat(document.getElementById("ir-p").value) || 0),
    i: Math.round(parseFloat(document.getElementById("ir-i").value) || 0),
    d: Math.round(parseFloat(document.getElementById("ir-d").value) || 0),
  };
  return pid;
}

function populatePid(pid) {
  for (const input of document.querySelectorAll("#pid-table input")) {
    const zone = input.dataset.zone, gain = input.dataset.gain, seg = parseInt(input.dataset.seg, 10);
    input.value = pid[zone][gain][seg];
  }
  document.getElementById("ir-p").value = pid.ir.p;
  document.getElementById("ir-i").value = pid.ir.i;
  document.getElementById("ir-d").value = pid.ir.d;
}

// ---------------- fan/vacuum timing fields ----------------

function readFanTiming() {
  return {
    vacuum_time: parseInt(document.getElementById("fan-vacuum-time").value, 10) || 0,
    fan_stop_temp: parseFloat(document.getElementById("fan-stop-temp").value) || 0,
    fan_stop_delay: parseInt(document.getElementById("fan-stop-delay").value, 10) || 0,
    advance_warning_time: parseInt(document.getElementById("fan-advance-warning").value, 10) || 0,
  };
}

function populateFanTiming(data) {
  document.getElementById("fan-vacuum-time").value = data.vacuum_time;
  document.getElementById("fan-stop-temp").value = data.fan_stop_temp;
  document.getElementById("fan-stop-delay").value = data.fan_stop_delay;
  document.getElementById("fan-advance-warning").value = data.advance_warning_time;
}

// ---------------- temperature compensation ----------------

function readTempComp() {
  return {
    upper: parseFloat(document.getElementById("tempcomp-upper").value) || 0,
    lower: parseFloat(document.getElementById("tempcomp-lower").value) || 0,
    ir: parseFloat(document.getElementById("tempcomp-ir").value) || 0,
    probe: parseFloat(document.getElementById("tempcomp-probe").value) || 0,
  };
}

function populateTempComp(data) {
  document.getElementById("tempcomp-upper").value = data.upper;
  document.getElementById("tempcomp-lower").value = data.lower;
  document.getElementById("tempcomp-ir").value = data.ir;
  document.getElementById("tempcomp-probe").value = data.probe;
}

// ---------------- live temperature gauges (2026-09-16) ----------------
// A semicircular arc per zone, replacing the old plain "Upper: 29.7C" text. Scaled 0 -> the
// zone's own configured safety-limit max (see loadSafetyLimits() below, which fills this in from
// /api/safety-limits - falls back to the safety_limits.py default of 320C until that first fetch
// completes). Reuses the exact 4 colors already used by the live-temperature graph's own legend
// (index.html's <i style="background:...">) so the gauges and the graph read as one consistent
// color language instead of inventing a new palette.
let gaugeMaxTemps = { upper: 320, lower: 320, ir: 320, probe: 320 };
const GAUGE_CONFIG = [
  { id: "gauge-upper", tempKey: "upper_temp", maxKey: "upper", zoneName: "Upper", color: "#e74c3c" },
  { id: "gauge-lower", tempKey: "lower_temp", maxKey: "lower", zoneName: "Lower", color: "#3498db" },
  { id: "gauge-ir", tempKey: "infrared_temp", maxKey: "ir", zoneName: "Infrared", color: "#f39c12" },
  { id: "gauge-probe", tempKey: "probe_temp", maxKey: "probe", zoneName: "Probe", color: "#2ecc71" },
];
// Center/radius of the semicircle arc path below ("M15,60 A40,40 0 0 1 95,60" - a standard top-
// half-circle: center (55,60), radius 40, sweeping from the left point up through the top to the
// right point). GAUGE_ARC_LEN is that arc's total length (half the circle's circumference) - the
// standard SVG "stroke-dasharray = length, animate stroke-dashoffset" technique for a partial-arc
// progress indicator.
const GAUGE_CX = 55, GAUGE_CY = 60, GAUGE_R = 40;
const GAUGE_ARC_LEN = Math.PI * GAUGE_R;

function buildGauges() {
  for (const cfg of GAUGE_CONFIG) {
    const container = document.getElementById(cfg.id);
    if (!container || container.dataset.built) continue;
    container.dataset.built = "1";
    container.innerHTML =
      `<svg viewBox="0 0 110 74" class="gauge-svg">` +
      `<path class="gauge-track" d="M15,60 A40,40 0 0 1 95,60"></path>` +
      `<path class="gauge-value-arc" d="M15,60 A40,40 0 0 1 95,60" stroke="${cfg.color}" ` +
      `style="stroke-dasharray:${GAUGE_ARC_LEN} ${GAUGE_ARC_LEN}; stroke-dashoffset:${GAUGE_ARC_LEN};"></path>` +
      `<line class="gauge-target-tick" x1="0" y1="0" x2="0" y2="0" style="display:none;"></line>` +
      `<text class="gauge-value-text" x="55" y="46" text-anchor="middle">--</text>` +
      `<text class="gauge-label-text" x="55" y="70" text-anchor="middle">${cfg.zoneName}</text>` +
      `</svg>`;
  }
}

function updateGauge(id, valueC, maxC, targetC) {
  const container = document.getElementById(id);
  if (!container) return;
  const arc = container.querySelector(".gauge-value-arc");
  const text = container.querySelector(".gauge-value-text");
  const tick = container.querySelector(".gauge-target-tick");
  const fraction = Math.max(0, Math.min(1, valueC / maxC));
  arc.style.strokeDasharray = `${GAUGE_ARC_LEN} ${GAUGE_ARC_LEN}`;
  arc.style.strokeDashoffset = `${GAUGE_ARC_LEN * (1 - fraction)}`;
  text.textContent = `${valueC.toFixed(1)}C`;
  if (targetC === null || targetC === undefined) {
    tick.style.display = "none";
    return;
  }
  // Target tick (2026-09-16) - a short radial mark at the nearest profile target for this zone
  // (process_intelligence.compute_zone_deviations - the same number already shown in the
  // process-status line below, "Xc from nearest target Yc"), so the gauge shows both where the
  // zone actually is AND where it's heading, at a glance. Probe has no profile-target concept at
  // all (see that module's docstring) so its gauge is never passed a targetC and never shows one.
  const targetFraction = Math.max(0, Math.min(1, targetC / maxC));
  const angle = Math.PI * (1 - targetFraction);
  const innerR = GAUGE_R - 8, outerR = GAUGE_R + 8;
  tick.setAttribute("x1", GAUGE_CX + innerR * Math.cos(angle));
  tick.setAttribute("y1", GAUGE_CY - innerR * Math.sin(angle));
  tick.setAttribute("x2", GAUGE_CX + outerR * Math.cos(angle));
  tick.setAttribute("y2", GAUGE_CY - outerR * Math.sin(angle));
  tick.style.display = "";
}

function updateAllGauges(reading, processStatus) {
  const targetsByZone = {};
  for (const d of (processStatus && processStatus.deviations) || []) {
    targetsByZone[d.zone] = d.nearest_target_c;
  }
  for (const cfg of GAUGE_CONFIG) {
    updateGauge(cfg.id, reading[cfg.tempKey], gaugeMaxTemps[cfg.maxKey], targetsByZone[cfg.zoneName]);
  }
}

// ---------------- live state (SSE) ----------------

function setIndicator(el, active, cls) {
  el.classList.toggle(cls, !!active);
}

// ---- Access control (review finding #13): any 401 means the session expired or was never created ----
(function () {
  const nativeFetch = window.fetch.bind(window);
  window.fetch = async (...args) => {
    const resp = await nativeFetch(...args);
    if (resp.status === 401 && !location.pathname.startsWith("/login")) location.href = "/login";
    return resp;
  };
})();

// ---- Stale-data awareness (review finding #16, 2026-11-12) -------------------------------------
// A lost stream used to leave the last temperatures, the RUNNING/IDLE label and enabled buttons on
// screen looking current. Now: a banner with the age of the last update, dimmed gauges, and every
// control except Stop disabled until fresh data returns. Stop stays reachable on purpose (recovery policy).
let lastStateAt = 0;            // performance.now() of the last SSE message
let streamErrored = false;
let serverTrouble = "";         // text from the server's own monitoring health, if any
let lastEnabledControls = [];

function dataIsStale() {
  return streamErrored || serverTrouble !== "" || lastStateAt === 0 || (performance.now() - lastStateAt) > 5000;
}

function renderStaleBanner() {
  let banner = document.getElementById("stale-banner");
  if (!banner) {
    banner = document.createElement("div");
    banner.id = "stale-banner";
    banner.setAttribute("role", "alert");
    banner.style.cssText = "position:sticky;top:0;z-index:9999;padding:10px 14px;font-weight:700;" +
      "background:#b00020;color:#fff;text-align:center;display:none";
    document.body.insertBefore(banner, document.body.firstChild);
  }
  const stale = dataIsStale();
  document.body.classList.toggle("data-stale", stale);
  if (stale) {
    const age = lastStateAt ? Math.round((performance.now() - lastStateAt) / 1000) : null;
    const why = serverTrouble || (streamErrored ? "Connection to the Pi lost - reconnecting..." : "No live data received.");
    banner.textContent = "STALE DATA - do not trust the numbers below. " + why +
      (age !== null ? ` Last update ${age}s ago.` : "") + " Stop remains available.";
    banner.style.display = "block";
  } else {
    banner.style.display = "none";
  }
  applyControlGating(lastEnabledControls);
}
setInterval(renderStaleBanner, 1000);

function connectEvents() {
  const es = new EventSource("/events");
  es.onmessage = (ev) => {
    const state = JSON.parse(ev.data);
    lastStateAt = performance.now();
    streamErrored = false;
    const mon = state.monitoring || {};
    const troubles = [];
    if (mon.bus_failure) troubles.push("Serial bus failed: " + mon.bus_failure + ".");
    if (mon.temps_age_s !== null && mon.temps_age_s !== undefined && mon.temps_age_s > 15)
      troubles.push(`No fresh temperatures from the machine for ${Math.round(mon.temps_age_s)}s.`);
    if (mon.status_age_s === null || mon.status_age_s === undefined || mon.status_age_s > 60)
      troubles.push("Machine status (running / self-tune) has not been read recently.");
    if (mon.failing_steps && mon.failing_steps.length) troubles.push("Monitoring step(s) failing: " + mon.failing_steps.join(", ") + ".");
    if (mon.protective_stop_pending) troubles.push("AUTOMATIC STOP NOT CONFIRMED - machine may still be heating, check it now.");
    serverTrouble = troubles.join(" ");
    renderStaleBanner();
    const r = state.reading;
    if (r && r.upper_temp !== undefined) {
      latestReading = r;
      updateAllGauges(r, state.process_status);
      setIndicator(document.getElementById("ind-running"), r.running, "active-running");
      setIndicator(document.getElementById("ind-vacuum"), r.vacuum, "active-vacuum");
      setIndicator(document.getElementById("ind-cooling"), r.cooling, "active-cooling");
      setIndicator(document.getElementById("ind-keep"), r.keep, "active-keep");

      plotData.t.push(state.t);
      plotData.upper.push(r.upper_temp);
      plotData.lower.push(r.lower_temp);
      plotData.ir.push(r.infrared_temp);
      plotData.probe.push(r.probe_temp);
      if (plotData.t.length > MAX_GRAPH_POINTS) {
        for (const k of ["t", "upper", "lower", "ir", "probe"]) plotData[k].shift();
      }

      const sectionIndex = state.process_status ? state.process_status.current_section_index : null;
      const sectionTotal = state.process_status ? state.process_status.current_section_total : null;
      if (sectionIndex !== null && sectionIndex !== undefined) {
        currentSectionLabel = `Section ${sectionIndex + 1} of ${sectionTotal} (estimated)`;
        if (sectionIndex !== lastShownSectionIndex) {
          sectionMarkers.push({ t: state.t, index: sectionIndex });
          lastShownSectionIndex = sectionIndex;
        }
        // Drop markers that have scrolled out of the graph's visible window for good - otherwise
        // this would grow unbounded over a long-running session (the Pi's web server can run for
        // days), even though only in-window markers are ever drawn.
        if (plotData.t.length) {
          sectionMarkers = sectionMarkers.filter((m) => m.t >= plotData.t[0]);
        }
      } else {
        currentSectionLabel = "";
        lastShownSectionIndex = null;
      }
      currentSectionTargets = state.process_status ? state.process_status.current_section_targets : null;

      drawGraph();
    }

    handleInterruptedRunNotice(state.interrupted_run_notice, state.process_status);
    updateActiveReadProgress(state.active_read_progress);
    updateActiveWriteProgress(state.active_write_progress);
    if (_startModalWriteProgressHook) _startModalWriteProgressHook(state.active_write_progress);
    updateActiveSearchProgress(state.active_search_progress);
    updateProcessStatus(state.process_status);
    updateCurveStatus(state.curve_count, state.curve_age);
    updatePidStatus(state.pid_count, state.pid_age);
    updateFanTimingStatus(state.fan_timing_count, state.fan_timing_age);
    updateTempCompStatus(state.temp_comp_count, state.temp_comp_age);

    if (state.machine_state) {
      const stateEl = document.getElementById("machine-state-value");
      stateEl.textContent = state.machine_state.toUpperCase();
      stateEl.className = `state-${state.machine_state}`;
      document.getElementById("btn-ack-fault").style.display = state.active_fault_count > 0 ? "inline-block" : "none";
      applyControlGating(state.enabled_controls || []);
    }

    const label = document.getElementById("active-profile-label");
    if (state.active_profile && state.active_profile !== "none") {
      label.textContent = `Monitoring against: ${state.active_profile}`;
      label.classList.add("set");
    }

    for (const alert of state.new_alerts || []) {
      const li = document.createElement("li");
      const prefix = { info: "[i]", warning: "[!]", critical: "[!!!]" }[alert.severity] || "";
      li.textContent = `${prefix} ${alert.message}`;
      li.className = alert.severity;
      const list = document.getElementById("alerts-list");
      list.appendChild(li);
      list.scrollTop = list.scrollHeight;
    }
  };
  es.onerror = () => {
    // EventSource auto-reconnects on its own; meanwhile make it obvious the screen is not live.
    streamErrored = true;
    renderStaleBanner();
    fetch("/api/state").catch(() => {});  // a 401 here sends the browser to /login (see the fetch wrapper)
  };
}

function handleInterruptedRunNotice(notice, processStatus) {
  // Only ever fires once per page load (see run_recovery.py's module docstring for why this
  // matters) - multiple open tabs/reconnects would otherwise each re-alert on the same notice
  // until it's acknowledged server-side. Deliberately waits for processStatus (a live reading) so
  // the alert can give a REAL, current running-state answer - 2026-09-08, external feature-gap
  // review, second pass - instead of just "check it yourself".
  if (!notice || interruptedRunNoticeShown || !processStatus) return;
  interruptedRunNoticeShown = true;
  const started = new Date(notice.started_at * 1000).toLocaleString();
  const liveState = processStatus.running
    ? "The machine's Running state is currently ON - it appears to still be actively executing the interrupted profile. If this wasn't intended, stop it now."
    : "The machine's Running state is currently OFF - it does not appear to still be actively running the interrupted profile.";
  alert(
    "POSSIBLE INTERRUPTED RUN DETECTED\n\n" +
    `Profile: ${notice.profile_name}\n` +
    `Started: ${started}\n` +
    `Log file: ${notice.log_filename}\n\n` +
    "This app restarted (or crashed) without a recorded Stop while this run was active.\n\n" +
    liveState + "\n\n" +
    "The run's history entry has been marked INTERRUPTED, not completed."
  );
  fetch("/api/interrupted-run/acknowledge", { method: "POST" });
}

const READ_PROGRESS_BUTTON_IDS = { "PID": "btn-pid-read", "fan timing": "btn-fan-read" };

function updateActiveReadProgress(progress) {
  // Surfaced through the existing SSE stream (see web_server.py's api_pid_read/
  // api_fan_timing_read) so a slow active read under real bus contention - confirmed live
  // 2026-08-22 taking tens of seconds - shows real progress instead of a static "Reading..."
  // that's indistinguishable from a hang.
  if (!progress) return;
  const btnId = READ_PROGRESS_BUTTON_IDS[progress.label];
  if (!btnId) return;
  const btn = document.getElementById(btnId);
  if (btn.disabled) {
    btn.textContent = `Reading ${progress.label}... (attempt ${progress.attempt}/${progress.max_attempts})`;
  }
}

const WRITE_PROGRESS_BUTTON_IDS = {
  "Starting": "btn-start", "Stopping": "btn-stop", "Vacuum": "btn-vacuum", "Cooling": "btn-cooling",
  // 2026-09-15: Keep never had a label at all server-side (see api_keep in web_server.py), so it
  // silently retried under contention with no visible feedback, unlike Vacuum/Cooling/Start/Stop.
  "Keep": "btn-keep",
  // 2026-09-13: the standalone "Send to machine" button never showed live chunk progress the way
  // Start/Stop/Vacuum/Cooling do - /api/curve/send reports the same {label:"Curve", attempt,
  // max_attempts, pass_num} shape (added when the pre-Start modal's status line needed it - see
  // _startModalWriteProgressHook below), this just also drives the button's own text with it.
  "Curve": "btn-send-machine",
};

function updateActiveWriteProgress(progress) {
  // Same idea as updateActiveReadProgress, for coil writes - confirmed live 2026-08-22 that
  // Start/Stop/Vacuum/Cooling can take 20-40+ seconds under bus contention, indistinguishable
  // from a hang without this.
  if (!progress) return;
  const btnId = WRITE_PROGRESS_BUTTON_IDS[progress.label];
  if (!btnId) return;
  const btn = document.getElementById(btnId);
  if (btn.disabled) {
    const passSuffix = progress.pass_num > 1 ? `, pass ${progress.pass_num}` : "";
    btn.textContent = `${progress.label}... (${progress.attempt}/${progress.max_attempts}${passSuffix})`;
  }
}

// Set while the pre-Start modal's "Send to machine" is in flight, so the SSE stream's live
// curve-write chunk progress ({label:"Curve", attempt, max_attempts, pass_num}) can drive the
// modal's status line. Cleared when the modal closes.
let _startModalWriteProgressHook = null;

function showStartModal(sendStatus, profile) {
  // sendStatus: "sent" | "unsent" | "empty" (from /api/preflight). Resolves "start" or "cancel".
  return new Promise((resolve) => {
    const modal = document.getElementById("start-modal");
    const title = document.getElementById("start-modal-title");
    const body = document.getElementById("start-modal-body");
    const statusEl = document.getElementById("start-modal-status");
    const sendBtn = document.getElementById("start-modal-send");
    const startBtn = document.getElementById("start-modal-start");
    const cancelBtn = document.getElementById("start-modal-cancel");
    const name = profile.name || "This profile";
    let curveSent = sendStatus === "sent";

    function setStatus(text, cls) {
      statusEl.textContent = text || "";
      statusEl.className = "modal-status" + (cls ? " " + cls : "");
    }
    function render() {
      if (sendStatus === "empty") {
        title.textContent = "No profile loaded";
        body.textContent =
          "The profile editor is empty - every curve value is 0. Starting now would run whatever " +
          "curve is already on the machine's control board, with nothing loaded here to monitor against.";
        sendBtn.hidden = true;
        startBtn.textContent = "Start anyway";
      } else if (curveSent) {
        title.textContent = "Start run";
        body.textContent =
          "Preflight checks passed. This will start an actual heating cycle on the physical machine.";
        sendBtn.hidden = true;
        startBtn.textContent = "Start";
      } else {
        title.textContent = "Profile not sent to the machine";
        body.textContent =
          `"${name}" is loaded in the editor but has NOT been sent to the machine from this app ` +
          "since you loaded it or last changed a value. The machine will heat to whatever curve is " +
          "currently on its control board - which may be a different profile. This app can't " +
          "reliably read the machine's curve back to check.";
        sendBtn.hidden = false;
        startBtn.textContent = "Start anyway";
      }
    }
    function setBusy(busy) {
      sendBtn.disabled = startBtn.disabled = cancelBtn.disabled = busy;
    }
    function close(choice) {
      _startModalWriteProgressHook = null;
      modal.style.display = "none";
      sendBtn.onclick = startBtn.onclick = cancelBtn.onclick = null;
      resolve(choice);
    }

    _startModalWriteProgressHook = (p) => {
      if (p && p.label === "Curve") {
        const passSuffix = p.pass_num > 1 ? `, pass ${p.pass_num}` : "";
        setStatus(`Sending curve to the machine... (chunk ${p.attempt}/${p.max_attempts}${passSuffix})`);
      }
    };

    sendBtn.onclick = () => {
      setBusy(true);
      setStatus("Sending curve to the machine...");
      postJson("/api/curve/send", profile).then((r) => {
        setBusy(false);
        if (!r.ok) {
          setStatus(`Send failed: ${r.detail}`, "error");
        } else if (r.verified_match === false) {
          setStatus(
            "The board ACK-confirmed the write, but a read-back does NOT match what was sent. " +
            "Verify on the touchscreen's Current Curve screen before starting.", "error");
        } else if (r.verified_match === true) {
          curveSent = true;
          setStatus(`Curve sent and read back exactly as written (attempt ${r.attempts}). Ready to start.`, "ok");
          render();
        } else {
          curveSent = true;
          setStatus(
            `Curve sent (${r.detail}). A read-back comparison couldn't be completed - glance at ` +
            "the touchscreen to confirm. You can start now.", "warn");
          render();
        }
      });
    };
    startBtn.onclick = () => close("start");
    cancelBtn.onclick = () => close("cancel");

    setStatus("");
    render();
    modal.style.display = "flex";
  });
}

function updateActiveSearchProgress(message) {
  // A profile search runs several web fetches plus one local-LLM call per page, which can
  // genuinely take minutes - the same "looks like a hang without feedback" problem as
  // everything else this session, just on a much longer timescale. Checks both buttons (2026-09-
  // 09: Paste URL added alongside Search) since either one can be the in-flight request driving
  // this same server-side progress string.
  if (!message) return;
  const searching = document.getElementById("btn-profile-search-go").disabled;
  const pastingUrl = document.getElementById("btn-profile-search-url-go").disabled;
  if (searching || pastingUrl) {
    document.getElementById("profile-search-status").textContent = message;
  }
}

function updateProcessStatus(status) {
  const el = document.getElementById("process-status");
  if (!status) {
    el.textContent = "Not running.";
    return;
  }
  let statusText;
  if (status.running && status.elapsed_s !== null && status.elapsed_s !== undefined) {
    const m = Math.floor(status.elapsed_s / 60);
    const s = Math.floor(status.elapsed_s % 60);
    statusText = `Running - elapsed ${m}m ${s}s`;
  } else {
    statusText = "Not running";
  }
  const devText = (status.deviations || []).map((d) => {
    if (d.deviation_c === null || d.deviation_c === undefined) {
      return `${d.zone} ${d.actual_c.toFixed(1)}C (no target in profile)`;
    }
    return `${d.zone} ${d.actual_c.toFixed(1)}C (${d.deviation_c.toFixed(1)}C from nearest target ${d.nearest_target_c.toFixed(0)}C)`;
  }).join("  ");
  const faultText = status.active_fault_count
    ? `${status.active_fault_count} active: ${status.active_faults.join("; ")}`
    : "No active faults";
  el.textContent = `${statusText}  |  ${devText}  |  ${faultText}`;
}

function updateCurveStatus(count, age) {
  const el = document.getElementById("curve-status");
  if (!count) {
    el.textContent = "Curve data: none observed yet - navigate to the curve editor screen on the touchscreen to make it send one";
    el.classList.remove("fresh");
    return;
  }
  el.textContent = `Curve data: ${count} received, last ${Math.round(age)}s ago`;
  el.classList.add("fresh");

  if (count > lastCurveCount) {
    lastCurveCount = count;
    if (document.getElementById("chk-autoload").checked) {
      fetch("/api/curve/latest").then((r) => (r.ok ? r.json() : null)).then((profile) => {
        if (profile) {
          profile.name = document.getElementById("profile-name").value || "FromMachine";
          populateTable(profile);
        }
      });
    }
  }
}

function updateFreshnessStatus(elId, label, count, age) {
  const el = document.getElementById(elId);
  if (!count) {
    el.textContent = `${label}: none observed yet - navigate to the Advanced Parameters screen on the touchscreen to make it send one`;
    el.classList.remove("fresh");
    return;
  }
  el.textContent = `${label}: ${count} received, last ${Math.round(age)}s ago`;
  el.classList.add("fresh");
}

// PID/fan-timing/temp-comp auto-populate (2026-09-16, real reported gap) - the exact same pattern
// updateCurveStatus() already used for the curve editor (see its own comment), just never
// finished for these three: /api/pid/latest, /api/fan-timing/latest, and /api/temp-comp/latest
// all existed server-side all along ("Cheap passive peek - see /api/curve/latest for the same
// pattern" - their own docstrings), but nothing ever called them. Reuses populatePid()/
// populateFanTiming()/populateTempComp() verbatim - the same functions the manual "Read from
// machine" buttons already use - since /latest and /read return the identical JSON shape
// (_pid_regs_to_json() etc are shared by both routes server-side). Clicking "Read from machine"
// also bumps this same passive-observation counter (read_pid_active() etc update it too, not just
// the passive listener), so a manual read harmlessly re-fetches and re-populates the same values
// it just showed - a wasted request, not a wrong one.
let lastPidCount = 0;
let lastFanTimingCount = 0;
let lastTempCompCount = 0;

function updatePidStatus(count, age) {
  updateFreshnessStatus("pid-status", "PID data", count, age);
  if (count > lastPidCount) {
    lastPidCount = count;
    if (document.getElementById("chk-pid-autoload").checked) {
      fetch("/api/pid/latest").then((r) => (r.ok ? r.json() : null)).then((pid) => { if (pid) populatePid(pid); });
    }
  }
}

function updateFanTimingStatus(count, age) {
  updateFreshnessStatus("fan-status", "Fan timing data", count, age);
  if (count > lastFanTimingCount) {
    lastFanTimingCount = count;
    if (document.getElementById("chk-fan-autoload").checked) {
      fetch("/api/fan-timing/latest").then((r) => (r.ok ? r.json() : null)).then((data) => { if (data) populateFanTiming(data); });
    }
  }
}

function updateTempCompStatus(count, age) {
  updateFreshnessStatus("tempcomp-status", "Temp compensation data", count, age);
  if (count > lastTempCompCount) {
    lastTempCompCount = count;
    if (document.getElementById("chk-tempcomp-autoload").checked) {
      fetch("/api/temp-comp/latest").then((r) => (r.ok ? r.json() : null)).then((data) => { if (data) populateTempComp(data); });
    }
  }
}

// ---------------- graph ----------------

function drawGraph() {
  const canvas = document.getElementById("graph");
  const ctx = canvas.getContext("2d");
  const w = canvas.width, h = canvas.height;
  ctx.clearRect(0, 0, w, h);

  if (plotData.t.length < 2) return;

  const allTemps = [...plotData.upper, ...plotData.lower, ...plotData.ir, ...plotData.probe, wettingTemp];
  const minY = Math.min(0, ...allTemps) - 5;
  const maxY = Math.max(...allTemps) + 5;
  const minX = plotData.t[0], maxX = plotData.t[plotData.t.length - 1];
  const padL = 40, padB = 24, padT = 10, padR = 10;
  const plotW = w - padL - padR, plotH = h - padT - padB;

  const xOf = (t) => padL + ((t - minX) / Math.max(1e-6, maxX - minX)) * plotW;
  const yOf = (v) => padT + plotH - ((v - minY) / Math.max(1e-6, maxY - minY)) * plotH;

  // grid + axis labels
  ctx.strokeStyle = "#333";
  ctx.fillStyle = "#888";
  ctx.font = "11px sans-serif";
  ctx.lineWidth = 1;
  for (let i = 0; i <= 4; i++) {
    const v = minY + ((maxY - minY) * i) / 4;
    const y = yOf(v);
    ctx.beginPath();
    ctx.moveTo(padL, y);
    ctx.lineTo(w - padR, y);
    ctx.stroke();
    ctx.fillText(v.toFixed(0), 4, y + 4);
  }

  // Target envelope - drawn behind the temperature lines (see the zone lines' loop right below),
  // one shaded band per zone around the currently estimated section's own target, matching the
  // desktop app's pg.LinearRegionItem bands. See currentSectionTargets' own comment for the data
  // source. A 0 target means "not controlled this section", same convention as everywhere else.
  if (currentSectionTargets) {
    const envelopeColors = [
      [currentSectionTargets.upper_temp, "rgba(231, 76, 60, 0.16)"],
      [currentSectionTargets.lower_temp, "rgba(52, 152, 219, 0.16)"],
      [currentSectionTargets.ir_temp, "rgba(243, 156, 18, 0.16)"],
    ];
    for (const [target, color] of envelopeColors) {
      if (!target) continue;
      const yTop = yOf(target + TARGET_ENVELOPE_TOLERANCE_C);
      const yBottom = yOf(target - TARGET_ENVELOPE_TOLERANCE_C);
      ctx.fillStyle = color;
      ctx.fillRect(padL, yTop, plotW, yBottom - yTop);
    }
  }

  // wetting line
  ctx.strokeStyle = "#e91e63";
  ctx.setLineDash([5, 4]);
  ctx.beginPath();
  ctx.moveTo(padL, yOf(wettingTemp));
  ctx.lineTo(w - padR, yOf(wettingTemp));
  ctx.stroke();
  ctx.setLineDash([]);

  const series = [
    [plotData.upper, "#e74c3c"],
    [plotData.lower, "#3498db"],
    [plotData.ir, "#f39c12"],
    [plotData.probe, "#2ecc71"],
  ];
  for (const [data, color] of series) {
    ctx.strokeStyle = color;
    ctx.lineWidth = 2;
    ctx.beginPath();
    for (let i = 0; i < data.length; i++) {
      const x = xOf(plotData.t[i]), y = yOf(data[i]);
      if (i === 0) ctx.moveTo(x, y);
      else ctx.lineTo(x, y);
    }
    ctx.stroke();
  }

  // Section boundary markers - estimated only, see currentSectionLabel/sectionMarkers comment.
  ctx.strokeStyle = "#888";
  ctx.fillStyle = "#aaa";
  ctx.font = "10px sans-serif";
  ctx.setLineDash([4, 3]);
  ctx.lineWidth = 1;
  for (const marker of sectionMarkers) {
    if (marker.t < minX || marker.t > maxX) continue;
    const x = xOf(marker.t);
    ctx.beginPath();
    ctx.moveTo(x, padT);
    ctx.lineTo(x, padT + plotH);
    ctx.stroke();
    ctx.fillText(`Sec ${marker.index + 1}`, x + 2, padT + 10);
  }
  ctx.setLineDash([]);

  if (currentSectionLabel) {
    ctx.fillStyle = "#ccc";
    ctx.font = "12px sans-serif";
    ctx.textAlign = "right";
    ctx.fillText(currentSectionLabel, w - padR, padT + 12);
    ctx.textAlign = "left";
  }
}

// ---------------- action buttons ----------------

// Buttons with a write currently in flight - see applyControlGating for why this exists: control
// gating needs to be able to RE-enable a button once the machine state allows it again (e.g.
// Start after Cooling turns back off), but must never fight an in-flight write's own disable/
// re-enable of that same button (confirmed live 2026-08-23 - Start toggled off by Cooling's
// gating stayed disabled forever afterward, since nothing else was allowed to turn it back on).
const busyButtonIds = new Set();

function withBusyButton(btn, fn) {
  busyButtonIds.add(btn.id);
  btn.disabled = true;
  const originalText = btn.textContent;
  return fn().finally(() => {
    busyButtonIds.delete(btn.id);
    btn.textContent = originalText;
    // Reapply the latest allowed-controls state instead of unconditionally re-enabling (review finding #16).
    btn.disabled = false;
    applyControlGating(lastEnabledControls);
  });
}

async function postJson(url, body) {
  // Never throws (2026-09-15, full-webapp bug pass) - a network failure (server unreachable, or a
  // connection dropped mid-request, e.g. during a service restart) or a non-JSON response used to
  // reject here, uncaught in nearly every caller's .then() chain (only a handful have their own
  // .catch()). From the operator's side that's a button click that silently does nothing at all -
  // no alert, no error, the busy state just clears via withBusyButton's .finally() -
  // indistinguishable from a hang. Falls back to a superset shape covering every response schema
  // this app's callers actually check (ok/detail for machine-action buttons, passed/checks for
  // preflight, errors/warnings/blocks_send for profile validation) so as many callers as possible
  // get a real, honest error message instead of a silent failure or a NEW uncaught error trying to
  // read a field that isn't there. See web_server.py's _handle_unhandled_exception for the other
  // half of this fix (guarantees the server itself always returns valid JSON, even on a crash).
  try {
    const res = await fetch(url, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body || {}),
    });
    return await res.json();
  } catch (err) {
    return {
      ok: false, detail: `Could not reach the server: ${err.message}`,
      passed: false, checks: [], errors: [], warnings: [], blocks_send: false,
    };
  }
}

function wireButtons() {
  document.getElementById("btn-start").addEventListener("click", () => {
    const profile = readTable();
    postJson("/api/preflight", { profile }).then((preflight) => {
      if (!preflight.passed) {
        const lines = preflight.checks.map((c) => `${c.passed ? "PASS" : "FAIL"}  ${c.name}: ${c.detail}`);
        alert("PREFLIGHT FAILED\n\n" + lines.join("\n") + "\n\nSTART BLOCKED");
        return;
      }
      // Pre-Start confirmation modal - lets the operator send the profile from here if it hasn't
      // been sent yet (see preflight.curve_send_status - the machine runs whatever curve is on its
      // control board, and this app can't reliably read that back to check).
      showStartModal(preflight.curve_send_status, profile).then((choice) => {
        if (choice !== "start") return;
        const btn = document.getElementById("btn-start");
        withBusyButton(btn, () =>
          postJson("/api/start", { profile }).then((result) => {
            if (!result.ok) alert(`Failed: Could not confirm Start was applied: ${result.detail}`);
          })
        );
      });
    });
  });

  document.getElementById("btn-stop").addEventListener("click", () => {
    if (latestReading && latestReading.running) {
      if (!confirm("Stop the active run?\n\nThis is a real write to the physical machine.")) return;
    }
    const btn = document.getElementById("btn-stop");
    withBusyButton(btn, () =>
      postJson("/api/stop").then((result) => {
        if (!result.ok) alert(`Failed: Could not confirm Stop was applied: ${result.detail}`);
      })
    );
  });

  for (const [id, url, label] of [
    ["btn-keep", "/api/keep", "KEEP"],
    ["btn-vacuum", "/api/vacuum", "VACUUM"],
    ["btn-cooling", "/api/cooling", "COOLING"],
  ]) {
    document.getElementById(id).addEventListener("click", () => {
      const btn = document.getElementById(id);
      withBusyButton(btn, () =>
        postJson(url).then((result) => {
          if (!result.ok) alert(`Failed: Could not confirm ${label} was applied: ${result.detail}`);
        })
      );
    });
  }

  document.getElementById("btn-send-machine").addEventListener("click", () => {
    const profile = readTable();
    postJson("/api/profiles/validate", profile).then((validation) => {
      if (validation.blocks_send) {
        alert("PROFILE VALIDATION\n\nERRORS (sending blocked):\n" + validation.errors.map((e) => `- ${e}`).join("\n"));
        return;
      }
      if (validation.warnings.length > 0) {
        const proceed = confirm("PROFILE VALIDATION\n\nWARNINGS:\n" + validation.warnings.map((w) => `- ${w}`).join("\n") + "\n\nSend anyway?");
        if (!proceed) return;
      }
      if (!confirm(`This will overwrite the curve currently loaded on the machine's control board with '${profile.name}'. This is a real write to the physical machine. Continue?`)) return;
      const btn = document.getElementById("btn-send-machine");
      // Pre-existing bug, found and fixed 2026-09-07 while adding a new button elsewhere:
      // withBusyButton captures btn.textContent as "original" at call time, BEFORE fn() runs -
      // setting the busy label here (before calling withBusyButton) meant it was itself captured
      // as "original", so the button was left permanently stuck on "Sending..." after every use.
      // The label must be set INSIDE the callback, same as every other button in this file.
      withBusyButton(btn, () => {
        btn.textContent = "Sending...";
        return postJson("/api/curve/send", profile).then((result) => {
          // verified_match (2026-09-07): a real read-back comparison, distinct from
          // independently_verified (which only means the board's write-command ACK was
          // confirmed, not that the value actually stuck - see web_server.py's api_curve_send).
          if (!result.ok) {
            alert(`Failed: Could not send the curve: ${result.detail}`);
          } else if (result.verified_match === true) {
            alert(`Profile Verified: Curve sent and read back exactly as written (attempt ${result.attempts}).`);
          } else if (result.verified_match === false) {
            alert(`READ-BACK MISMATCH: The board ACK-confirmed the write, but reading the curve back afterward does NOT match what was sent. Do not trust this profile until you verify it directly on the touchscreen's Current Curve screen.`);
          } else {
            alert(`Sent (unconfirmed): ${result.detail}\n\nA read-back comparison couldn't be completed. Please glance at the touchscreen to confirm.`);
          }
        });
      });
    });
  });

  document.getElementById("btn-predict-thermal").addEventListener("click", () => {
    const profile = readTable();
    const btn = document.getElementById("btn-predict-thermal");
    withBusyButton(btn, () =>
      postJson("/api/thermal-model/predict", { profile }).then((result) => {
        const el = document.getElementById("thermal-prediction");
        if (!result.available) {
          el.className = "unavailable";
          el.textContent = result.message || result.detail || "Not available.";
          return;
        }
        el.className = "";
        const zoneOrder = ["upper", "lower", "infrared"];
        const zoneLabel = { upper: "Upper", lower: "Lower", infrared: "Infrared" };
        const lines = zoneOrder
          .filter((zone) => result.predictions[zone])
          .map((zone) => {
            const p = result.predictions[zone];
            const fitNote = p.r_squared === null ? "flat model - no target variation seen yet" : `R2=${p.r_squared.toFixed(2)}`;
            return `${zoneLabel[zone]}: commanded ${p.commanded_peak_c.toFixed(0)}C -> predicted actual peak ` +
              `${p.predicted_peak_c.toFixed(1)}C (+/-${p.typical_error_c.toFixed(1)}C typical), ` +
              `~${(p.predicted_time_to_peak_s / 60).toFixed(1)} min to peak (${fitNote}, n=${p.sample_count} clean runs)`;
          });
        el.textContent = lines.length
          ? lines.join("\n")
          : "This profile has no real target for any zone the model has trained on.";
      })
    );
  });

  document.getElementById("btn-mirror").addEventListener("click", () => {
    postJson("/api/curve/mirror", readTable()).then((result) => {
      const label = document.getElementById("active-profile-label");
      label.textContent = `Monitoring against: ${result.name}`;
      label.classList.add("set");
    });
  });

  document.getElementById("btn-load").addEventListener("click", () => {
    const name = document.getElementById("profile-name").value;
    if (!name) return;
    loadProfileByName(name);
  });

  // A real <select> that always shows every saved profile on a single click, replacing the
  // previous <input list>/<datalist> combo - datalist suggestions only appear once the text is
  // cleared and re-typed in most browsers, which is not how a profile picker should behave
  // (confirmed live 2026-08-22: "the only way to bring up the profile is to delete the word
  // default then click the arrow"). Picking a profile here also loads it immediately, instead of
  // requiring a separate Load click.
  document.getElementById("profile-select").addEventListener("change", (e) => {
    const name = e.target.value;
    if (!name) return;
    document.getElementById("profile-name").value = name;
    loadProfileByName(name);
  });

  document.getElementById("btn-save").addEventListener("click", () => {
    const profile = readTable();
    postJson("/api/profiles", profile).then((result) => {
      // 2026-09-08 audit finding - postJson() never checked for an error response, so a genuine
      // save failure (e.g. a filename collision with a different profile - see
      // profile_store.save_profile()'s own docstring) previously showed the nonsensical
      // "Saved to undefined" instead of the real error, while still refreshing the list as if it
      // had worked.
      if (result.error) { alert(`Save failed: ${result.error}`); return; }
      alert(`Saved to ${result.path}`);
      refreshProfileList();
    });
  });

  document.getElementById("btn-rename").addEventListener("click", () => {
    const oldName = document.getElementById("profile-name").value;
    if (!oldName) return;
    const newName = prompt("New name:", oldName);
    if (!newName || !newName.trim() || newName === oldName) return;
    fetch(`/api/profiles/${encodeURIComponent(oldName)}/rename`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ new_name: newName.trim() }),
    }).then((r) => r.json()).then((result) => {
      if (result.error) { alert(result.error); return; }
      document.getElementById("profile-name").value = newName.trim();
      refreshProfileList();
    }).catch((err) => {
      alert(`Could not rename '${oldName}': ${err.message}`);
    });
  });

  document.getElementById("btn-delete").addEventListener("click", () => {
    const name = document.getElementById("profile-name").value;
    if (!name) return;
    if (!confirm(`Delete the saved local profile '${name}'? This only removes it from this app's local storage - it does not affect anything currently loaded on the machine.`)) return;
    fetch(`/api/profiles/${encodeURIComponent(name)}`, { method: "DELETE" }).then((r) => {
      // 2026-09-08 fix - this previously refreshed the list unconditionally regardless of
      // whether the delete actually succeeded, which is exactly how the <name>-route slash bug
      // (see web_server.py's profile routes) went unnoticed: the request 404'd, but the UI
      // looked like the delete had worked.
      if (!r.ok) {
        alert(`Could not delete '${name}' - the server returned an error (HTTP ${r.status}).`);
        return;
      }
      refreshProfileList();
    });
  });

  // Recipe metadata (2026-09-07, external feature-gap review) - replaces the old prompt() chain
  // now that there are 7 fields, not 3. See profile_validator.py's own docstring for why "status"
  // is purely a soft, non-blocking reminder, never enforced or auto-changed.
  document.getElementById("btn-metadata").addEventListener("click", () => {
    const name = document.getElementById("profile-name").value;
    if (!name) return;
    fetch(`/api/profiles/${encodeURIComponent(name)}/metadata`).then((r) => r.json()).then((existing) => {
      document.getElementById("meta-board").value = existing.board || "";
      document.getElementById("meta-component").value = existing.component || "";
      document.getElementById("meta-process-type").value = existing.process_type || "";
      document.getElementById("meta-solder").value = existing.solder || "";
      document.getElementById("meta-probe-placement").value = existing.probe_placement || "";
      document.getElementById("meta-status").value = existing.status || "";
      document.getElementById("meta-notes").value = existing.notes || "";
      const existingLimits = existing.process_limits || {};
      const setLimitField = (id, key) => {
        const v = existingLimits[key];
        document.getElementById(id).value = (v === null || v === undefined) ? "" : v;
      };
      setLimitField("meta-limit-min-tal", "min_time_above_wetting_s");
      setLimitField("meta-limit-max-tal", "max_time_above_wetting_s");
      setLimitField("meta-limit-max-upper", "max_peak_upper_c");
      setLimitField("meta-limit-max-lower", "max_peak_lower_c");
      setLimitField("meta-limit-max-ir", "max_peak_ir_c");
      document.getElementById("metadata-panel").style.display = "block";
    });
    // Profile qualification (2026-09-08, external feature-gap review, second pass) - purely
    // informational, shown alongside Status the same way the Draft-warning already is.
    fetch(`/api/profiles/${encodeURIComponent(name)}/qualification`).then((r) => r.json()).then((q) => {
      const el = document.getElementById("meta-qualification");
      el.textContent = q.qualified
        ? `Qualified: ${q.thermal_pass_count} thermal PASS run(s), ${q.repair_success_count} logged repair Success outcome(s).`
        : `Not yet qualified: ${q.thermal_pass_count} thermal PASS run(s), ${q.repair_success_count} logged repair Success outcome(s) (recommend >= ${q.threshold} of either before marking Approved).`;
    });
  });

  // Empty string -> null (not specified), never a fabricated 0 - see process_limits.py.
  function optionalNumberField(id) {
    const raw = document.getElementById(id).value;
    if (raw === "" || raw === null) return null;
    const n = parseFloat(raw);
    return isNaN(n) ? null : n;
  }

  document.getElementById("btn-metadata-cancel").addEventListener("click", () => {
    document.getElementById("metadata-panel").style.display = "none";
  });

  document.getElementById("btn-metadata-save").addEventListener("click", () => {
    const name = document.getElementById("profile-name").value;
    if (!name) return;
    const metadata = {
      board: document.getElementById("meta-board").value,
      component: document.getElementById("meta-component").value,
      process_type: document.getElementById("meta-process-type").value,
      solder: document.getElementById("meta-solder").value,
      probe_placement: document.getElementById("meta-probe-placement").value,
      status: document.getElementById("meta-status").value,
      notes: document.getElementById("meta-notes").value,
      process_limits: {
        min_time_above_wetting_s: optionalNumberField("meta-limit-min-tal"),
        max_time_above_wetting_s: optionalNumberField("meta-limit-max-tal"),
        max_peak_upper_c: optionalNumberField("meta-limit-max-upper"),
        max_peak_lower_c: optionalNumberField("meta-limit-max-lower"),
        max_peak_ir_c: optionalNumberField("meta-limit-max-ir"),
      },
    };
    postJson(`/api/profiles/${encodeURIComponent(name)}/metadata`, metadata).then((result) => {
      // 2026-09-08 audit finding - same class of bug as the profile Save button: this ignored
      // any error response and always claimed success.
      if (result && result.error) { alert(`Save failed: ${result.error}`); return; }
      document.getElementById("metadata-panel").style.display = "none";
      alert("Metadata saved.");
    });
  });

  document.getElementById("btn-history").addEventListener("click", () => {
    const name = document.getElementById("profile-name").value;
    if (!name) return;
    const panel = document.getElementById("profile-history-panel");
    if (panel.style.display !== "none") {
      panel.style.display = "none";
      return;
    }
    fetch(`/api/profiles/${encodeURIComponent(name)}/history`).then((r) => r.json()).then(({ current_version, history }) => {
      panel.innerHTML = "";
      const header = document.createElement("div");
      header.textContent = `Current version: ${current_version}`;
      panel.appendChild(header);
      if (history.length === 0) {
        const none = document.createElement("div");
        none.textContent = "No prior versions saved yet - every save from now on will be kept.";
        panel.appendChild(none);
      }
      for (const entry of [...history].reverse()) {
        const row = document.createElement("div");
        row.className = "history-row";
        const label = document.createElement("span");
        label.textContent = `Version ${entry.version} - saved ${new Date(entry.saved_at * 1000).toLocaleString()}`;
        row.appendChild(label);
        const btn = document.createElement("button");
        btn.textContent = "Restore";
        btn.addEventListener("click", () => {
          if (!confirm(`Restore version ${entry.version} of '${name}'? This loads it into the table and saves it as a new current version - it does not delete any history.`)) return;
          postJson(`/api/profiles/${encodeURIComponent(name)}/restore`, { version: entry.version }).then((profile) => {
            populateTable(profile);
            panel.style.display = "none";
          });
        });
        row.appendChild(btn);
        panel.appendChild(row);
      }
      panel.style.display = "block";
    });
  });

  document.getElementById("btn-recommend").addEventListener("click", () => {
    const profile = readTable();
    postJson("/api/profiles/recommend", { profile }).then((rec) => {
      if (!rec) {
        alert(`Need at least 3 logged runs of a profile named '${profile.name}' to look for a pattern.`);
        return;
      }
      alert(
        `PROFILE ANALYSIS - '${rec.profile_name}'\n\n` +
        `Based on ${rec.run_count} logged run(s).\n\n` +
        `Average peak Upper: ${rec.avg_peak_upper.toFixed(1)}C\n` +
        `Target Upper: ${rec.target_peak_upper.toFixed(0)}C\n\n` +
        rec.suggestion
      );
    });
  });

  document.getElementById("btn-import-file").addEventListener("click", () => {
    document.getElementById("file-import-input").click();
  });

  document.getElementById("btn-import-backup").addEventListener("click", () => {
    document.getElementById("file-backup-input").click();
  });

  document.getElementById("file-backup-input").addEventListener("change", (e) => {
    const file = e.target.files[0];
    if (!file) return;
    if (!confirm(`Restore configuration from '${file.name}'? This overwrites current safety limits and alert thresholds, and restores every profile in the backup into your local library (each as a new version, existing history preserved). Continue?`)) {
      e.target.value = "";
      return;
    }
    const formData = new FormData();
    formData.append("file", file);
    fetch("/api/backup/import", { method: "POST", body: formData })
      .then((r) => r.json().then((body) => ({ ok: r.ok, body })))
      .then(({ ok, body }) => {
        e.target.value = "";
        if (!ok) { alert(`Restore failed: ${body.error}`); return; }
        loadSafetyLimits();
        loadAlertConfig();
        refreshProfileList();
        alert(formatRestoreSummary(body));
      });
  });

  document.getElementById("file-import-input").addEventListener("change", (e) => {
    const file = e.target.files[0];
    if (!file) return;
    const statusEl = document.getElementById("import-status");
    statusEl.textContent = `Importing ${file.name}...`;
    statusEl.className = "";

    const formData = new FormData();
    formData.append("file", file);
    fetch("/api/profiles/import", { method: "POST", body: formData })
      .then((r) => r.json().then((body) => ({ ok: r.ok, body })))
      .then(({ ok, body }) => {
        if (!ok) {
          statusEl.textContent = `Import failed: ${body.error}`;
          statusEl.className = "error";
          return;
        }
        const lines = [];
        if (body.imported.length) {
          lines.push(`Imported ${body.imported.length} profile(s): ${body.imported.join(", ")}`);
        } else {
          lines.push("No profiles were imported.");
        }
        for (const w of body.warnings) lines.push(`- ${w}`);
        statusEl.textContent = lines.join("\n");
        statusEl.className = body.imported.length ? (body.warnings.length ? "warn" : "ok") : "error";
        refreshProfileList();
      })
      .catch((err) => {
        statusEl.textContent = `Import failed: ${err}`;
        statusEl.className = "error";
      })
      .finally(() => {
        e.target.value = "";
      });
  });

  document.getElementById("wetting-temp").addEventListener("change", (e) => {
    wettingTemp = parseFloat(e.target.value) || WETTING_DEFAULT;
    postJson("/api/wetting", { value: wettingTemp });
    drawProfileChart();
  });

  for (const [id, field] of [
    ["cfg-stuck-seconds", "stuck_sensor_seconds"],
    ["cfg-stuck-epsilon", "stuck_sensor_epsilon"],
    ["cfg-max-jump", "max_jump_per_reading"],
    ["cfg-section-deviation", "section_deviation_c"],
  ]) {
    document.getElementById(id).addEventListener("change", (e) => {
      const value = parseFloat(e.target.value);
      if (isNaN(value)) return;
      postJson("/api/alert-config", { [field]: value });
    });
  }

  for (const [id, field, isMinutes] of [
    ["cfg-max-upper", "max_upper_temp_c", false],
    ["cfg-max-lower", "max_lower_temp_c", false],
    ["cfg-max-ir", "max_ir_temp_c", false],
    ["cfg-max-probe", "max_probe_temp_c", false],
    ["cfg-max-ramp", "max_observed_ramp_c_per_min", false],
    ["cfg-min-ramp-interval", "min_ramp_sample_interval_s", false],
    ["cfg-max-duration", "max_run_duration_s", true],
    ["cfg-max-outage", "max_communication_outage_s", false],
  ]) {
    document.getElementById(id).addEventListener("change", (e) => {
      const value = parseFloat(e.target.value);
      if (isNaN(value)) return;
      const oldValue = e.target.dataset.lastValue !== undefined ? parseFloat(e.target.dataset.lastValue) : value;
      // Safety limits are enforcement-grade (a wrong value either fails to catch a real problem
      // or stops a perfectly good run) - the roadmap explicitly calls for confirmation here,
      // unlike the advisory Alert Thresholds above.
      if (value !== oldValue && !confirm(`Change ${field.replace(/_/g, " ")} from ${oldValue} to ${value}?\n\nThis changes when the software automatically stops a run for safety.`)) {
        e.target.value = oldValue;
        return;
      }
      e.target.dataset.lastValue = value;
      postJson("/api/safety-limits", { [field]: isMinutes ? value * 60.0 : value });
    });
  }

  // Not a hard safety limit (doesn't stop a run) - a display/logging correction, so no confirm
  // dialog here, unlike the loop above. See templates/index.html's #ir-reading-correction tooltip.
  document.getElementById("cfg-ir-correction").addEventListener("change", (e) => {
    const value = parseFloat(e.target.value);
    if (isNaN(value)) return;
    postJson("/api/safety-limits", { ir_reading_correction_c: value });
  });

  document.getElementById("btn-ack-fault").addEventListener("click", () => {
    postJson("/api/faults/acknowledge").then(() => {
      document.getElementById("btn-ack-fault").style.display = "none";
    });
  });

  document.getElementById("btn-pid-read").addEventListener("click", () => {
    const btn = document.getElementById("btn-pid-read");
    withBusyButton(btn, () => {
      btn.textContent = "Reading...";
      return fetch("/api/pid/read", { method: "POST" }).then((r) => {
        if (!r.ok) {
          alert("No response from the board after repeated attempts - if the bus is very busy, try again.");
          return null;
        }
        return r.json();
      }).then((pid) => { if (pid) populatePid(pid); });
    });
  });

  document.getElementById("btn-pid-send").addEventListener("click", () => {
    if (!confirm("This will overwrite the PID gains currently loaded on the machine's control board. This is a real write to the physical machine. Continue?")) return;
    const btn = document.getElementById("btn-pid-send");
    withBusyButton(btn, () =>
      postJson("/api/pid/send", readPid()).then((result) => {
        if (result.ok && result.independently_verified) {
          alert(`Sent: PID gains sent and confirmed (attempt ${result.attempts}).`);
        } else if (result.ok) {
          alert(`Sent (unconfirmed): ${result.detail}`);
        } else {
          alert(`Failed: Could not send PID gains: ${result.detail}`);
        }
      })
    );
  });

  let _pidHistoryOrdered = [];

  function diffPidValues(oldValues, newValues) {
    if (oldValues === null || oldValues === undefined) return [];
    const diffs = [];
    newValues.forEach((v, i) => {
      if (v !== oldValues[i]) diffs.push(`register ${i}: ${oldValues[i]} -> ${v}`);
    });
    return diffs;
  }

  function showPidHistoryDiff() {
    const select = document.getElementById("pid-history-select");
    const idx = select.selectedIndex;
    const diffEl = document.getElementById("pid-history-diff");
    if (idx < 0) {
      diffEl.textContent = "";
      return;
    }
    const entry = _pidHistoryOrdered[idx];
    const diffs = diffPidValues(entry.old_values, entry.new_values);
    diffEl.textContent = diffs.length ? diffs.join("\n") : "(no register-level differences to show)";
  }

  document.getElementById("btn-pid-history").addEventListener("click", () => {
    fetch("/api/pid/history").then((r) => r.json()).then((history) => {
      if (history.length === 0) {
        alert("No PID sends recorded yet - every future send will be logged here.");
        return;
      }
      _pidHistoryOrdered = [...history].reverse();  // newest first
      const select = document.getElementById("pid-history-select");
      select.innerHTML = "";
      for (const entry of _pidHistoryOrdered) {
        const when = new Date(entry.timestamp * 1000).toLocaleString();
        const diffs = diffPidValues(entry.old_values, entry.new_values);
        let summary;
        if (entry.old_values === null || entry.old_values === undefined) {
          summary = "(no prior read to compare against)";
        } else {
          summary = diffs.length ? `${diffs.length} register(s) changed` : "no values actually changed";
        }
        const note = entry.note ? ` [${entry.note}]` : "";
        const opt = document.createElement("option");
        opt.textContent = `${when} - ${summary}${note}`;
        select.appendChild(opt);
      }
      select.selectedIndex = 0;
      showPidHistoryDiff();
      document.getElementById("pid-history-panel").style.display = "block";
    });
  });

  document.getElementById("pid-history-select").addEventListener("change", showPidHistoryDiff);

  document.getElementById("btn-pid-history-close").addEventListener("click", () => {
    document.getElementById("pid-history-panel").style.display = "none";
  });

  document.getElementById("btn-pid-history-restore").addEventListener("click", () => {
    const idx = document.getElementById("pid-history-select").selectedIndex;
    if (idx < 0) return;
    const entry = _pidHistoryOrdered[idx];
    const when = new Date(entry.timestamp * 1000).toLocaleString();
    if (!confirm(`This will send the PID values from ${when} back to the machine, overwriting whatever is currently loaded. This is a real write to the physical machine. Continue?`)) return;
    const btn = document.getElementById("btn-pid-history-restore");
    withBusyButton(btn, () =>
      postJson("/api/pid/history/restore", { timestamp: entry.timestamp }).then((result) => {
        if (result.ok) {
          alert(`PID values from ${when} sent to the machine.`);
          document.getElementById("pid-history-panel").style.display = "none";
        } else {
          alert(`Failed: Could not restore PID values: ${result.detail}`);
        }
      })
    );
  });

  document.getElementById("btn-fan-read").addEventListener("click", () => {
    const btn = document.getElementById("btn-fan-read");
    withBusyButton(btn, () => {
      btn.textContent = "Reading...";
      return fetch("/api/fan-timing/read", { method: "POST" }).then((r) => r.json().then((data) => {
        // 2026-09-09: read the server's own error text (which now carries the real, confirmed
        // cause - see web_server.py's FAN_TIMING_TEMP_COMP_NO_RESPONSE_MESSAGE) instead of a
        // hardcoded string that would otherwise drift from it.
        if (!r.ok) {
          alert(data.error || "No response from the board after repeated attempts.");
          return null;
        }
        return data;
      })).then((data) => { if (data) populateFanTiming(data); });
    });
  });

  document.getElementById("btn-fan-send").addEventListener("click", () => {
    if (!confirm("This will overwrite the fan/vacuum timing settings on the machine's control board. This is a real write to the physical machine. Continue?")) return;
    const btn = document.getElementById("btn-fan-send");
    withBusyButton(btn, () =>
      postJson("/api/fan-timing/send", readFanTiming()).then((result) => {
        if (result.ok && result.independently_verified) {
          alert(`Sent: Fan timing sent and confirmed (attempt ${result.attempts}).`);
        } else if (result.ok) {
          alert(`Sent (unconfirmed): ${result.detail}`);
        } else {
          alert(`Failed: Could not send fan timing: ${result.detail}`);
        }
      })
    );
  });

  document.getElementById("btn-tempcomp-read").addEventListener("click", () => {
    const btn = document.getElementById("btn-tempcomp-read");
    withBusyButton(btn, () => {
      btn.textContent = "Reading...";
      return fetch("/api/temp-comp/read", { method: "POST" }).then((r) => r.json().then((data) => {
        if (!r.ok) {
          alert(data.error || "No response from the board after repeated attempts.");
          return null;
        }
        return data;
      })).then((data) => { if (data) populateTempComp(data); });
    });
  });

  document.getElementById("btn-tempcomp-send").addEventListener("click", () => {
    if (!confirm("This will overwrite the temperature compensation settings on the machine's control board. This register mapping is a HYPOTHESIS, not independently confirmed - verify against the touchscreen before trusting this. This is a real write to the physical machine. Continue?")) return;
    const btn = document.getElementById("btn-tempcomp-send");
    withBusyButton(btn, () =>
      postJson("/api/temp-comp/send", readTempComp()).then((result) => {
        if (result.ok && result.independently_verified) {
          alert(`Sent: Temp compensation sent and confirmed (attempt ${result.attempts}).`);
        } else if (result.ok) {
          alert(`Sent (unconfirmed): ${result.detail}`);
        } else {
          alert(`Failed: Could not send temp compensation: ${result.detail}`);
        }
      })
    );
  });

  const CAL_ZONE_READING_KEY = { upper: "upper_temp", lower: "lower_temp", ir: "infrared_temp", probe: "probe_temp" };

  function updateCalOffset(row) {
    const ref = parseFloat(row.querySelector(".cal-ref").value);
    const machine = parseFloat(row.querySelector(".cal-machine").value);
    const offsetCell = row.querySelector(".cal-offset");
    offsetCell.textContent = (isNaN(ref) || isNaN(machine)) ? "-" : (ref - machine).toFixed(1);
  }

  document.getElementById("btn-calibration-wizard").addEventListener("click", () => {
    const panel = document.getElementById("calibration-panel");
    if (panel.style.display !== "none") {
      panel.style.display = "none";
      return;
    }
    for (const row of document.querySelectorAll("#cal-table tbody tr")) {
      const zone = row.dataset.zone;
      const machineInput = row.querySelector(".cal-machine");
      if (latestReading && latestReading[CAL_ZONE_READING_KEY[zone]] !== undefined) {
        machineInput.value = latestReading[CAL_ZONE_READING_KEY[zone]].toFixed(1);
      }
      updateCalOffset(row);
    }
    panel.style.display = "block";
  });

  for (const input of document.querySelectorAll("#cal-table .cal-ref, #cal-table .cal-machine")) {
    input.addEventListener("input", (e) => updateCalOffset(e.target.closest("tr")));
  }

  document.getElementById("btn-cal-save").addEventListener("click", () => {
    const points = [];
    for (const row of document.querySelectorAll("#cal-table tbody tr")) {
      const ref = parseFloat(row.querySelector(".cal-ref").value);
      const machine = parseFloat(row.querySelector(".cal-machine").value);
      if (!isNaN(ref) && !isNaN(machine)) {
        points.push({ zone: row.dataset.zone, reference_c: ref, machine_c: machine });
      }
    }
    if (points.length === 0) {
      alert("Enter at least one zone's reference and machine reading before saving.");
      return;
    }
    const record = {
      operator: document.getElementById("cal-operator").value,
      reference_instrument: document.getElementById("cal-instrument").value,
      note: document.getElementById("cal-note").value,
      points,
    };
    postJson("/api/calibration", record).then((result) => {
      if (result.error) {
        alert(`Failed: ${result.error}`);
        return;
      }
      alert("Calibration record saved.");
      document.getElementById("cal-note").value = "";
    });
  });

  document.getElementById("btn-webhook-save").addEventListener("click", () => {
    postJson("/api/webhook", readWebhookForm()).then(() => {
      alert("Webhook settings saved.");
    });
  });

  document.getElementById("btn-webhook-test").addEventListener("click", () => {
    const url = document.getElementById("webhook-url").value.trim();
    if (!url) {
      alert("Enter a webhook URL first.");
      return;
    }
    postJson("/api/webhook/test", { url }).then((result) => {
      if (result.ok) {
        alert(`Success: ${result.detail}`);
      } else {
        alert(`Failed: ${result.detail}`);
      }
    });
  });

  document.getElementById("btn-ollama-refresh-models").addEventListener("click", () => {
    const host = document.getElementById("ollama-host").value.trim();
    if (!host) {
      alert("Enter an Ollama host URL first.");
      return;
    }
    postJson("/api/ollama-config/models", { host }).then((result) => {
      if (result.error) {
        alert(`Failed: ${result.error}`);
        return;
      }
      const select = document.getElementById("ollama-model");
      const current = select.value;
      select.innerHTML = "";
      for (const name of result.models) {
        const opt = document.createElement("option");
        opt.value = name;
        opt.textContent = name;
        select.appendChild(opt);
      }
      if (result.models.includes(current)) select.value = current;
      alert(`Found ${result.models.length} model(s).`);
    });
  });

  // Web search (DuckDuckGo, 2026-09-09) - reverted back here after briefly switching to the
  // Google Custom Search JSON API and then confirming live that Google closed that API to new
  // customers back in 2025 - see profile_search.py's module docstring for the full history.
  // Keyless, no config to save - only a way to test it's actually working right now, since
  // DuckDuckGo's own bot-detection can block this app's requests without warning.
  document.getElementById("btn-web-search-test").addEventListener("click", () => {
    const query = document.getElementById("web-search-test-query").value.trim() || "BGA rework reflow profile";
    const btn = document.getElementById("btn-web-search-test");
    // withBusyButton captures btn.textContent as "original" at call time, BEFORE fn() runs - so
    // the busy label must be set INSIDE the callback (see withBusyButton's own docstring).
    withBusyButton(btn, () => {
      btn.textContent = "Testing...";
      return postJson("/api/websearch/test", { query }).then((result) => {
        if (result.error) {
          alert(`Failed: ${result.error}`);
          return;
        }
        if (result.hits.length === 0) {
          alert("Connected OK, but 0 results for that query - try a broader query.");
          return;
        }
        const lines = result.hits.map((h) => `- ${h.title}\n  ${h.url}`).join("\n");
        alert(`Found ${result.hits.length} result(s) via ${result.provider || "web search"}:\n\n${lines}`);
      });
    });
  });

  // Search provider (2026-11-12): the API key is write-only - the server never sends it back, only
  // whether one is stored and its last 4 characters.
  function applySerperStatus(s) {
    document.getElementById("serper-enabled").checked = !!s.enabled;
    document.getElementById("serper-key-hint").textContent = s.configured ? `(a key ending ${s.key_hint} is stored)` : "(no key stored)";
    document.getElementById("web-search-provider-status").textContent =
      s.enabled && s.configured ? "Serper (Google results), DuckDuckGo as fallback" : "DuckDuckGo only";
  }
  window.loadSerperConfig = () => fetch("/api/serper-config").then((r) => r.json()).then(applySerperStatus).catch(() => {});
  document.getElementById("btn-serper-save").addEventListener("click", () => {
    const keyEl = document.getElementById("serper-key");
    postJson("/api/serper-config", { api_key: keyEl.value.trim(), enabled: document.getElementById("serper-enabled").checked })
      .then((res) => {
        if (res.ok === false) { alert(res.error || res.detail || "Could not save."); return; }
        keyEl.value = "";
        applySerperStatus(res);
        alert("Search settings saved.");
      });
  });

  document.getElementById("btn-ollama-save").addEventListener("click", () => {
    const config = {
      host: document.getElementById("ollama-host").value.trim(),
      model: document.getElementById("ollama-model").value,
      enabled: document.getElementById("ollama-enabled").checked,
    };
    postJson("/api/ollama-config", config).then(() => alert("Ollama settings saved."));
  });

  document.getElementById("btn-search-online").addEventListener("click", () => {
    const panel = document.getElementById("profile-search-panel");
    panel.style.display = panel.style.display === "none" ? "block" : "none";
  });

  function renderProfileSearchResult(result, emptyMessage) {
    // Shared by both Search (automatic DuckDuckGo) and Paste URL (manual, added 2026-09-09) -
    // both return the exact same {candidates, errors} shape from profile_search.py.
    const statusEl = document.getElementById("profile-search-status");
    const resultsEl = document.getElementById("profile-search-results");
    statusEl.textContent = `Done. ${result.candidates.length} candidate(s) found` +
      (result.errors.length ? `, ${result.errors.length} page(s) skipped (see below).` : ".");
    resultsEl.innerHTML = "";
    for (const candidate of result.candidates) {
      const row = document.createElement("div");
      row.className = "history-row";
      const bits = [candidate.ai_generated ? "AI ESTIMATE (no real source - model's own guess)" :
        (candidate.source_title || candidate.source_url)];
      if (candidate.machine_match) bits.push("[mentions LY-R690]");
      if (!candidate.ai_generated && !candidate.quotes_verified) bits.push("[UNVERIFIED - review carefully]");
      const label = document.createElement("span");
      label.textContent = bits.join(" - ");
      if (candidate.ai_generated) label.style.color = "#e74c3c";
      row.appendChild(label);
      if (candidate.notes && candidate.notes.some((n) => n && n.trim())) {
        const notesEl = document.createElement("div");
        notesEl.className = "history-point";
        notesEl.style.fontStyle = "italic";
        notesEl.textContent = candidate.notes.filter((n) => n && n.trim()).join(" | ");
        row.appendChild(notesEl);
      }
      // Real excerpts that informed an AI estimate (2026-11-12). They are verbatim text from web
      // pages, i.e. untrusted: built with textContent only (never innerHTML), and a link is made
      // only for an http(s) URL.
      if (candidate.evidence && candidate.evidence.length) {
        const details = document.createElement("details");
        details.className = "history-point";
        const summary = document.createElement("summary");
        summary.textContent = `Informed by ${candidate.evidence.length} real excerpt(s) from pages the search read - click to see them`;
        details.appendChild(summary);
        candidate.evidence.forEach((e, i) => {
          const item = document.createElement("div");
          item.appendChild(document.createTextNode(`[E${i + 1}] “${e.text}” — `));
          if (/^https?:\/\//i.test(e.source_url || "")) {
            const link = document.createElement("a");
            link.href = e.source_url;
            link.target = "_blank";
            link.rel = "noopener noreferrer";
            link.textContent = e.source_title || e.source_url;
            item.appendChild(link);
          } else {
            item.appendChild(document.createTextNode(e.source_title || ""));
          }
          details.appendChild(item);
        });
        row.appendChild(details);
      }
      const loadBtn = document.createElement("button");
      loadBtn.textContent = "Load into table";
      loadBtn.addEventListener("click", () => {
        let note;
        if (candidate.ai_generated) {
          note = "\n\nThis was NOT found on any real webpage - it is the AI model's own " +
            "guess, generated because no real documented profile could be found online. " +
            "Treat every value as an unverified starting point only.";
        } else {
          note = candidate.quotes_verified ? "" :
            "\n\nThe model's cited source quotes could NOT be verified against the actual webpage text - this candidate may be paraphrased or partly invented. Treat it with extra caution.";
        }
        const sourceLine = candidate.ai_generated ? "Source: none - AI-generated estimate" : `Source: ${candidate.source_url}`;
        const notesLines = (candidate.notes || []).filter((n) => n && n.trim());
        const notesBlock = notesLines.length ? `\n\n${notesLines.join("\n")}` : "";
        if (!confirm(
          `Load this into the Profile Editor table for review?\n\n${sourceLine}\n\n` +
          `Nothing is saved or sent automatically.${note}${notesBlock}`
        )) return;
        populateTable(candidate.profile);
        document.getElementById("profile-search-panel").style.display = "none";
      });
      row.appendChild(loadBtn);
      resultsEl.appendChild(row);
    }
    for (const err of result.errors) {
      const row = document.createElement("div");
      row.className = "history-point";
      row.textContent = `(skipped) ${err}`;
      resultsEl.appendChild(row);
    }
    if (result.candidates.length === 0 && result.errors.length === 0) {
      resultsEl.textContent = emptyMessage;
    }
  }

  document.getElementById("btn-profile-search-go").addEventListener("click", () => {
    const boardName = document.getElementById("profile-search-board").value.trim();
    if (!boardName) {
      alert("Enter a board / component name first.");
      return;
    }
    const btn = document.getElementById("btn-profile-search-go");
    document.getElementById("profile-search-results").innerHTML = "";
    document.getElementById("profile-search-status").textContent = "Starting search...";
    withBusyButton(btn, () =>
      postJson("/api/profile-search", { board_name: boardName }).then((result) => {
        renderProfileSearchResult(result, "No candidate profiles found - try a more specific or differently-worded board name.");
      })
    );
  });

  // Manual-URL mode (2026-09-09) - see profile_search.extract_profile_from_url's docstring for
  // why this exists (DuckDuckGo's bot-detection can block Search above without warning) and why
  // it deliberately never falls back to an AI-generated guess the way Search does.
  document.getElementById("btn-profile-search-url-go").addEventListener("click", () => {
    const boardName = document.getElementById("profile-search-board").value.trim();
    const url = document.getElementById("profile-search-url").value.trim();
    if (!boardName) {
      alert("Enter a board / component name first.");
      return;
    }
    if (!url) {
      alert("Paste a page URL first.");
      return;
    }
    const btn = document.getElementById("btn-profile-search-url-go");
    document.getElementById("profile-search-results").innerHTML = "";
    document.getElementById("profile-search-status").textContent = "Reading pasted URL...";
    withBusyButton(btn, () =>
      postJson("/api/profile-search/url", { board_name: boardName, url }).then((result) => {
        renderProfileSearchResult(result, "No profile found on that page.");
      })
    );
  });

  document.getElementById("btn-diag-reset").addEventListener("click", () => {
    fetch("/api/diagnostics/reset", { method: "POST" }).then((r) => r.json()).then(applyDiagnostics);
  });

  // Restart the web service without sudo (2026-11-12) - see web_server.py's /api/admin/restart for
  // what the server refuses and why. The server only answers {ok:true} once it has decided to exit;
  // it goes down a moment later, so wait for it to be seen DOWN and then UP again before reloading
  // (reloading while the old process is still answering would land on a dying server).
  document.getElementById("btn-restart-service").addEventListener("click", async () => {
    const btn = document.getElementById("btn-restart-service");
    const status = document.getElementById("restart-service-status");
    if (!confirm(
      "Restart the web service now?\n\nThe machine and the Pi itself are not affected. Live monitoring, " +
      "safety checks and logging pause for about 10 seconds. If the machine's state can't be read " +
      "right now, make sure it is NOT in the middle of a run before continuing."
    )) return;
    btn.disabled = true;
    status.textContent = "Requesting restart...";
    const body = await postJson("/api/admin/restart", { confirm: true });
    if (!body.ok) {
      status.textContent = body.detail || "Restart refused.";
      alert(status.textContent);
      btn.disabled = false;
      return;
    }
    status.textContent = "Restarting - this page will reload when the service is back...";
    const started = Date.now();
    let sawDown = false;
    while (Date.now() - started < 90000) {
      await new Promise((resolve) => setTimeout(resolve, 1500));
      try {
        const res = await fetch("/api/state", { cache: "no-store" });
        // Up again after having been down - or, if it was never seen down within 20s, assume the
        // restart was so quick that the gap fell between two polls.
        if (res.ok && (sawDown || Date.now() - started > 20000)) { location.reload(); return; }
      } catch (err) {
        sawDown = true;
      }
    }
    status.textContent = "The service did not come back within 90 seconds - check the Pi (journalctl -u ly-r690-web).";
    btn.disabled = false;
  });

  document.getElementById("btn-reg-viewer-read").addEventListener("click", readRegisters);

  document.getElementById("btn-cal-history").addEventListener("click", () => {
    const list = document.getElementById("cal-history-list");
    if (list.style.display !== "none") {
      list.style.display = "none";
      return;
    }
    fetch("/api/calibration").then((r) => r.json()).then((history) => {
      list.innerHTML = "";
      if (history.length === 0) {
        const none = document.createElement("div");
        none.textContent = "No calibration records saved yet.";
        list.appendChild(none);
      }
      for (const entry of [...history].reverse()) {
        const row = document.createElement("div");
        row.className = "history-row";
        const header = document.createElement("div");
        header.className = "history-row-header";
        const when = new Date(entry.timestamp * 1000).toLocaleString();
        header.textContent = `${when} - ${entry.operator || "(no operator)"} - ${entry.reference_instrument || "(no instrument noted)"}`;
        row.appendChild(header);
        for (const p of entry.points) {
          const line = document.createElement("div");
          line.className = "history-point";
          line.textContent = `${p.zone}: ref ${p.reference_c}C, machine ${p.machine_c}C, offset ${p.offset_c.toFixed(1)}C`;
          row.appendChild(line);
        }
        if (entry.note) {
          const note = document.createElement("div");
          note.className = "history-point";
          note.textContent = `Note: ${entry.note}`;
          row.appendChild(note);
        }
        list.appendChild(row);
      }
      list.style.display = "block";
    });
  });

  for (const [id, zone, label] of [
    ["btn-selftune-upper", "upper", "Upper"],
    ["btn-selftune-lower", "lower", "Lower"],
    ["btn-selftune-ir", "ir", "IR"],
  ]) {
    document.getElementById(id).addEventListener("click", () => {
      if (!confirm(
        `This starts a REAL self-tuning (PID autotune) routine on the ${label} zone. It will ` +
        `perturb the actual heating elements to characterize their response - this is a real ` +
        `physical action on the machine, not a settings change. Continue?`
      )) return;
      const btn = document.getElementById(id);
      withBusyButton(btn, () =>
        postJson(`/api/selftune/${zone}`, { value: true }).then((result) => {
          if (!result.ok) alert(`Failed: Could not confirm ${label} self-tuning was triggered: ${result.detail}`);
        })
      );
    });
  }

  document.getElementById("btn-refresh-runs").addEventListener("click", refreshRunHistory);
  document.getElementById("btn-compare-runs").addEventListener("click", compareSelectedRuns);
  document.getElementById("btn-close-run-detail").addEventListener("click", () => {
    document.getElementById("run-detail-panel").style.display = "none";
  });
  document.getElementById("btn-save-run-notes").addEventListener("click", () => {
    if (!_runDetailFilename) return;
    const notes = document.getElementById("run-detail-notes").value;
    postJson(`/api/runs/${encodeURIComponent(_runDetailFilename)}/notes`, { notes }).then(() => {
      refreshRunHistory();
    });
  });
  document.getElementById("btn-save-repair-outcome").addEventListener("click", () => {
    if (!_runDetailFilename) return;
    const outcome = document.getElementById("run-detail-repair-outcome").value;
    const failure_reason = document.getElementById("run-detail-repair-reason").value;
    postJson(`/api/runs/${encodeURIComponent(_runDetailFilename)}/repair-outcome`, { outcome, failure_reason }).then(() => {
      refreshRunHistory();
    });
  });
  document.getElementById("btn-refresh-maintenance").addEventListener("click", refreshMaintenanceStatus);
  document.getElementById("btn-save-maintenance-config").addEventListener("click", () => {
    const calibration_interval_days = parseFloat(document.getElementById("maint-calibration-interval").value);
    const self_tune_review_interval_days = parseFloat(document.getElementById("maint-selftune-interval").value);
    if (isNaN(calibration_interval_days) || isNaN(self_tune_review_interval_days)) return;
    postJson("/api/maintenance/config", { calibration_interval_days, self_tune_review_interval_days }).then(refreshMaintenanceStatus);
  });
  document.getElementById("btn-save-known-good-config").addEventListener("click", saveKnownGoodConfig);
  document.getElementById("btn-pid-save-known-good").addEventListener("click", saveKnownGoodConfig);
  document.getElementById("btn-check-config-drift").addEventListener("click", checkConfigDrift);
  document.getElementById("btn-restore-auto-backup").addEventListener("click", restoreAutomaticBackup);
  document.getElementById("btn-run-diagnostics").addEventListener("click", runDiagnostics);
  document.getElementById("btn-refresh-repair-stats").addEventListener("click", refreshRepairOutcomeStats);
  document.getElementById("repair-stats-profile").addEventListener("change", refreshRepairOutcomeStats);
  document.getElementById("btn-safety-limits-history").addEventListener("click", loadSafetyLimitsHistory);
  document.getElementById("safety-limits-history-select").addEventListener("change", showSafetyLimitsHistoryDiff);
  document.getElementById("btn-safety-limits-history-close").addEventListener("click", () => {
    document.getElementById("safety-limits-history-panel").style.display = "none";
  });
  document.getElementById("btn-safety-limits-history-restore").addEventListener("click", restoreSafetyLimitsHistory);

  // AI Process Assistant (2026-09-07) - see process_assistant.py's module docstring.
  // withBusyButton captures btn.textContent as "original" at call time, BEFORE fn() runs - so
  // the busy label must be set INSIDE the callback (not before calling withBusyButton), or the
  // busy text itself gets captured as "original" and the button never restores its real label.
  const askAssistant = () => {
    const question = document.getElementById("assistant-question").value.trim();
    if (!question) return;
    const answerEl = document.getElementById("assistant-answer");
    const btn = document.getElementById("btn-ask-assistant");
    answerEl.textContent = "";
    withBusyButton(btn, () => {
      btn.textContent = "Thinking...";
      return postJson("/api/process-assistant", { question }).then((result) => {
        answerEl.textContent = result.error ? `Error: ${result.error}` : result.answer;
      });
    });
  };
  document.getElementById("btn-ask-assistant").addEventListener("click", askAssistant);
  document.getElementById("assistant-question").addEventListener("keydown", (e) => {
    if (e.key === "Enter") askAssistant();
  });

  document.getElementById("btn-reconnect").addEventListener("click", () => {
    const port = document.getElementById("com-port-input").value.trim();
    if (!port) return;
    const btn = document.getElementById("btn-reconnect");
    withBusyButton(btn, () => {
      btn.textContent = "Connecting...";
      return postJson("/api/connection", { com_port: port }).then((result) => {
        btn.textContent = "Reconnect";
        applyConnectionStatus(result);
        if (!result.connected) alert(`Could not open serial port '${result.com_port}':\n${result.error}`);
      });
    });
  });

  document.getElementById("btn-refresh-ports").addEventListener("click", refreshPortList);

  document.getElementById("btn-test-port").addEventListener("click", () => {
    const port = document.getElementById("com-port-input").value.trim();
    if (!port) return;
    const btn = document.getElementById("btn-test-port");
    withBusyButton(btn, () => {
      btn.textContent = "Testing...";
      return postJson("/api/ports/test", { port }).then((result) => {
        btn.textContent = "Test";
        alert(result.detail);
      });
    });
  });
}

function refreshPortList() {
  fetch("/api/ports").then((r) => r.json()).then((ports) => {
    const list = document.getElementById("detected-ports");
    list.innerHTML = "";
    for (const p of ports) {
      const opt = document.createElement("option");
      opt.value = p.device;
      opt.label = p.description ? `${p.device} - ${p.description}` : p.device;
      list.appendChild(opt);
    }
  });
}

// ---------------- connection status ----------------
// COM port was previously only settable via a launch argument baked into the systemd service -
// no way to change it, or retry after a failed connection, without SSH access. Added 2026-08-19.

function applyConnectionStatus(status) {
  const input = document.getElementById("com-port-input");
  input.value = status.com_port;
  const el = document.getElementById("connection-status");
  if (status.connected) {
    el.textContent = `Connected: ${status.com_port}`;
    el.className = "connected";
  } else {
    el.textContent = `Not connected (${status.com_port})` + (status.error ? `: ${status.error}` : "");
    el.className = "disconnected";
  }
  // USB adapter persistent identity (2026-09-08, external feature-gap review, second pass) - see
  // connection_identity.py's module docstring. A hover tooltip, not a dedicated UI row - this
  // matters most for the headless auto-resolve-on-startup behavior, which needs no UI at all;
  // this is just visibility into what's remembered.
  const ra = status.remembered_adapter;
  input.title = ra
    ? `Remembered adapter: ${ra.description || "unknown device"} (serial ${ra.serial_number || "none"}), ` +
      `last used on ${ra.last_known_port}. Currently resolves to: ${ra.currently_resolves_to || "not found - check it is plugged in"}.`
    : "No remembered adapter yet - one is recorded automatically on first successful connection.";
}

function refreshConnectionStatus() {
  fetch("/api/connection").then((r) => r.json()).then(applyConnectionStatus);
}

function applyDiagnostics(diag) {
  const readRate = diag.read_attempts ? (diag.read_successes / diag.read_attempts * 100) : 0;
  const writeRate = diag.write_attempts ? (diag.write_successes / diag.write_attempts * 100) : 0;
  const avgText = diag.avg_response_time_s !== null && diag.avg_response_time_s !== undefined
    ? `${Math.round(diag.avg_response_time_s * 1000)} ms` : "n/a";
  document.getElementById("comm-diag").textContent =
    `Reads: ${diag.read_successes}/${diag.read_attempts} ok (${readRate.toFixed(0)}%)\n` +
    `Writes: ${diag.write_successes}/${diag.write_attempts} ok (${writeRate.toFixed(0)}%)\n` +
    `Retries: ${diag.retry_count}\n` +
    `Avg response time: ${avgText} (last ${diag.response_time_sample_count} sample(s))`;
}

function refreshDiagnostics() {
  fetch("/api/diagnostics").then((r) => r.json()).then(applyDiagnostics);
}

// ---------------- safety foundation ----------------
// Mirrors main_window.py's control gating - disables a control the current state doesn't allow,
// and RE-enables one the state now does allow, but only if that button has no write in flight
// (see busyButtonIds/withBusyButton). Without the busy check, gating and the busy-disable-during-
// a-write mechanism would race each other (confirmed as a real risk for a simpler case earlier
// this session: coil-toggle staleness). Without the re-enable half, a button disabled by an
// unrelated transition (e.g. Start disabled while Cooling was on) stayed disabled forever once
// that transition reversed, since nothing else was left to turn it back on - confirmed live
// 2026-08-23. See machine_state.py's module docstring for why this logic lives in one shared
// place rather than being reimplemented per frontend.

const CONTROL_BUTTON_IDS = {
  start: "btn-start", stop: "btn-stop", keep: "btn-keep",
  vacuum: "btn-vacuum", cooling: "btn-cooling", reconnect: "btn-reconnect",
  send_profile: "btn-send-machine", send_pid: "btn-pid-send",
};

function applyControlGating(enabledControls) {
  lastEnabledControls = enabledControls;
  const stale = dataIsStale();
  for (const [name, id] of Object.entries(CONTROL_BUTTON_IDS)) {
    const el = document.getElementById(id);
    if (!el || busyButtonIds.has(id)) continue;
    // While the data is stale only Stop stays usable - everything else would act on a state we cannot see.
    el.disabled = stale ? name !== "stop" : !enabledControls.includes(name);
  }
}

function loadSafetyLimits() {
  fetch("/api/safety-limits").then((r) => r.json()).then((limits) => {
    // Gauges scale 0 -> the zone's real configured max, not a hardcoded number - see
    // gaugeMaxTemps's own comment.
    gaugeMaxTemps = {
      upper: limits.max_upper_temp_c, lower: limits.max_lower_temp_c,
      ir: limits.max_ir_temp_c, probe: limits.max_probe_temp_c,
    };
    const fields = [
      ["cfg-max-upper", limits.max_upper_temp_c],
      ["cfg-max-lower", limits.max_lower_temp_c],
      ["cfg-max-ir", limits.max_ir_temp_c],
      ["cfg-max-probe", limits.max_probe_temp_c],
      ["cfg-max-ramp", limits.max_observed_ramp_c_per_min],
      ["cfg-min-ramp-interval", limits.min_ramp_sample_interval_s],
      ["cfg-max-duration", limits.max_run_duration_s / 60.0],
      ["cfg-max-outage", limits.max_communication_outage_s],
      ["cfg-ir-correction", limits.ir_reading_correction_c],
    ];
    for (const [id, value] of fields) {
      const el = document.getElementById(id);
      el.value = value;
      // Baseline for the change-confirmation check in wireButtons() - without this, the very
      // first edit after a page load would have no "old value" to compare against and would
      // never prompt for confirmation.
      el.dataset.lastValue = value;
    }
  });
}

function loadAlertConfig() {
  fetch("/api/alert-config").then((r) => r.json()).then((cfg) => {
    document.getElementById("wetting-temp").value = cfg.wetting_temp_c;
    wettingTemp = cfg.wetting_temp_c;
    document.getElementById("cfg-stuck-seconds").value = cfg.stuck_sensor_seconds;
    document.getElementById("cfg-stuck-epsilon").value = cfg.stuck_sensor_epsilon;
    document.getElementById("cfg-max-jump").value = cfg.max_jump_per_reading;
    document.getElementById("cfg-section-deviation").value = cfg.section_deviation_c;
    drawProfileChart();
  });
}

const WEBHOOK_EVENT_TYPES = ["run_started", "run_completed", "run_failed", "critical_fault"];

function loadWebhookConfig() {
  fetch("/api/webhook").then((r) => r.json()).then((cfg) => {
    document.getElementById("webhook-url").value = cfg.url || "";
    document.getElementById("webhook-enabled").checked = !!cfg.enabled;
    for (const event of WEBHOOK_EVENT_TYPES) {
      document.getElementById(`webhook-event-${event}`).checked = cfg.events ? !!cfg.events[event] : true;
    }
  });
}

function loadOllamaConfig() {
  fetch("/api/ollama-config").then((r) => r.json()).then((cfg) => {
    document.getElementById("ollama-host").value = cfg.host || "";
    document.getElementById("ollama-enabled").checked = !!cfg.enabled;
    const select = document.getElementById("ollama-model");
    if (cfg.model) {
      select.innerHTML = "";
      const opt = document.createElement("option");
      opt.value = cfg.model;
      opt.textContent = cfg.model;
      select.appendChild(opt);
    }
  });
}

function readWebhookForm() {
  const events = {};
  for (const event of WEBHOOK_EVENT_TYPES) {
    events[event] = document.getElementById(`webhook-event-${event}`).checked;
  }
  return {
    url: document.getElementById("webhook-url").value.trim(),
    enabled: document.getElementById("webhook-enabled").checked,
    events,
  };
}

// ---------------- tabs ----------------
// Mirrors the desktop app's QTabWidget (same 3 tabs) - previously every section was stacked in
// one long page here, requiring a lot of scrolling to reach anything below the graph.

const HEALTH_POLL_INTERVAL_MS = 2000;

function wireTabs() {
  for (const btn of document.querySelectorAll(".tab-btn")) {
    btn.addEventListener("click", () => {
      for (const b of document.querySelectorAll(".tab-btn")) b.classList.remove("active");
      for (const p of document.querySelectorAll(".tab-panel")) p.classList.remove("active");
      btn.classList.add("active");
      document.getElementById(btn.dataset.tab).classList.add("active");

      // System Health's sparklines only mean something while live-updating - poll only while this
      // tab is actually the one showing, not for the life of the page (see refreshSystemHealthTab's
      // own comment: the endpoint is cheap, but there's no reason to keep redrawing charts nobody's
      // looking at).
      if (healthPollTimer) {
        clearInterval(healthPollTimer);
        healthPollTimer = null;
      }
      if (btn.dataset.tab === "tab-system-health") {
        refreshSystemHealthTab();
        healthPollTimer = setInterval(refreshSystemHealthTab, HEALTH_POLL_INTERVAL_MS);
      }
    });
  }
}

function refreshProfileList() {
  fetch("/api/profiles").then((r) => r.json()).then((names) => {
    const select = document.getElementById("profile-select");
    const currentName = document.getElementById("profile-name").value;
    select.innerHTML = "";
    const placeholder = document.createElement("option");
    placeholder.value = "";
    placeholder.textContent = "-- saved profiles --";
    select.appendChild(placeholder);
    for (const name of names) {
      const opt = document.createElement("option");
      opt.value = name;
      opt.textContent = name;
      select.appendChild(opt);
    }
    // Keep the dropdown showing the currently-loaded profile (if it's one of the saved ones)
    // instead of always resetting to the placeholder after every refresh (e.g. after a Save).
    select.value = names.includes(currentName) ? currentName : "";
  });
}

// ---------------- run history ----------------
// Every run has always been logged to a CSV under pc-app/logs/ (see data_logger.py), but nothing
// surfaced that history back into either app's UI before now. Read-only, never touches the machine.

function refreshRunHistory() {
  fetch("/api/runs").then((r) => r.json()).then((runs) => {
    const tbody = document.querySelector("#run-history-table tbody");
    tbody.innerHTML = "";
    for (const run of runs) {
      const tr = document.createElement("tr");
      const checkTd = document.createElement("td");
      const checkbox = document.createElement("input");
      checkbox.type = "checkbox";
      checkbox.dataset.filename = run.filename;
      checkTd.appendChild(checkbox);
      tr.appendChild(checkTd);
      const cells = [
        run.started, run.interrupted ? `${run.profile_name} [INTERRUPTED]` : run.profile_name, run.duration_s.toFixed(0),
        run.peak_upper.toFixed(1), run.peak_lower.toFixed(1), run.peak_ir.toFixed(1),
        run.time_to_peak_upper_s.toFixed(0),
        run.fault_count === null || run.fault_count === undefined ? "-" : run.fault_count,
        run.warning_count === null || run.warning_count === undefined ? "-" : run.warning_count,
        // Added 2026-09-07 - see run_finalization.classify_process_result()'s docstring for
        // exactly what this is (and isn't - no target-window metallurgical judgment).
        run.process_result || "-",
      ];
      for (const value of cells) {
        const td = document.createElement("td");
        td.textContent = value;
        tr.appendChild(td);
      }
      // Golden-run marking (2026-09-07) - see run_finalization.set_run_golden()'s docstring for
      // why this reuses the existing compare-runs mechanism rather than building a separate one.
      const goldenTd = document.createElement("td");
      const goldenBtn = document.createElement("button");
      goldenBtn.textContent = run.golden ? "★" : "☆";  // filled/outline star
      goldenBtn.title = run.golden ? "Golden run - click to unmark" : "Mark as golden run";
      goldenBtn.className = "golden-star-btn";
      goldenBtn.addEventListener("click", () => {
        postJson(`/api/runs/${encodeURIComponent(run.filename)}/golden`, { golden: !run.golden }).then(() => {
          refreshRunHistory();
        });
      });
      goldenTd.appendChild(goldenBtn);
      tr.appendChild(goldenTd);
      const linkTd = document.createElement("td");
      const link = document.createElement("a");
      link.href = `/api/runs/${encodeURIComponent(run.filename)}`;
      link.textContent = "Download CSV";
      linkTd.appendChild(link);
      // "Details" (2026-09-07) - the event timeline + config snapshot were being saved into
      // every run's sidecar all along, but nothing ever surfaced them - see viewRunDetails().
      const detailBtn = document.createElement("button");
      detailBtn.textContent = "Details";
      detailBtn.style.marginLeft = "6px";
      detailBtn.addEventListener("click", () => viewRunDetails(run));
      linkTd.appendChild(detailBtn);
      // Delete (2026-09-16, real reported gap: no way to delete a run at all) - irreversible,
      // same confirm()-then-check-r.ok pattern already established for deleting a saved profile
      // (see btn-delete's own handler) rather than inventing a different convention here.
      const deleteBtn = document.createElement("button");
      deleteBtn.textContent = "Delete";
      deleteBtn.className = "btn-danger";
      deleteBtn.style.marginLeft = "6px";
      deleteBtn.addEventListener("click", () => {
        if (!confirm(`Permanently delete this run (${run.started}, ${run.profile_name})? This cannot be undone.`)) return;
        fetch(`/api/runs/${encodeURIComponent(run.filename)}`, { method: "DELETE" }).then((r) => {
          if (!r.ok) {
            r.json().then((body) => alert(`Could not delete run: ${body.detail || `HTTP ${r.status}`}`)).catch(() => alert(`Could not delete run: HTTP ${r.status}`));
            return;
          }
          refreshRunHistory();
        });
      });
      linkTd.appendChild(deleteBtn);
      tr.appendChild(linkTd);
      tbody.appendChild(tr);
    }
  });
  refreshThermalModelStatus();
}

let _runDetailFilename = null;

// Historical run graph (2026-09-16, explicit request: "just like if i had imported the csv into
// a csv to graphing tool ... automatic") - parses the run's own logged CSV (the same file
// "Download CSV" already serves) client-side and draws it with the exact same per-zone colors as
// the live graph/gauges. Kept separate from the live drawGraph() above since a historical run has
// no "current section"/live-target concept - it's a plain finished trace, not a moving window.

function parseCsvLine(line) {
  // Quote-aware comma split - a run's profile_name field could in principle contain a comma
  // (Python's csv.DictWriter quotes it correctly on write), so a naive line.split(",") would
  // silently misalign every column after it. Doesn't handle an embedded newline inside a quoted
  // field - not a realistic shape for anything this app ever logs (numbers, booleans, a name).
  const fields = [];
  let cur = "";
  let inQuotes = false;
  for (let i = 0; i < line.length; i++) {
    const c = line[i];
    if (inQuotes) {
      if (c === '"') {
        if (line[i + 1] === '"') { cur += '"'; i++; } else { inQuotes = false; }
      } else {
        cur += c;
      }
    } else if (c === '"') {
      inQuotes = true;
    } else if (c === ",") {
      fields.push(cur);
      cur = "";
    } else {
      cur += c;
    }
  }
  fields.push(cur);
  return fields;
}

function parseRunCsv(text) {
  const lines = text.split(/\r\n|\n/).filter((l) => l.length > 0);
  if (lines.length < 2) return [];
  const header = parseCsvLine(lines[0]);
  const idx = {};
  header.forEach((name, i) => { idx[name] = i; });
  const rows = [];
  for (let i = 1; i < lines.length; i++) {
    const fields = parseCsvLine(lines[i]);
    const elapsed_s = parseFloat(fields[idx.elapsed_s]);
    if (Number.isNaN(elapsed_s)) continue;
    rows.push({
      elapsed_s,
      upper_temp: parseFloat(fields[idx.upper_temp]),
      lower_temp: parseFloat(fields[idx.lower_temp]),
      infrared_temp: parseFloat(fields[idx.infrared_temp]),
      probe_temp: parseFloat(fields[idx.probe_temp]),
    });
  }
  return rows;
}

function drawRunDetailGraph(rows) {
  const canvas = document.getElementById("run-detail-graph");
  const ctx = canvas.getContext("2d");
  const w = canvas.width, h = canvas.height;
  ctx.clearRect(0, 0, w, h);

  const clean = rows.filter((r) =>
    Number.isFinite(r.elapsed_s) && Number.isFinite(r.upper_temp) && Number.isFinite(r.lower_temp) &&
    Number.isFinite(r.infrared_temp) && Number.isFinite(r.probe_temp)
  );
  if (clean.length < 2) {
    ctx.fillStyle = "#888";
    ctx.font = "13px sans-serif";
    ctx.fillText("Not enough logged data to graph.", 10, h / 2);
    return;
  }

  const allTemps = clean.flatMap((r) => [r.upper_temp, r.lower_temp, r.infrared_temp, r.probe_temp]);
  const minY = Math.min(0, ...allTemps) - 5;
  const maxY = Math.max(...allTemps) + 5;
  const minX = clean[0].elapsed_s, maxX = clean[clean.length - 1].elapsed_s;
  const padL = 40, padB = 24, padT = 10, padR = 10;
  const plotW = w - padL - padR, plotH = h - padT - padB;

  const xOf = (t) => padL + ((t - minX) / Math.max(1e-6, maxX - minX)) * plotW;
  const yOf = (v) => padT + plotH - ((v - minY) / Math.max(1e-6, maxY - minY)) * plotH;

  ctx.strokeStyle = "#333";
  ctx.fillStyle = "#888";
  ctx.font = "11px sans-serif";
  ctx.lineWidth = 1;
  for (let i = 0; i <= 4; i++) {
    const v = minY + ((maxY - minY) * i) / 4;
    const y = yOf(v);
    ctx.beginPath();
    ctx.moveTo(padL, y);
    ctx.lineTo(w - padR, y);
    ctx.stroke();
    ctx.fillText(v.toFixed(0), 4, y + 4);
  }

  // X-axis time labels (minutes) - unlike the live graph, where the right edge is always "now",
  // a historical trace benefits from knowing when things actually happened.
  ctx.textAlign = "center";
  for (let i = 0; i <= 4; i++) {
    const t = minX + ((maxX - minX) * i) / 4;
    ctx.fillText(`${(t / 60).toFixed(1)}m`, xOf(t), h - 6);
  }
  ctx.textAlign = "left";

  const series = [
    [clean.map((r) => r.upper_temp), "#e74c3c"],
    [clean.map((r) => r.lower_temp), "#3498db"],
    [clean.map((r) => r.infrared_temp), "#f39c12"],
    [clean.map((r) => r.probe_temp), "#2ecc71"],
  ];
  for (const [data, color] of series) {
    ctx.strokeStyle = color;
    ctx.lineWidth = 2;
    ctx.beginPath();
    for (let i = 0; i < data.length; i++) {
      const x = xOf(clean[i].elapsed_s), y = yOf(data[i]);
      if (i === 0) ctx.moveTo(x, y); else ctx.lineTo(x, y);
    }
    ctx.stroke();
  }
}

function viewRunDetails(run) {
  _runDetailFilename = run.filename;
  document.getElementById("run-detail-notes").value = run.notes || "";
  document.getElementById("run-detail-repair-outcome").value = run.repair_outcome || "";
  document.getElementById("run-detail-repair-reason").value = run.repair_failure_reason || "";
  // Independent of the summary fetch below - a run with no summary sidecar (predates that
  // feature, or never finalized) still has its raw CSV and should still get a graph.
  fetch(`/api/runs/${encodeURIComponent(run.filename)}`).then((r) => (r.ok ? r.text() : null)).then((text) => {
    drawRunDetailGraph(text ? parseRunCsv(text) : []);
  }).catch(() => drawRunDetailGraph([]));
  fetch(`/api/runs/${encodeURIComponent(run.filename)}/summary`).then((r) => {
    if (!r.ok) {
      return r.json().then((body) => { throw new Error(body.error || `HTTP ${r.status}`); });
    }
    return r.json();
  }).then((summary) => {
    document.getElementById("run-detail-title").textContent = `${run.started} (${run.profile_name})`;
    // process_result_basis/process_limit_violations (2026-09-07, external feature-gap review,
    // second pass) - see run_finalization.finalize_run()'s docstring.
    const resultLines = [`Result: ${summary.process_result || "-"}`];
    if (summary.process_result_basis === "process_limits") {
      resultLines.push("Basis: this profile's own configured process limits (see its metadata).");
      const violations = summary.process_limit_violations || [];
      if (violations.length) {
        resultLines.push("", "Violations:");
        violations.forEach((v) => resultLines.push(`  - ${v}`));
      } else {
        resultLines.push("", "No violations - within every configured limit.");
      }
    } else if (summary.process_result_basis === "conservative") {
      resultLines.push(
        "Basis: conservative fallback (no process limits configured for this profile) - FAIL only "
        + "means a fault occurred; INCOMPLETE means no fault but wetting was never reached, which "
        + "is deliberately ambiguous. Add process limits in this profile's metadata for a real "
        + "pass/fail window."
      );
    } else {
      resultLines.push("(This run predates process-result classification.)");
    }
    document.getElementById("run-detail-result").textContent = resultLines.join("\n");
    const events = summary.events || [];
    document.getElementById("run-detail-events").textContent = events.length
      ? events.map((e) => `[${e.elapsed_s.toFixed(1).padStart(8)}s]  ${e.description}`).join("\n")
      : "No events recorded for this run (predates the event timeline feature).";
    document.getElementById("run-detail-config").textContent = summary.config_snapshot
      ? JSON.stringify(summary.config_snapshot, null, 2)
      : "No configuration snapshot recorded for this run (predates the config-snapshot feature).";
    document.getElementById("run-detail-panel").style.display = "block";
  }).catch((err) => {
    alert(`Could not load run details: ${err.message}`);
  });
}

function refreshThermalModelStatus() {
  fetch("/api/thermal-model/status").then((r) => r.json()).then((status) => {
    document.getElementById("thermal-model-status").textContent = status.message;
  });
}

// ---------------- maintenance (2026-09-07, external feature-gap review) ----------------
// Everything shown here is built from data already logged (run history, calibration records,
// self-tune sessions) - see maintenance.py's own module docstring for why nothing here is a
// fabricated hardware-wear estimate.

function statusBadge(status) {
  const labels = { ok: "OK", due_soon: "Due soon", overdue: "OVERDUE", never: "Never" };
  const span = document.createElement("span");
  span.className = `status-badge ${status}`;
  span.textContent = labels[status] || status;
  return span;
}

function formatDaysSince(days) {
  return days === null || days === undefined ? "-" : `${days.toFixed(1)} days ago`;
}

function formatTimestamp(ts) {
  return ts === null || ts === undefined ? "Never" : new Date(ts * 1000).toLocaleString();
}

function refreshMaintenanceStatus() {
  refreshAutomaticBackups();
  populateRepairStatsProfileDropdown();
  refreshRepairOutcomeStats();
  fetch("/api/maintenance").then((r) => r.json()).then((status) => {
    document.getElementById("maint-calibration-interval").value = status.config.calibration_interval_days;
    document.getElementById("maint-selftune-interval").value = status.config.self_tune_review_interval_days;

    const runs = status.runs;
    document.getElementById("maint-run-summary").textContent =
      `${runs.total_runs} total run(s), ${runs.total_run_hours.toFixed(1)} total logged hours, ` +
      `${runs.total_faults} fault(s), ${runs.total_warnings} warning(s) ` +
      `(${runs.runs_with_finalization_data} of ${runs.total_runs} run(s) have full data).`;

    const calBody = document.querySelector("#maint-calibration-table tbody");
    calBody.innerHTML = "";
    for (const [zone, info] of Object.entries(status.calibration)) {
      const tr = document.createElement("tr");
      const cells = [zone.charAt(0).toUpperCase() + zone.slice(1), formatTimestamp(info.last_calibrated_at), formatDaysSince(info.days_since)];
      for (const value of cells) {
        const td = document.createElement("td");
        td.textContent = value;
        tr.appendChild(td);
      }
      const statusTd = document.createElement("td");
      statusTd.appendChild(statusBadge(info.status));
      tr.appendChild(statusTd);
      calBody.appendChild(tr);
    }

    const stBody = document.querySelector("#maint-selftune-table tbody");
    stBody.innerHTML = "";
    for (const [zone, info] of Object.entries(status.self_tune)) {
      const tr = document.createElement("tr");
      const cells = [zone.charAt(0).toUpperCase() + zone.slice(1), formatTimestamp(info.last_self_tuned_at), formatDaysSince(info.days_since), info.session_count];
      for (const value of cells) {
        const td = document.createElement("td");
        td.textContent = value;
        tr.appendChild(td);
      }
      const statusTd = document.createElement("td");
      statusTd.appendChild(statusBadge(info.status));
      tr.appendChild(statusTd);
      stBody.appendChild(tr);
    }
  });
}

// Known-good machine configuration + drift detection (2026-09-08, external feature-gap review,
// second pass) - see machine_config_snapshot.py's module docstring.

function refreshKnownGoodConfigStatus() {
  fetch("/api/machine-config/known-good").then((r) => r.json()).then((data) => {
    const el = document.getElementById("known-good-config-status");
    el.textContent = data.exists
      ? `Known-good configuration saved: ${new Date(data.captured_at * 1000).toLocaleString()}`
      : "No known-good configuration saved yet.";
  });
}

function saveKnownGoodConfig(event) {
  // Reachable from two buttons (the Maintenance tab, and the PID Gains panel - see
  // btn-pid-save-known-good) - operate on whichever one was actually clicked (event.target), not
  // a hardcoded id, so the OTHER entry point doesn't sit there un-disabled/un-labeled while a
  // save is in flight (same bug class just fixed on the Run Diagnostics button).
  const btn = event.target;
  withBusyButton(btn, () => {
    btn.textContent = "Saving...";
    return fetch("/api/machine-config/save-known-good", { method: "POST" }).then((r) => r.json()).then((data) => {
      if (!data.ok) {
        alert(`Could not save known-good configuration: ${data.detail || "unknown error"}`);
        return;
      }
      refreshKnownGoodConfigStatus();
      alert("Current machine configuration saved as known-good.");
    });
  });
}

function checkConfigDrift() {
  const btn = document.getElementById("btn-check-config-drift");
  withBusyButton(btn, () => {
    // Immediate feedback (2026-09-08 fix - same bug class as Run Diagnostics: this involves a
    // real active read of the board, which can take a few seconds under bus contention, but
    // previously gave no visible sign anything was happening while it ran) - see
    // withBusyButton()'s own docstring for why the busy label is set HERE, inside the callback.
    btn.textContent = "Checking...";
    const result = document.getElementById("config-drift-result");
    result.textContent = "Checking against the machine now (this reads PID and fan-timing/compensation directly from the board, which can take a few seconds)...";
    return fetch("/api/machine-config/check", { method: "POST" }).then((r) => {
      if (!r.ok) {
        return r.json().then((body) => { throw new Error(body.error || `HTTP ${r.status}`); });
      }
      return r.json();
    }).then((drift) => {
      const allDiffs = [...drift.pid_diffs, ...drift.fan_timing_diffs, ...drift.temp_comp_diffs];
      result.textContent = drift.has_drift
        ? "DRIFT DETECTED:\n" + allDiffs.map((d) => `- ${d}`).join("\n")
        : "No drift - matches the saved known-good reference.";
    }).catch((err) => {
      result.textContent = "";
      alert(`Could not check configuration: ${err.message}`);
    });
  });
}

// ---------------- System Health tab (2026-09-15) ----------------
// Card grid styled after Fluidd/Klipper's "System Utilization" panel - a small sparkline above a
// label/value row per metric. Combines the Pi's own host health (/api/system-health's original
// fields) with its Modbus link's comm-diagnostics counters and disk usage (both bundled into the
// same endpoint - see api_system_health()'s docstring). Only polls while this tab is actually
// visible (see wireTabs() below) - the endpoint itself is cheap (no fresh Modbus transaction, see
// its docstring) but there's no reason to keep redrawing sparklines nobody's looking at.
const HEALTH_CARDS = [
  { id: "cpu-load", label: "Pi CPU Load" },
  { id: "memory", label: "Pi Memory" },
  { id: "disk", label: "Pi Disk Used" },
  { id: "read-success", label: "Modbus Read Success" },
  { id: "write-success", label: "Modbus Write Success" },
  { id: "retry-rate", label: "Modbus Retries / Request" },
  { id: "response-time", label: "Modbus Response Time" },
  { id: "uptime", label: "Pi Uptime" },
];
const HEALTH_HISTORY_LENGTH = 60;
const healthHistory = {};
let healthPollTimer = null;

function buildSystemHealthGrid() {
  const grid = document.getElementById("system-health-grid");
  if (grid.children.length) return; // already built - just update in place from here on
  for (const card of HEALTH_CARDS) {
    healthHistory[card.id] = [];
    const div = document.createElement("div");
    div.className = "health-card";
    div.id = `health-card-${card.id}`;
    div.innerHTML =
      `<svg class="health-sparkline" viewBox="0 0 200 50" preserveAspectRatio="none">` +
      `<polyline id="health-spark-${card.id}" points=""></polyline></svg>` +
      `<div class="health-card-row">` +
      `<span class="health-card-label">${card.label}</span>` +
      `<span class="health-card-value" id="health-value-${card.id}">-</span></div>`;
    grid.appendChild(div);
  }
}

function pushHealthSample(id, displayText, pct, level) {
  const hist = healthHistory[id];
  hist.push(pct == null ? 0 : Math.max(0, Math.min(100, pct)));
  if (hist.length > HEALTH_HISTORY_LENGTH) hist.shift();
  const points = hist.map((v, i) => {
    const x = (i / Math.max(1, HEALTH_HISTORY_LENGTH - 1)) * 200;
    const y = 48 - (v / 100) * 46;
    return `${x.toFixed(1)},${y.toFixed(1)}`;
  }).join(" ");
  document.getElementById(`health-spark-${id}`).setAttribute("points", points);
  document.getElementById(`health-value-${id}`).textContent = displayText;
  const card = document.getElementById(`health-card-${id}`);
  card.className = `health-card level-${level}`;
}

function refreshSystemHealthTab() {
  buildSystemHealthGrid();
  fetch("/api/system-health").then((r) => r.json()).then((status) => {
    const unavailableNote = document.getElementById("system-health-unavailable");
    if (!status.available) {
      unavailableNote.textContent = `Pi host health not available: ${status.error}`;
      unavailableNote.style.display = "block";
      pushHealthSample("cpu-load", "N/A", null, "unavailable");
      pushHealthSample("memory", "N/A", null, "unavailable");
      pushHealthSample("uptime", "N/A", null, "unavailable");
    } else {
      unavailableNote.style.display = "none";
      const loadPct = Math.min(100, (status.load_avg[0] / status.cpu_count) * 100);
      pushHealthSample("cpu-load", `${status.load_avg[0].toFixed(2)} / ${status.cpu_count}`, loadPct, status.load_level);
      pushHealthSample("memory", `${status.mem_used_pct.toFixed(1)}%`, status.mem_used_pct, status.mem_level);
      pushHealthSample("uptime", `${(status.uptime_s / 3600).toFixed(1)} h`, null, "ok");
    }

    const disk = status.disk || {};
    if (disk.ok) {
      const usedPct = 100 - disk.free_pct;
      pushHealthSample("disk", `${usedPct.toFixed(1)}%`, usedPct, disk.level);
    } else {
      pushHealthSample("disk", "Unknown", null, "unavailable");
    }

    // Cosmetic thresholds below (ok/warning/critical coloring only) - NOT safety limits and not
    // tied to any existing calibrated constant. Picked from what this tab itself observed as a
    // normal idle baseline on this machine (~0.3s response time, near-zero retries) - purely to
    // give the operator a rough at-a-glance read on bus health, same spirit as the existing
    // comm-diagnostics panel this data already came from (Advanced Parameters tab).
    const comm = status.comm || {};
    const totalReads = comm.read_attempts || 0;
    const totalWrites = comm.write_attempts || 0;
    if (totalReads > 0) {
      const readPct = (comm.read_successes / totalReads) * 100;
      pushHealthSample("read-success", `${readPct.toFixed(1)}%`, readPct, readPct >= 99 ? "ok" : readPct >= 90 ? "warning" : "critical");
    } else {
      pushHealthSample("read-success", "No reads yet", null, "unavailable");
    }
    if (totalWrites > 0) {
      const writePct = (comm.write_successes / totalWrites) * 100;
      pushHealthSample("write-success", `${writePct.toFixed(1)}%`, writePct, writePct >= 99 ? "ok" : writePct >= 90 ? "warning" : "critical");
    } else {
      pushHealthSample("write-success", "No writes yet", null, "unavailable");
    }
    const totalRequests = totalReads + totalWrites;
    if (totalRequests > 0) {
      const retryRate = comm.retry_count / totalRequests;
      pushHealthSample("retry-rate", retryRate.toFixed(2), Math.min(100, retryRate * 50), retryRate < 0.5 ? "ok" : retryRate < 2 ? "warning" : "critical");
    } else {
      pushHealthSample("retry-rate", "-", null, "unavailable");
    }
    if (comm.avg_response_time_s != null) {
      const ms = comm.avg_response_time_s * 1000;
      pushHealthSample("response-time", `${ms.toFixed(0)} ms`, Math.min(100, ms / 10), ms < 600 ? "ok" : ms < 1500 ? "warning" : "critical");
    } else {
      pushHealthSample("response-time", "-", null, "unavailable");
    }
  });
}

function refreshAutomaticBackups() {
  fetch("/api/backup/automatic").then((r) => r.json()).then((backups) => {
    const statusEl = document.getElementById("auto-backup-status");
    const select = document.getElementById("auto-backup-select");
    select.innerHTML = "";
    if (!backups.length) {
      statusEl.textContent = "No automatic backups yet - the first one is created within an hour of the server running.";
      return;
    }
    const newest = backups[0];
    const when = newest.created_at ? new Date(newest.created_at * 1000).toLocaleString() : "unknown time";
    statusEl.textContent = `Last automatic backup: ${when} (${backups.length} kept, ${newest.profile_count} profile(s)).`;
    for (const b of backups) {
      const opt = document.createElement("option");
      opt.value = b.filename;
      const label = b.created_at ? new Date(b.created_at * 1000).toLocaleString() : b.filename;
      opt.textContent = `${label} (${b.profile_count} profile(s))`;
      select.appendChild(opt);
    }
  });
}

function restoreAutomaticBackup() {
  const filename = document.getElementById("auto-backup-select").value;
  if (!filename) return;
  if (!confirm("This will restore the selected backup's profiles into your local library (each as a new version, existing history preserved), and overwrite the current safety limits and alert thresholds. Continue?")) return;
  postJson(`/api/backup/automatic/${encodeURIComponent(filename)}/restore`, {}).then((result) => {
    if (result.error) {
      alert(`Restore failed: ${result.error}`);
      return;
    }
    alert(formatRestoreSummary(result));
  });
}

// profiles_skipped (2026-09-08 audit finding) - see config_backup.restore_backup()'s own
// docstring: a profile whose name collides with an existing, DIFFERENT profile after filename
// sanitization is skipped rather than silently corrupting the other one. Shared by both restore
// entry points (manual "Import backup..." and "Restore from automatic backup...").
function formatRestoreSummary(body) {
  let message = `Restored ${body.profiles_restored.length} profile(s), safety limits, and alert thresholds.`;
  const skipped = body.profiles_skipped || [];
  if (skipped.length) {
    message += "\n\nSkipped (name conflicts with an existing profile):\n" +
      skipped.map((s) => `- ${s.name}: ${s.reason}`).join("\n");
  }
  return message;
}

// System self-test / Run Diagnostics (2026-09-08, external feature-gap review, second pass) -
// see system_diagnostics.py's module docstring.

// Raw register/coil viewer (2026-09-08, external feature-gap review, second pass) - see
// register_viewer.py's module docstring. Read-only by construction.

function readRegisters(event) {
  const btn = event.target;
  const func_code = parseInt(document.getElementById("reg-viewer-func-code").value, 10);
  const start_address = parseInt(document.getElementById("reg-viewer-start").value, 10);
  const count = parseInt(document.getElementById("reg-viewer-count").value, 10);
  const statusEl = document.getElementById("reg-viewer-status");
  if (isNaN(func_code) || isNaN(start_address) || isNaN(count)) {
    statusEl.textContent = "Start address and count must be numbers.";
    return;
  }
  withBusyButton(btn, () => {
    btn.textContent = "Reading...";
    statusEl.textContent = "";
    return fetch("/api/registers/read", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ func_code, start_address, count }),
    }).then((r) => r.json()).then((result) => {
      const body = document.querySelector("#reg-viewer-table tbody");
      body.innerHTML = "";
      if (!result.ok) {
        statusEl.textContent = result.detail || "Read failed.";
        return;
      }
      statusEl.textContent = `${result.rows.length} value(s) read.`;
      for (const row of result.rows) {
        const tr = document.createElement("tr");
        const addrTd = document.createElement("td");
        addrTd.textContent = row.address;
        tr.appendChild(addrTd);
        const valueTd = document.createElement("td");
        valueTd.textContent = String(row.value);
        tr.appendChild(valueTd);
        const hexTd = document.createElement("td");
        hexTd.textContent = row.hex || "";
        tr.appendChild(hexTd);
        body.appendChild(tr);
      }
    });
  });
}

function runDiagnostics() {
  const btn = document.getElementById("btn-run-diagnostics");
  withBusyButton(btn, () => {
    // Immediate feedback (2026-09-08 fix - the button previously gave no visible sign anything
    // was happening: no busy label, and the results table renders below the fold with nothing
    // scrolling it into view, so a click looked like it silently did nothing) - see
    // withBusyButton()'s own docstring for why the busy label is set HERE, inside the callback,
    // not before this call.
    btn.textContent = "Running...";
    return fetch("/api/diagnostics/run", { method: "POST" }).then((r) => {
      if (!r.ok) {
        return r.json().then((body) => { throw new Error(body.error || `HTTP ${r.status}`); });
      }
      return r.json();
    }).then((result) => {
      const overallEl = document.getElementById("diagnostics-overall");
      overallEl.innerHTML = "";
      const badge = document.createElement("span");
      badge.className = `status-badge ${result.overall}`;
      badge.textContent = `Overall: ${result.overall.toUpperCase()}`;
      overallEl.appendChild(badge);

      const body = document.querySelector("#diagnostics-table tbody");
      body.innerHTML = "";
      for (const check of result.checks) {
        const tr = document.createElement("tr");
        const nameTd = document.createElement("td");
        nameTd.textContent = check.name;
        tr.appendChild(nameTd);
        const statusTd = document.createElement("td");
        const statusBadgeEl = document.createElement("span");
        statusBadgeEl.className = `status-badge ${check.status}`;
        statusBadgeEl.textContent = check.status.toUpperCase();
        statusTd.appendChild(statusBadgeEl);
        tr.appendChild(statusTd);
        const detailTd = document.createElement("td");
        detailTd.textContent = check.detail;
        tr.appendChild(detailTd);
        body.appendChild(tr);
      }
      overallEl.scrollIntoView({ behavior: "smooth", block: "center" });
    }).catch((err) => {
      alert(`Could not run diagnostics: ${err.message}`);
    });
  });
}

// Safety limits history / undo (2026-09-08, external feature-gap review, second pass) - see
// config_change_history.py's module docstring.

let _safetyLimitsHistoryOrdered = [];

function diffConfigValues(oldValues, newValues) {
  if (oldValues === null || oldValues === undefined) return [];
  return Object.keys(newValues).sort().filter((k) => oldValues[k] !== newValues[k])
    .map((k) => `${k}: ${oldValues[k]} -> ${newValues[k]}`);
}

function showSafetyLimitsHistoryDiff() {
  const select = document.getElementById("safety-limits-history-select");
  const idx = select.selectedIndex;
  const diffEl = document.getElementById("safety-limits-history-diff");
  if (idx < 0) {
    diffEl.textContent = "";
    return;
  }
  const entry = _safetyLimitsHistoryOrdered[idx];
  const diffs = diffConfigValues(entry.old_values, entry.new_values);
  diffEl.textContent = diffs.length ? diffs.join("\n") : "(no field-level differences to show)";
}

function loadSafetyLimitsHistory() {
  fetch("/api/safety-limits/history").then((r) => r.json()).then((history) => {
    if (history.length === 0) {
      alert("No safety-limit changes recorded yet - every future change will be logged here.");
      return;
    }
    _safetyLimitsHistoryOrdered = [...history].reverse();
    const select = document.getElementById("safety-limits-history-select");
    select.innerHTML = "";
    for (const entry of _safetyLimitsHistoryOrdered) {
      const when = new Date(entry.timestamp * 1000).toLocaleString();
      const diffs = diffConfigValues(entry.old_values, entry.new_values);
      let summary;
      if (entry.old_values === null || entry.old_values === undefined) {
        summary = "(no prior value to compare against)";
      } else {
        summary = diffs.length ? `${diffs.length} field(s) changed` : "no values actually changed";
      }
      const note = entry.note ? ` [${entry.note}]` : "";
      const opt = document.createElement("option");
      opt.textContent = `${when} - ${summary}${note}`;
      select.appendChild(opt);
    }
    select.selectedIndex = 0;
    showSafetyLimitsHistoryDiff();
    document.getElementById("safety-limits-history-panel").style.display = "block";
  });
}

function restoreSafetyLimitsHistory() {
  const idx = document.getElementById("safety-limits-history-select").selectedIndex;
  if (idx < 0) return;
  const entry = _safetyLimitsHistoryOrdered[idx];
  const when = new Date(entry.timestamp * 1000).toLocaleString();
  if (!confirm(`This will apply the safety limits from ${when}, overwriting whatever is currently configured. Continue?`)) return;
  postJson("/api/safety-limits/history/restore", { timestamp: entry.timestamp }).then((result) => {
    if (result.error) {
      alert(`Restore failed: ${result.error}`);
      return;
    }
    loadSafetyLimits();
    document.getElementById("safety-limits-history-panel").style.display = "none";
    alert(`Safety limits from ${when} applied.`);
  });
}

// Repair outcome summary (2026-09-08 audit finding) - compute_repair_outcome_stats() and its
// GET /api/runs/repair-outcome-stats route have existed since the repair-outcome tracking
// feature was built, but nothing ever called it from either UI. See run_analysis.py's own
// docstring for what this deliberately is (and isn't) - a tally, not a CRM.

function refreshRepairOutcomeStats() {
  const profileSelect = document.getElementById("repair-stats-profile");
  const profileName = profileSelect.value;
  const url = profileName
    ? `/api/runs/repair-outcome-stats?profile_name=${encodeURIComponent(profileName)}`
    : "/api/runs/repair-outcome-stats";
  fetch(url).then((r) => r.json()).then((stats) => {
    const el = document.getElementById("repair-stats-summary");
    const c = stats.counts;
    const rate = stats.success_rate === null || stats.success_rate === undefined
      ? "n/a (no outcomes recorded yet)"
      : `${(stats.success_rate * 100).toFixed(0)}%`;
    el.textContent =
      `${stats.total_runs} run(s) total | Success: ${c.Success}, Partial: ${c.Partial}, ` +
      `Failed: ${c.Failed}, Unknown: ${c.Unknown}, not recorded: ${c.not_recorded} | ` +
      `Success rate (of recorded outcomes): ${rate}`;
  });
}

function populateRepairStatsProfileDropdown() {
  fetch("/api/profiles").then((r) => r.json()).then((names) => {
    const select = document.getElementById("repair-stats-profile");
    const current = select.value;
    select.innerHTML = '<option value="">All profiles</option>';
    for (const name of names) {
      const opt = document.createElement("option");
      opt.value = name;
      opt.textContent = name;
      select.appendChild(opt);
    }
    select.value = current;
  });
}

function compareSelectedRuns() {
  const filenames = Array.from(document.querySelectorAll("#run-history-table input[type=checkbox]:checked")).map((cb) => cb.dataset.filename);
  const panel = document.getElementById("run-comparison-panel");
  if (filenames.length < 2) {
    alert("Select 2 or more runs (checkboxes in the first column) to compare.");
    return;
  }
  postJson("/api/runs/compare", { filenames }).then((runs) => {
    panel.innerHTML = "";
    for (const run of runs) {
      const row = document.createElement("div");
      let text = `${run.started} (${run.profile_name}): Peak Upper ${run.peak_upper.toFixed(1)}C ` +
        `(at ${run.time_to_peak_upper_s.toFixed(0)}s), Lower ${run.peak_lower.toFixed(1)}C, ` +
        `IR ${run.peak_ir.toFixed(1)}C, Probe ${(run.peak_probe || 0).toFixed(1)}C, ` +
        `Duration ${run.duration_s.toFixed(0)}s`;
      // Fault/warning counts and comm/cooling stats only exist for runs finalized after Run
      // Finalization was added - null/undefined means "not recorded", never shown as a fake 0.
      if (run.fault_count !== null && run.fault_count !== undefined) {
        text += ` | Faults: ${run.fault_count}, Warnings: ${run.warning_count}`;
      }
      if (run.time_above_wetting_s !== null && run.time_above_wetting_s !== undefined) {
        text += `, Time above wetting: ${run.time_above_wetting_s.toFixed(0)}s`;
      }
      if (run.cooling_rate_c_per_min !== null && run.cooling_rate_c_per_min !== undefined) {
        text += `, Cooling rate: ${run.cooling_rate_c_per_min.toFixed(1)}C/min`;
      }
      if (run.process_result) {
        text += `, Process result: ${run.process_result}`;
      }
      row.textContent = text;
      panel.appendChild(row);
    }
    panel.style.display = "block";
  });
}

// ---------------- init ----------------

buildTable();
buildPidTable();
buildGauges();
wireButtons();
wireTabs();
drawProfileChart();
refreshProfileList();
refreshRunHistory();
refreshMaintenanceStatus();
refreshKnownGoodConfigStatus();
refreshConnectionStatus();
refreshPortList();
loadSafetyLimits();
loadAlertConfig();
loadWebhookConfig();
loadOllamaConfig();
if (window.loadSerperConfig) window.loadSerperConfig();
refreshDiagnostics();
setInterval(refreshDiagnostics, 5000);
connectEvents();
