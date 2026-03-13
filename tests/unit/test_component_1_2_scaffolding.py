"""Component 1.2 Project Scaffolding — QA Test Suite.

Tests all requirements for Component 1.2: Project Scaffolding.
Each test maps to a specific spec requirement.
"""

import importlib
import importlib.util
import os
import sys
import tomllib

import pytest

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

BASE = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def path(*parts: str) -> str:
    return os.path.join(BASE, *parts)


def file_exists(rel_path: str) -> bool:
    return os.path.isfile(path(rel_path))


def dir_exists(rel_path: str) -> bool:
    return os.path.isdir(path(rel_path))


def read_file(rel_path: str) -> str:
    with open(path(rel_path), encoding="utf-8") as fh:
        return fh.read()


# ---------------------------------------------------------------------------
# Test 1 — Required directories exist
# ---------------------------------------------------------------------------

REQUIRED_DIRS = [
    "app/src",
    "app/src/api",
    "app/src/services",
    "app/src/auth",
    "app/src/agent",
    "app/config",
    "app/config/prompts",
    "app/docs",
    "infra",
    "infra/stacks",
    "tests",
    "tests/unit",
    "tests/integration",
    "scripts",
]


@pytest.mark.parametrize("directory", REQUIRED_DIRS)
def test_required_directory_exists(directory):
    """Test 1: All required directories exist."""
    assert dir_exists(directory), f"Required directory missing: {directory}"


# ---------------------------------------------------------------------------
# Test 2 — __init__.py files exist with module-level docstrings
# ---------------------------------------------------------------------------

REQUIRED_INIT_FILES = [
    "app/__init__.py",
    "app/src/__init__.py",
    "app/src/api/__init__.py",
    "app/src/services/__init__.py",
    "app/src/auth/__init__.py",
    "app/src/agent/__init__.py",
    "app/config/__init__.py",
    "infra/__init__.py",
    "infra/stacks/__init__.py",
    "tests/__init__.py",
    "tests/unit/__init__.py",
    "tests/integration/__init__.py",
]


@pytest.mark.parametrize("init_file", REQUIRED_INIT_FILES)
def test_init_file_exists(init_file):
    """Test 2a: All required __init__.py files exist."""
    assert file_exists(init_file), f"Missing __init__.py: {init_file}"


@pytest.mark.parametrize("init_file", REQUIRED_INIT_FILES)
def test_init_file_has_docstring(init_file):
    """Test 2b: All __init__.py files have module-level docstrings."""
    content = read_file(init_file)
    assert content.strip().startswith('"""') or content.strip().startswith(
        "'''"
    ), f"No module-level docstring in {init_file}. Content starts: {content[:80]!r}"


# ---------------------------------------------------------------------------
# Test 3 — app/src/__init__.py defines __version__ = "0.1.0"
# ---------------------------------------------------------------------------


def test_version_defined_in_src_init():
    """Test 3: app/src/__init__.py defines __version__ = '0.1.0'."""
    content = read_file("app/src/__init__.py")
    assert (
        '__version__ = "0.1.0"' in content or "__version__ = '0.1.0'" in content
    ), f"__version__ not found in app/src/__init__.py. Content: {content!r}"


# ---------------------------------------------------------------------------
# Test 4 — pyproject.toml is valid and has all required sections
# ---------------------------------------------------------------------------

REQUIRED_PYPROJECT_SECTIONS = [
    "[project]",
    "[build-system]",
    "[tool.black]",
    "[tool.isort]",
    "[tool.pytest.ini_options]",
    "[project.optional-dependencies]",
]


def test_pyproject_toml_is_valid():
    """Test 4a: pyproject.toml is syntactically valid TOML."""
    content = read_file("pyproject.toml")
    try:
        parsed = tomllib.loads(content)
    except Exception as e:
        pytest.fail(f"pyproject.toml is not valid TOML: {e}")
    assert isinstance(parsed, dict), "pyproject.toml did not parse to a dict"


@pytest.mark.parametrize("section", REQUIRED_PYPROJECT_SECTIONS)
def test_pyproject_has_required_sections(section):
    """Test 4b: pyproject.toml has all required sections."""
    content = read_file("pyproject.toml")
    assert section in content, f"pyproject.toml missing section: {section}"


# ---------------------------------------------------------------------------
# Test 5 — Required production dependencies present in pyproject.toml
# ---------------------------------------------------------------------------

REQUIRED_PROD_DEPS = [
    "fastapi>=0.115.0",
    "uvicorn[standard]>=0.34.0",
    "pydantic>=2.0",
    "boto3>=1.35.0",
    "httpx>=0.28.0",
    "pyyaml>=6.0",
]


@pytest.mark.parametrize("dep", REQUIRED_PROD_DEPS)
def test_production_dependency_present(dep):
    """Test 5: All required production dependencies are in pyproject.toml."""
    content = read_file("pyproject.toml")
    assert dep in content, f"Production dependency missing: {dep}"


# ---------------------------------------------------------------------------
# Test 6 — Required dev dependencies present in pyproject.toml
# ---------------------------------------------------------------------------

