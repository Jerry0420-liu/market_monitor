from __future__ import annotations

from fastapi.testclient import TestClient


def test_liveness_reports_service_without_market_state() -> None:
    from market_monitor_api.app import app

    response = TestClient(app).get("/health/live")

    assert response.status_code == 200
    assert response.json() == {"service": "market-monitor", "status": "ok"}
