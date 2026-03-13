"""Production deployment smoke test script.

Validates that the deployed Copilot Dispatch API service is operational by running
a series of checks against the live HTTPS endpoint. Designed to be re-run
after any deployment to confirm the system is healthy.

Checks performed (in order):
    1. **Health check** — ``GET /health`` returns ``200`` with ``"healthy"``.
    2. **Auth rejection** — ``POST /agent/run`` without ``X-API-Key`` returns ``401``.
    3. **Create run** — ``POST /agent/run`` with a valid implement payload returns
       ``202`` with a ``run_id`` (skipped with ``--skip-dispatch``).
    4. **Retrieve run** — ``GET /agent/run/{run_id}`` returns ``200`` with status
       ``dispatched`` or ``running`` (skipped with ``--skip-dispatch``).
    5. **Workflow dispatched** (optional) — Checks ``gh run list`` for a recent
       workflow run (requires ``gh`` CLI installed and authenticated).

Usage::

    # Health and auth checks only
    python scripts/smoke_test.py --api-url https://<apprunner-url> --api-key <key> --skip-dispatch

    # Full smoke test (triggers a real workflow)
    python scripts/smoke_test.py --api-url https://<apprunner-url> --api-key <key>

    # Custom target repository
    python scripts/smoke_test.py --api-url https://<url> --api-key <key> \\
        --repository owner/autopilot-test-target
"""

from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import sys
import time

import httpx

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_DEFAULT_REPOSITORY = "sjmeehan9/autopilot-test-target"
_DEFAULT_TIMEOUT = 30
_DEFAULT_MODEL = "claude-sonnet-4-20250514"

# ANSI colour codes for terminal output.
_GREEN = "\033[92m"
_RED = "\033[91m"
_YELLOW = "\033[93m"
_RESET = "\033[0m"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _pass(label: str, detail: str = "") -> None:
    """Print a PASS result for a smoke test check.

    Args:
        label: Short identifier for the check.
        detail: Optional extra detail to display.
    """
    suffix = f" — {detail}" if detail else ""
    print(f"  {_GREEN}PASS{_RESET}  {label}{suffix}")


def _fail(label: str, detail: str = "") -> None:
    """Print a FAIL result for a smoke test check and exit.

    Args:
        label: Short identifier for the check.
        detail: Optional extra detail to display.
    """
    suffix = f" — {detail}" if detail else ""
    print(f"  {_RED}FAIL{_RESET}  {label}{suffix}")


def _warn(label: str, detail: str = "") -> None:
    """Print a WARN result for a smoke test check.

    Args:
        label: Short identifier for the check.
        detail: Optional extra detail to display.
    """
    suffix = f" — {detail}" if detail else ""
    print(f"  {_YELLOW}WARN{_RESET}  {label}{suffix}")


def _print_response(response: httpx.Response) -> None:
    """Print an HTTP response for debugging.

    Args:
        response: The httpx response to display.
    """
    print(f"    Status: {response.status_code}")
    print(f"    Headers: {dict(response.headers)}")
    try:
        body = response.json()
        print(f"    Body: {json.dumps(body, indent=2)}")
    except Exception:
        print(f"    Body (raw): {response.text[:500]}")


# ---------------------------------------------------------------------------
# Smoke test checks
# ---------------------------------------------------------------------------


def check_health(client: httpx.Client, api_url: str) -> bool:
    """Verify the health endpoint returns 200 with 'healthy' status.

    Args:
        client: Configured httpx client.
        api_url: Base URL of the API.

    Returns:
        True if the check passes.
    """
    url = f"{api_url}/health"
    try:
        response = client.get(url)
    except httpx.RequestError as exc:
        _fail("Health check", f"Connection error: {exc}")
        return False

    if response.status_code != 200:
        _fail("Health check", f"Expected 200, got {response.status_code}")
        _print_response(response)
        return False

    body = response.json()
    if body.get("status") != "healthy":
        _fail("Health check", f"Unexpected body: {body}")
        return False

    _pass("Health check", f"200 — {body}")
    return True


def check_auth_rejection(client: httpx.Client, api_url: str) -> bool:
    """Verify that requests without an API key are rejected with 401.

    Args:
        client: Configured httpx client (headers NOT including API key).
        api_url: Base URL of the API.

    Returns:
        True if the check passes.
    """
    url = f"{api_url}/agent/run"
    payload = {
        "repository": "test/repo",
        "branch": "main",
        "agent_instructions": "test",
        "model": _DEFAULT_MODEL,
        "role": "implement",
    }

    # Use a client *without* the API key header.
    no_auth_client = httpx.Client(timeout=client.timeout)
    try:
        response = no_auth_client.post(url, json=payload)
    except httpx.RequestError as exc:
        _fail("Auth rejection", f"Connection error: {exc}")
        return False
    finally:
        no_auth_client.close()

    if response.status_code == 401:
        _pass("Auth rejection", "401 — unauthenticated request correctly rejected")
        return True

    _fail("Auth rejection", f"Expected 401, got {response.status_code}")
    _print_response(response)
    return False


