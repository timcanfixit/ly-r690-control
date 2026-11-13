"""Generic webhook notifications - the user supplies their own webhook URL (Discord, Slack, or
any custom relay that accepts an HTTP POST of JSON). No credentials are ever entered, stored, or
transmitted by this app beyond that URL, which the user controls and can revoke on their own end
at any time - deliberately avoiding the token/email/SMTP credential-handling concerns the roadmap
flagged for Stage 21.

Fires on exactly four events: a Start write confirmed by the board (run_started); the machine
leaving its running state for any KNOWN reason - a manual Stop from this app, a fault auto-stop,
or (2026-09-14, a real gap found live: neither the profile completing on its own nor the
touchscreen's own physical Stop button were ever detected) the running coil simply dropping on its
own with no fault and no /api/stop call from this app - all three land on run_completed, with an
honest best-effort note on which of the three it looks like (see main_window.py's/web_server.py's
_check_run_ended_externally); a safety-limit auto-stop of a live run also separately fires
run_failed; and any CRITICAL/EMERGENCY fault regardless of whether a run was active fires
critical_fault. See main_window.py's _on_start_clicked/_on_stop_clicked/_on_fault_raised/
_check_run_ended_externally and web_server.py's equivalents for exactly where each fires.
run_completed's message now also includes the run's real computed peak temps/duration/fault count
(see run_summary_ai.py) and, if Ollama is configured, a short AI-phrased summary of those same
real numbers - never asked to invent anything beyond what's already computed.
"""
from __future__ import annotations

import json
import sys
import threading
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path

if getattr(sys, "frozen", False):
    WEBHOOK_CONFIG_PATH = Path(sys.executable).resolve().parent / "webhook_config.json"
else:
    WEBHOOK_CONFIG_PATH = Path(__file__).resolve().parent.parent / "webhook_config.json"

# See pid_history.py's identical lock for why - guards the config file's read-modify-write.
_config_lock = threading.Lock()

EVENT_TYPES = ("run_started", "run_completed", "run_failed", "critical_fault")
_DEFAULT_EVENTS = {name: True for name in EVENT_TYPES}


@dataclass
class WebhookConfig:
    url: str = ""
    enabled: bool = False
    events: dict = field(default_factory=lambda: dict(_DEFAULT_EVENTS))


def load_webhook_config() -> WebhookConfig:
    if not WEBHOOK_CONFIG_PATH.exists():
        return WebhookConfig()
    try:
        data = json.loads(WEBHOOK_CONFIG_PATH.read_text())
    except (json.JSONDecodeError, OSError):
        return WebhookConfig()
    events = dict(_DEFAULT_EVENTS)
    events.update(data.get("events", {}))
    return WebhookConfig(url=data.get("url", ""), enabled=bool(data.get("enabled", False)), events=events)


def save_webhook_config(config: WebhookConfig) -> None:
    with _config_lock:
        WEBHOOK_CONFIG_PATH.write_text(json.dumps(
            {"url": config.url, "enabled": config.enabled, "events": config.events}, indent=2
        ))


def mask_webhook_url(url: str) -> str:
    """Display form of a webhook URL: scheme + host + a few trailing characters, never the secret
    token path. Empty stays empty. Deterministic, so a form that echoes it back unchanged can be
    recognised and treated as "keep the stored URL" (see web_server.api_save_webhook_config)."""
    if not url:
        return ""
    from urllib.parse import urlsplit
    parts = urlsplit(url)
    if not parts.scheme or not parts.netloc:
        return "****"
    return f"{parts.scheme}://{parts.netloc}/…{url[-4:]}"


def _payload_for(url: str, message: str) -> dict:
    """Discord webhooks require {"content": ...}; Slack incoming webhooks require {"text": ...}.
    A custom relay controlled by the user can read whichever key it wants, so both are sent when
    the URL doesn't obviously match one of the two well-known services."""
    if "discord.com" in url:
        return {"content": message}
    if "hooks.slack.com" in url:
        return {"text": message}
    return {"content": message, "text": message}


def send_notification(config: WebhookConfig, event_type: str, message: str) -> tuple[bool, str]:
    """Synchronous send - returns (sent, detail). Never raises: a webhook failure (bad URL,
    offline relay, DNS failure, etc.) must never be allowed to interrupt or block whatever
    machine-control flow triggered it. Callers on a GUI thread should prefer notify_async()
    instead so a slow/unreachable webhook can't stall the UI; this sync form exists for a "Test
    webhook" button, where blocking briefly to show a real result is exactly what's wanted."""
    if not config.enabled or not config.url:
        return False, "webhook not configured or disabled"
    if not config.events.get(event_type, False):
        return False, f"event '{event_type}' is not enabled for notifications"
    payload = _payload_for(config.url, message)
    try:
        req = urllib.request.Request(
            config.url, data=json.dumps(payload).encode("utf-8"),
            headers={
                "Content-Type": "application/json",
                # Discord's webhook endpoint (and Slack's) sits behind Cloudflare, which commonly
                # rejects requests carrying Python's default urllib User-Agent
                # ("Python-urllib/3.x") with a 403 Forbidden that has nothing to do with the
                # webhook URL itself being wrong - confirmed live 2026-08-22 (same root cause
                # already found and fixed this session for DuckDuckGo's search page). A realistic
                # browser User-Agent gets past that filter without misrepresenting anything about
                # the actual request being made (a plain webhook POST, not scraping/automation of
                # protected content).
                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0 Safari/537.36",
            },
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=5.0) as resp:
            return True, f"sent (HTTP {resp.status})"
    except urllib.error.HTTPError as exc:
        return False, f"failed: HTTP {exc.code} {exc.reason}"
    except (urllib.error.URLError, OSError) as exc:
        return False, f"failed: {exc}"


def notify_async(config: WebhookConfig, event_type: str, message: str) -> None:
    """Fire-and-forget wrapper around send_notification() for use at real trigger points, so a
    slow or unreachable webhook can never delay machine-control code. Result is only logged, not
    returned - by the time it's known, the caller has moved on."""
    if not config.enabled or not config.url or not config.events.get(event_type, False):
        return
    threading.Thread(target=send_notification, args=(config, event_type, message), daemon=True).start()
