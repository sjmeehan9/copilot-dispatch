"""Domain exceptions and FastAPI exception handlers for the Copilot Dispatch API.

Defines custom exception classes for domain-specific errors and provides
a function to register exception handlers with a FastAPI application.
"""

from __future__ import annotations

import logging
from typing import Any

from fastapi import FastAPI, Request
from fastapi.encoders import jsonable_encoder
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse

from app.src.api.models import ErrorResponse

logger = logging.getLogger(__name__)


class AppError(Exception):
    """Base exception class for all Copilot Dispatch domain errors."""

    def __init__(
        self,
        error_message: str,
        error_code: str = "INTERNAL_ERROR",
        status_code: int = 500,
        error_details: dict[str, Any] | None = None,
    ) -> None:
        """Initialise the exception.

        Args:
            error_message: A human-readable error message.
            error_code: A machine-readable error code.
            status_code: The HTTP status code to return.
            error_details: Additional context or details about the error.
        """
        super().__init__(error_message)
        self.error_message = error_message
        self.error_code = error_code
        self.status_code = status_code
        self.error_details = error_details


class PayloadValidationError(AppError):
    """Exception raised when request payload validation fails."""

    def __init__(
        self, error_message: str, error_details: dict[str, Any] | None = None
    ) -> None:
        """Initialise the exception."""
        super().__init__(
            error_message=error_message,
            error_code="VALIDATION_ERROR",
            status_code=400,
            error_details=error_details,
        )


class AuthenticationError(AppError):
    """Exception raised when authentication fails."""

    def __init__(
        self, error_message: str, error_details: dict[str, Any] | None = None
    ) -> None:
        """Initialise the exception."""
        super().__init__(
            error_message=error_message,
            error_code="AUTHENTICATION_ERROR",
            status_code=401,
            error_details=error_details,
        )


class RunNotFoundError(AppError):
    """Exception raised when an agent run cannot be found."""

    def __init__(
        self, error_message: str, error_details: dict[str, Any] | None = None
    ) -> None:
        """Initialise the exception."""
        super().__init__(
            error_message=error_message,
            error_code="RUN_NOT_FOUND",
            status_code=404,
            error_details=error_details,
        )


class ConflictError(AppError):
    """Exception raised when there is a conflict with the current state."""

    def __init__(
        self, error_message: str, error_details: dict[str, Any] | None = None
    ) -> None:
        """Initialise the exception."""
        super().__init__(
            error_message=error_message,
            error_code="CONFLICT",
            status_code=409,
            error_details=error_details,
        )


class DispatchError(AppError):
    """Exception raised when dispatching a workflow fails."""

    def __init__(
        self, error_message: str, error_details: dict[str, Any] | None = None
    ) -> None:
        """Initialise the exception."""
        super().__init__(
            error_message=error_message,
            error_code="DISPATCH_ERROR",
            status_code=500,
            error_details=error_details,
        )


class ExternalServiceError(AppError):
    """Exception raised when an external service call fails."""

    def __init__(
        self, error_message: str, error_details: dict[str, Any] | None = None
    ) -> None:
        """Initialise the exception."""
        super().__init__(
            error_message=error_message,
            error_code="EXTERNAL_SERVICE_ERROR",
            status_code=502,
            error_details=error_details,
        )


class ActionsNotFoundError(AppError):
    """Exception raised when a GitHub Actions resource is not found (404)."""

    def __init__(
        self, error_message: str, error_details: dict[str, Any] | None = None
    ) -> None:
        """Initialise the exception."""
        super().__init__(
            error_message=error_message,
            error_code="ACTIONS_NOT_FOUND",
            status_code=404,
            error_details=error_details,
        )


class ActionsPermissionError(AppError):
    """Exception raised when GitHub denies access to an Actions resource (403)."""

    def __init__(
        self, error_message: str, error_details: dict[str, Any] | None = None
    ) -> None:
        """Initialise the exception."""
        super().__init__(
            error_message=error_message,
            error_code="ACTIONS_PERMISSION_DENIED",
            status_code=403,
            error_details=error_details,
        )


class ActionsValidationError(AppError):
    """Exception raised when GitHub rejects an Actions request payload (422)."""

    def __init__(
        self, error_message: str, error_details: dict[str, Any] | None = None
    ) -> None:
        """Initialise the exception."""
        super().__init__(
            error_message=error_message,
            error_code="ACTIONS_VALIDATION_ERROR",
            status_code=422,
            error_details=error_details,
        )


class ActionsGitHubError(AppError):
    """Exception raised when the GitHub API returns a server error (5xx)."""

    def __init__(
        self, error_message: str, error_details: dict[str, Any] | None = None
    ) -> None:
        """Initialise the exception."""
        super().__init__(
            error_message=error_message,
            error_code="ACTIONS_GITHUB_ERROR",
            status_code=502,
            error_details=error_details,
        )


def register_exception_handlers(app: FastAPI) -> None:
    """Register custom exception handlers with the FastAPI application.

    Args:
        app: The FastAPI application instance.
    """

    @app.exception_handler(AppError)
    async def app_error_handler(request: Request, exc: AppError) -> JSONResponse:
        """Handle domain-specific Copilot Dispatch errors."""
        error_response = ErrorResponse(
            error_code=exc.error_code,
            error_message=exc.error_message,
            error_details=exc.error_details,
        )
        return JSONResponse(
            status_code=exc.status_code,
            content=error_response.model_dump(exclude_none=True),
        )

    @app.exception_handler(RequestValidationError)
    async def validation_exception_handler(
        request: Request, exc: RequestValidationError
    ) -> JSONResponse:
        """Handle FastAPI request validation errors."""
        serialised_errors = jsonable_encoder(exc.errors())
        error_response = ErrorResponse(
            error_code="VALIDATION_ERROR",
            error_message="Request payload validation failed.",
            error_details={"errors": serialised_errors},
        )
        return JSONResponse(
            status_code=422,
            content=error_response.model_dump(exclude_none=True),
        )

    @app.exception_handler(Exception)
    async def generic_exception_handler(
        request: Request, exc: Exception
    ) -> JSONResponse:
        """Handle all other unhandled exceptions."""
        logger.exception("Unhandled exception occurred: %s", str(exc))
        error_response = ErrorResponse(
            error_code="INTERNAL_ERROR",
            error_message="An unexpected internal server error occurred.",
        )
        return JSONResponse(
            status_code=500,
            content=error_response.model_dump(exclude_none=True),
        )