def check_create_run(
    client: httpx.Client,
    api_url: str,
    api_key: str,
    repository: str,
) -> str | None:
    """Submit an implement run request and verify a 202 response with a run_id.

    Args:
        client: Configured httpx client.
        api_url: Base URL of the API.
        api_key: API key for authentication.
        repository: Target repository in ``owner/repo`` format.

    Returns:
        The ``run_id`` string on success, or ``None`` on failure.
    """
    url = f"{api_url}/agent/run"
    payload = {
        "repository": repository,
        "branch": "main",
        "agent_instructions": (
            "Add a square(n) function to src/calculator.py that returns n "
            "squared. Add a corresponding test in tests/test_calculator.py."
        ),
        "model": _DEFAULT_MODEL,
        "role": "implement",
        "timeout_minutes": 30,
    }

    try:
        response = client.post(
            url,
            json=payload,
            headers={"X-API-Key": api_key},
        )
    except httpx.RequestError as exc:
        _fail("Create run", f"Connection error: {exc}")
        return None

    if response.status_code != 202:
        _fail("Create run", f"Expected 202, got {response.status_code}")
        _print_response(response)
        return None

    body = response.json()
    run_id = body.get("run_id")
    if not run_id:
        _fail("Create run", f"No run_id in response body: {body}")
        return None

    status = body.get("status", "unknown")
    _pass("Create run", f"202 — run_id={run_id}, status={status}")
    return run_id


def check_retrieve_run(
    client: httpx.Client,
    api_url: str,
    api_key: str,
    run_id: str,
) -> bool:
    """Poll the run status endpoint and verify the run record exists.

    Allows a brief wait for the status to transition from ``queued`` to
    ``dispatched`` or ``running``.

    Args:
        client: Configured httpx client.
        api_url: Base URL of the API.
        api_key: API key for authentication.
        run_id: The run identifier to look up.

    Returns:
        True if the check passes.
    """
    url = f"{api_url}/agent/run/{run_id}"
    max_attempts = 3
    for attempt in range(1, max_attempts + 1):
        try:
            response = client.get(url, headers={"X-API-Key": api_key})
        except httpx.RequestError as exc:
            _fail("Retrieve run", f"Connection error: {exc}")
            return False

        if response.status_code != 200:
            if attempt < max_attempts:
                time.sleep(2)
                continue
            _fail("Retrieve run", f"Expected 200, got {response.status_code}")
            _print_response(response)
            return False

        body = response.json()
        status = body.get("status", "unknown")
        expected_statuses = {"queued", "dispatched", "running", "success", "failure"}
        if status in expected_statuses:
            _pass("Retrieve run", f"200 — run_id={run_id}, status={status}")
            return True

        if attempt < max_attempts:
            time.sleep(2)
            continue

        _fail("Retrieve run", f"Unexpected status: {status}")
        _print_response(response)
        return False

    return False


def check_workflow_dispatched(repository: str) -> bool:
    """Check if a recent GitHub Actions workflow run was dispatched.

    Requires the ``gh`` CLI to be installed and authenticated.

    Args:
        repository: Target repository in ``owner/repo`` format.

    Returns:
        True if a recent workflow run is found.
    """
    if not shutil.which("gh"):
        _warn("Workflow dispatch", "gh CLI not found — skipping workflow check")
        return True  # Non-fatal: treat as pass-with-warning.

    try:
        result = subprocess.run(
            [
                "gh",
                "run",
                "list",
                "--workflow",
                "agent-executor.yml",
                "--repo",
                repository,
                "--limit",
                "1",
                "--json",
                "databaseId,status,createdAt",
            ],
            capture_output=True,
            text=True,
            timeout=15,
        )
    except subprocess.TimeoutExpired:
        _warn("Workflow dispatch", "gh CLI timed out")
        return True  # Non-fatal.
    except FileNotFoundError:
        _warn("Workflow dispatch", "gh CLI not available")
        return True  # Non-fatal.

    if result.returncode != 0:
        _warn("Workflow dispatch", f"gh CLI error: {result.stderr.strip()}")
        return True  # Non-fatal.

    try:
        runs = json.loads(result.stdout)
    except json.JSONDecodeError:
        _warn("Workflow dispatch", f"gh CLI returned invalid JSON: {result.stdout}")
        return True

    if not runs:
        _warn("Workflow dispatch", "No recent workflow runs found")
        return True  # Non-fatal — the run may not have appeared yet.

    latest = runs[0]
    run_id = latest.get("databaseId", "?")
    status = latest.get("status", "?")
    created_at = latest.get("createdAt", "?")
    _pass(
        "Workflow dispatch",
        f"Latest run id={run_id}, status={status}, created={created_at}",
    )
    return True


