import threading
import time
from unittest.mock import patch, MagicMock

import pytest

import app as flask_app_module
from app import app


@pytest.fixture()
def client():
    app.config["TESTING"] = True
    with app.test_client() as c:
        yield c


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


def test_triage_calls_run_triage_with_auto_apply(client):
    called = threading.Event()

    def fake_triage(**kwargs):
        assert kwargs == {"auto_apply": True}
        called.set()

    with patch.object(flask_app_module, "run_triage", side_effect=fake_triage):
        client.post("/triage")

    assert called.wait(timeout=2), "run_triage was not called within 2 seconds"
