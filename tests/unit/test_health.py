"""Unit tests for the health check endpoint."""

from __future__ import annotations

import pytest
from httpx import ASGITransport, AsyncClient

from app.src.main import create_app


@pytest.mark.asyncio
async def test_health_check(test_client: AsyncClient) -> None:
    """Verify the /health endpoint returns 200 OK and healthy status."""
    response = await test_client.get("/health")

    assert response.status_code == 200
    assert response.json() == {"status": "healthy"}


@pytest.mark.asyncio
async def test_lifespan(monkeypatch: pytest.MonkeyPatch) -> None:
    """Verify the application lifespan context manager executes without error."""
    monkeypatch.setenv("GITHUB_PAT", "test-pat")
    monkeypatch.setenv("DISPATCH_WEBHOOK_SECRET", "test-webhook-secret")

    app = create_app()
    # Call the lifespan context manager directly to ensure coverage
    async with app.router.lifespan_context(app):
        pass