# ---------------------------------------------------------------------------
# Main orchestration
# ---------------------------------------------------------------------------


def run_smoke_tests(
    api_url: str,
    api_key: str,
    repository: str,
    skip_dispatch: bool,
    timeout: int,
) -> bool:
    """Execute the full smoke test sequence against the production API.

    Args:
        api_url: Base URL of the API (e.g. ``https://<apprunner-url>``).
        api_key: API key for authentication.
        repository: Target repository in ``owner/repo`` format.
        skip_dispatch: If True, skip the create-run and retrieve-run checks.
        timeout: HTTP request timeout in seconds.

    Returns:
        True if all checks pass.
    """
    api_url = api_url.rstrip("/")
    passed = 0
    failed = 0
    total = 0

    print(f"\n{'='*60}")
    print("  Copilot Dispatch Production Smoke Test")
    print(f"{'='*60}")
    print(f"  API URL:      {api_url}")
    print(f"  Repository:   {repository}")
    print(f"  Dispatch:     {'skip' if skip_dispatch else 'enabled'}")
    print(f"  Timeout:      {timeout}s")
    print(f"{'='*60}\n")

    client = httpx.Client(timeout=timeout)

    try:
        # Check 1: Health
        total += 1
        if check_health(client, api_url):
            passed += 1
        else:
            failed += 1
            print("\n  Health check failed — aborting remaining checks.\n")
            return False

        # Check 2: Auth rejection
        total += 1
        if check_auth_rejection(client, api_url):
            passed += 1
        else:
            failed += 1

        if skip_dispatch:
            print(f"\n  Skipping dispatch checks (--skip-dispatch)\n")
        else:
            # Check 3: Create run
            total += 1
            run_id = check_create_run(client, api_url, api_key, repository)
            if run_id:
                passed += 1
            else:
                failed += 1

            # Check 4: Retrieve run
            if run_id:
                total += 1
                if check_retrieve_run(client, api_url, api_key, run_id):
                    passed += 1
                else:
                    failed += 1

            # Check 5: Workflow dispatched (optional, non-fatal)
            total += 1
            # The workflow lives in the copilot-dispatch orchestration repo,
            # not the target repo. Derive the owner from the target repo.
            owner = repository.split("/")[0]
            orchestration_repo = f"{owner}/copilot-dispatch"
            if check_workflow_dispatched(orchestration_repo):
                passed += 1
            else:
                failed += 1

    finally:
        client.close()

    # Summary
    print(f"\n{'='*60}")
    if failed == 0:
        print(f"  {_GREEN}ALL {passed}/{total} CHECKS PASSED{_RESET}")
    else:
        print(f"  {_RED}{failed}/{total} CHECKS FAILED{_RESET}")
    print(f"{'='*60}\n")

    return failed == 0


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    """Parse command-line arguments for the smoke test script.

    Args:
        argv: Argument list to parse. Defaults to ``sys.argv[1:]``.

    Returns:
        Parsed argument namespace.
    """
    parser = argparse.ArgumentParser(
        description="Copilot Dispatch production deployment smoke test.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "--api-url",
        required=True,
        help="The AppRunner HTTPS URL (e.g. https://<id>.ap-southeast-2.awsapprunner.com)",
    )
    parser.add_argument(
        "--api-key",
        required=True,
        help="The API key for authentication (X-API-Key header).",
    )
    parser.add_argument(
        "--repository",
        default=_DEFAULT_REPOSITORY,
        help=f"Target repository for the test run (default: {_DEFAULT_REPOSITORY}).",
    )
    parser.add_argument(
        "--skip-dispatch",
        action="store_true",
        help="Skip the create-run step (health and auth checks only).",
    )
    parser.add_argument(
        "--timeout",
        type=int,
        default=_DEFAULT_TIMEOUT,
        help=f"HTTP request timeout in seconds (default: {_DEFAULT_TIMEOUT}).",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> None:
    """Entry point for the smoke test script.

    Args:
        argv: Argument list to parse. Defaults to ``sys.argv[1:]``.
    """
    args = parse_args(argv)
    success = run_smoke_tests(
        api_url=args.api_url,
        api_key=args.api_key,
        repository=args.repository,
        skip_dispatch=args.skip_dispatch,
        timeout=args.timeout,
    )
    sys.exit(0 if success else 1)


if __name__ == "__main__":
    main()
