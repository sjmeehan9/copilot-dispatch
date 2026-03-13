"""Unit tests for app.src.agent.prompts.PromptLoader.

Tests cover:
- All three production roles (implement, review, merge) load successfully.
- system_instructions placeholder is substituted correctly.
- repo_instructions placeholder is substituted correctly.
- None instructions result in no placeholder artifacts in output.
- Invalid role raises FileNotFoundError.
- Custom prompts_dir is respected.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from app.src.agent.prompts import PromptLoader

# ---------------------------------------------------------------------------
# Fixtures — production prompts directory
# ---------------------------------------------------------------------------

_PROJECT_ROOT: Path = Path(__file__).resolve().parent.parent.parent
_REAL_PROMPTS_DIR: Path = _PROJECT_ROOT / "app" / "config" / "prompts"


@pytest.fixture()
def loader() -> PromptLoader:
    """Return a PromptLoader pointing at the real production prompts directory."""
    return PromptLoader(prompts_dir=_REAL_PROMPTS_DIR)


# ---------------------------------------------------------------------------
# Fixtures — temporary prompts directory for controlled placeholder tests
# ---------------------------------------------------------------------------

_TEMPLATE_CONTENT = """\
# Test Template

## Repo Instructions
{repo_instructions}

## System Instructions
{system_instructions}

## Body
This is the body of the test prompt.
"""


@pytest.fixture()
def tmp_loader(tmp_path: Path) -> PromptLoader:
    """Return a PromptLoader backed by a temporary directory with test templates."""
    for role in ("implement", "review", "merge"):
        (tmp_path / f"{role}.md").write_text(_TEMPLATE_CONTENT, encoding="utf-8")
    return PromptLoader(prompts_dir=tmp_path)


# ---------------------------------------------------------------------------
# Test 1 — All three production roles load without error
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("role", ["implement", "review", "merge"])
def test_production_role_loads(loader: PromptLoader, role: str) -> None:
    """All three production prompt files are present and non-empty."""
    template = loader.load_template(role)
    assert isinstance(template, str)
    assert len(template.strip()) > 0, f"Template for role '{role}' is empty"


@pytest.mark.parametrize("role", ["implement", "review", "merge"])
def test_production_role_contains_placeholders(loader: PromptLoader, role: str) -> None:
    """All three production templates contain both required placeholders."""
    template = loader.load_template(role)
    assert (
        "{system_instructions}" in template
    ), f"Template '{role}.md' is missing {{system_instructions}} placeholder"
    assert (
        "{repo_instructions}" in template
    ), f"Template '{role}.md' is missing {{repo_instructions}} placeholder"


# ---------------------------------------------------------------------------
# Test 2 — system_instructions placeholder substitution
# ---------------------------------------------------------------------------


def test_system_instructions_substituted(tmp_loader: PromptLoader) -> None:
    """build_system_message() replaces {system_instructions} with provided value."""
    message = tmp_loader.build_system_message(
        role="implement",
        system_instructions="Focus on backward compatibility.",
    )
    assert "Focus on backward compatibility." in message
    assert "{system_instructions}" not in message


def test_system_instructions_for_all_roles(tmp_loader: PromptLoader) -> None:
    """system_instructions substitution works for implement, review, and merge."""
    value = "Use camelCase for all identifiers."
    for role in ("implement", "review", "merge"):
        message = tmp_loader.build_system_message(role=role, system_instructions=value)
        assert value in message
        assert "{system_instructions}" not in message


# ---------------------------------------------------------------------------
# Test 3 — repo_instructions placeholder substitution
# ---------------------------------------------------------------------------


def test_repo_instructions_substituted(tmp_loader: PromptLoader) -> None:
    """build_system_message() replaces {repo_instructions} with provided value."""
    repo_content = "All code must pass Black and isort. Use PEP 8."
    message = tmp_loader.build_system_message(
        role="implement",
        repo_instructions=repo_content,
    )
    assert repo_content in message
    assert "{repo_instructions}" not in message


def test_both_placeholders_substituted(tmp_loader: PromptLoader) -> None:
    """Both placeholders are substituted when both arguments are provided."""
    system_val = "Do not modify test files."
    repo_val = "Run pytest before committing."

    message = tmp_loader.build_system_message(
        role="review",
        system_instructions=system_val,
        repo_instructions=repo_val,
    )

    assert system_val in message
    assert repo_val in message
    assert "{system_instructions}" not in message
    assert "{repo_instructions}" not in message


# ---------------------------------------------------------------------------
# Test 4 — None instructions leave no placeholder artifacts
# ---------------------------------------------------------------------------


def test_none_system_instructions_no_artifact(tmp_loader: PromptLoader) -> None:
    """build_system_message() with system_instructions=None leaves no placeholder."""
    message = tmp_loader.build_system_message(
        role="implement", system_instructions=None
    )
    assert "{system_instructions}" not in message


def test_none_repo_instructions_no_artifact(tmp_loader: PromptLoader) -> None:
    """build_system_message() with repo_instructions=None leaves no placeholder."""
    message = tmp_loader.build_system_message(role="implement", repo_instructions=None)
    assert "{repo_instructions}" not in message


def test_no_instructions_no_artifacts(tmp_loader: PromptLoader) -> None:
    """build_system_message() with no optional args leaves no placeholder artifacts."""
    message = tmp_loader.build_system_message(role="merge")
    assert "{system_instructions}" not in message
    assert "{repo_instructions}" not in message


def test_none_instructions_replaced_with_empty_string(tmp_loader: PromptLoader) -> None:
    """None instructions are replaced with empty string, not the literal 'None'."""
    message = tmp_loader.build_system_message(
        role="implement", system_instructions=None
    )
    # The word "None" should not appear where the placeholder was
    # (the template body must not contain the literal "None" from substitution).
    # We verify by checking the section that contained the placeholder.
    assert "## System Instructions\nNone" not in message


# ---------------------------------------------------------------------------
# Test 5 — Invalid role raises FileNotFoundError
# ---------------------------------------------------------------------------


def test_invalid_role_raises_file_not_found(tmp_loader: PromptLoader) -> None:
    """load_template() raises FileNotFoundError for an unknown role."""
    with pytest.raises(FileNotFoundError) as exc_info:
        tmp_loader.load_template("nonexistent_role")

    assert "nonexistent_role" in str(exc_info.value)


def test_build_system_message_invalid_role_raises(tmp_loader: PromptLoader) -> None:
    """build_system_message() propagates FileNotFoundError for unknown role."""
    with pytest.raises(FileNotFoundError):
        tmp_loader.build_system_message(role="deploy")


# ---------------------------------------------------------------------------
# Test 6 — Custom prompts_dir is respected
# ---------------------------------------------------------------------------


def test_custom_prompts_dir(tmp_path: Path) -> None:
    """PromptLoader respects a custom prompts_dir passed to __init__."""
    custom_dir = tmp_path / "custom_prompts"
    custom_dir.mkdir()
    (custom_dir / "custom_role.md").write_text(
        "Hello {system_instructions} and {repo_instructions}!",
        encoding="utf-8",
    )

    loader = PromptLoader(prompts_dir=custom_dir)
    message = loader.build_system_message(
        role="custom_role",
        system_instructions="world",
        repo_instructions="universe",
    )
    assert message == "Hello world and universe!"


def test_default_loader_uses_production_prompts() -> None:
    """PromptLoader() with no args defaults to the production prompts directory."""
    loader = PromptLoader()
    # All three production roles should be loadable from the default path.
    for role in ("implement", "review", "merge"):
        template = loader.load_template(role)
        assert (
            len(template.strip()) > 0
        ), f"Default loader returned empty template for '{role}'"


# ---------------------------------------------------------------------------
# Test 7 — Template content is not mutated between calls
# ---------------------------------------------------------------------------


def test_template_not_mutated_between_calls(tmp_loader: PromptLoader) -> None:
    """Successive calls to build_system_message do not affect subsequent calls."""
    first = tmp_loader.build_system_message(
        role="implement",
        system_instructions="First call.",
        repo_instructions="Repo A.",
    )
    second = tmp_loader.build_system_message(
        role="implement",
        system_instructions="Second call.",
        repo_instructions="Repo B.",
    )

    assert "First call." in first
    assert "Repo A." in first
    assert "Second call." in second
    assert "Repo B." in second
    # Cross-contamination check
    assert "First call." not in second
    assert "Second call." not in first


# ---------------------------------------------------------------------------
# Test 8 — context dict substitution
# ---------------------------------------------------------------------------

_TEMPLATE_WITH_CONTEXT = """\
# Test Template

