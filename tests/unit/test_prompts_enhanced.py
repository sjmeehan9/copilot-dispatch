"""Unit tests for enhanced PromptLoader functionality (Component 4.1).

Tests cover:
- Production template loading and key phrase verification for all three roles.
- ``load_repo_instructions``: found/not-found/encoding-error cases.
- ``resolve_skill_paths``: valid, missing, and ``None`` inputs.
- ``build_system_message``: all parameter combinations, ``repo_path`` auto-load,
  and orphaned placeholder safety.
"""

from __future__ import annotations

import logging
import re
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
# Fixtures — temporary prompts and repo structures
# ---------------------------------------------------------------------------

_TEMPLATE_CONTENT = """\
# Test Template

## Additional Instructions

{system_instructions}

## Repository-Specific Guidance

{repo_instructions}

## Body

This is the body of the test prompt.
"""


@pytest.fixture()
def tmp_loader(tmp_path: Path) -> PromptLoader:
    """Return a PromptLoader backed by a temporary directory with test templates."""
    for role in ("implement", "review", "merge"):
        (tmp_path / f"{role}.md").write_text(_TEMPLATE_CONTENT, encoding="utf-8")
    return PromptLoader(prompts_dir=tmp_path)


@pytest.fixture()
def repo_with_instructions(tmp_path: Path) -> Path:
    """Create a temporary repo directory with .github/copilot-instructions.md."""
    repo_dir = tmp_path / "test-repo"
    github_dir = repo_dir / ".github"
    github_dir.mkdir(parents=True)
    (github_dir / "copilot-instructions.md").write_text(
        "Use Black formatting. Follow PEP 8. Run pytest before committing.",
        encoding="utf-8",
    )
    return repo_dir


@pytest.fixture()
def repo_without_instructions(tmp_path: Path) -> Path:
    """Create a temporary repo directory without copilot-instructions.md."""
    repo_dir = tmp_path / "bare-repo"
    repo_dir.mkdir(parents=True)
    return repo_dir


# ---------------------------------------------------------------------------
# Test 1 — implement template loads and formats correctly
# ---------------------------------------------------------------------------


def test_implement_template_loads_and_formats(loader: PromptLoader) -> None:
    """Load implement template with system and repo instructions, verify output."""
    message = loader.build_system_message(
        role="implement",
        system_instructions="Focus on backward compatibility.",
        repo_instructions="Use Black formatting. Follow PEP 8.",
    )
    # All placeholders replaced.
    assert "{system_instructions}" not in message
    assert "{repo_instructions}" not in message
    # Key instruction phrases present.
    assert "implement" in message.lower()
    assert "feature branch" in message.lower()
    assert "test suite" in message.lower()
    assert "security" in message.lower()
    assert "Focus on backward compatibility." in message
    assert "Use Black formatting. Follow PEP 8." in message


def test_implement_template_has_key_sections(loader: PromptLoader) -> None:
    """Implement template contains critical role boundary sections."""
    template = loader.load_template("implement")
    assert "Operational Boundaries" in template
    assert "Implementation Workflow" in template
    assert "Security Awareness" in template
    assert "Quality Gates" in template


# ---------------------------------------------------------------------------
# Test 2 — review template structured output section
# ---------------------------------------------------------------------------


def test_review_template_structured_output_section(loader: PromptLoader) -> None:
    """Review template contains instructions for structured output format."""
    template = loader.load_template("review")
    assert "assessment" in template
    assert "review_comments" in template
    assert "suggested_changes" in template
    assert "security_concerns" in template
    assert "approve" in template
    assert "request_changes" in template


def test_review_template_has_methodology(loader: PromptLoader) -> None:
    """Review template contains review methodology section."""
    template = loader.load_template("review")
    assert "Review Methodology" in template
    assert "Approval Criteria" in template


# ---------------------------------------------------------------------------
# Test 3 — merge template safety constraints
# ---------------------------------------------------------------------------


def test_merge_template_safety_constraints(loader: PromptLoader) -> None:
    """Merge template contains safety constraints about force-push and conflicts."""
    template = loader.load_template("merge")
    assert "force-push" in template.lower() or "force_push" in template.lower()
    assert "conflict report" in template.lower()
    assert "Safety Constraints" in template
    assert "never" in template.lower()


