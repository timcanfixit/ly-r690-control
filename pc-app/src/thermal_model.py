"""Stage 24 thermal modeling - a real, trained-from-data model (2026-09-16), replacing the
scaffolding this module used to be.

What it predicts: for each heater zone that has a real target in a profile (Upper/Lower/Infrared -
Probe has no profile-settable target at all, see process_intelligence.py's identical exclusion),
given that profile's own commanded PEAK target for the zone, predict the peak temperature the zone
will ACTUALLY reach and how long it will take to get there. This is deliberately the simplest
model that's still genuinely predictive and genuinely trained from this machine's own real history,
not a fabricated capability: a per-zone linear fit (ordinary least squares, stdlib only - this
project has consistently avoided adding a numpy/scipy dependency for something this small, see
system_health.py's identical stated philosophy) of (commanded peak) -> (actual peak, actual time-
to-peak), trained only on this machine's own logged runs.

Training data quality (2026-09-16, real reported concern): training - and the "is there enough
data yet" readiness count - previously used EVERY logged run indiscriminately, including faulted,
manually-stopped-early, and interrupted ones. A run that never got anywhere near its target (an
emergency stop, a probe fault mid-ramp) doesn't represent the machine's normal thermal behavior and
would poison a real fit with anomalous, incomplete curves. Both now use only runs whose OWN already-
computed process_result was "PASS" (run_finalization.classify_process_result - no fault raised, and
wetting temperature was actually reached) - see _load_clean_run_summaries().

Every prediction reports its own R-squared and sample count alongside the numbers, rather than
hiding behind a big "enough data" threshold that looks reassuring but isn't actually calibrated to
anything - MIN_CLEAN_RUNS_FOR_MODEL only gates whether a fit is attempted AT ALL, not how much to
trust it once attempted; trust is read from R-squared and sample_count on every response, always.
A zone whose historical runs never varied their target at all (same profile run repeatedly) can't
have a real slope fit from a single X value - that's reported honestly as a flat model (slope 0,
r_squared None) rather than a fabricated line through one point.

Explicitly NOT attempted: predicting a full temperature-vs-time CURVE (not just peak/time-to-peak).
The per-tick run log has no per-row record of which profile SECTION was active at that timestamp,
so reconstructing one would mean re-running the same best-effort "nearest matching section"
estimate already used live (alerts.RunMonitor.estimate_current_section) against historical data and
then fitting a second model on top of that estimate's own uncertainty - compounding one inference
on another. Peak and time-to-peak per run are already directly, unambiguously computed and saved
(run_finalization.compute_zone_statistics) with no such estimation step, which is why this model is
built from those numbers instead.
"""
from __future__ import annotations

from dataclasses import dataclass, asdict
from typing import Optional

from data_logger import LOGS_DIR
from modbus_client import Profile
from run_finalization import load_summary

# Every zone with a real, profile-settable target - see modbus_client.CurveSection's fields.
# Probe is deliberately excluded: it's a passive measurement point, not a heater zone with its own
# setpoint, so there is no "commanded target" to correlate its actual behavior against at all (see
# process_intelligence.py's identical exclusion and docstring for the same reasoning).
_ZONE_PROFILE_ATTR = {"upper": "upper_temp", "lower": "lower_temp", "infrared": "ir_temp"}

# Lowered from 20 (2026-09-16, real reported feedback: "20 seems a lot for a thermal model") - 20
# was an arbitrary round number with no calibration behind it. Each zone model here is a 2-
# parameter line (slope + intercept), not a high-dimensional model - a handful of clean runs is
# already enough for that to mean something, and unlike the old flat threshold, every prediction
# now also reports its own R-squared and sample_count, so the model is always honest about its real
# confidence rather than hiding behind a big number that wasn't actually tied to fit quality.
MIN_CLEAN_RUNS_FOR_MODEL = 5


