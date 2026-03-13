"""Unit tests for Markdown custom agent definition loading.

Validates the PromptLoader adapter that translates ``.agent.md`` files into
Copilot SDK ``custom_agents`` dictionaries.
"""

from __future__ import annotations

import logging
from pathlib import Path

import pytest

from app.src.agent.prompts import PromptLoader


def test_load_agent_definition_valid_markdown(tmp_path: Path) -> None:
    """Valid markdown agent files load to SDK-compatible dictionaries."""
    agent_file = tmp_path / "security-reviewer.agent.md"
    agent_file.write_text(
        """---
name: Security Reviewer
description: Reviews auth and security posture.
tools: ["grep", "glob", "view"]
infer: true
---

# Agent: Security Reviewer

Review the change for security issues.
""",
        encoding="utf-8",
    )

    result = PromptLoader.load_agent_definition(agent_file)

    assert result["name"] == "security-reviewer"
    assert result["display_name"] == "Security Reviewer"
    assert result["description"] == "Reviews auth and security posture."
    assert result["tools"] == ["grep", "glob", "view"]
    assert result["infer"] is True
    assert "Review the change" in str(result["prompt"])


def test_load_agent_definition_uses_body_as_prompt(tmp_path: Path) -> None:
    """Markdown body is preserved as the SDK prompt."""
    agent_file = tmp_path / "doc.agent.md"
    body = "# Agent: Docs\n\nUpdate docs and examples."
    agent_file.write_text(
        f"""---
name: Docs Updater
description: Updates docs.
---

{body}
""",
        encoding="utf-8",
    )

    result = PromptLoader.load_agent_definition(agent_file)
    assert result["prompt"] == body


def test_load_agent_definition_slugifies_name_when_needed(tmp_path: Path) -> None:
    """Human-readable names are slugified when machine_name is absent."""
    agent_file = tmp_path / "implementation.agent.md"
    agent_file.write_text(
        """---
name: Implementation Agent v2
description: Implements scoped changes.
---

Do implementation work.
""",
        encoding="utf-8",
    )

    result = PromptLoader.load_agent_definition(agent_file)
    assert result["name"] == "implementation-agent-v2"


def test_load_agent_definition_preserves_display_name(tmp_path: Path) -> None:
    """Explicit display_name overrides fallback display name."""
    agent_file = tmp_path / "review.agent.md"
    agent_file.write_text(
        """---
name: Internal Name
display_name: Review Guardian
description: Reviews pull requests.
---

Review carefully.
""",
        encoding="utf-8",
    )

    result = PromptLoader.load_agent_definition(agent_file)
    assert result["display_name"] == "Review Guardian"


def test_load_agent_definition_machine_name_override(tmp_path: Path) -> None:
    """machine_name is used verbatim when provided."""
    agent_file = tmp_path / "review.agent.md"
    agent_file.write_text(
        """---
name: Security Reviewer
machine_name: sec-review
description: Reviews security.
---

Review carefully.
""",
        encoding="utf-8",
    )

    result = PromptLoader.load_agent_definition(agent_file)
    assert result["name"] == "sec-review"


def test_load_agent_definition_includes_tools(tmp_path: Path) -> None:
    """tools metadata is passed through when valid."""
    agent_file = tmp_path / "tools.agent.md"
    agent_file.write_text(
        """---
name: Tool User
description: Uses tools.
tools: ["grep", "view"]
---

Use tools.
""",
        encoding="utf-8",
    )

    result = PromptLoader.load_agent_definition(agent_file)
    assert result["tools"] == ["grep", "view"]


def test_load_agent_definition_includes_infer(tmp_path: Path) -> None:
    """infer metadata is passed through when valid."""
    agent_file = tmp_path / "infer.agent.md"
    agent_file.write_text(
        """---
name: Infer Agent
description: Uses infer.
infer: false
---

Infer settings.
""",
        encoding="utf-8",
    )

    result = PromptLoader.load_agent_definition(agent_file)
    assert result["infer"] is False


def test_load_agent_definition_ignores_argument_hint(tmp_path: Path) -> None:
    """Non-SDK argument-hint metadata is ignored."""
    agent_file = tmp_path / "hint.agent.md"
    agent_file.write_text(
        """---
name: Hint Agent
description: Contains argument hint.
argument-hint: Help text
---

Prompt body.
""",
        encoding="utf-8",
    )

    result = PromptLoader.load_agent_definition(agent_file)
    assert "argument-hint" not in result


def test_load_agent_definition_missing_frontmatter_name(tmp_path: Path) -> None:
    """Missing name in frontmatter raises ValueError."""
    agent_file = tmp_path / "invalid.agent.md"
    agent_file.write_text(
        """---
description: Missing name.
---

Body.
""",
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="name"):
        PromptLoader.load_agent_definition(agent_file)


def test_load_agent_definition_missing_description(tmp_path: Path) -> None:
    """Missing description in frontmatter raises ValueError."""
    agent_file = tmp_path / "invalid.agent.md"
    agent_file.write_text(
        """---
name: Missing Description
---

Body.
""",
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="description"):
        PromptLoader.load_agent_definition(agent_file)


