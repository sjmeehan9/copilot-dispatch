"""FastAPI application entrypoint.

Provides the application factory and core routing for the Copilot Dispatch API.
"""

from __future__ import annotations

import logging
from contextlib import asynccontextmanager
from typing import AsyncGenerator

from fastapi import FastAPI

from app.src.api.actions_routes import actions_router
from app.src.api.middleware import RequestLoggingMiddleware
from app.src.api.routes import router as agent_router
from app.src.config import get_settings
from app.src.exceptions import register_exception_handlers
from app.src.services.actions import get_actions_service
from app.src.services.dispatcher import get_dispatch_service
from app.src.services.webhook import get_webhook_service


def setup_logging() -> None:
    """Configure structured JSON logging based on application settings."""
    settings = get_settings()
    level_name = settings.log_level.upper()
    level = getattr(logging, level_name, logging.INFO)

    # Basic configuration for now; can be expanded to JSON formatting later
    logging.basicConfig(
        level=level,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        datefmt="%Y-%m-%dT%H:%M:%S%z",
    )


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncGenerator[None, None]:
    """Manage application lifecycle events.

    Args:
        app: The FastAPI application instance.
    """
    logger = logging.getLogger("dispatch.lifespan")
    logger.info("Starting Copilot Dispatch API service")

    # Initialize shared services
    dispatch_service = get_dispatch_service()
    webhook_service = get_webhook_service()
    actions_service = get_actions_service()
    app.state.dispatch_service = dispatch_service
    app.state.webhook_service = webhook_service
    app.state.actions_service = actions_service

    yield

    logger.info("Shutting down Copilot Dispatch API service")
    await dispatch_service.close()
    await webhook_service.close()
    await actions_service.close()


def create_app() -> FastAPI:
    """Create and configure the FastAPI application instance.

    Returns:
        A configured FastAPI application.
    """
    setup_logging()

    app = FastAPI(
        title="Copilot Dispatch",
        description="Copilot Agent Application API",
        version="0.1.0",
        lifespan=lifespan,
    )

    app.add_middleware(RequestLoggingMiddleware)
    register_exception_handlers(app)
    app.include_router(agent_router)
    app.include_router(actions_router)

    @app.get("/health")
    def health_check() -> dict[str, str]:
        """Liveness probe endpoint.

        Returns:
            A dictionary indicating healthy status.
        """
        return {"status": "healthy"}

    return app


# Export the application instance for uvicorn
app = create_app()