@dataclass
class ZoneModel:
    zone: str
    sample_count: int
    slope: float
    intercept: float
    r_squared: Optional[float]  # None when there wasn't enough target variation to fit a real slope
    mean_abs_error_c: float  # in-sample only (not cross-validated) - see module docstring
    time_slope: float
    time_intercept: float
    time_r_squared: Optional[float]


@dataclass
class ZonePrediction:
    zone: str
    commanded_peak_c: float
    predicted_peak_c: float
    predicted_time_to_peak_s: float
    typical_error_c: float  # the trained model's own mean_abs_error_c - an honest error bar
    sample_count: int
    r_squared: Optional[float]


@dataclass
class ModelStatus:
    available: bool
    total_runs_logged: int
    clean_runs_logged: int
    runs_needed: int
    message: str
    zone_models: Optional[dict] = None  # {zone: ZoneModel} once available


def _fit_line(xs: list[float], ys: list[float]) -> tuple[float, float, Optional[float]]:
    """Ordinary least squares, y = slope*x + intercept. Returns r_squared=None (and slope forced
    to 0.0, intercept = mean(ys)) when every x is identical - there is no slope to fit from a
    single X value, and reporting one anyway would be a fabricated line through a point, not a
    real relationship. See module docstring for why this is stdlib math, not a numpy dependency."""
    n = len(xs)
    x_mean = sum(xs) / n
    y_mean = sum(ys) / n
    ss_xx = sum((x - x_mean) ** 2 for x in xs)
    if ss_xx == 0:
        return 0.0, y_mean, None
    ss_xy = sum((x - x_mean) * (y - y_mean) for x, y in zip(xs, ys))
    slope = ss_xy / ss_xx
    intercept = y_mean - slope * x_mean
    ss_tot = sum((y - y_mean) ** 2 for y in ys)
    if ss_tot == 0:
        return slope, intercept, 1.0  # every actual value identical too - a perfect (flat) fit
    ss_res = sum((y - (slope * x + intercept)) ** 2 for x, y in zip(xs, ys))
    return slope, intercept, 1.0 - ss_res / ss_tot


def _commanded_peak(sections: list[dict], attr: str) -> Optional[float]:
    """The profile's own peak target for this zone across all sections - same "peak of the
    target values" definition profile_simulation.py already uses. None (not 0.0) when the profile
    never sets a real target for this zone at all - e.g. an IR-only profile has no Upper target -
    so that run is excluded from this zone's training data instead of being counted as "commanded
    0, achieved room temperature", which would be a fabricated data point, not a real one."""
    peak = max((s.get(attr, 0) or 0 for s in sections), default=0)
    return float(peak) if peak > 0 else None


def _load_clean_run_summaries() -> list[dict]:
    """Every logged run whose own already-computed process_result was PASS - see module docstring
    for why faulted/interrupted/incomplete runs are excluded from both this count and training. A
    run with no summary at all (predates the summary sidecar feature, or crashed before
    finalization) has no result to check and is excluded rather than assumed clean."""
    if not LOGS_DIR.exists():
        return []
    summaries = []
    for path in LOGS_DIR.glob("run_*.csv"):
        summary = load_summary(path)
        if summary is not None and summary.get("process_result") == "PASS":
            summaries.append(summary)
    return summaries


