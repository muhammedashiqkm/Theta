async def test_health_needs_no_token(make_client):
    async with make_client() as client:
        response = await client.get("/health")
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


async def test_ready_is_503_and_names_the_broken_store(make_client):
    async with make_client(database_ok=False) as client:
        response = await client.get("/ready")
    body = response.json()
    assert response.status_code == 503
    assert body["ready"] is False
    assert body["checks"]["database"] == {"ok": False, "detail": "connection refused"}
    assert body["checks"]["redis"]["ok"] is True


async def test_status_rejects_a_missing_token(make_client, caller_headers):
    del caller_headers["X-Theta-Token"]
    async with make_client() as client:
        response = await client.get("/v1/status", headers=caller_headers)
    assert response.status_code == 401


async def test_status_rejects_a_wrong_token(make_client, caller_headers):
    caller_headers["X-Theta-Token"] = "not-the-token"
    async with make_client() as client:
        response = await client.get("/v1/status", headers=caller_headers)
    assert response.status_code == 401


async def test_status_refuses_everyone_when_no_token_is_configured(make_client, caller_headers):
    caller_headers["X-Theta-Token"] = ""
    async with make_client(configured_token="") as client:
        response = await client.get("/v1/status", headers=caller_headers)
    assert response.status_code == 401


async def test_status_requires_the_caller_context(make_client, caller_headers):
    del caller_headers["X-Theta-Institution-Id"]
    async with make_client() as client:
        response = await client.get("/v1/status", headers=caller_headers)
    assert response.status_code == 422


async def test_status_rejects_an_unknown_role(make_client, caller_headers):
    caller_headers["X-Theta-Role"] = "superuser"
    async with make_client() as client:
        response = await client.get("/v1/status", headers=caller_headers)
    assert response.status_code == 422


async def test_status_echoes_the_caller_and_both_stores(make_client, caller_headers):
    async with make_client(redis_ok=False) as client:
        response = await client.get("/v1/status", headers=caller_headers)
    body = response.json()
    assert response.status_code == 200
    assert body["service"] == "Theta"
    assert body["caller"] == {"institution_id": 12, "user_id": 345, "role": "teacher"}
    assert body["database"] == {"ok": True, "detail": "PostgreSQL 16.4"}
    assert body["redis"] == {"ok": False, "detail": "connection refused"}
