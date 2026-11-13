"""Coverage for the global JSON error handler (2026-09-15, full-webapp bug pass) - see
_handle_unhandled_exception's own docstring in web_server.py. Before this, an unhandled exception
in any route fell through to Flask's default HTML error page, which made every button's
postJson()/fetch(...).then(r => r.json()) call throw a SyntaxError - uncaught in nearly every
caller, so a real server-side bug looked identical to a button doing nothing at all, with no alert
and no error shown anywhere.
"""
from werkzeug.exceptions import NotFound

import web_server

# Registering new routes on web_server.app inside a test would break once ANY test in the shared
# suite has already sent it a request (Flask locks route registration after the first request,
# and this module-level Flask app is shared across the whole test session) - calling the handler
# function directly, inside a request context, tests the exact same code without that fragility.


def test_unhandled_exception_returns_json_not_html():
    with web_server.app.test_request_context("/api/vacuum", method="POST"):
        body_resp, status_code = web_server._handle_unhandled_exception(RuntimeError("kaboom"))

    assert status_code == 500
    body = body_resp.get_json()
    assert body["ok"] is False
    assert "kaboom" in body["detail"]


def test_http_exception_keeps_its_own_status_code_and_is_still_json():
    exc = NotFound(description="nothing here")
    with web_server.app.test_request_context("/api/does-not-exist"):
        body_resp, status_code = web_server._handle_unhandled_exception(exc)

    assert status_code == 404
    body = body_resp.get_json()
    assert body["ok"] is False
    assert "nothing here" in body["detail"]


def test_genuinely_unknown_route_still_returns_json_404():
    """A real 404 (no matching route at all, not a deliberate abort()) must also come back as
    JSON, not Flask's default HTML 404 page - this is the case a real client hitting a typo'd or
    stale URL would trigger."""
    client = web_server.app.test_client()
    resp = client.get("/api/this-route-does-not-exist")

    assert resp.status_code == 404
    body = resp.get_json()
    assert body["ok"] is False
