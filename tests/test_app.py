import threading
from datetime import datetime
from unittest.mock import patch
from zoneinfo import ZoneInfo

import pytest

import app as flask_app_module
from app import app, next_scheduled_run

TZ = ZoneInfo("America/New_York")


@pytest.fixture()
def client():
    app.config["TESTING"] = True
    with app.test_client() as c:
        yield c


def wait_for_triage_idle():
    """Block until any in-flight triage worker has released the lock."""
    assert flask_app_module._triage_lock.acquire(timeout=2), "triage lock not released"
    flask_app_module._triage_lock.release()


def test_health_returns_ok(client):
    resp = client.get("/health")
    assert resp.status_code == 200
    assert resp.data == b"ok"


def test_index_returns_html_with_button(client):
    resp = client.get("/")
    assert resp.status_code == 200
    assert resp.content_type.startswith("text/html")
    assert b"Triage Tickets" in resp.data


def test_triage_returns_200_immediately(client):
    with patch.object(flask_app_module, "run_triage"):
        resp = client.post("/triage")
        assert resp.status_code == 200
        wait_for_triage_idle()


def test_triage_calls_run_triage_with_auto_apply(client):
    called = threading.Event()

    def fake_triage(**kwargs):
        assert kwargs == {"auto_apply": True}
        called.set()

    with patch.object(flask_app_module, "run_triage", side_effect=fake_triage):
        client.post("/triage")
        assert called.wait(timeout=2), "run_triage was not called within 2 seconds"
        wait_for_triage_idle()


def test_triage_returns_409_while_run_in_progress(client):
    with patch.object(flask_app_module, "run_triage") as mock_triage:
        assert flask_app_module._triage_lock.acquire(blocking=False)
        try:
            resp = client.post("/triage")
        finally:
            flask_app_module._triage_lock.release()
    assert resp.status_code == 409
    mock_triage.assert_not_called()


def test_scheduled_start_skipped_while_run_in_progress():
    with patch.object(flask_app_module, "run_triage") as mock_triage:
        assert flask_app_module._triage_lock.acquire(blocking=False)
        try:
            assert flask_app_module.start_triage("schedule") is False
        finally:
            flask_app_module._triage_lock.release()
    mock_triage.assert_not_called()


def test_next_scheduled_run_same_day_before_830():
    now = datetime(2026, 7, 22, 8, 0, tzinfo=TZ)
    target = next_scheduled_run(now)
    assert target == datetime(2026, 7, 22, 8, 30, tzinfo=TZ)
    assert target.timestamp() - now.timestamp() == 30 * 60


def test_next_scheduled_run_next_day_after_830():
    now = datetime(2026, 7, 22, 9, 0, tzinfo=TZ)
    assert next_scheduled_run(now) == datetime(2026, 7, 23, 8, 30, tzinfo=TZ)


def test_next_scheduled_run_handles_dst_spring_forward():
    # DST starts 2026-03-08 at 2 AM ET: the 12.5h wall-clock gap is 11.5h real.
    # Compare epoch timestamps — same-tz aware subtraction is wall-clock naive.
    now = datetime(2026, 3, 7, 20, 0, tzinfo=TZ)
    target = next_scheduled_run(now)
    assert target == datetime(2026, 3, 8, 8, 30, tzinfo=TZ)
    assert target.timestamp() - now.timestamp() == 11.5 * 3600
