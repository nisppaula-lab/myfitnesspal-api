import json

import pytest
from starlette.testclient import TestClient

from mfp_mcp import server


@pytest.fixture(scope="module")
def client():
    original_token = server.REST_API_TOKEN
    original_get_diary = server.mfp_get_diary
    original_add_food = server.mfp_add_food_to_diary

    async def fake_get_diary(params):
        return json.dumps({"date": params.date, "meals": []})

    async def fake_add_food(params):
        return json.dumps(
            {
                "added": True,
                "food_id": params.mfp_id,
                "meal": params.meal,
                "quantity": params.quantity,
            }
        )

    server.REST_API_TOKEN = "test-token"
    server.mfp_get_diary = fake_get_diary
    server.mfp_add_food_to_diary = fake_add_food
    app = server.BearerAuthMiddleware(server.mcp.streamable_http_app())
    try:
        with TestClient(app, raise_server_exceptions=False) as test_client:
            yield test_client
    finally:
        server.REST_API_TOKEN = original_token
        server.mfp_get_diary = original_get_diary
        server.mfp_add_food_to_diary = original_add_food


def test_health_reports_configured(client):
    response = client.get("/health")
    assert response.status_code == 200
    assert response.json() == {"ok": True, "status": "ok", "configured": True}


def test_mcp_endpoint_is_not_public(client):
    response = client.post("/mcp", json={})
    assert response.status_code == 401


def test_diary_requires_bridge_token(client):
    response = client.get("/diary?date=2026-10-08")
    assert response.status_code == 401


def test_diary_returns_upstream_json(client):
    response = client.get(
        "/diary?date=2026-10-08",
        headers={"Authorization": "Bearer test-token"},
    )
    assert response.status_code == 200
    assert response.json() == {"date": "2026-10-08", "meals": []}


def test_add_food_validates_and_maps_fields(client):
    response = client.post(
        "/diary/entries",
        headers={"Authorization": "Bearer test-token"},
        json={"food_id": "123", "meal": "Lunch", "quantity": 2},
    )
    assert response.status_code == 200
    assert response.json() == {
        "added": True,
        "food_id": "123",
        "meal": "Lunch",
        "quantity": 2.0,
    }
