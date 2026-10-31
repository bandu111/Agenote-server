import asyncio
from datetime import timedelta
from unittest.mock import AsyncMock, patch
import jwt
import pytest
from sqlalchemy import func, select
from sqlalchemy.exc import SQLAlchemyError
from app.core.database.mixins import now
from app.modules.identity.models.identity import AuthSession, RefreshToken, User
from app.main import create_app
from httpx import ASGITransport, AsyncClient

PASSWORD = "sample-only-password-2026"


async def register(client, email="yan@example.com"):
    response = await client.post(
        "/api/v1/auth/register",
        json={"name": "言言", "email": email, "password": PASSWORD},
    )
    assert response.status_code == 201
    return response.json()["data"]


def bearer(data):
    return {"Authorization": "Bearer " + data["access_token"]}


async def test_register_login_me_and_persistence(client, app):
    data = await register(client, "  YAN@Example.COM  ")
    assert data["user"]["email"] == "yan@example.com"
    assert data["user"]["email_verified"] is False
    assert PASSWORD not in str(data)
    result = await client.get("/api/v1/auth/me", headers=bearer(data))
    assert result.status_code == 200
    assert result.json()["data"] == data["user"]
    async with app.state.database.sessions() as db:
        user = await db.scalar(select(User))
        refresh = await db.scalar(select(RefreshToken))
        assert user.password_hash.startswith("$argon2id$")
        assert user.password_hash != PASSWORD
        assert refresh.token_hash != data["refresh_token"]
        assert len(refresh.token_hash) == 64
    login = await client.post(
        "/api/v1/auth/login", json={"email": "YAN@example.com", "password": PASSWORD}
    )
    assert login.status_code == 200
    assert login.json()["data"]["user"]["id"] == data["user"]["id"]
    assert login.headers["cache-control"] == "no-store"
    assert login.headers["x-request-id"]


async def test_duplicate_registration(client, app):
    await register(client)
    response = await client.post(
        "/api/v1/auth/register",
        json={"name": "另一个人", "email": "YAN@example.com", "password": PASSWORD},
    )
    assert response.status_code == 409
    async with app.state.database.sessions() as db:
        assert await db.scalar(select(func.count()).select_from(User)) == 1


@pytest.mark.parametrize(
    "payload",
    [
        {"name": "", "email": "yan@example.com", "password": PASSWORD},
        {"name": "   ", "email": "yan@example.com", "password": PASSWORD},
        {"name": "甲" * 41, "email": "yan@example.com", "password": PASSWORD},
        {"name": "甲", "email": "invalid", "password": PASSWORD},
        {"name": "甲", "email": "yan@example.com", "password": "short"},
        {"name": "甲", "email": "yan@example.com", "password": " " * 10},
        {"name": "甲", "email": "yan@example.com", "password": "x" * 129},
        {
            "name": "甲",
            "email": "yan@example.com",
            "password": PASSWORD,
            "is_active": True,
        },
    ],
)
async def test_validation_never_echoes_password(client, payload):
    response = await client.post("/api/v1/auth/register", json=payload)
    assert response.status_code == 422
    assert response.json() == {"code": 422, "message": "invalid_request", "data": None}
    assert response.headers["cache-control"] == "no-store"


async def test_wrong_password_and_unknown_email_same_error(client):
    await register(client)
    first = await client.post(
        "/api/v1/auth/login",
        json={"email": "yan@example.com", "password": "wrong-password"},
    )
    second = await client.post(
        "/api/v1/auth/login",
        json={"email": "unknown@example.com", "password": PASSWORD},
    )
    assert first.status_code == second.status_code == 401
    assert first.json() == second.json()
    assert first.headers["www-authenticate"] == "Bearer"


async def test_refresh_rotation_and_reuse_revokes_session(client):
    initial = await register(client)
    response = await client.post(
        "/api/v1/auth/refresh", json={"refresh_token": initial["refresh_token"]}
    )
    assert response.status_code == 200
    current = response.json()["data"]
    assert current["refresh_token"] != initial["refresh_token"]
    replay = await client.post(
        "/api/v1/auth/refresh", json={"refresh_token": initial["refresh_token"]}
    )
    assert replay.status_code == 401
    assert replay.json()["message"] == "refresh_token_reused"
    assert (
        await client.get("/api/v1/auth/me", headers=bearer(current))
    ).status_code == 401
    assert (
        await client.post(
            "/api/v1/auth/refresh", json={"refresh_token": current["refresh_token"]}
        )
    ).status_code == 401


