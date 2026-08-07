import asyncio

from httpx import ASGITransport, AsyncClient

from main import app


async def get(path: str):
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        return await client.get(path)


def test_health() -> None:
    response = asyncio.run(get("/health"))

    assert response.status_code == 200
    assert response.json() == {
        "status": "ok",
        "service": "DWP Agent Runtime",
        "version": "0.1.0",
    }


def test_openapi_contains_only_system_api() -> None:
    response = asyncio.run(get("/openapi.json"))

    assert response.status_code == 200
    assert set(response.json()["paths"]) == {"/health"}
