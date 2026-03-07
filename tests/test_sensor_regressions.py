import json

from econsignals.lib.db import get_last_sensor_run
from econsignals.lenses import newsletter
from econsignals.sensors import bluesky, conferences, funding


def test_funding_run_marks_partial_success_on_fetch_failure(monkeypatch):
    monkeypatch.setattr(
        funding,
        "FUNDING_SOURCES",
        {
            "ok": {
                "name": "Working Source",
                "url": "https://example.com/ok",
                "org": "IGC",
            },
            "bad": {
                "name": "Broken Source",
                "url": "https://example.com/bad",
                "org": "IGC",
            },
        },
    )

    def fake_fetch(self, url, headers=None, timeout=30):
        if url.endswith("/bad"):
            raise RuntimeError("boom")
        return b"<html><p>Applications due January 15, 2030</p></html>"

    monkeypatch.setattr(funding.FundingSensor, "fetch_url", fake_fetch)

    result = funding.FundingSensor().run()

    assert result["status"] == "partial_success"
    assert result["errors"] == 1
    assert result["found"] == 1

    run = get_last_sensor_run("funding")
    assert run["status"] == "partial_success"
    assert run["items_found"] == 1


def test_conferences_skip_fallback_when_base_fetch_fails(monkeypatch):
    monkeypatch.setattr(
        conferences,
        "CONFERENCES",
        {
            "broken": {
                "name": "Broken Conference",
                "url": "https://example.com/broken",
                "org": "BrokenOrg",
                "typical_deadline_month": 11,
                "typical_event_month": 3,
                "relevance": 1.0,
            }
        },
    )

    def fake_fetch(self, url, headers=None, timeout=30):
        raise RuntimeError("fetch failed")

    monkeypatch.setattr(conferences.ConferencesSensor, "fetch_url", fake_fetch)

    result = conferences.ConferencesSensor().run()

    assert result["status"] == "partial_success"
    assert result["errors"] == 1
    assert result["found"] == 0

    run = get_last_sensor_run("conferences")
    assert run["status"] == "partial_success"
    assert run["items_found"] == 0


def test_deadline_registries_use_updated_urls():
    assert funding.FUNDING_SOURCES["russell_sage"]["url"] == "https://www.russellsage.org/apply"
    assert conferences.CONFERENCES["pacdev"]["url"] == "https://pacdev.ucdavis.edu/"
    assert conferences.CONFERENCES["bread"]["url"] == "https://www.ibread.org/conferences"
    assert "isa_south_asia" not in conferences.CONFERENCES


def test_bluesky_public_mode_skips_search(monkeypatch):
    sensor = bluesky.BlueskySensor()

    def fail_fetch(url, headers=None):
        raise AssertionError("public mode should not call searchPosts")

    monkeypatch.setattr(sensor, "fetch_json", fail_fetch)

    assert sensor._collect_search_posts(None) == []


def test_resend_requires_configured_sender(monkeypatch):
    monkeypatch.setenv("RESEND_API_KEY", "test-key")
    monkeypatch.delenv("RESEND_FROM", raising=False)
    monkeypatch.delenv("RESEND_FROM_EMAIL", raising=False)
    monkeypatch.delenv("RESEND_FROM_NAME", raising=False)

    def fail_urlopen(*args, **kwargs):
        raise AssertionError("Resend request should be skipped without a configured sender")

    monkeypatch.setattr(newsletter, "urlopen", fail_urlopen)

    assert newsletter._send_resend("<p>hi</p>", "hi", "to@example.com") is False


def test_resend_uses_configured_sender(monkeypatch):
    monkeypatch.setenv("RESEND_API_KEY", "test-key")
    monkeypatch.setenv("RESEND_FROM", "EconSignals <verified@example.com>")

    captured = {}

    class FakeResponse:
        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

        def read(self):
            return b'{"id":"msg_123"}'

    def fake_urlopen(req, timeout=30, context=None):
        captured["payload"] = json.loads(req.data.decode("utf-8"))
        return FakeResponse()

    monkeypatch.setattr(newsletter, "urlopen", fake_urlopen)

    assert newsletter._send_resend("<p>hi</p>", "hi", "to@example.com") is True
    assert captured["payload"]["from"] == "EconSignals <verified@example.com>"