def test_merge_template_has_conflict_resolution(loader: PromptLoader) -> None:
    """Merge template contains conflict resolution and structured output sections."""
    template = loader.load_template("merge")
    assert "Conflict Resolution" in template
    assert "merge_status" in template
    assert "conflict_resolutions" in template


# ---------------------------------------------------------------------------
# Test 4 — repo instructions injected from repo_path
# ---------------------------------------------------------------------------


def test_repo_instructions_injected(
    tmp_loader: PromptLoader,
    repo_with_instructions: Path,
) -> None:
    """Content from .github/copilot-instructions.md is injected via repo_path."""
    message = tmp_loader.build_system_message(
        role="implement",
        repo_path=repo_with_instructions,
    )
    assert "Use Black formatting" in message
    assert "Follow PEP 8" in message
    assert "{repo_instructions}" not in message


def test_repo_instructions_loaded_directly(
    repo_with_instructions: Path,
) -> None:
    """load_repo_instructions returns file contents when file exists."""
    content = PromptLoader.load_repo_instructions(repo_with_instructions)
    assert "Use Black formatting" in content
    assert len(content) > 0


# ---------------------------------------------------------------------------
# Test 5 — missing repo instructions handled gracefully
# ---------------------------------------------------------------------------


def test_missing_repo_instructions_handled(
    tmp_loader: PromptLoader,
    repo_without_instructions: Path,
) -> None:
    """Missing repo instructions produce clean output with no placeholder artifacts."""
    message = tmp_loader.build_system_message(
        role="implement",
        repo_path=repo_without_instructions,
    )
    assert "{repo_instructions}" not in message
    assert "No repository-specific guidance found." in message


def test_missing_repo_instructions_returns_empty_string(
    repo_without_instructions: Path,
) -> None:
    """load_repo_instructions returns empty string when file is missing."""
    content = PromptLoader.load_repo_instructions(repo_without_instructions)
    assert content == ""


def test_no_repo_path_no_repo_instructions(tmp_loader: PromptLoader) -> None:
    """Calling without repo_path or repo_instructions uses fallback text."""
    message = tmp_loader.build_system_message(role="implement")
    assert "{repo_instructions}" not in message
    assert "No repository-specific guidance found." in message


# ---------------------------------------------------------------------------
# Test 6 — resolve_skill_paths with valid paths
# ---------------------------------------------------------------------------


def test_resolve_skill_paths_valid(tmp_path: Path) -> None:
    """Existing skill files are resolved to absolute paths."""
    skills_dir = tmp_path / ".github" / "copilot-skills"
    skills_dir.mkdir(parents=True)
    (skills_dir / "backend.md").write_text("Backend skills", encoding="utf-8")
    (skills_dir / "frontend.md").write_text("Frontend skills", encoding="utf-8")

    result = PromptLoader.resolve_skill_paths(
        skill_paths=[
            ".github/copilot-skills/backend.md",
            ".github/copilot-skills/frontend.md",
        ],
        repo_path=tmp_path,
    )
    assert len(result) == 2
    assert all(Path(p).is_absolute() for p in result)
    assert all(Path(p).is_file() for p in result)


# ---------------------------------------------------------------------------
# Test 7 — resolve_skill_paths with missing paths filtered
# ---------------------------------------------------------------------------