def test_load_agent_definition_empty_body(tmp_path: Path) -> None:
    """Empty markdown body raises ValueError."""
    agent_file = tmp_path / "invalid.agent.md"
    agent_file.write_text(
        """---
name: Empty Body
description: No prompt body.
---
""",
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="non-empty body"):
        PromptLoader.load_agent_definition(agent_file)


def test_load_agent_definition_invalid_frontmatter(tmp_path: Path) -> None:
    """Malformed YAML frontmatter raises ValueError."""
    agent_file = tmp_path / "invalid.agent.md"
    agent_file.write_text(
        """---
name: Broken
description: [oops
---

Body.
""",
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="invalid YAML"):
        PromptLoader.load_agent_definition(agent_file)


def test_load_agent_definition_invalid_tools_type(tmp_path: Path) -> None:
    """Non-list tools metadata raises ValueError."""
    agent_file = tmp_path / "invalid.agent.md"
    agent_file.write_text(
        """---
name: Invalid Tools
description: Invalid tools.
tools: grep
---

Body.
""",
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="tools"):
        PromptLoader.load_agent_definition(agent_file)


def test_load_agent_definition_invalid_infer_type(tmp_path: Path) -> None:
    """Non-boolean infer metadata raises ValueError."""
    agent_file = tmp_path / "invalid.agent.md"
    agent_file.write_text(
        """---
name: Invalid Infer
description: Invalid infer.
infer: maybe
---

Body.
""",
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="infer"):
        PromptLoader.load_agent_definition(agent_file)


def test_load_agent_definition_file_not_found(tmp_path: Path) -> None:
    """Missing file raises FileNotFoundError."""
    missing = tmp_path / "missing.agent.md"
    with pytest.raises(FileNotFoundError):
        PromptLoader.load_agent_definition(missing)


def test_resolve_agent_paths_none(tmp_path: Path) -> None:
    """Passing None returns an empty list."""
    assert PromptLoader.resolve_agent_paths(None, tmp_path) == []


def test_resolve_agent_paths_empty(tmp_path: Path) -> None:
    """Passing an empty list returns an empty list."""
    assert PromptLoader.resolve_agent_paths([], tmp_path) == []


def test_resolve_agent_paths_valid(tmp_path: Path) -> None:
    """Valid agent paths are resolved and loaded."""
    repo = tmp_path / "repo"
    agent_dir = repo / ".github" / "agents"
    agent_dir.mkdir(parents=True)

    one = agent_dir / "one.agent.md"
    one.write_text(
        """---
name: One Agent
description: First.
---

Prompt one.
""",
        encoding="utf-8",
    )

    two = agent_dir / "two.agent.md"
    two.write_text(
        """---
name: Two Agent
description: Second.
---

Prompt two.
""",
        encoding="utf-8",
    )

    result = PromptLoader.resolve_agent_paths(
        [
            ".github/agents/one.agent.md",
            ".github/agents/two.agent.md",
        ],
        repo,
    )

    assert len(result) == 2
    assert {str(agent["name"]) for agent in result} == {"one-agent", "two-agent"}


def test_resolve_agent_paths_mixed(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """Invalid/missing paths are skipped with warnings."""
    repo = tmp_path / "repo"
    agent_dir = repo / ".github" / "agents"
    agent_dir.mkdir(parents=True)

    valid = agent_dir / "valid.agent.md"
    valid.write_text(
        """---
name: Valid Agent
description: Valid file.
---

Prompt.
""",
        encoding="utf-8",
    )

    invalid = agent_dir / "invalid.agent.md"
    invalid.write_text(
        """---
name: Invalid Agent
---

Prompt.
""",
        encoding="utf-8",
    )

    with caplog.at_level(logging.WARNING, logger="dispatch.agent.prompts"):
        result = PromptLoader.resolve_agent_paths(
            [
                ".github/agents/valid.agent.md",
                ".github/agents/invalid.agent.md",
                ".github/agents/missing.agent.md",
            ],
            repo,
        )

    assert len(result) == 1
    assert result[0]["name"] == "valid-agent"
    assert any(
        "Custom agent path will be skipped" in rec.message for rec in caplog.records
    )


def test_resolve_agent_paths_duplicate_name_skipped(
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Duplicate machine names are skipped to avoid SDK collisions."""
    repo = tmp_path / "repo"
    agent_dir = repo / ".github" / "agents"
    agent_dir.mkdir(parents=True)

    first = agent_dir / "one.agent.md"
    first.write_text(
        """---
name: Security Reviewer
description: One.
---

Prompt one.
""",
        encoding="utf-8",
    )

    second = agent_dir / "two.agent.md"
    second.write_text(
        """---
name: Security Reviewer
description: Two.
---

Prompt two.
""",
        encoding="utf-8",
    )

    with caplog.at_level(logging.WARNING, logger="dispatch.agent.prompts"):
        result = PromptLoader.resolve_agent_paths(
            [
                ".github/agents/one.agent.md",
                ".github/agents/two.agent.md",
            ],
            repo,
        )

    assert len(result) == 1
    assert any("Duplicate custom agent name" in rec.message for rec in caplog.records)
