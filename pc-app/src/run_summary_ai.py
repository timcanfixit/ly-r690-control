"""AI-phrased natural-language summary of a just-finished run, for the completion webhook
notification (2026-09-14, explicit request: "a summary of the temps using ai"). Strictly
EXTRACTIVE - the model is given only the already-computed real stats from
run_finalization.finalize_run()'s own summary dict and asked to phrase them as a couple of plain
sentences. Same "never invent, only phrase what's real" rule this project applies everywhere else
Ollama touches real data (see profile_search.py's/process_assistant.py's module docstrings) - the
prompt explicitly forbids estimating or adding any number not given, and this returns "" rather
than raising whenever Ollama isn't configured/enabled or the call fails for any reason. Callers
always send the plain numeric summary regardless of whether the AI text comes back - this only
ever adds a sentence or two on top, never gates the notification itself.
"""
from __future__ import annotations

from ollama_client import call_ollama_raw

_PROMPT = """Summarize this completed BGA rework machine run in 2-3 plain sentences for a \
technician glancing at a phone notification. Use ONLY the numbers given below - do not estimate, \
calculate, guess, or add any number not explicitly listed here. Do not just list every number \
back - pick out what's actually notable (whether it reached a sensible peak, how long it ran, \
whether there were faults or warnings).

Profile: {profile_name}
Outcome: {outcome}
Duration: {duration_s:.0f} seconds
Faults: {fault_count}    Warnings: {warning_count}
Peak temperatures (C): Upper {upper_max}, Lower {lower_max}, Infrared {ir_max}, Probe {probe_max}
Time above wetting temperature: {time_above_wetting_s:.0f} seconds

Respond with plain text only - no markdown, no headers, no bullet points, just the sentences."""


def _fmt_peak(zone_statistics: dict, zone: str) -> str:
    z = zone_statistics.get(zone)
    if not z or "max_c" not in z:
        return "unknown"
    return f"{z['max_c']:.1f}"


def generate_run_summary_text(profile_name: str, outcome: str, summary: dict, duration_s: float, ollama_config) -> str:
    """Returns a short AI-phrased summary, or "" if Ollama isn't configured/enabled or the call
    fails - see module docstring for why a failure here is never fatal to the notification."""
    if not ollama_config.enabled or not ollama_config.model:
        return ""
    zone_statistics = summary.get("zone_statistics") or {}
    prompt = _PROMPT.format(
        profile_name=profile_name or "Untitled",
        outcome=outcome,
        duration_s=duration_s,
        fault_count=summary.get("fault_count", 0),
        warning_count=summary.get("warning_count", 0),
        upper_max=_fmt_peak(zone_statistics, "upper"),
        lower_max=_fmt_peak(zone_statistics, "lower"),
        ir_max=_fmt_peak(zone_statistics, "infrared"),
        probe_max=_fmt_peak(zone_statistics, "probe"),
        time_above_wetting_s=summary.get("time_above_wetting_s") or 0.0,
    )
    text, error = call_ollama_raw(ollama_config.host, ollama_config.model, prompt)
    if error or not text.strip():
        return ""
    return text.strip()


def format_run_summary_notification(header: str, summary: dict, duration_s: float, ai_text: str = "") -> str:
    """The plain-numbers half of the notification - always included, regardless of whether the AI
    text above came back. `header` is the caller's own one-line "what happened" statement (see
    web_server.py/main_window.py's run-ended handling for how that's worded depending on how much
    can honestly be claimed about why the run ended)."""
    zone_statistics = summary.get("zone_statistics") or {}
    lines = [
        header,
        f"Peak: Upper {_fmt_peak(zone_statistics, 'upper')}C, Lower {_fmt_peak(zone_statistics, 'lower')}C, "
        f"IR {_fmt_peak(zone_statistics, 'infrared')}C, Probe {_fmt_peak(zone_statistics, 'probe')}C",
        f"Duration: {duration_s:.0f}s | Result: {summary.get('process_result', 'unknown')} | "
        f"Faults: {summary.get('fault_count', 0)} | Warnings: {summary.get('warning_count', 0)}",
    ]
    if ai_text:
        lines.append("")
        lines.append(ai_text)
    return "\n".join(lines)