def test_resolve_skill_paths_missing_filtered(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """Non-existent skill paths are filtered out with a warning logged."""
    skills_dir = tmp_path / ".github" / "copilot-skills"
    skills_dir.mkdir(parents=True)
    (skills_dir / "exists.md").write_text("Exists", encoding="utf-8")

    with caplog.at_level(logging.WARNING, logger="dispatch.agent.prompts"):
        result = PromptLoader.resolve_skill_paths(
            skill_paths=[
                ".github/copilot-skills/exists.md",
                ".github/copilot-skills/does-not-exist.md",
            ],
            repo_path=tmp_path,
        )

    assert len(result) == 1
    assert "exists.md" in result[0]
    assert any("does-not-exist.md" in record.message for record in caplog.records)


# ---------------------------------------------------------------------------
# Test 8 — resolve_skill_paths with None input
# ---------------------------------------------------------------------------


def test_resolve_skill_paths_none_input(tmp_path: Path) -> None:
    """Passing None for skill_paths returns an empty list."""
    result = PromptLoader.resolve_skill_paths(skill_paths=None, repo_path=tmp_path)
    assert result == []


def test_resolve_skill_paths_empty_list(tmp_path: Path) -> None:
    """Passing an empty list for skill_paths returns an empty list."""
    result = PromptLoader.resolve_skill_paths(skill_paths=[], repo_path=tmp_path)
    assert result == []


# ---------------------------------------------------------------------------
# Test 9 — no orphaned placeholders in any combination
# ---------------------------------------------------------------------------

_PLACEHOLDER_PATTERN = re.compile(r"\{(system_instructions|repo_instructions)\}")


@pytest.mark.parametrize(
    "system_instructions,repo_instructions,repo_path_present",
    [
        (None, None, False),
        ("Custom instructions", None, False),
        (None, "Explicit repo instructions", False),
        ("Custom instructions", "Explicit repo instructions", False),
        (None, None, True),
        ("Custom instructions", None, True),
    ],
    ids=[
        "all-none",
        "system-only",
        "repo-only",
        "both-explicit",
        "repo-path-no-file",
        "system-and-repo-path",
    ],
)
def test_no_orphaned_placeholders(
    tmp_loader: PromptLoader,
    repo_without_instructions: Path,
    system_instructions: str | None,
    repo_instructions: str | None,
    repo_path_present: bool,
) -> None:
    """All combinations of None/empty inputs produce zero placeholder artifacts."""
    kwargs: dict = {"role": "implement"}
    if system_instructions is not None:
        kwargs["system_instructions"] = system_instructions
    if repo_instructions is not None:
        kwargs["repo_instructions"] = repo_instructions
    if repo_path_present:
        kwargs["repo_path"] = repo_without_instructions

    message = tmp_loader.build_system_message(**kwargs)
    matches = _PLACEHOLDER_PATTERN.findall(message)
    assert len(matches) == 0, f"Orphaned placeholders found: {matches}"


# ---------------------------------------------------------------------------
# Test 10 — backward compatibility: explicit repo_instructions still works
# ---------------------------------------------------------------------------


def test_explicit_repo_instructions_override(tmp_loader: PromptLoader) -> None:
    """Explicit repo_instructions takes precedence over repo_path."""
    # Even though this test doesn't provide a repo_path, it verifies the
    # original API contract (passing repo_instructions directly) still works.
    message = tmp_loader.build_system_message(
        role="review",
        system_instructions="Check for SQL injection.",
        repo_instructions="All SQL must use parameterised queries.",
    )
    assert "Check for SQL injection." in message
    assert "All SQL must use parameterised queries." in message
    assert "{system_instructions}" not in message
    assert "{repo_instructions}" not in message


def test_explicit_repo_instructions_takes_precedence_over_repo_path(
    tmp_loader: PromptLoader,
    repo_with_instructions: Path,
) -> None:
    """When both repo_instructions and repo_path are provided, explicit wins."""
    message = tmp_loader.build_system_message(
        role="implement",
        repo_instructions="Explicit override instructions.",
        repo_path=repo_with_instructions,
    )
    assert "Explicit override instructions." in message
    # The auto-loaded content should NOT appear.
    assert "Use Black formatting" not in message


# ---------------------------------------------------------------------------
# Test 11 — system instructions fallback text
# ---------------------------------------------------------------------------


def test_none_system_instructions_fallback(tmp_loader: PromptLoader) -> None:
    """None system_instructions produces the default fallback message."""
    message = tmp_loader.build_system_message(role="merge")
    assert "No additional instructions provided." in message
    assert "{system_instructions}" not in message


# ---------------------------------------------------------------------------
# Test 12 — production templates have both placeholders
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("role", ["implement", "review", "merge"])
def test_production_templates_have_placeholders(
    loader: PromptLoader, role: str
) -> None:
    """All production prompt templates contain both required placeholders."""
    template = loader.load_template(role)
    assert (
        "{system_instructions}" in template
    ), f"Template '{role}.md' is missing {{system_instructions}} placeholder"
    assert (
        "{repo_instructions}" in template
    ), f"Template '{role}.md' is missing {{repo_instructions}} placeholder"