REQUIRED_DEV_DEPS = [
    "pytest>=8.0",
    "pytest-cov>=6.0",
    "pytest-asyncio>=0.25.0",
    "black>=24.0",
    "isort>=5.13",
]


@pytest.mark.parametrize("dep", REQUIRED_DEV_DEPS)
def test_dev_dependency_present(dep):
    """Test 6: All required dev dependencies are in pyproject.toml."""
    content = read_file("pyproject.toml")
    assert dep in content, f"Dev dependency missing: {dep}"


# ---------------------------------------------------------------------------
# Test 7 — Dockerfile requirements
# ---------------------------------------------------------------------------


def test_dockerfile_exists():
    """Test 7a: Dockerfile exists."""
    assert file_exists("Dockerfile"), "Dockerfile not found"


def test_dockerfile_base_image():
    """Test 7b: Dockerfile uses python:3.13-slim base image."""
    content = read_file("Dockerfile")
    assert (
        "python:3.13-slim" in content
    ), f"Dockerfile missing 'python:3.13-slim'. Found: {content[:200]!r}"


def test_dockerfile_non_root_user():
    """Test 7c: Dockerfile creates and uses non-root user 'appuser'."""
    content = read_file("Dockerfile")
    assert "appuser" in content, "Dockerfile missing non-root user 'appuser'"
    assert "USER appuser" in content, "Dockerfile does not switch to 'USER appuser'"


def test_dockerfile_healthcheck():
    """Test 7d: Dockerfile has HEALTHCHECK instruction."""
    content = read_file("Dockerfile")
    assert "HEALTHCHECK" in content, "Dockerfile missing HEALTHCHECK instruction"


def test_dockerfile_expose_8000():
    """Test 7e: Dockerfile has EXPOSE 8000."""
    content = read_file("Dockerfile")
    assert "EXPOSE 8000" in content, "Dockerfile missing 'EXPOSE 8000'"


# ---------------------------------------------------------------------------
# Test 8 — .env/.env.example has all 12 required variables
# ---------------------------------------------------------------------------

REQUIRED_ENV_VARS = [
    "DISPATCH_API_KEY",
    "DISPATCH_WEBHOOK_SECRET",
    "AWS_REGION",
    "AWS_ACCESS_KEY_ID",
    "AWS_SECRET_ACCESS_KEY",
    "DYNAMODB_TABLE_NAME",
    "DYNAMODB_ENDPOINT_URL",
    "GITHUB_PAT",
    "GITHUB_OWNER",
    "GITHUB_REPO",
    "GITHUB_WORKFLOW_ID",
    "APP_ENV",
    # LOG_LEVEL is bonus — included in the count
]


def test_env_example_exists():
    """Test 8a: .env/.env.example exists."""
    assert file_exists(".env/.env.example"), ".env/.env.example not found"


@pytest.mark.parametrize("var", REQUIRED_ENV_VARS)
def test_env_example_has_variable(var):
    """Test 8b: .env/.env.example contains all required variables."""
    content = read_file(".env/.env.example")
    assert var in content, f".env.example missing variable: {var}"


def test_env_example_has_12_vars():
    """Test 8c: .env/.env.example has at least 12 required variables."""
    content = read_file(".env/.env.example")
    # Count lines that define variables (KEY=value format, not comments)
    var_lines = [
        line.strip()
        for line in content.splitlines()
        if line.strip() and not line.strip().startswith("#") and "=" in line
    ]
    assert (
        len(var_lines) >= 12
    ), f"Expected at least 12 variable definitions, found {len(var_lines)}: {var_lines}"


# ---------------------------------------------------------------------------
# Test 9 — .env/.env.test exists with test-safe values
# ---------------------------------------------------------------------------


def test_env_test_exists():
    """Test 9a: .env/.env.test exists."""
    assert file_exists(".env/.env.test"), ".env/.env.test not found"


def test_env_test_has_required_vars():
    """Test 9b: .env/.env.test has all required variables with test-safe values."""
    content = read_file(".env/.env.test")
    for var in REQUIRED_ENV_VARS:
        assert var in content, f".env.test missing variable: {var}"


def test_env_test_no_real_credentials():
    """Test 9c: .env/.env.test does not contain real-looking credentials."""
    content = read_file(".env/.env.test")
    # Verify APP_ENV is 'test' — confirms file is test-specific
    assert "APP_ENV=test" in content, ".env/.env.test APP_ENV is not 'test'"
    # Verify no real AWS key format (AKIA prefix is real AWS access key prefix)
    assert (
        "AKIA" not in content
    ), ".env/.env.test appears to contain a real AWS access key"


# ---------------------------------------------------------------------------
# Test 10 — README.md has required sections
# ---------------------------------------------------------------------------

REQUIRED_README_SECTIONS = [
    "## Overview",
    "## Prerequisites",
    "## Quickstart",
    "## API Usage",
    "## Deployment",
    "## Development",
    "## Documentation",
]


def test_readme_exists():
    """Test 10a: README.md exists."""
    assert file_exists("README.md"), "README.md not found"