def _build_zone_model(zone: str, clean_summaries: list[dict]) -> Optional[ZoneModel]:
    attr = _ZONE_PROFILE_ATTR[zone]
    xs, ys_peak, ys_time = [], [], []
    for summary in clean_summaries:
        sections = ((summary.get("config_snapshot") or {}).get("profile") or {}).get("sections") or []
        commanded = _commanded_peak(sections, attr)
        zstats = (summary.get("zone_statistics") or {}).get(zone)
        if commanded is None or zstats is None:
            continue
        xs.append(commanded)
        ys_peak.append(zstats["max_c"])
        ys_time.append(zstats["time_to_peak_s"])
    if len(xs) < 2:
        return None  # not even enough (profile, outcome) pairs for this zone to fit anything
    slope, intercept, r_squared = _fit_line(xs, ys_peak)
    time_slope, time_intercept, time_r_squared = _fit_line(xs, ys_time)
    predicted = [slope * x + intercept for x in xs]
    mean_abs_error = sum(abs(actual - pred) for actual, pred in zip(ys_peak, predicted)) / len(ys_peak)
    return ZoneModel(
        zone=zone, sample_count=len(xs), slope=slope, intercept=intercept, r_squared=r_squared,
        mean_abs_error_c=mean_abs_error, time_slope=time_slope, time_intercept=time_intercept,
        time_r_squared=time_r_squared,
    )


def get_model_status() -> ModelStatus:
    total_runs = len(list(LOGS_DIR.glob("run_*.csv"))) if LOGS_DIR.exists() else 0
    clean_summaries = _load_clean_run_summaries()
    clean_count = len(clean_summaries)
    needed = max(0, MIN_CLEAN_RUNS_FOR_MODEL - clean_count)
    if needed > 0:
        message = (
            f"Not enough clean data yet: {clean_count}/{MIN_CLEAN_RUNS_FOR_MODEL} PASS runs logged "
            f"({total_runs} total run(s) logged, but faulted/incomplete runs aren't used for "
            f"training). {needed} more clean run(s) needed."
        )
        return ModelStatus(
            available=False, total_runs_logged=total_runs, clean_runs_logged=clean_count,
            runs_needed=needed, message=message,
        )

    zone_models = {}
    for zone in _ZONE_PROFILE_ATTR:
        model = _build_zone_model(zone, clean_summaries)
        if model is not None:
            zone_models[zone] = model
    if not zone_models:
        message = (
            f"{clean_count} clean runs logged, but none had enough matching (profile target, "
            f"outcome) data to fit a model yet."
        )
        return ModelStatus(
            available=False, total_runs_logged=total_runs, clean_runs_logged=clean_count,
            runs_needed=0, message=message,
        )
    fit_desc = ", ".join(
        f"{zone} (R2={model.r_squared:.2f}, n={model.sample_count})" if model.r_squared is not None
        else f"{zone} (flat - every run so far used the same target, n={model.sample_count})"
        for zone, model in zone_models.items()
    )
    message = f"{clean_count} clean runs logged - model trained: {fit_desc}."
    return ModelStatus(
        available=True, total_runs_logged=total_runs, clean_runs_logged=clean_count,
        runs_needed=0, message=message, zone_models=zone_models,
    )


def predict_profile_response(profile: Profile) -> Optional[dict]:
    """Returns {zone: ZonePrediction} for every zone that both has a trained model AND a real
    target in THIS profile - or None if no model is trained at all yet (see get_model_status -
    never fabricate a prediction from insufficient data). Applies the zone's fitted line to this
    profile's own commanded peak; typical_error_c is the training fit's own in-sample mean
    absolute error, reported so a caller can see how much to trust the number instead of treating
    it as exact."""
    status = get_model_status()
    if not status.available or not status.zone_models:
        return None
    predictions = {}
    section_dicts = [asdict(s) for s in profile.sections]
    for zone, model in status.zone_models.items():
        attr = _ZONE_PROFILE_ATTR[zone]
        commanded = _commanded_peak(section_dicts, attr)
        if commanded is None:
            continue
        predicted_peak = model.slope * commanded + model.intercept
        predicted_time = max(0.0, model.time_slope * commanded + model.time_intercept)
        predictions[zone] = ZonePrediction(
            zone=zone, commanded_peak_c=commanded, predicted_peak_c=predicted_peak,
            predicted_time_to_peak_s=predicted_time, typical_error_c=model.mean_abs_error_c,
            sample_count=model.sample_count, r_squared=model.r_squared,
        )
    return predictions or None
