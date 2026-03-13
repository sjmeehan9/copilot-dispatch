"""Prompt template loader for agent roles.

Loads Markdown prompt templates from the ``app/config/prompts/`` directory and
renders them by substituting runtime placeholders with caller-supplied values.
Supports injecting repository-level instructions and resolving skill paths
for the Copilot SDK session configuration.
"""

from __future__ import annotations

import logging
import re
from pathlib import Path

import yaml

logger = logging.getLogger("dispatch.agent.prompts")

# ---------------------------------------------------------------------------
# Project root — three levels above this file:
#   app/src/agent/prompts.py → app/src/agent/ → app/src/ → app/ → project root
# ---------------------------------------------------------------------------
_PROJECT_ROOT: Path = Path(__file__).resolve().parent.parent.parent.parent
_DEFAULT_PROMPTS_DIR: Path = _PROJECT_ROOT / "app" / "config" / "prompts"

# Path within a target repo where Copilot instructions may be found.
_REPO_INSTRUCTIONS_PATH = ".github/copilot-instructions.md"


class PromptLoader:
    """Loads and renders role-specific system prompt templates.

    Templates are Markdown files stored in the prompts directory, one per
    agent role (e.g., ``implement.md``, ``review.md``, ``merge.md``). Each
    template contains ``{system_instructions}`` and ``{repo_instructions}``
    placeholders that are replaced at render time with caller-supplied values.

    Example:
        >>> loader = PromptLoader()
        >>> message = loader.build_system_message(
        ...     role="implement",
        ...     system_instructions="Focus on backward compatibility.",
        ...     repo_path=Path("/tmp/my-repo"),
        ... )

    Attributes:
        prompts_dir: Absolute path to the directory containing prompt template
            files.
    """

    def __init__(self, prompts_dir: Path | None = None) -> None:
        """Initialise the PromptLoader.

        Args:
            prompts_dir: Path to the directory containing ``.md`` prompt
                template files. Defaults to ``app/config/prompts/`` relative to
                the project root when ``None``.
        """
        self.prompts_dir: Path = (
            prompts_dir if prompts_dir is not None else _DEFAULT_PROMPTS_DIR
        )

    def load_template(self, role: str) -> str:
        """Load the raw Markdown template for the given agent role.

        Reads ``{prompts_dir}/{role}.md`` from the filesystem and returns its
        full contents as a string.

        Args:
            role: Agent role identifier (e.g., ``"implement"``, ``"review"``,
                ``"merge"``). Must correspond to a ``.md`` file in the prompts
                directory.

        Returns:
            The raw template string with placeholders still present.

        Raises:
            FileNotFoundError: If ``{prompts_dir}/{role}.md`` does not exist.
        """
        template_path: Path = self.prompts_dir / f"{role}.md"
        if not template_path.exists():
            raise FileNotFoundError(
                f"Prompt template for role '{role}' not found at "
                f"'{template_path}'. Ensure a '{role}.md' file exists in "
                f"'{self.prompts_dir}'."
            )
        return template_path.read_text(encoding="utf-8")

    @staticmethod
    def load_repo_instructions(repo_path: Path) -> str:
        """Load repository-level Copilot instructions from a target repo.

        Looks for ``.github/copilot-instructions.md`` in the given repository
        path. If found, reads and returns its contents. If not found or if an
        error occurs during reading, returns an empty string.

        Args:
            repo_path: Absolute path to the root of the checked-out target
                repository.

        Returns:
            The contents of the repo-level instructions file, or an empty
            string if the file does not exist or cannot be read.
        """
        instructions_path = repo_path / _REPO_INSTRUCTIONS_PATH
        if not instructions_path.is_file():
            logger.debug(
                "No repo instructions found at %s",
                instructions_path,
            )
            return ""

        try:
            content = instructions_path.read_text(encoding="utf-8", errors="replace")
            logger.debug(
                "Loaded repo instructions from %s (%d chars)",
                instructions_path,
                len(content),
            )
            return content
        except OSError as exc:
            logger.warning(
                "Failed to read repo instructions at %s: %s",
                instructions_path,
                exc,
            )
            return ""

    @staticmethod
    def resolve_skill_paths(
        skill_paths: list[str] | None,
        repo_path: Path,
    ) -> list[str]:
        """Resolve relative skill paths to absolute filesystem paths.

        Given a list of relative skill paths (e.g.,
        ``[".github/copilot-skills/backend.md"]``) and the repo checkout path,
        resolves each to an absolute path. Paths that do not exist on the
        filesystem are filtered out and a warning is logged for each.

        Args:
            skill_paths: List of relative skill file paths within the repo.
                If ``None`` or empty, returns an empty list.
            repo_path: Absolute path to the root of the checked-out target
                repository.

        Returns:
            List of absolute path strings for skill files that exist on disk.
        """
        if not skill_paths:
            return []

        resolved: list[str] = []
        for relative_path in skill_paths:
            absolute_path = (repo_path / relative_path).resolve()
            if absolute_path.is_file():
                resolved.append(str(absolute_path))
                logger.debug("Resolved skill path: %s", absolute_path)
            else:
                logger.warning(
                    "Skill path does not exist and will be skipped: %s "
                    "(resolved to %s)",
                    relative_path,
                    absolute_path,
                )
        return resolved

    @staticmethod
    def _parse_agent_markdown(
        agent_text: str,
        agent_path: Path,
    ) -> tuple[dict[str, object], str]:
        """Parse a Markdown agent file into frontmatter metadata and body.

        Args:
            agent_text: Full text of the agent markdown file.
            agent_path: Path to the source file, used for diagnostics.

        Returns:
            A tuple of ``(metadata, body_markdown)``.

        Raises:
            ValueError: If frontmatter is missing, malformed, or the body is
                empty.
        """
        if not agent_text.startswith("---"):
            raise ValueError(
                f"Agent file '{agent_path}' must start with YAML frontmatter ('---')."
            )

        frontmatter_end = agent_text.find("\n---", 3)
        if frontmatter_end == -1:
            raise ValueError(
                f"Agent file '{agent_path}' has malformed frontmatter: missing closing '---'."
            )

        frontmatter_raw = agent_text[3:frontmatter_end].strip()
        body_start = frontmatter_end + len("\n---")
        body = agent_text[body_start:].strip()

        try:
            parsed = yaml.safe_load(frontmatter_raw) if frontmatter_raw else {}
        except yaml.YAMLError as exc:
            raise ValueError(
                f"Agent file '{agent_path}' has invalid YAML frontmatter: {exc}"
            ) from exc

        if parsed is None:
            metadata: dict[str, object] = {}
        elif isinstance(parsed, dict):
            metadata = {str(key): value for key, value in parsed.items()}
        else:
            raise ValueError(
                f"Agent file '{agent_path}' frontmatter must be a mapping."
            )

        if not body:
            raise ValueError(
                f"Agent file '{agent_path}' must contain a non-empty body."
            )

        return metadata, body

    @staticmethod
    def _slugify_agent_name(name: str) -> str:
        """Convert a human-readable agent name to a SDK-safe machine name.

        Args:
            name: Human readable name.

        Returns:
            Lowercase slug using alphanumeric and hyphen separators.

        Raises:
            ValueError: If slugification yields an empty name.
        """
        slug = re.sub(r"[^a-z0-9]+", "-", name.strip().lower()).strip("-")
        if not slug:
            raise ValueError("Unable to derive a machine-readable agent name.")
        return slug

    @staticmethod
    def load_agent_definition(agent_path: Path) -> dict[str, object]:
        """Load and validate a custom agent definition from Markdown.

        Args:
            agent_path: Absolute path to a ``.md`` / ``.agent.md`` file in the
                target repository.

        Returns:
            A dictionary suitable for SDK ``custom_agents`` configuration.

        Raises:
            FileNotFoundError: If the path does not exist.
            ValueError: If required fields are missing or invalid.
        """
        if not agent_path.is_file():
            raise FileNotFoundError(f"Agent file not found: {agent_path}")

        text = agent_path.read_text(encoding="utf-8")
        metadata, prompt = PromptLoader._parse_agent_markdown(text, agent_path)

        raw_name = metadata.get("name")
        if not isinstance(raw_name, str) or not raw_name.strip():
            raise ValueError(
                f"Agent file '{agent_path}' must define a non-empty frontmatter 'name'."
            )
        display_name_from_name = raw_name.strip()

        raw_description = metadata.get("description")
        if not isinstance(raw_description, str) or not raw_description.strip():
            raise ValueError(
                f"Agent file '{agent_path}' must define a non-empty frontmatter 'description'."
            )
        description = raw_description.strip()

        display_name: str
        raw_display_name = metadata.get("display_name")
        if raw_display_name is not None:
            if not isinstance(raw_display_name, str) or not raw_display_name.strip():
                raise ValueError(
                    f"Agent file '{agent_path}' has invalid 'display_name' (expected non-empty string)."
                )
            display_name = raw_display_name.strip()
        else:
            display_name = display_name_from_name

        raw_machine_name = metadata.get("machine_name")
        if raw_machine_name is not None:
            if not isinstance(raw_machine_name, str) or not raw_machine_name.strip():
                raise ValueError(
                    f"Agent file '{agent_path}' has invalid 'machine_name' (expected non-empty string)."
                )
            machine_name = raw_machine_name.strip()
        else:
            machine_name = PromptLoader._slugify_agent_name(display_name)

        custom_agent: dict[str, object] = {
            "name": machine_name,
            "display_name": display_name,
            "description": description,
            "prompt": prompt,
        }

        raw_tools = metadata.get("tools")
        if raw_tools is not None:
            if not isinstance(raw_tools, list) or any(
                not isinstance(tool, str) or not tool.strip() for tool in raw_tools
            ):
                raise ValueError(
                    f"Agent file '{agent_path}' has invalid 'tools' (expected list[str])."
                )
            custom_agent["tools"] = [tool.strip() for tool in raw_tools]

        raw_infer = metadata.get("infer")
        if raw_infer is not None:
            if not isinstance(raw_infer, bool):
                raise ValueError(
                    f"Agent file '{agent_path}' has invalid 'infer' (expected boolean)."
                )
            custom_agent["infer"] = raw_infer

        supported_keys = {
            "name",
            "display_name",
            "machine_name",
            "description",
            "tools",
            "infer",
            "argument-hint",
        }
        ignored = sorted(k for k in metadata.keys() if k not in supported_keys)
        if ignored:
            logger.debug(
                "Ignoring unsupported custom agent frontmatter keys for %s: %s",
                agent_path,
                ", ".join(ignored),
            )

        logger.debug(
            "Loaded custom agent definition from %s as machine name '%s'",
            agent_path,
            machine_name,
        )
        return custom_agent

    @staticmethod
    def resolve_agent_paths(
        agent_paths: list[str] | None,
        repo_path: Path,
    ) -> list[dict[str, object]]:
        """Resolve markdown agent definition paths and load their contents.

        Args:
            agent_paths: Relative paths to agent markdown files in the target
                repository.
            repo_path: Absolute path to the checked-out target repository.

        Returns:
            A list of validated SDK-compatible custom agent definitions.
        """
        if not agent_paths:
            return []

        resolved_agents: list[dict[str, object]] = []
        seen_names: set[str] = set()

        for relative_path in agent_paths:
            absolute_path = (repo_path / relative_path).resolve()
            try:
                agent = PromptLoader.load_agent_definition(absolute_path)
            except (ValueError, FileNotFoundError) as exc:
                logger.warning(
                    "Custom agent path will be skipped: %s (resolved to %s): %s",
                    relative_path,
                    absolute_path,
                    exc,
                )
                continue

            machine_name = str(agent.get("name", "")).strip()
            if machine_name in seen_names:
                logger.warning(
                    "Duplicate custom agent name '%s' detected at %s; skipping.",
                    machine_name,
                    absolute_path,
                )
                continue

            seen_names.add(machine_name)
            resolved_agents.append(agent)

        return resolved_agents

    def build_system_message(
        self,
        role: str,
        system_instructions: str | None = None,
        repo_instructions: str | None = None,
        repo_path: Path | None = None,
        context: dict[str, str] | None = None,
    ) -> str:
        """Build a fully-rendered system message for the given agent role.

        Loads the template for ``role`` and substitutes:

        - ``{system_instructions}`` with ``system_instructions`` (or a default
          fallback message if ``None``).
        - ``{repo_instructions}`` with ``repo_instructions``, or the contents
          of ``.github/copilot-instructions.md`` loaded from ``repo_path``,
          or a default fallback message if neither is available.
        - Any additional ``{key}`` placeholders found in the template are
          substituted with values from the ``context`` dictionary.

        When ``repo_path`` is provided and ``repo_instructions`` is not, the
        method will call :meth:`load_repo_instructions` to read the repo-level
        instructions file automatically.

        Args:
            role: Agent role identifier (e.g., ``"implement"``, ``"review"``,
                ``"merge"``). Must correspond to a ``.md`` file in the prompts
                directory.
            system_instructions: Caller-provided instructions to inject into the
                ``{system_instructions}`` placeholder. Pass ``None`` or omit to
                use the default fallback text.
            repo_instructions: Repository-level instructions (typically the
                contents of ``.github/copilot-instructions.md``) to inject into
                the ``{repo_instructions}`` placeholder. Pass ``None`` or omit
                to auto-load from ``repo_path`` or use the default fallback.
            repo_path: Path to the checked-out target repository. When provided
                and ``repo_instructions`` is ``None``, the method loads repo
                instructions from ``.github/copilot-instructions.md`` within
                this path.
            context: Optional dictionary of additional template variables to
                substitute. Keys correspond to ``{placeholder}`` names in the
                template (e.g., ``{"feature_branch": "feature/abc-123"}``
                replaces ``{feature_branch}`` in the template).

        Returns:
            The fully-rendered system message string with all placeholders
            replaced.

        Raises:
            FileNotFoundError: Propagated from :meth:`load_template` if the
                template file for ``role`` does not exist.
        """
        template: str = self.load_template(role)

        # Resolve system instructions.
        resolved_system: str = (
            system_instructions
            if system_instructions is not None
            else "No additional instructions provided."
        )

        # Resolve repo instructions: explicit value > auto-load from path > fallback.
        if repo_instructions is not None:
            resolved_repo = repo_instructions
        elif repo_path is not None:
            loaded = self.load_repo_instructions(repo_path)
            resolved_repo = (
                loaded if loaded else "No repository-specific guidance found."
            )
        else:
            resolved_repo = "No repository-specific guidance found."

        rendered = template.replace("{system_instructions}", resolved_system).replace(
            "{repo_instructions}", resolved_repo
        )

        # Apply additional context substitutions.
        if context:
            for key, value in context.items():
                rendered = rendered.replace(f"{{{key}}}", value)

        # Safety: strip any remaining unreplaced placeholders.
        rendered = re.sub(r"\{system_instructions\}", "", rendered)
        rendered = re.sub(r"\{repo_instructions\}", "", rendered)

        # Normalise excessive blank lines (3+ consecutive) to double newlines.
        rendered = re.sub(r"\n{3,}", "\n\n", rendered)

        return rendered.strip()
