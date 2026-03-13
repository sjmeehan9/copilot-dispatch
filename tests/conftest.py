"""Root pytest configuration and shared fixtures."""

from __future__ import annotations

from typing import AsyncGenerator

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from app.src.config import Settings, get_settings
from app.src.main import create_app


def pytest_addoption(parser: pytest.Parser) -> None:
    """Register custom command-line options for pytest."""
    parser.addoption(
        "--e2e-confirm",
        action="store_true",
        default=False,
        help="Run end-to-end tests that require external systems",
    )


def pytest_collection_modifyitems(
    config: pytest.Config, items: list[pytest.Item]
) -> None:
    """Skip tests marked with requires_e2e unless --e2e-confirm is passed."""
    if not config.getoption("--e2e-confirm"):
        skip_e2e = pytest.mark.skip(reason="need --e2e-confirm option to run")
        for item in items:
            if "requires_e2e" in item.keywords:
                item.add_marker(skip_e2e)


@pytest.fixture
def monkeypatch_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Set common test environment variables."""
    monkeypatch.setenv("DISPATCH_APP_ENV", "test")
    monkeypatch.setenv("DISPATCH_LOG_LEVEL", "DEBUG")
    monkeypatch.setenv("DISPATCH_DYNAMODB_TABLE_NAME", "dispatch-runs-test")
    monkeypatch.setenv("DISPATCH_DYNAMODB_ENDPOINT_URL", "http://localhost:8100")
    monkeypatch.setenv("DISPATCH_GITHUB_PAT", "test-pat-not-real")
    monkeypatch.setenv("DISPATCH_GITHUB_OWNER", "test-owner")
    monkeypatch.setenv("DISPATCH_GITHUB_REPO", "test-repo")


@pytest.fixture
def test_settings(monkeypatch_env: None) -> Settings:
    """Provide a Settings instance configured for testing."""
    # Clear the lru_cache to ensure fresh settings are loaded
    get_settings.cache_clear()
    return get_settings()


@pytest.fixture
def app(test_settings: Settings) -> FastAPI:
    """Provide a configured FastAPI application instance for testing."""
    return create_app()


@pytest.fixture
async def test_client(app: FastAPI) -> AsyncGenerator[AsyncClient, None]:
    """Provide an asynchronous test client for the FastAPI application."""
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://testserver") as client:
        yield client
