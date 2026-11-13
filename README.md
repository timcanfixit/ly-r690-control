# LY-R690 web control panel

External monitoring and control software for an **LY-R690 BGA rework station**, talking Modbus RTU over RS485.
A Flask web app runs on a Raspberry Pi wired to the machine's RS485 bus; a Windows desktop app (PySide6,
`pc-app/src/main_window.py`) shares the same core modules. Features: live temperatures, curve/PID/fan-timing
send with read-back verification, profile library with web-search + local-LLM (Ollama) assisted profile
suggestions, run logging and summaries, a safety layer (limits, ramp-rate, fault handling), and notifications.

> ## WARNING - read before use
> This software **can switch the machine's heaters on**. It is an unofficial, community project, is not
> affiliated with or endorsed by the manufacturer, has had limited real-hardware verification, and comes with
> **no warranty** (see LICENSE). Never leave a running machine unattended. The software safety checks
> complement - they do not replace - the machine's own protections and your own supervision. Behavior such as
> "does writing a self-tune coil off really cancel an autotune" has **not** been verified on hardware.
> AI-estimated profiles are suggestions, not tested process recipes.

## Security
The web panel requires an **access token**. On first start it creates `pc-app/auth_config.json` (owner-only
permissions) and logs the token once: `journalctl -u ly-r690-web | grep "access token"`. Open the page, enter the
token on `/login`; scripts may send `Authorization: Bearer <token>`. To rotate: delete `auth_config.json` and
restart. Requests from the Pi itself (loopback, with no proxy headers) are trusted so local tooling works.
Failed logins are rate-limited. The server speaks plain HTTP - **do not expose port 8080 to the internet**; use
a VPN such as Tailscale, or a reverse proxy with TLS. `LY_R690_AUTH=off` disables auth (not recommended).

## Install on the Pi
```bash
cd pc-app
python3 -m venv venv && venv/bin/pip install -r requirements-web.txt
sudo cp ly-r690-web.service /etc/systemd/system/   # edit User=, paths and serial port first
sudo systemctl daemon-reload && sudo systemctl enable --now ly-r690-web
```
Then browse to `http://<pi>:8080`. `tools/deploy-to-pi.ps1` copies the code from Windows and restarts the
service without sudo (the service exits non-zero on request and systemd restarts it).

## Tests
```bash
cd pc-app && python -m pytest tests -q
```
(needs pymodbus, pyserial, Flask, openpyxl; the desktop-app tests also need PySide6 and pyqtgraph.)

## Layout
- `pc-app/src/machine_bus.py` - serial bus: passive listener plus coordinated, verified reads/writes
- `pc-app/src/modbus_client.py` - register map and constants; `docs/06-modbus-register-map.md` explains it
- `safety_limits.py`, `fault_manager.py`, `machine_state.py`, `preflight.py` - safety layer
- `web_server.py`, `web_auth.py`, `static/`, `templates/` - the Pi web app
- `profile_*.py` - profile store, import, validation and online/LLM suggestion

Your own settings, saved profiles, API keys and run logs are never part of this repository (see `.gitignore`).
