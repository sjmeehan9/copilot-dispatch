"""E2E test configuration and shared fixtures.

Provides session-scoped fixtures for E2E test configuration, an authenticated
HTTP client, and an interactive confirmation gate.  All E2E tests require the
``--e2e-confirm`` pytest CLI flag and will auto-skip in CI or when the flag is
omitted.

Environment variables (read at session start)::

    DISPATCH_E2E_API_URL      — Production API base URL (required)
    DISPATCH_E2E_API_KEY      — API key for authentication (required)
    DISPATCH_E2E_REPOSITORY   — Target repo (default: sjmeehan9/autopilot-test-target)
    DISPATCH_E2E_BRANCH       — Base branch (default: main)
    DISPATCH_E2E_MODEL        — Model identifier (default: gpt-5)
    DISPATCH_E2E_TIMEOUT      — Polling timeout in seconds (default: 1800)
"""

from __future__ import annotations

import os

import pytest

from tests.e2e.helpers import E2EClient

# ---------------------------------------------------------------------------
# Defaults
# ---------------------------------------------------------------------------

_DEFAULT_REPOSITORY = "sjmeehan9/autopilot-test-target"
_DEFAULT_BRANCH = "main"
_DEFAULT_MODEL = "gpt-5"
_DEFAULT_TIMEOUT = 1800  # 30 minutes


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(scope="session")
def e2e_config(request: pytest.FixtureRequest) -> dict[str, str | int]:
    """Load E2E configuration from environment variables.

    When ``--e2e-confirm`` is active and a required variable is missing, the
    user is prompted interactively.  Without the flag, missing variables cause
    a skip (although the test should already be skipped by the collection hook
    in the root ``conftest.py``).

    Returns:
        A dictionary with keys: ``api_url``, ``api_key``, ``repository``,
        ``branch``, ``model``, ``timeout``.
    """
    is_confirmed = request.config.getoption("--e2e-confirm", default=False)

    api_url = os.environ.get("DISPATCH_E2E_API_URL", "")
    api_key = os.environ.get("DISPATCH_E2E_API_KEY", "")
    repository = os.environ.get("DISPATCH_E2E_REPOSITORY", _DEFAULT_REPOSITORY)
    branch = os.environ.get("DISPATCH_E2E_BRANCH", _DEFAULT_BRANCH)
    model = os.environ.get("DISPATCH_E2E_MODEL", _DEFAULT_MODEL)
    timeout = int(os.environ.get("DISPATCH_E2E_TIMEOUT", str(_DEFAULT_TIMEOUT)))

    if is_confirmed:
        if not api_url:
            api_url = input("DISPATCH_E2E_API_URL (production URL): ").strip()
        if not api_key:
            api_key = input("DISPATCH_E2E_API_KEY (API key): ").strip()

    if not api_url or not api_key:
        pytest.skip(
            "E2E config incomplete — set DISPATCH_E2E_API_URL and DISPATCH_E2E_API_KEY"
        )

    # Strip trailing slash for consistency
    api_url = api_url.rstrip("/")

    return {
        "api_url": api_url,
        "api_key": api_key,
        "repository": repository,
        "branch": branch,
        "model": model,
        "timeout": timeout,
    }


@pytest.fixture(scope="session")
def e2e_client(e2e_config: dict[str, str | int]) -> E2EClient:
    """Provide a session-scoped E2E HTTP client.

    The client is initialised with the production API URL and API key from
    ``e2e_config``.  It is closed automatically at session teardown.

    Args:
        e2e_config: The E2E configuration dictionary.

    Returns:
        A configured ``E2EClient`` instance.
    """
    client = E2EClient(
        base_url=str(e2e_config["api_url"]),
        api_key=str(e2e_config["api_key"]),
        timeout=int(e2e_config["timeout"]),
    )
    yield client  # type: ignore[misc]
    client.close()


@pytest.fixture(scope="session")
def confirm_e2e_execution(
    request: pytest.FixtureRequest,
    e2e_config: dict[str, str | int],
) -> None:
    """Gate E2E execution behind interactive confirmation.

    When ``--e2e-confirm`` is passed, prompts the user once to confirm they
    want to run live tests against the production stack.  If the user
    declines, all E2E tests in the session are skipped.

    Args:
        request: Pytest fixture request (for CLI option access).
        e2e_config: The E2E configuration dictionary (for display).
    """
    if not request.config.getoption("--e2e-confirm", default=False):
        pytest.skip("E2E tests require --e2e-confirm")

    print("\n" + "=" * 60)
    print("  E2E VALIDATION — LIVE PRODUCTION TESTS")
    print("=" * 60)
    print(f"  API URL:    {e2e_config['api_url']}")
    print(f"  Repository: {e2e_config['repository']}")
    print(f"  Branch:     {e2e_config['branch']}")
    print(f"  Model:      {e2e_config['model']}")
    print(f"  Timeout:    {e2e_config['timeout']}s")
    print("=" * 60)

    answer = input(
        "\nThis will execute agent runs against the live production stack "
        "and the test repository.\nContinue? [y/N] "
    ).strip()

    if answer.lower() not in ("y", "yes"):
        pytest.skip("User declined E2E execution")