async def test_concurrent_refresh_serialized_by_postgres(client):
    initial = await register(client)
    responses = await asyncio.gather(
        *[
            client.post(
                "/api/v1/auth/refresh", json={"refresh_token": initial["refresh_token"]}
            )
            for _ in range(2)
        ]
    )
    assert sorted(r.status_code for r in responses) == [200, 401]
    succeeded = next(r.json()["data"] for r in responses if r.status_code == 200)
    assert (
        await client.get("/api/v1/auth/me", headers=bearer(succeeded))
    ).status_code == 401


async def test_logout_revokes_only_current_device_session(client):
    initial = await register(client)
    second = (
        await client.post(
            "/api/v1/auth/login",
            json={"email": "yan@example.com", "password": PASSWORD},
        )
    ).json()["data"]
    assert (
        await client.post("/api/v1/auth/logout", headers=bearer(initial))
    ).status_code == 200
    assert (
        await client.get("/api/v1/auth/me", headers=bearer(initial))
    ).status_code == 401
    assert (
        await client.post(
            "/api/v1/auth/refresh", json={"refresh_token": initial["refresh_token"]}
        )
    ).status_code == 401
    assert (
        await client.get("/api/v1/auth/me", headers=bearer(second))
    ).status_code == 200


async def test_disabled_account_cannot_authenticate_or_refresh(client, app):
    initial = await register(client)
    async with app.state.database.sessions() as db:
        user = await db.get(User, initial["user"]["id"])
        user.is_active = False
        await db.commit()
    assert (
        await client.get("/api/v1/auth/me", headers=bearer(initial))
    ).status_code == 401
    assert (
        await client.post(
            "/api/v1/auth/login",
            json={"email": "yan@example.com", "password": PASSWORD},
        )
    ).status_code == 401
    assert (
        await client.post(
            "/api/v1/auth/refresh", json={"refresh_token": initial["refresh_token"]}
        )
    ).status_code == 401


async def test_expired_and_invalid_tokens(client, app):
    data = await register(client)
    settings = app.state.settings
    claims = jwt.decode(
        data["access_token"],
        settings.jwt_secret.get_secret_value(),
        algorithms=["HS256"],
        audience=settings.jwt_audience,
    )
    claims["exp"] = now() - timedelta(seconds=10)
    expired = jwt.encode(
        claims, settings.jwt_secret.get_secret_value(), algorithm="HS256"
    )
    for token in [expired, "invalid", data["refresh_token"]]:
        assert (
            await client.get(
                "/api/v1/auth/me", headers={"Authorization": "Bearer " + token}
            )
        ).status_code == 401
    assert (await client.get("/api/v1/auth/me")).status_code == 401
    async with app.state.database.sessions() as db:
        session = await db.scalar(select(AuthSession))
        session.expires_at = now() - timedelta(seconds=1)
        await db.commit()
    assert (
        await client.post(
            "/api/v1/auth/refresh", json={"refresh_token": data["refresh_token"]}
        )
    ).status_code == 401


async def test_service_rollback_prevents_partial_registration(client, app):
    with patch(
        "app.modules.identity.services.auth_service.AuthService.create_session",
        new=AsyncMock(side_effect=RuntimeError("simulated failure")),
    ):
        response = await client.post(
            "/api/v1/auth/register",
            json={"name": "甲", "email": "rollback@example.com", "password": PASSWORD},
        )
    assert response.status_code == 500
    assert response.json() == {"code": 500, "message": "internal_error", "data": None}
    async with app.state.database.sessions() as db:
        assert await db.scalar(select(func.count()).select_from(User)) == 0


async def test_duplicate_concurrent_registration_unique_constraint(client, app):
    payload = {"name": "甲", "email": "race@example.com", "password": PASSWORD}
    responses = await asyncio.gather(
        *[client.post("/api/v1/auth/register", json=payload) for _ in range(2)]
    )
    assert sorted(r.status_code for r in responses) == [201, 409]
    async with app.state.database.sessions() as db:
        assert await db.scalar(select(func.count()).select_from(User)) == 1
        assert await db.scalar(select(func.count()).select_from(AuthSession)) == 1


async def test_rate_limit_and_uniform_errors(client, app):
    app.state.settings.auth_requests_per_minute = 1
    payload = {"email": "unknown@example.com", "password": PASSWORD}
    assert (await client.post("/api/v1/auth/login", json=payload)).status_code == 401
    response = await client.post("/api/v1/auth/login", json=payload)
    assert response.status_code == 429
    assert response.headers["retry-after"] == "60"
    assert (await client.get("/unknown")).json() == {
        "code": 404,
        "message": "not_found",
        "data": None,
    }
    assert (await client.get("/api/v1/auth/login")).status_code == 405


