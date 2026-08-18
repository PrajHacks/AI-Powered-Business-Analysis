from __future__ import annotations

"""Tests for static frontend serving at root and /static/ paths."""

from fastapi.testclient import TestClient

from app.main import create_app


def test_root_serves_index_html() -> None:
    app = create_app()
    client = TestClient(app)

    response = client.get("/")
    assert response.status_code == 200
    assert "text/html" in response.headers.get("content-type", "")
    assert "AI Business Analyst" in response.text
    assert "Plotly" in response.text or "plotly" in response.text


def test_static_assets_served() -> None:
    app = create_app()
    client = TestClient(app)

    js_resp = client.get("/static/app.js")
    assert js_resp.status_code == 200
    assert "activeConnectionId" in js_resp.text

    css_resp = client.get("/static/style.css")
    assert css_resp.status_code == 200
    assert "app-header" in css_resp.text