@pytest.mark.parametrize("section", REQUIRED_README_SECTIONS)
def test_readme_has_required_section(section):
    """Test 10b: README.md has all required sections."""
    content = read_file("README.md")
    assert section in content, f"README.md missing section: {section}"


# ---------------------------------------------------------------------------
# Test 11 — .gitignore includes project-specific entries
# ---------------------------------------------------------------------------

REQUIRED_GITIGNORE_ENTRIES = [
    ".env/.env.local",
    ".venv/",
    "cdk.out/",
    "data/",
]


def test_gitignore_exists():
    """Test 11a: .gitignore exists."""
    assert file_exists(".gitignore"), ".gitignore not found"


@pytest.mark.parametrize("entry", REQUIRED_GITIGNORE_ENTRIES)
def test_gitignore_has_required_entry(entry):
    """Test 11b: .gitignore includes all required project-specific entries."""
    content = read_file(".gitignore")
    assert entry in content, f".gitignore missing entry: {entry}"


# ---------------------------------------------------------------------------
# Test 12 — implementation-context-phase-1.md has Component 1.2 entry
# ---------------------------------------------------------------------------


def test_implementation_context_has_component_1_2():
    """Test 12: docs/implementation-context-phase-1.md has Component 1.2 entry."""
    assert file_exists(
        "docs/implementation-context-phase-1.md"
    ), "docs/implementation-context-phase-1.md not found"
    content = read_file("docs/implementation-context-phase-1.md")
    assert (
        "Component 1.2" in content
    ), "implementation-context-phase-1.md missing 'Component 1.2' entry"
    assert (
        "Project Scaffolding" in content
    ), "implementation-context-phase-1.md missing 'Project Scaffolding' in Component 1.2"


# ---------------------------------------------------------------------------
# Test 13 — pip install -e ".[dev]" succeeds (packages importable in venv)
# ---------------------------------------------------------------------------


def test_package_is_installed():
    """Test 13: Package 'copilot-dispatch' is installed (pip install -e '.[dev]' succeeded)."""
    spec = importlib.util.find_spec("app")
    assert (
        spec is not None
    ), "Package 'app' is not importable. Run: pip install -e '.[dev]'"


def test_dev_tools_installed():
    """Test 13b: Dev tools (pytest, black, isort) are importable."""
    for tool in ["pytest", "black", "isort"]:
        spec = importlib.util.find_spec(tool)
        assert (
            spec is not None
        ), f"Dev tool '{tool}' is not importable. Run: pip install -e '.[dev]'"


# ---------------------------------------------------------------------------
# Test 14 — from app.src import __version__ returns "0.1.0"
# ---------------------------------------------------------------------------


def test_version_import():
    """Test 14: from app.src import __version__ returns '0.1.0'."""
    # Force reimport to avoid stale cache
    if "app.src" in sys.modules:
        del sys.modules["app.src"]
    from app.src import __version__

    assert (
        __version__ == "0.1.0"
    ), f"Expected __version__ == '0.1.0', got {__version__!r}"


# ---------------------------------------------------------------------------
# Test 15 — All subpackages are importable
# ---------------------------------------------------------------------------

IMPORTABLE_MODULES = [
    "app.src.api",
    "app.src.services",
    "app.src.auth",
    "app.src.agent",
]


@pytest.mark.parametrize("module", IMPORTABLE_MODULES)
def test_subpackage_importable(module):
    """Test 15: All app sub-packages are importable."""
    if module in sys.modules:
        del sys.modules[module]
    try:
        importlib.import_module(module)
    except ImportError as exc:
        pytest.fail(f"Cannot import {module}: {exc}")


# ---------------------------------------------------------------------------
# Test 16 — .env/.env.local exists (not read, just existence check)
# ---------------------------------------------------------------------------


@pytest.mark.skipif(
    os.environ.get("CI") == "true",
    reason=".env/.env.local is gitignored and only exists in local dev (Component 1.1 human setup)",
)
def test_env_local_exists():
    """Test 16: .env/.env.local exists (was not deleted or overwritten)."""
    assert file_exists(
        ".env/.env.local"
    ), ".env/.env.local does not exist. It should have been pre-existing from Component 1.1."


# ---------------------------------------------------------------------------
# Test 17 — app/src/main.py does NOT exist yet (Component 1.4 responsibility)
# ---------------------------------------------------------------------------


def test_main_py_exists():
    """Test 17: app/src/main.py exists (Component 1.4 responsibility)."""
    assert file_exists(
        "app/src/main.py"
    ), "app/src/main.py does not exist. Component 1.4 should create it."


# ---------------------------------------------------------------------------
# Test 18 — app/config/settings.yaml is owned by Component 1.3
# ---------------------------------------------------------------------------


def test_settings_yaml_owned_by_component_1_3():
    """Test 18: app/config/settings.yaml exists (created by Component 1.3, not 1.2)."""
    # This test was originally a negative test asserting settings.yaml didn't exist
    # during 1.2 scope validation. Component 1.3 has since created it correctly.
    # We now verify it exists as a cross-component integration assertion.
    assert file_exists(
        "app/config/settings.yaml"
    ), "app/config/settings.yaml should exist — it is created by Component 1.3."