async def test_health_and_openapi_contract(client):
    assert (await client.get("/health")).json()["data"]["status"] == "ok"
    assert (await client.get("/health/database")).json()["data"][
        "status"
    ] == "connected"
    paths = (await client.get("/openapi.json")).json()["paths"]
    # The route contract is additive; feature modules may register more authenticated paths.
    assert {
        "/api/v1/meetings/live-hints",
        "/api/v1/meetings/{client_id}/realtime",
        "/api/v1/meetings/analysis/capabilities",
        "/api/v1/tasks/{task_id}/binding",
        "/api/v1/search",
        "/api/v1/meetings/sync-delta",
        "/api/v1/auth/profile",
        "/api/v1/auth/password",
        "/api/v1/auth/delete-account",
        '/api/v1/knowledge/preview',
        '/api/v1/knowledge/index-status',
        '/api/v1/tasks',
        '/api/v1/tasks/{task_id}/status',
        "/api/v1/actions",
        "/api/v1/home",
        "/api/v1/push/devices",
        "/api/v1/meetings/asr/capabilities",
        "/api/v1/actions/{action_id}/decision",
        "/api/v1/actions/{action_id}/receipt",
        "/api/v1/actions/{action_id}/reschedule",
        "/api/v1/actions/{action_id}/cancel",
        "/api/v1/knowledge",
        "/api/v1/knowledge/assistance",
        "/api/v1/knowledge/assistance/decision",
        "/api/v1/knowledge/followup",
        "/api/v1/knowledge/sync",
        "/api/v1/knowledge/search",
        "/api/v1/knowledge/resolve",
        "/health",
        "/health/database",
        "/api/v1/auth/register",
        "/api/v1/auth/login",
        "/api/v1/auth/refresh",
        "/api/v1/auth/logout",
        "/api/v1/auth/me",
        "/api/v1/meetings",
        "/api/v1/meetings/sync",
        "/api/v1/meetings/polish",
        "/api/v1/meetings/{client_id}",
        "/api/v1/meetings/{client_id}/analysis",
        "/api/v1/meetings/{client_id}/commitments",
        "/api/v1/meetings/{client_id}/archive",
        "/api/v1/meetings/{client_id}/jobs",
        "/api/v1/meetings/jobs/{job_id}",
        "/api/v1/meetings/jobs/{job_id}/events",
        "/api/v1/progress",
        "/api/v1/progress/{item_id}/complete",
        "/api/v1/progress/goals",
        "/api/v1/progress/goals/{item_id}",
        "/api/v1/progress/commitments/{item_id}/status",
        "/api/v1/progress/commitments/{item_id}/reschedule",
        "/api/v1/progress/artifacts/{item_id}/approve",
    }.issubset(set(paths))


async def test_database_failure_and_malformed_json_are_sanitized(client):
    with patch(
        "sqlalchemy.ext.asyncio.AsyncSession.execute",
        new=AsyncMock(side_effect=SQLAlchemyError("private database detail")),
    ):
        response = await client.get("/health/database")
    assert response.status_code == 503
    assert response.json() == {
        "code": 503,
        "message": "database_unavailable",
        "data": None,
    }
    assert response.headers["cache-control"] == "no-store"
    malformed = await client.post(
        "/api/v1/auth/login",
        content='{"password":"private value",',
        headers={"Content-Type": "application/json"},
    )
    assert malformed.status_code == 422
    assert malformed.json() == {"code": 422, "message": "invalid_request", "data": None}


async def test_new_application_instance_recovers_database_identity_and_revocation(
    client, app
):
    data = await register(client)
    restarted = create_app(app.state.settings)
    async with restarted.router.lifespan_context(restarted):
        async with AsyncClient(
            transport=ASGITransport(app=restarted), base_url="http://test"
        ) as other:
            result = await other.get("/api/v1/auth/me", headers=bearer(data))
            assert result.status_code == 200
            assert result.json()["data"]["id"] == data["user"]["id"]
            assert (
                await other.post("/api/v1/auth/logout", headers=bearer(data))
            ).status_code == 200
    assert (
        await client.get("/api/v1/auth/me", headers=bearer(data))
    ).status_code == 401


@pytest.mark.parametrize("change", ["issuer", "audience", "kind", "missing_exp"])
async def test_jwt_claims_cannot_bypass_validation(client, app, change):
    data = await register(client)
    settings = app.state.settings
    claims = jwt.decode(
        data["access_token"],
        settings.jwt_secret.get_secret_value(),
        algorithms=["HS256"],
        audience=settings.jwt_audience,
    )
    if change == "issuer":
        claims["iss"] = "untrusted"
    elif change == "audience":
        claims["aud"] = "another-app"
    elif change == "kind":
        claims["type"] = "refresh"
    else:
        del claims["exp"]
    token = jwt.encode(
        claims, settings.jwt_secret.get_secret_value(), algorithm="HS256"
    )
    assert (
        await client.get(
            "/api/v1/auth/me", headers={"Authorization": "Bearer " + token}
        )
    ).status_code == 401
