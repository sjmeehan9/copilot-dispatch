"""FastAPI dependencies for the Copilot Dispatch API.

Provides dependency injection functions for services and configuration.
"""

from __future__ import annotations

from app.src.services.actions import ActionsService, get_actions_service
from app.src.services.dispatcher import DispatchService, get_dispatch_service
from app.src.services.run_store import RunStore, get_run_store
from app.src.services.webhook import WebhookService, get_webhook_service


async def get_run_store_dep() -> RunStore:
    """Provide a RunStore instance for dependency injection.

    Returns:
        A configured RunStore instance.
    """
    return get_run_store()


async def get_dispatch_service_dep() -> DispatchService:
    """Provide a DispatchService instance for dependency injection.

    Returns:
        A configured DispatchService instance.
    """
    return get_dispatch_service()


async def get_webhook_service_dep() -> WebhookService:
    """Provide a WebhookService instance for dependency injection.

    Returns:
        A configured WebhookService instance.
    """
    return get_webhook_service()


async def get_actions_service_dep() -> ActionsService:
    """Provide an ActionsService instance for dependency injection.

    Returns:
        A configured ActionsService instance.
    """
    return get_actions_service()
