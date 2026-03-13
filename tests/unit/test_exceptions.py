"""Unit tests for domain exceptions and exception handlers."""

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.src.exceptions import (
    AuthenticationError,
    AppError,
    ConflictError,
    DispatchError,
    ExternalServiceError,
    PayloadValidationError,
    RunNotFoundError,
    register_exception_handlers,
)


def test_exception_attributes():
    """Test that custom exceptions have the correct attributes."""
    exc = PayloadValidationError("Invalid payload", {"field": "error"})
    assert exc.error_code == "VALIDATION_ERROR"
    assert exc.status_code == 400
    assert exc.error_message == "Invalid payload"
    assert exc.error_details == {"field": "error"}

    exc = AuthenticationError("Invalid token")
    assert exc.error_code == "AUTHENTICATION_ERROR"
    assert exc.status_code == 401

    exc = RunNotFoundError("Run not found")
    assert exc.error_code == "RUN_NOT_FOUND"
    assert exc.status_code == 404

    exc = ConflictError("Conflict detected")
    assert exc.error_code == "CONFLICT"
    assert exc.status_code == 409

    exc = DispatchError("Failed to dispatch")
    assert exc.error_code == "DISPATCH_ERROR"
    assert exc.status_code == 500

    exc = ExternalServiceError("GitHub API down")
    assert exc.error_code == "EXTERNAL_SERVICE_ERROR"
    assert exc.status_code == 502


def test_exception_handlers():
    """Test that exception handlers return the correct JSON responses."""
    app = FastAPI()
    register_exception_handlers(app)

    @app.get("/test-app-error")
    def route_app_error():
        raise RunNotFoundError("Run 123 not found", {"run_id": "123"})

    @app.get("/test-generic-error")
    def route_generic_error():
        raise RuntimeError("Something went wrong")

    client = TestClient(app, raise_server_exceptions=False)

    # Test AppError handler
    response = client.get("/test-app-error")
    assert response.status_code == 404
    data = response.json()
    assert data["error_code"] == "RUN_NOT_FOUND"
    assert data["error_message"] == "Run 123 not found"
    assert data["error_details"] == {"run_id": "123"}

    # Test generic Exception handler
    response = client.get("/test-generic-error")
    assert response.status_code == 500
    data = response.json()
    assert data["error_code"] == "INTERNAL_ERROR"
    assert data["error_message"] == "An unexpected internal server error occurred."
    assert "error_details" not in data


def test_validation_exception_handler():
    """Test that RequestValidationError is handled correctly."""
    from pydantic import BaseModel

    app = FastAPI()
    register_exception_handlers(app)

    class Item(BaseModel):
        name: str
        price: float

    @app.post("/items/")
    def create_item(item: Item):
        return item

    client = TestClient(app)

    # Send invalid payload to trigger RequestValidationError
    response = client.post("/items/", json={"name": "Test"})  # missing price
    assert response.status_code == 422
    data = response.json()
    assert data["error_code"] == "VALIDATION_ERROR"
    assert data["error_message"] == "Request payload validation failed."
    assert "error_details" in data
    assert "errors" in data["error_details"]
