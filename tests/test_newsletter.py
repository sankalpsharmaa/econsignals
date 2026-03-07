"""Tests for newsletter email delivery helpers."""

import json

from econsignals.lenses import newsletter


class _FakeSmtp:
    def __init__(self, host, port, timeout):
        self.host = host
        self.port = port
        self.timeout = timeout
        self.login_args = None
        self.message = None

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False

    def login(self, username, password):
        self.login_args = (username, password)

    def send_message(self, message):
        self.message = message


class _FakeResponse:
    def __init__(self, body):
        self._body = body

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False

    def read(self):
        return self._body


def test_send_gmail_uses_configured_sender(monkeypatch):
    captured = {}

    def fake_smtp(host, port, timeout):
        client = _FakeSmtp(host, port, timeout)
        captured["client"] = client
        return client

    monkeypatch.setenv("GMAIL_APP_PASSWORD", "app-pass")
    monkeypatch.setenv("GMAIL_EMAIL", "sender@example.com")
    monkeypatch.setattr("smtplib.SMTP_SSL", fake_smtp)

    sent = newsletter._send_gmail("<p>Hello</p>", "Hello", "dest@example.com")

    assert sent is True
    client = captured["client"]
    assert client.login_args == ("sender@example.com", "app-pass")
    assert client.message["From"] == "sender@example.com"
    assert client.message["To"] == "dest@example.com"


def test_send_resend_uses_configured_sender(monkeypatch):
    captured = {}

    def fake_urlopen(request, timeout, context):
        captured["payload"] = json.loads(request.data.decode("utf-8"))
        captured["auth"] = request.headers["Authorization"]
        return _FakeResponse(b'{"id":"email_123"}')

    monkeypatch.setenv("RESEND_API_KEY", "resend-key")
    monkeypatch.setenv("RESEND_EMAIL_FROM", "alerts@example.com")
    monkeypatch.setattr(newsletter, "urlopen", fake_urlopen)

    sent = newsletter._send_resend("<p>Hello</p>", "Hello", "dest@example.com")

    assert sent is True
    assert captured["auth"] == "Bearer resend-key"
    assert captured["payload"]["from"] == "EconSignals <alerts@example.com>"
    assert captured["payload"]["to"] == ["dest@example.com"]


def test_send_resend_skips_without_sender(monkeypatch, capsys):
    monkeypatch.setenv("RESEND_API_KEY", "resend-key")
    monkeypatch.delenv("RESEND_EMAIL_FROM", raising=False)
    monkeypatch.delenv("ECONSIGNALS_EMAIL_FROM", raising=False)

    sent = newsletter._send_resend("<p>Hello</p>", "Hello", "dest@example.com")

    assert sent is False
    assert "skipping Resend" in capsys.readouterr().err
