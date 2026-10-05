"""Smoke tests for the HTTP API, run against the built-in demo data (no broker connection needed)."""
import pytest
from flask import Flask

from oi_dashboard.api import ALL_BLUEPRINTS
from oi_dashboard.core import state


@pytest.fixture()
def client():
    app = Flask(__name__)
    for bp in ALL_BLUEPRINTS:
        app.register_blueprint(bp)
    previous = state.demo_mode
    yield app.test_client()
    state.demo_mode = previous


def test_ping(client):
    assert client.get("/api/ping").status_code == 200


def test_demo_payload_has_the_fields_the_dashboard_reads(client):
    client.get("/api/demo/on")
    payload = client.get("/api/data").get_json()
    for key in ("spot", "chain", "pcr", "max_pain", "support", "resistance"):
        assert key in payload
    assert len(payload["chain"]) > 10


def test_paper_trades_endpoint_returns_json(client):
    assert client.get("/api/paper_trades").is_json
