"""Regression coverage for the Discord/Slack webhook 403 fix (2026-08-22): Discord and Slack's
webhook endpoints sit behind Cloudflare, which rejects requests carrying Python's default urllib
User-Agent ("Python-urllib/3.x") with a 403 Forbidden unrelated to the webhook URL itself being
wrong - the same root cause already found for DuckDuckGo's search page earlier this session. Fixed
by sending a realistic browser User-Agent. Verified live against a real Discord webhook (HTTP 204)
before this test was written; no real HTTP requests are made here - urlopen is mocked."""
import urllib.error

import notifications as n


class _FakeResp:
    status = 204
    def __enter__(self): return self
    def __exit__(self, *a): return False


def test_send_notification_sets_a_realistic_browser_user_agent(monkeypatch):
    captured = {}

    def fake_urlopen(req, timeout=5.0):
        captured["headers"] = dict(req.header_items())
        return _FakeResp()

    monkeypatch.setattr(n.urllib.request, "urlopen", fake_urlopen)
    config = n.WebhookConfig(url="https://discord.com/api/webhooks/x/y", enabled=True,
                              events={"run_started": True})
    ok, detail = n.send_notification(config, "run_started", "test")

    assert ok is True
    assert "sent (HTTP 204)" in detail
    user_agent = captured["headers"].get("User-agent", "")
    assert "Python-urllib" not in user_agent
    assert "Mozilla" in user_agent


def test_send_notification_reports_http_error_clearly(monkeypatch):
    def fake_urlopen(req, timeout=5.0):
        raise urllib.error.HTTPError(req.full_url, 403, "Forbidden", hdrs=None, fp=None)

    monkeypatch.setattr(n.urllib.request, "urlopen", fake_urlopen)
    config = n.WebhookConfig(url="https://discord.com/api/webhooks/x/y", enabled=True,
                              events={"run_started": True})
    ok, detail = n.send_notification(config, "run_started", "test")

    assert ok is False
    assert "403" in detail
    assert "Forbidden" in detail
