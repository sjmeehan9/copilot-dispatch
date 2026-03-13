"""Unit tests for app.src.config.

Tests cover:
- Valid YAML loading producing a correct Settings object.
- Environment variable overrides for string and integer fields.
- Missing YAML file raises FileNotFoundError with a helpful message.
- Malformed YAML raises ValueError.
- Invalid field type raises pydantic.ValidationError.
- get_settings() cache behaviour.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest
import yaml
from pydantic import ValidationError

from app.src.config import Settings, get_settings, load_settings

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _write_yaml(path: Path, data: dict) -> Path:
    """Write a dict as YAML to a file and return the Path."""
    path.write_text(yaml.dump(data), encoding="utf-8")
    return path


# ---------------------------------------------------------------------------
# Minimal valid YAML fixture
# ---------------------------------------------------------------------------

MINIMAL_VALID_DATA: dict = {
    "app_env": "local",
    "log_level": "INFO",
    "api_host": "0.0.0.0",
    "api_port": 8000,
    "default_model": "claude-sonnet-4-20250514",
    "default_timeout_minutes": 30,
    "max_timeout_minutes": 60,
    "default_callback_url": None,
    "dynamodb_table_name": "dispatch-runs",
    "dynamodb_endpoint_url": None,
    "dynamodb_ttl_days": 90,
    "github_owner": None,
    "github_repo": "copilot-dispatch",
    "github_workflow_id": "agent-executor.yml",
    "service_url": None,
    "security_mode": "advisory",
}


# ---------------------------------------------------------------------------
# Test 1 — Valid YAML loads correctly
# ---------------------------------------------------------------------------


def test_load_settings_valid_yaml(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """load_settings() returns a Settings object with correct values from YAML."""
    # Clear any DISPATCH_ env vars that could leak into the test.
    for key in list(os.environ):
        if key.startswith("DISPATCH_"):
            monkeypatch.delenv(key)
    config_file = _write_yaml(tmp_path / "settings.yaml", MINIMAL_VALID_DATA)
    settings = load_settings(config_file)

    assert isinstance(settings, Settings)
    assert settings.app_env == "local"
    assert settings.log_level == "INFO"
    assert settings.api_host == "0.0.0.0"
    assert settings.api_port == 8000
    assert settings.default_model == "claude-sonnet-4-20250514"
    assert settings.default_timeout_minutes == 30
    assert settings.max_timeout_minutes == 60
    assert settings.default_callback_url is None
    assert settings.dynamodb_table_name == "dispatch-runs"
    assert settings.dynamodb_endpoint_url is None
    assert settings.dynamodb_ttl_days == 90
    assert settings.github_owner is None
    assert settings.github_repo == "copilot-dispatch"
    assert settings.github_workflow_id == "agent-executor.yml"
    assert settings.service_url is None
    assert settings.security_mode == "advisory"


def test_load_settings_partial_yaml(tmp_path: Path) -> None:
    """load_settings() uses field defaults for keys absent from YAML."""
    # Only supply the minimum non-default fields; everything else should
    # fall back to Settings field defaults.
    config_file = _write_yaml(tmp_path / "settings.yaml", {"log_level": "WARNING"})
    settings = load_settings(config_file)

    assert settings.log_level == "WARNING"
    # Defaults kick in for omitted keys
    assert settings.app_env == "local"
    assert settings.api_port == 8000
    assert settings.dynamodb_table_name == "dispatch-runs"


# ---------------------------------------------------------------------------
# Test 2 — Environment variable overrides
# ---------------------------------------------------------------------------


def test_env_var_overrides_string_field(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """DISPATCH_LOG_LEVEL env var overrides log_level from YAML."""
    monkeypatch.setenv("DISPATCH_LOG_LEVEL", "DEBUG")
    config_file = _write_yaml(tmp_path / "settings.yaml", MINIMAL_VALID_DATA)
    settings = load_settings(config_file)

    assert settings.log_level == "DEBUG"


def test_env_var_overrides_integer_field(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """DISPATCH_API_PORT env var overrides api_port from YAML."""
    monkeypatch.setenv("DISPATCH_API_PORT", "9000")
    config_file = _write_yaml(tmp_path / "settings.yaml", MINIMAL_VALID_DATA)
    settings = load_settings(config_file)

    assert settings.api_port == 9000


def test_env_var_overrides_null_to_value(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """DISPATCH_GITHUB_OWNER env var sets a nullable field to a string value."""
    monkeypatch.setenv("DISPATCH_GITHUB_OWNER", "myorg")
    config_file = _write_yaml(tmp_path / "settings.yaml", MINIMAL_VALID_DATA)
    settings = load_settings(config_file)

    assert settings.github_owner == "myorg"


def test_env_var_null_string_sets_none(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """DISPATCH_GITHUB_OWNER=null env var resets a nullable field to None."""
    monkeypatch.setenv("DISPATCH_GITHUB_OWNER", "null")
    data = {**MINIMAL_VALID_DATA, "github_owner": "some-org"}
    config_file = _write_yaml(tmp_path / "settings.yaml", data)
    settings = load_settings(config_file)

    assert settings.github_owner is None


def test_env_var_invalid_int_raises_value_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """DISPATCH_API_PORT with a non-integer value raises ValueError."""
    monkeypatch.setenv("DISPATCH_API_PORT", "not-a-number")
    config_file = _write_yaml(tmp_path / "settings.yaml", MINIMAL_VALID_DATA)

    with pytest.raises(ValueError, match="DISPATCH_API_PORT"):
        load_settings(config_file)


# ---------------------------------------------------------------------------
# Test 3 — Missing YAML file raises FileNotFoundError
# ---------------------------------------------------------------------------


def test_load_settings_missing_file_raises(tmp_path: Path) -> None:
    """load_settings() raises FileNotFoundError when the YAML file is absent."""
    missing = tmp_path / "nonexistent.yaml"
    with pytest.raises(FileNotFoundError) as exc_info:
        load_settings(missing)

    # Error message should be helpful — include the path
    assert str(missing) in str(exc_info.value)


# ---------------------------------------------------------------------------
# Test 4 — Malformed YAML raises ValueError
# ---------------------------------------------------------------------------


def test_load_settings_malformed_yaml_raises(tmp_path: Path) -> None:
    """load_settings() raises ValueError when the YAML is syntactically invalid."""
    bad_yaml = tmp_path / "settings.yaml"
    bad_yaml.write_text(
        "log_level: INFO\n  bad_indent: [unclosed",
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="Failed to parse YAML"):
        load_settings(bad_yaml)


def test_load_settings_non_mapping_yaml_raises(tmp_path: Path) -> None:
    """load_settings() raises ValueError when the YAML root is not a mapping."""
    list_yaml = tmp_path / "settings.yaml"
    list_yaml.write_text("- item1\n- item2\n", encoding="utf-8")

    with pytest.raises(ValueError, match="mapping"):
        load_settings(list_yaml)


# ---------------------------------------------------------------------------
# Test 5 — Pydantic ValidationError on invalid field type
# ---------------------------------------------------------------------------


def test_load_settings_invalid_field_type_raises(tmp_path: Path) -> None:
    """load_settings() raises ValidationError when a field value has an invalid type."""
    bad_data = {**MINIMAL_VALID_DATA, "api_port": "not-an-integer-and-not-coercible"}
    config_file = _write_yaml(tmp_path / "settings.yaml", bad_data)

    # Pydantic v2 cannot coerce an arbitrary string to int, so this should fail.
    with pytest.raises((ValidationError, ValueError)):
        load_settings(config_file)


# ---------------------------------------------------------------------------
# Test 6 — get_settings() cache behaviour
# ---------------------------------------------------------------------------


def test_get_settings_returns_settings_instance() -> None:
    """get_settings() returns a Settings instance (default config path)."""
    get_settings.cache_clear()
    settings = get_settings()
    assert isinstance(settings, Settings)
    get_settings.cache_clear()


def test_get_settings_is_cached() -> None:
    """get_settings() returns the same object on repeated calls."""
    get_settings.cache_clear()
    first = get_settings()
    second = get_settings()
    assert first is second
    get_settings.cache_clear()


# ---------------------------------------------------------------------------
# Test 7 — Settings field defaults are correct
# ---------------------------------------------------------------------------


def test_settings_defaults() -> None:
    """Settings constructed with no arguments has the expected defaults."""
    s = Settings()
    assert s.app_env == "local"
    assert s.log_level == "INFO"
    assert s.api_host == "0.0.0.0"
    assert s.api_port == 8000
    assert s.default_model == "claude-sonnet-4-20250514"
    assert s.default_timeout_minutes == 30
    assert s.max_timeout_minutes == 60
    assert s.default_callback_url is None
    assert s.dynamodb_table_name == "dispatch-runs"
    assert s.dynamodb_endpoint_url is None
    assert s.dynamodb_ttl_days == 90
    assert s.github_owner is None
    assert s.github_repo == "copilot-dispatch"
    assert s.github_workflow_id == "agent-executor.yml"
    assert s.service_url is None
    assert s.security_mode == "advisory"
