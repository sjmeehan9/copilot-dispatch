"""FastAPI middleware for the Copilot Dispatch API.

Provides request/response logging and other cross-cutting concerns.
"""

from __future__ import annotations

import logging
import time
from collections.abc import Callable

from fastapi import Request, Response
from starlette.middleware.base import BaseHTTPMiddleware


class RequestLoggingMiddleware(BaseHTTPMiddleware):
    """Middleware to log HTTP requests and responses.

    Logs the start of the request and the completion with status code and duration.
    Does not log request or response bodies to avoid leaking sensitive information.
    """

    def __init__(self, app):
        """Initialize the middleware.

        Args:
            app: The ASGI application.
        """
        super().__init__(app)
        self.logger = logging.getLogger("dispatch.api.request")

    async def dispatch(self, request: Request, call_next: Callable) -> Response:
        """Process the request and log details.

        Args:
            request: The incoming HTTP request.
            call_next: The next middleware or route handler.

        Returns:
            The HTTP response.
        """
        start_time = time.time()
        client_ip = request.headers.get(
            "X-Forwarded-For", request.client.host if request.client else "unknown"
        )

        self.logger.info(
            "Request started",
            extra={
                "method": request.method,
                "path": request.url.path,
                "client_ip": client_ip,
            },
        )

        try:
            response = await call_next(request)
            duration_ms = (time.time() - start_time) * 1000

            log_level = logging.INFO
            if response.status_code >= 500:
                log_level = logging.ERROR
            elif response.status_code >= 400:
                log_level = logging.WARNING

            self.logger.log(
                log_level,
                "Request completed",
                extra={
                    "method": request.method,
                    "path": request.url.path,
                    "status_code": response.status_code,
                    "duration_ms": round(duration_ms, 2),
                },
            )
            return response
        except Exception as e:
            duration_ms = (time.time() - start_time) * 1000
            self.logger.error(
                "Request failed with exception",
                exc_info=True,
                extra={
                    "method": request.method,
                    "path": request.url.path,
                    "duration_ms": round(duration_ms, 2),
                },
            )
            raise e
