"""Application configuration module.

Loads application settings from a YAML file and applies environment variable
overrides. Provides a singleton accessor suitable for use as a FastAPI
dependency.

Environment override convention:
    Each settings key maps to ``DISPATCH_<UPPER_SNAKE_CASE_KEY>``.
    For example, ``log_level`` is overridden by ``DISPATCH_LOG_LEVEL``.
"""

from __future__ import annotations

import functools
import os
from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel, ValidationError

# ---------------------------------------------------------------------------
# Project root — two levels above this file: app/src/config.py → project root
# ---------------------------------------------------------------------------
_PROJECT_ROOT: Path = Path(__file__).resolve().parent.parent.parent
_DEFAULT_CONFIG_PATH: Path = _PROJECT_ROOT / "app" / "config" / "settings.yaml"

# Env var prefix used for all overrides.
_ENV_PREFIX: str = "DISPATCH_"


class Settings(BaseModel):
    """Application settings loaded from ``app/config/settings.yaml``.

    All fields correspond to keys in the YAML configuration file. Each field
    can be overridden at runtime via an ``DISPATCH_<UPPER_SNAKE_CASE_KEY>``
    environment variable.

    Attributes:
        app_env: Deployment environment identifier ("local" or "production").
        log_level: Python logging level string (DEBUG, INFO, WARNING, ERROR,
            CRITICAL).
        api_host: Network interface the uvicorn server binds to.
        api_port: TCP port the uvicorn server listens on.
        default_model: Default Claude model identifier for agent sessions.
        default_timeout_minutes: Default agent session timeout in minutes.
        max_timeout_minutes: Maximum allowed agent session timeout in minutes.
        default_callback_url: Optional fallback webhook URL for run results.
        dynamodb_table_name: DynamoDB table name for run record persistence.
        dynamodb_endpoint_url: Optional custom DynamoDB endpoint (for local dev).
        dynamodb_ttl_days: Days before DynamoDB run records are auto-deleted.
        github_owner: GitHub account or organisation owning the target repo.
        github_repo: Target GitHub repository name.
        github_workflow_id: Workflow dispatch filename in the target repo.
        security_mode: Security enforcement mode ("advisory" or "strict").
    """

    # Runtime environment
    app_env: str = "local"
    log_level: str = "INFO"

    # API server
    api_host: str = "0.0.0.0"
    api_port: int = 8000

    # Agent settings
    default_model: str = "claude-sonnet-4-20250514"
    default_timeout_minutes: int = 30
    max_timeout_minutes: int = 60
    default_callback_url: str | None = None

    # DynamoDB
    dynamodb_table_name: str = "dispatch-runs"
    dynamodb_endpoint_url: str | None = None
    dynamodb_ttl_days: int = 90

    # GitHub
    github_owner: str | None = None
    github_repo: str = "copilot-dispatch"
    github_workflow_id: str = "agent-executor.yml"

    # Service URL
    service_url: str | None = None

    # Security
    security_mode: str = "advisory"


def _apply_env_overrides(data: dict[str, Any]) -> dict[str, Any]:
    """Apply environment variable overrides to a settings dictionary.

    Iterates over every key present in ``data`` and checks for a matching
    environment variable named ``DISPATCH_<UPPER_SNAKE_CASE_KEY>``. If found,
    the environment variable value replaces the value from the YAML file.

    Type coercion is performed for integer fields: if the existing value in
    ``data`` is an ``int``, the env var string is cast to ``int`` before
    insertion. For ``None``-valued fields (nullable strings), the env var value
    is stored as a string (Pydantic validates further). The special string
    value ``"null"`` maps back to ``None`` for nullable fields.

    Args:
        data: Mutable settings dictionary parsed from the YAML config file.

    Returns:
        The same dictionary with any env-var-specified values applied in place.

    Raises:
        ValueError: If an environment variable intended for an integer field
            cannot be converted to ``int``.
    """
    for key, current_value in list(data.items()):
        env_var = _ENV_PREFIX + key.upper()
        env_value = os.environ.get(env_var)
        if env_value is None:
            continue

        # Map the literal string "null" back to Python None for nullable fields.
        if env_value.lower() == "null":
            data[key] = None
            continue

        # Preserve the type of the original YAML value where possible.
        if isinstance(current_value, int):
            try:
                data[key] = int(env_value)
            except ValueError as exc:
                raise ValueError(
                    f"Environment variable {env_var}={env_value!r} cannot be "
                    f"converted to int for settings key '{key}'."
                ) from exc
        else:
            data[key] = env_value

    return data


def load_settings(config_path: Path | None = None) -> Settings:
    """Load application settings from a YAML file with environment overrides.

    Resolution order (highest priority last — last wins):
    1. Built-in field defaults defined on the ``Settings`` class.
    2. Values from the YAML configuration file.
    3. Environment variables (``DISPATCH_<UPPER_SNAKE_CASE_KEY>``).

    Args:
        config_path: Absolute or relative path to the YAML settings file. If
            ``None``, defaults to ``app/config/settings.yaml`` relative to the
            project root (two levels above this module).

    Returns:
        A fully-validated :class:`Settings` instance.

    Raises:
        FileNotFoundError: If the YAML config file does not exist at the
            resolved path.
        ValueError: If the YAML file contains invalid syntax or if an
            environment variable cannot be coerced to the expected type.
        pydantic.ValidationError: If the merged settings data fails Pydantic
            field validation (e.g., wrong type that cannot be coerced).
    """
    resolved_path: Path = (
        config_path if config_path is not None else _DEFAULT_CONFIG_PATH
    )

    if not resolved_path.exists():
        raise FileNotFoundError(
            f"Settings file not found at '{resolved_path}'. "
            f"Ensure 'app/config/settings.yaml' exists at the project root "
            f"or pass an explicit config_path to load_settings()."
        )

    try:
        raw_yaml = resolved_path.read_text(encoding="utf-8")
        data: Any = yaml.safe_load(raw_yaml)
    except yaml.YAMLError as exc:
        raise ValueError(
            f"Failed to parse YAML settings file '{resolved_path}': {exc}"
        ) from exc

    if not isinstance(data, dict):
        raise ValueError(
            f"Settings file '{resolved_path}' must contain a YAML mapping at "
            f"the top level, got {type(data).__name__}."
        )

    # Apply environment variable overrides before Pydantic validation so that
    # Pydantic performs type coercion on the final merged values.
    data = _apply_env_overrides(data)

    # Pydantic will raise ValidationError if any field value is invalid.
    return Settings(**data)


@functools.lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Return the application :class:`Settings` singleton.

    Loads settings on first call and caches the result for the lifetime of the
    process. This function is safe to use as a FastAPI dependency via
    ``Depends(get_settings)``.

    Subsequent calls return the cached instance without re-reading the file or
    re-evaluating environment variables. To force a reload (e.g., in tests),
    call ``get_settings.cache_clear()`` before invoking ``get_settings()``
    again.

    Returns:
        The cached :class:`Settings` singleton.

    Raises:
        FileNotFoundError: Propagated from :func:`load_settings` if the config
            file is missing on first call.
        ValueError: Propagated from :func:`load_settings` if the YAML is
            malformed on first call.
    """
    return load_settings()
