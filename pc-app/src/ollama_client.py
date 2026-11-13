"""Shared low-level Ollama HTTP client - factored out of profile_search.py (2026-09-07) so
process_assistant.py can reuse the exact same tested request/timeout/error-handling logic instead
of each new Ollama-consuming feature reimplementing its own copy. profile_search.py itself now
imports call_ollama_json/extract_json_object from here under its original private names
(_call_ollama/_extract_json_object) so its own existing tests - which monkeypatch those exact
names on the profile_search module - keep working unchanged; this is a pure code-sharing
refactor, not a behavior change.
"""
from __future__ import annotations

import json
import urllib.error
import urllib.request
from typing import Optional

OLLAMA_TIMEOUT_S = 120.0  # local inference on a big model can genuinely take a while

# Confirmed live 2026-09-13: `ollama ps` showed qwen3:14b loaded with CONTEXT 40960 (its own
# published Modelfile default, not anything this app requested - see call sites below, which
# previously sent no `options` at all) and a 10%/90% CPU/GPU split even on a 16GB card, total
# footprint 16GB against a 9.3GB Q4_K_M weights file - the oversized KV cache for a 40960-token
# context, not the weights, was what didn't fit. Every prompt this app actually sends is small and
# bounded by design (profile_search.py's _MAX_PAGE_CHARS=8000 chars / _MAX_REFERENCE_EXAMPLES=4,
# process_assistant.py's _MAX_RECENT_RUNS=15 / _MAX_PROFILES=30) - nowhere close to needing 40960
# tokens of context. Capping it here, in the shared client both features use, means every request
# is explicit about what it actually needs regardless of whatever context length a given model's
# own Modelfile happens to default to, and works the same on any machine this is pointed at
# without the operator having to separately set OLLAMA_CONTEXT_LENGTH on their inference box.
# 8192 leaves comfortable headroom above this app's real worst-case prompt size.
_OLLAMA_NUM_CTX = 8192


def extract_json_object(text: str) -> Optional[dict]:
    """Finds and parses the first top-level {...} JSON object in free-form text - used for calls
    made without strict JSON-grammar mode, where the model may wrap its answer in a little prose
    or a markdown code fence around the actual object."""
    start = text.find("{")
    end = text.rfind("}")
    if start == -1 or end == -1 or end < start:
        return None
    try:
        result = json.loads(text[start:end + 1])
    except json.JSONDecodeError:
        return None
    return result if isinstance(result, dict) else None


def call_ollama_raw(host: str, model: str, prompt: str) -> tuple[str, str]:
    """Returns (response_text, error) - the model's raw text response, with NO JSON parsing at
    all. Use this for a free-text answer (see process_assistant.py); use call_ollama_json when
    the caller needs a structured object back (see profile_search.py)."""
    body = {"model": model, "prompt": prompt, "stream": False, "options": {"num_ctx": _OLLAMA_NUM_CTX}}
    try:
        req = urllib.request.Request(
            host.rstrip("/") + "/api/generate", data=json.dumps(body).encode("utf-8"),
            headers={"Content-Type": "application/json"}, method="POST",
        )
        with urllib.request.urlopen(req, timeout=OLLAMA_TIMEOUT_S) as resp:
            outer = json.loads(resp.read().decode("utf-8", errors="replace"))
    except (urllib.error.URLError, OSError) as exc:
        return "", f"Could not reach Ollama at {host}: {exc}"
    except json.JSONDecodeError:
        return "", "Ollama's own response envelope was not valid JSON."
    return outer.get("response", ""), ""


def call_ollama_json(host: str, model: str, prompt: str, use_json_format: bool = True) -> tuple[Optional[dict], str]:
    """Returns (parsed_json, error). Ollama's format="json" mode constrains decoding to valid JSON
    grammar - this works well for a straightforward extraction prompt but was found live
    2026-08-23 to collapse a thinking-capable model (qwen3:14b) into emitting an empty {} after
    just 2 tokens on a shorter, more abstract prompt: the grammar constraint seems to cut off the
    model's normal internal reasoning before it can produce real content. Without the constraint
    the same model produced a full, honestly-reasoned answer. Callers needing that more abstract
    style of prompt should pass use_json_format=False and rely on extract_json_object() to pull
    the JSON out of whatever the model actually said - still validated after parsing, never
    trusted blindly, exactly like the format="json" path."""
    body = {"model": model, "prompt": prompt, "stream": False, "options": {"num_ctx": _OLLAMA_NUM_CTX}}
    if use_json_format:
        body["format"] = "json"
    try:
        req = urllib.request.Request(
            host.rstrip("/") + "/api/generate", data=json.dumps(body).encode("utf-8"),
            headers={"Content-Type": "application/json"}, method="POST",
        )
        with urllib.request.urlopen(req, timeout=OLLAMA_TIMEOUT_S) as resp:
            outer = json.loads(resp.read().decode("utf-8", errors="replace"))
    except (urllib.error.URLError, OSError) as exc:
        return None, f"Could not reach Ollama at {host}: {exc}"
    text = outer.get("response", "")
    if use_json_format:
        try:
            return json.loads(text), ""
        except json.JSONDecodeError:
            return None, "Ollama's response was not valid JSON - discarded rather than guessed at."
    parsed = extract_json_object(text)
    if parsed is None:
        return None, "Ollama's response did not contain a parseable JSON object - discarded rather than guessed at."
    return parsed, ""