Branch: {feature_branch}
Run: {run_id}

## System Instructions
{system_instructions}

## Repo Instructions
{repo_instructions}
"""


def test_context_dict_substitutes_placeholders(tmp_path: Path) -> None:
    """build_system_message() replaces context dict placeholders."""
    (tmp_path / "implement.md").write_text(_TEMPLATE_WITH_CONTEXT, encoding="utf-8")
    loader = PromptLoader(prompts_dir=tmp_path)

    message = loader.build_system_message(
        role="implement",
        system_instructions="Focus on tests.",
        context={"feature_branch": "feature/abc-123", "run_id": "abc-123"},
    )

    assert "feature/abc-123" in message
    assert "Run: abc-123" in message
    assert "{feature_branch}" not in message
    assert "{run_id}" not in message


def test_context_none_leaves_no_error(tmp_path: Path) -> None:
    """build_system_message() with context=None does not raise."""
    (tmp_path / "implement.md").write_text(_TEMPLATE_WITH_CONTEXT, encoding="utf-8")
    loader = PromptLoader(prompts_dir=tmp_path)

    message = loader.build_system_message(role="implement", context=None)
    # Unreplaced context placeholders should remain as-is (not an error).
    assert isinstance(message, str)


def test_context_empty_dict_is_no_op(tmp_loader: PromptLoader) -> None:
    """build_system_message() with an empty context dict behaves like no context."""
    message = tmp_loader.build_system_message(role="implement", context={})
    assert isinstance(message, str)
    assert "{system_instructions}" not in message


def test_production_implement_feature_branch_placeholder() -> None:
    """Production implement.md contains {feature_branch} placeholder."""
    loader = PromptLoader()
    template = loader.load_template("implement")
    assert "{feature_branch}" in template
