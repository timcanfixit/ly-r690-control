"""Access control for the web control panel (review finding #13, 2026-11-12).

Model: one shared access token, stored in auth_config.json beside the app (created on first start with
owner-only permissions, never committed - see .gitignore). A browser logs in once at /login with the token
and gets a signed session cookie (HttpOnly, SameSite=Strict); scripts may send `Authorization: Bearer <token>`.

Everything except /login and the static login assets requires authentication - including the live
read-only data - because the page also exposes saved profiles, settings and run history.

Loopback exemption: requests from 127.0.0.1/::1 with no proxy headers are trusted. That is what lets
tools/deploy-to-pi.ps1 (which curls the service from the Pi itself over ssh) keep working, and anyone
with a shell on the Pi can already open the serial port directly. If you put a reverse proxy on the same
host, it MUST set X-Forwarded-For (most do) - requests carrying proxy headers are never treated as local.

Failed logins are rate-limited per client address. Set LY_R690_AUTH=off in the service environment to disable
authentication entirely (not recommended; only for a network you fully control)."""
from __future__ import annotations

import hmac
import json
import os
import secrets
import sys
import threading
import time
from pathlib import Path

if getattr(sys, "frozen", False):
    AUTH_CONFIG_PATH = Path(sys.executable).resolve().parent / "auth_config.json"
else:
    AUTH_CONFIG_PATH = Path(__file__).resolve().parent.parent / "auth_config.json"

SESSION_DAYS = 30
MAX_FAILURES = 5
LOCKOUT_S = 300.0

_lock = threading.Lock()
_failures: dict[str, list] = {}   # address -> [count, locked_until_monotonic]


def auth_disabled() -> bool:
    return os.environ.get("LY_R690_AUTH", "").strip().lower() in ("off", "0", "false", "disabled")


def load_or_create_credentials() -> tuple[str, str, bool]:
    """Returns (access_token, session_secret, created_now). Creates the file (mode 600) on first use.
    A corrupt or incomplete file is replaced, which simply invalidates old sessions and token."""
    try:
        data = json.loads(AUTH_CONFIG_PATH.read_text(encoding="utf-8"))
        token, secret = data.get("access_token"), data.get("session_secret")
        if isinstance(token, str) and len(token) >= 24 and isinstance(secret, str) and len(secret) >= 32:
            return token, secret, False
    except (OSError, ValueError):
        pass
    token, secret = secrets.token_urlsafe(24), secrets.token_hex(32)
    payload = json.dumps({"access_token": token, "session_secret": secret}, indent=2)
    fd = os.open(str(AUTH_CONFIG_PATH), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        f.write(payload)
    return token, secret, True


def is_local_request(remote_addr: str | None, headers) -> bool:
    if remote_addr not in ("127.0.0.1", "::1"):
        return False
    return not any(h in headers for h in ("X-Forwarded-For", "X-Real-IP", "Forwarded"))


def token_matches(supplied: str, expected: str) -> bool:
    return bool(supplied) and hmac.compare_digest(supplied.encode(), expected.encode())


def bearer_token(headers) -> str:
    value = headers.get("Authorization", "")
    return value[7:].strip() if value.lower().startswith("bearer ") else ""


def locked_out(address: str) -> float:
    """Seconds remaining on a lockout for this address (0 if none)."""
    with _lock:
        entry = _failures.get(address)
        if entry and entry[1] > time.monotonic():
            return entry[1] - time.monotonic()
        return 0.0


def record_failure(address: str) -> None:
    with _lock:
        entry = _failures.setdefault(address, [0, 0.0])
        if entry[1] and entry[1] <= time.monotonic():
            entry[0], entry[1] = 0, 0.0
        entry[0] += 1
        if entry[0] >= MAX_FAILURES:
            entry[1] = time.monotonic() + LOCKOUT_S


def record_success(address: str) -> None:
    with _lock:
        _failures.pop(address, None)


def reset_state_for_tests() -> None:
    with _lock:
        _failures.clear()


LOGIN_PAGE = """<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>LY-R690 login</title>
<style>
 body{font-family:system-ui,sans-serif;background:#111;color:#eee;display:flex;min-height:100vh;align-items:center;justify-content:center;margin:0}
 form{background:#1c1c1c;padding:28px;border-radius:10px;width:min(360px,92vw);box-shadow:0 4px 24px #0008}
 h1{font-size:1.2rem;margin:0 0 6px} p{color:#aaa;font-size:.85rem;margin:0 0 16px}
 input,button{width:100%;box-sizing:border-box;padding:10px;font-size:1rem;border-radius:6px;border:1px solid #444}
 input{background:#0d0d0d;color:#eee;margin-bottom:12px} button{background:#2b6cb0;color:#fff;border:0;cursor:pointer}
 .err{color:#ff8080;margin:0 0 12px;font-size:.9rem}
</style></head><body>
<form method="post" action="/login">
 <h1>LY-R690 control panel</h1>
 <p>Enter the access token (stored in auth_config.json on the Pi).</p>
 __ERROR__
 <input type="password" name="token" autocomplete="current-password" autofocus required placeholder="Access token">
 <button type="submit">Sign in</button>
</form></body></html>"""
