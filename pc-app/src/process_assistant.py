"""AI Process Assistant - 2026-09-07, external feature-gap review.

Expands the existing Ollama integration (profile_search.py) in a deliberately different
direction: instead of searching the open web for a profile, this answers a free-form question
about THIS machine's own already-logged data - run history, saved profile metadata, maintenance
status - grounded ONLY in that real, local data. It is never asked to draw on general BGA rework
knowledge the way profile_search.py's explicitly-fenced-off generation fallback is (see that
module's own docstring) - if the data below doesn't answer the question, the model is instructed
to say so, not fall back on training knowledge that could be mistaken for a real fact about this
machine specifically.

Evidence model (the one hard rule every answer must follow): every claim is tagged inline as
  [MEASURED]   - a fact taken directly from the data provided, unchanged
  [HISTORICAL] - something computed/derived from that data (an average, a count, a trend)
  [INFERENCE]  - a pattern or judgment the model is drawing that ISN'T directly stated in the
                 data - still a real, useful answer, just honestly flagged as a guess, not a fact
This is enforced by instruction, not a hard grammar constraint (free-text answers don't have a
schema to validate against the way profile extraction's JSON does) - the tags are surfaced to the
operator as-is, not stripped or re-interpreted, so a model that ignores the instruction is visibly
failing to follow it rather than silently producing an unlabeled answer that looks authoritative.

Hard architectural rule: this module is READ-ONLY. It has no access to anything that writes to the
machine, changes a setting, or starts/stops a run - it can only gather already-logged data and ask
a question about it. See ask_process_assistant()'s own docstring for how the prompt itself also
tells the model plainly that it cannot act, only explain, for the (expected) case where an
operator phrases something as a request ("start a run", "change the safety limit") rather than a
question.
"""
from __future__ import annotations

from typing import Optional

import data_logger
import maintenance
import profile_store
from ollama_client import call_ollama_raw
from profile_search import OllamaConfig, MACHINE_NAME

_MAX_RECENT_RUNS = 15  # bounds the context size - see _build_context()'s own note on why
_MAX_PROFILES = 30


def _format_run_line(run: dict) -> str:
    parts = [
        run.get("started", "?"),
        run.get("profile_name", "?"),
        f"{(run.get('duration_s') or 0):.0f}s",
        f"peak Upper {(run.get('peak_upper') or 0):.0f}C",
        f"peak Lower {(run.get('peak_lower') or 0):.0f}C",
        f"peak IR {(run.get('peak_ir') or 0):.0f}C",
    ]
    if run.get("fault_count") is not None:
        parts.append(f"{run['fault_count']} fault(s), {run.get('warning_count', 0)} warning(s)")
    if run.get("time_above_wetting_s") is not None:
        parts.append(f"{run['time_above_wetting_s']:.0f}s above wetting")
    if run.get("process_result"):
        parts.append(f"result={run['process_result']}")
    if run.get("golden"):
        parts.append("GOLDEN")
    return "- " + ", ".join(parts)


def _format_profile_line(name: str) -> str:
    meta = profile_store.get_profile_metadata(name)
    bits = [name]
    for key, label in (("board", "board"), ("component", "component"), ("status", "status"), ("solder", "solder")):
        if meta.get(key):
            bits.append(f"{label}={meta[key]}")
    return "- " + ", ".join(bits)


def _format_maintenance(status: dict) -> str:
    lines = []
    runs = status["runs"]
    lines.append(
        f"Totals across all logged runs: {runs['total_runs']} run(s), "
        f"{runs['total_run_hours']:.1f} logged hours, {runs['total_faults']} fault(s), "
        f"{runs['total_warnings']} warning(s)."
    )
    for zone, info in status["calibration"].items():
        days = f"{info['days_since']:.0f} days ago" if info["days_since"] is not None else "never"
        lines.append(f"Calibration - {zone}: last {days}, status={info['status']}.")
    for zone, info in status["self_tune"].items():
        days = f"{info['days_since']:.0f} days ago" if info["days_since"] is not None else "never"
        lines.append(f"Self-tune - {zone}: last {days} ({info['session_count']} session(s) total), status={info['status']}.")
    return "\n".join(lines)


def _build_context() -> str:
    """Bounded, not a full dump - see _MAX_RECENT_RUNS/_MAX_PROFILES. This is deliberately a
    simple "everything recent, always the same shape" bundle rather than a real retrieval/search
    system that picks what's relevant to the specific question - a much bigger problem this
    project doesn't need solved yet at the data volumes a single machine actually produces. If the
    library grows large enough that this stops being enough context, that's the point where real
    retrieval becomes worth building - not before."""
    runs = data_logger.list_runs()[:_MAX_RECENT_RUNS]
    run_lines = "\n".join(_format_run_line(r) for r in runs) if runs else "(no runs logged yet)"

    try:
        profile_names = profile_store.list_profiles()[:_MAX_PROFILES]
    except OSError:
        profile_names = []
    profile_lines = "\n".join(_format_profile_line(n) for n in profile_names) if profile_names else "(no profiles saved yet)"

    maint_text = _format_maintenance(maintenance.compute_maintenance_status())

    return (
        f"=== Recent runs (most recent {len(runs)} of however many are logged) ===\n{run_lines}\n\n"
        f"=== Saved profiles ===\n{profile_lines}\n\n"
        f"=== Maintenance status ===\n{maint_text}\n"
    )


_ASSISTANT_PROMPT = """You are a process-analysis assistant for a {machine_name} BGA rework machine's control software. Answer the operator's question using ONLY the real data provided below - this is data actually logged by the machine-control software itself (run history, saved profile metadata, maintenance records). Do NOT use your own general knowledge about BGA rework/reflow soldering to answer - if the question needs that instead of this machine's own data, say so plainly rather than answering from general knowledge as if it were specific to this machine.

CRITICAL RULES:
1. Tag every distinct factual claim in your answer, right before the claim, with exactly one of: [MEASURED] (a fact taken directly from the data below, unchanged), [HISTORICAL] (something you computed or derived from that data - an average, a count, a trend), or [INFERENCE] (a pattern or judgment you're drawing that is NOT directly stated in the data below - still give your honest best answer, just flagged as a guess rather than a fact).
2. If the data below does not contain what's needed to answer the question, say so plainly - do not guess or fall back on general knowledge.
3. Never state a number that isn't actually present in, or directly computable from, the data below.
4. You cannot control this machine, change any setting, start/stop a run, or modify a profile - you can only analyze and explain the data below. If the operator's question asks you to DO something rather than explain something, say plainly that you can only analyze/explain, and that the requested action has to be done by the operator through the actual software controls.
5. Answer in plain text, not JSON. Keep it concise and directly responsive to the question.

DATA (from this machine's own logs, current as of now):
{context}

OPERATOR'S QUESTION: {question}
"""


def ask_process_assistant(question: str, config: OllamaConfig) -> tuple[str, str]:
    """Returns (answer, error). Read-only by construction - see module docstring for the
    architectural line this respects (no import here can reach anything that writes to the
    machine or changes a setting; this module only reads via data_logger/maintenance/
    profile_store's own read-only functions and calls Ollama)."""
    if not config.enabled or not config.model:
        return "", "Ollama integration is not configured - set a host and model first."
    if not question.strip():
        return "", "Enter a question first."

    context = _build_context()
    prompt = _ASSISTANT_PROMPT.format(machine_name=MACHINE_NAME, context=context, question=question.strip())
    answer, error = call_ollama_raw(config.host, config.model, prompt)
    if error:
        return "", error
    if not answer.strip():
        return "", "Ollama returned an empty response."
    return answer.strip(), ""
