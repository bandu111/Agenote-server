"""验证公共组件拆分后，各类响应仍具有统一追踪和错误保护。"""
from uuid import UUID
import pytest
from sqlalchemy.exc import SQLAlchemyError
from starlette.exceptions import HTTPException


@pytest.mark.parametrize("status", [400, 500, 503])
async def test_failures_keep_safe_response_and_request_context(client, app, status):
    private_detail = "private-password-token-and-database-detail"

    @app.get("/test/failure")
    async def fail():
        if status == 400:
            raise HTTPException(status_code=400, detail=private_detail)
        if status == 503:
            raise SQLAlchemyError(private_detail)
        raise RuntimeError(private_detail)

    response = await client.get("/test/failure")
    expected = {400: "request_failed", 500: "internal_error", 503: "database_unavailable"}
    assert response.status_code == status
    assert response.json() == {"code": status, "message": expected[status], "data": None}
    assert private_detail not in response.text
    assert response.headers["cache-control"] == "no-store"
    assert response.headers["pragma"] == "no-cache"
    UUID(response.headers["x-request-id"])


async def test_success_and_http_errors_have_unique_request_ids(client):
    responses = [
        await client.get("/health"),
        await client.get("/unknown"),
        await client.get("/api/v1/auth/login"),
        await client.get("/api/v1/auth/me"),
        await client.post("/api/v1/auth/login", json={}),
    ]
    assert [response.status_code for response in responses] == [200, 404, 405, 401, 422]
    request_ids = set()
    for response in responses:
        request_ids.add(UUID(response.headers["x-request-id"]))
        assert response.headers["cache-control"] == "no-store"
        assert response.headers["pragma"] == "no-cache"
    assert len(request_ids) == len(responses)
    assert responses[3].headers["www-authenticate"] == "Bearer"
