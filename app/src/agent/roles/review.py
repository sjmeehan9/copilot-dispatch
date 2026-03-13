"""Review role logic — PR diff analysis, structured review, approval.

Orchestrates the complete review role workflow:

1. **Pre-processing** — fetch PR metadata, checkout the PR branch, fetch the
   base branch for context, retrieve the PR diff.
2. **Execution** — run the Copilot SDK agent with the review system prompt,
   enriched instructions including PR context and diff summary.
3. **Post-processing** — parse the agent's structured review output, submit the
   review via the GitHub API, approve the PR when assessment is "approve", and
   return structured data for ``ReviewResult`` construction.

Example::

    role = ReviewRole(
        run_id="abc-123",
        repo_path=Path("/workspace/target-repo"),
        branch="main",
        pr_number=42,
        agent_instructions="Review this PR for security and correctness.",
        model="claude-sonnet-4-20250514",
        repository="owner/repo",
    )
    await role.pre_process()
    session_log = await role.execute(runner)
    result = await role.post_process(session_log)
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
from pathlib import Path
from typing import Any

from app.src.agent.prompts import PromptLoader
from app.src.agent.runner import AgentExecutionError, AgentRunner, AgentSessionLog

logger = logging.getLogger("dispatch.agent.roles.review")

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

# Maximum diff size (in characters) before truncation.
_MAX_DIFF_SIZE = 50_000


class ReviewRole:
    """Implements the full review role lifecycle.

    Checks out the PR branch, fetches PR metadata and diff, invokes the
    Copilot SDK agent to produce a structured code review, then submits the
    review via the GitHub API and approves the PR when appropriate.

    Attributes:
        run_id: Unique identifier for the agent run.
        repo_path: Absolute path to the checked-out target repository.
        branch: Base branch of the pull request.
        pr_number: The pull request number to review.
        agent_instructions: Natural-language instructions for the agent.
        model: LLM model identifier.
        system_instructions: Optional caller-supplied system instructions.
        skill_paths: Optional list of relative skill file paths.
        timeout_minutes: Maximum session duration in minutes.
        repository: Target repository in ``owner/repo`` format.
    """

    def __init__(
        self,
        run_id: str,
        repo_path: Path,
        branch: str,
        pr_number: int,
        agent_instructions: str,
        model: str,
        system_instructions: str | None = None,
        skill_paths: list[str] | None = None,
        agent_paths: list[str] | None = None,
        timeout_minutes: int = 30,
        repository: str = "",
    ) -> None:
        """Initialise the ReviewRole.

        Args:
            run_id: Unique identifier for the agent run.
            repo_path: Absolute path to the checked-out target repository.
            branch: Base branch of the pull request.
            pr_number: The pull request number to review.
            agent_instructions: Natural-language instructions for the agent.
            model: LLM model identifier (e.g., ``"claude-sonnet-4-20250514"``).
            system_instructions: Optional caller-supplied system-level
                instructions to merge into the prompt.
            skill_paths: Optional list of skill file paths relative to the
                repo root, resolved to absolute paths for the SDK.
            agent_paths: Optional list of custom agent definition file paths
                relative to the repo root.
            timeout_minutes: Maximum session duration in minutes before
                forced termination. Defaults to ``30``.
            repository: Target repository in ``owner/repo`` format, used for
                GitHub API calls. Required for review submission and approval.
        """
        self._run_id = run_id
        self._repo_path = repo_path
        self._branch = branch
        self._pr_number = pr_number
        self._agent_instructions = agent_instructions
        self._model = model
        self._system_instructions = system_instructions
        self._skill_paths = skill_paths
        self._agent_paths = agent_paths
        self._timeout_minutes = timeout_minutes
        self._repository = repository

        # Populated during pre_process.
        self._pr_metadata: dict[str, Any] = {}
        self._pr_diff: str = ""
        self._head_ref: str = ""
        self._base_ref: str = ""

    # ------------------------------------------------------------------
    # Lifecycle methods
    # ------------------------------------------------------------------

    async def pre_process(self) -> None:
        """Prepare the repository for the review agent run.

        Fetches PR metadata (branch names, title, body, change statistics),
        checks out the PR branch, fetches the base branch for context, and
        retrieves the full PR diff.

        Raises:
            AgentExecutionError: If any git or ``gh`` command fails, with
                ``error_code="PR_PRE_PROCESS_ERROR"`` and the command's
                stderr in ``error_details``.
        """
        logger.info(
            "Starting review pre-processing for run %s: PR #%d",
            self._run_id,
            self._pr_number,
            extra={"run_id": self._run_id, "pr_number": self._pr_number},
        )

        # 1. Fetch PR metadata.
        self._pr_metadata = await self._fetch_pr_metadata()
        self._head_ref = self._pr_metadata.get("headRefName", "")
        self._base_ref = self._pr_metadata.get("baseRefName", self._branch)

        if not self._head_ref:
            raise AgentExecutionError(
                error_message=(
                    f"PR #{self._pr_number} metadata missing headRefName — "
                    "cannot determine PR branch."
                ),
                error_code="PR_PRE_PROCESS_ERROR",
                error_details={"pr_metadata": self._pr_metadata},
            )

        # 2. Checkout the PR branch.
        await self._run_git("fetch", "origin", self._head_ref)
        await self._run_git("checkout", self._head_ref)

        # 3. Fetch the base branch for context.
        await self._run_git("fetch", "origin", self._base_ref)

        # 4. Fetch the PR diff.
        self._pr_diff = await self._fetch_pr_diff()

        logger.info(
            "Review pre-processing complete for run %s — PR #%d, "
            "head=%s, base=%s, diff=%d chars",
            self._run_id,
            self._pr_number,
            self._head_ref,
            self._base_ref,
            len(self._pr_diff),
            extra={"run_id": self._run_id, "pr_number": self._pr_number},
        )

    async def execute(self, runner: AgentRunner) -> AgentSessionLog:
        """Run the Copilot SDK agent with the review system prompt.

        Builds the system message from the review template, enriches the
        agent instructions with PR context (title, description, diff summary),
        and delegates to the :class:`AgentRunner`.

        Args:
            runner: A configured :class:`AgentRunner` instance. The runner
                is provided externally so that creation parameters
                (system_message, skill_directories, etc.) can be configured
                by the caller or orchestrator.

        Returns:
            The :class:`AgentSessionLog` produced by the agent session.
        """
        logger.info(
            "Executing review role agent for run %s, PR #%d",
            self._run_id,
            self._pr_number,
            extra={"run_id": self._run_id, "pr_number": self._pr_number},
        )

        enriched_instructions = self._enrich_instructions()
        return await runner.run(enriched_instructions)

    async def post_process(self, session_log: AgentSessionLog) -> dict[str, Any]:
        """Parse review output, submit review, and collect result data.

        After agent execution:
        1. Parses the agent's structured review output.
        2. Submits the PR review via the GitHub API.
        3. Approves the PR if the assessment is ``"approve"``.
        4. Returns structured data for ``ReviewResult`` construction.

        Args:
            session_log: The session log from the completed agent run.

        Returns:
            A dictionary with all fields needed for
            :class:`~app.src.api.models.ReviewResult`:
            ``review_url``, ``assessment``, ``review_comments``,
            ``suggested_changes``, ``security_concerns``, ``pr_approved``,
            ``session_summary``, ``agent_log_url``.
        """
        logger.info(
            "Starting review post-processing for run %s, PR #%d",
            self._run_id,
            self._pr_number,
            extra={"run_id": self._run_id, "pr_number": self._pr_number},
        )

        # 1. Parse the structured review output.
        review_data = self._parse_review_output(session_log)

        # 2. Submit the PR review.
        review_url = await self._submit_review(review_data)

        # 3. Approve the PR if assessment is "approve".
        pr_approved = False
        if review_data["assessment"] == "approve":
            pr_approved = await self._approve_pr()

        result: dict[str, Any] = {
            "review_url": review_url,
            "assessment": review_data["assessment"],
            "review_comments": review_data["review_comments"],
            "suggested_changes": review_data["suggested_changes"],
            "security_concerns": review_data["security_concerns"],
            "pr_approved": pr_approved,
            "session_summary": (
                session_log.final_message or "Agent review session completed."
            ),
            "agent_log_url": None,
        }

        logger.info(
            "Review post-processing complete for run %s, PR #%d — "
            "assessment=%s, approved=%s, comments=%d",
            self._run_id,
            self._pr_number,
            review_data["assessment"],
            pr_approved,
            len(review_data["review_comments"]),
            extra={
                "run_id": self._run_id,
                "pr_number": self._pr_number,
                "assessment": review_data["assessment"],
            },
        )
        return result

    # ------------------------------------------------------------------
    # PR metadata and diff
    # ------------------------------------------------------------------

    async def _fetch_pr_metadata(self) -> dict[str, Any]:
        """Fetch PR metadata via ``gh pr view``.

        Retrieves the PR's head and base branch names, title, body, and
        change statistics (additions, deletions, changed files).

        Returns:
            A dictionary with PR metadata fields.

        Raises:
            AgentExecutionError: If the ``gh`` command fails, with
                ``error_code="PR_PRE_PROCESS_ERROR"``.
        """
        output = await self._run_cmd_async(
            "gh",
            "pr",
            "view",
            str(self._pr_number),
            "--json",
            "headRefName,baseRefName,title,body,additions,deletions,changedFiles",
        )
        try:
            metadata: dict[str, Any] = json.loads(output.strip())
        except json.JSONDecodeError as exc:
            raise AgentExecutionError(
                error_message=(
                    f"Failed to parse PR #{self._pr_number} metadata JSON: {exc}"
                ),
                error_code="PR_PRE_PROCESS_ERROR",
                error_details={
                    "raw_output": output[:500],
                    "exception_type": type(exc).__name__,
                },
            ) from exc

        logger.info(
            "Fetched PR #%d metadata: title=%s, +%d/-%d, %d files",
            self._pr_number,
            metadata.get("title", ""),
            metadata.get("additions", 0),
            metadata.get("deletions", 0),
            metadata.get("changedFiles", 0),
            extra={"run_id": self._run_id, "pr_number": self._pr_number},
        )
        return metadata

    async def _fetch_pr_diff(self) -> str:
        """Fetch the full PR diff via ``gh pr diff``.

        If the diff exceeds ``_MAX_DIFF_SIZE`` characters, it is truncated
        with a descriptive note appended.

        Returns:
            The diff string, potentially truncated.
        """
        diff = await self._run_cmd_async(
            "gh",
            "pr",
            "diff",
            str(self._pr_number),
        )

        total_lines = diff.count("\n")
        if len(diff) > _MAX_DIFF_SIZE:
            truncated = diff[:_MAX_DIFF_SIZE]
            truncated += f"\n\n... [diff truncated — {total_lines} total lines changed]"
            logger.info(
                "PR diff truncated for run %s: %d chars → %d chars " "(%d total lines)",
                self._run_id,
                len(diff),
                _MAX_DIFF_SIZE,
                total_lines,
                extra={"run_id": self._run_id},
            )
            return truncated

        logger.debug(
            "PR diff for run %s: %d chars, %d lines",
            self._run_id,
            len(diff),
            total_lines,
            extra={"run_id": self._run_id},
        )
        return diff

    # ------------------------------------------------------------------
    # Instruction enrichment
    # ------------------------------------------------------------------

    def _enrich_instructions(self) -> str:
        """Enrich the agent instructions with PR context.

        Prepends PR metadata (title, description, statistics) and a
        summary of the diff to the caller's original instructions.

        Returns:
            The enriched instruction string for the agent.
        """
        pr_title = self._pr_metadata.get("title", "Untitled PR")
        pr_body = self._pr_metadata.get("body", "") or ""
        additions = self._pr_metadata.get("additions", 0)
        deletions = self._pr_metadata.get("deletions", 0)
        changed_files = self._pr_metadata.get("changedFiles", 0)

        # Build the enriched instruction string.
        sections: list[str] = [
            f"## Pull Request Context",
            f"",
            f"**PR #{self._pr_number}**: {pr_title}",
            f"**Base branch**: {self._base_ref}",
            f"**Head branch**: {self._head_ref}",
            f"**Changes**: +{additions}/-{deletions} across {changed_files} files",
        ]

        if pr_body.strip():
            sections.extend(["", "### PR Description", "", pr_body.strip()])

        sections.extend(
            [
                "",
                "## PR Diff",
                "",
                "```diff",
                self._pr_diff,
                "```",
                "",
                "## Review Instructions",
                "",
                self._agent_instructions,
            ]
        )

        return "\n".join(sections)

    # ------------------------------------------------------------------
    # Review output parsing
    # ------------------------------------------------------------------

    def _parse_review_output(self, session_log: AgentSessionLog) -> dict[str, Any]:
        """Extract the structured review from the agent's output.

        Uses a two-tier parsing strategy:

        1. **JSON extraction** — scans the agent's messages for a JSON code
           block (````json ... `````) containing the review structure. This
           is the most reliable path when the agent follows the structured
           output instructions.
        2. **Text heuristic fallback** — if no valid JSON is found, parses
           the text for assessment keywords, file-level comments, and
           security concerns.

        The ``assessment`` field defaults to ``"comment"`` when parsing is
        ambiguous.

        Args:
            session_log: The session log containing assistant messages.

        Returns:
            A dictionary with keys: ``assessment``, ``review_comments``,
            ``suggested_changes``, ``security_concerns``.
        """
        # Attempt JSON extraction from all messages (prefer the last one).
        for message in reversed(session_log.messages):
            content = str(message.get("content", "") or "")
            parsed = self._extract_json_review(content)
            if parsed is not None:
                logger.info(
                    "Parsed structured JSON review for run %s — "
                    "assessment=%s, %d comments, %d suggestions, %d concerns",
                    self._run_id,
                    parsed["assessment"],
                    len(parsed["review_comments"]),
                    len(parsed["suggested_changes"]),
                    len(parsed["security_concerns"]),
                    extra={"run_id": self._run_id},
                )
                return parsed

        # Fallback to text heuristic parsing.
        logger.warning(
            "No structured JSON review found in session log for run %s — "
            "falling back to text heuristic parsing",
            self._run_id,
            extra={"run_id": self._run_id},
        )
        return self._parse_review_text_fallback(session_log)

    def _extract_json_review(self, text: str) -> dict[str, Any] | None:
        """Attempt to extract a structured review from a JSON code block.

        Searches the text for a JSON block delimited by ````json ... `````
        and parses it into the expected review structure. Validates the
        ``assessment`` value and normalises the field names.

        Args:
            text: The assistant message text to search.

        Returns:
            A dictionary with normalised review fields, or ``None`` if no
            valid JSON review is found.
        """
        # Find JSON blocks.
        json_pattern = re.compile(r"```json\s*\n(.*?)\n\s*```", re.DOTALL)
        matches = json_pattern.findall(text)

        for json_str in matches:
            try:
                data = json.loads(json_str)
            except json.JSONDecodeError:
                continue

            if not isinstance(data, dict):
                continue

            # Check for expected review fields.
            if "assessment" not in data:
                continue

            return self._normalise_review_data(data)

        return None

    def _normalise_review_data(self, data: dict[str, Any]) -> dict[str, Any]:
        """Normalise parsed review data into the expected structure.

        Ensures the ``assessment`` value is one of the valid choices, and
        that list fields are present with correct types.

        Args:
            data: The raw parsed JSON dictionary.

        Returns:
            A normalised dictionary with all expected review fields.
        """
        # Normalise assessment.
        raw_assessment = str(data.get("assessment", "comment")).lower().strip()
        # Map common variants.
        assessment_map: dict[str, str] = {
            "approve": "approve",
            "approved": "approve",
            "request_changes": "request_changes",
            "request changes": "request_changes",
            "changes_requested": "request_changes",
            "comment": "comment",
        }
        assessment = assessment_map.get(raw_assessment, "comment")

        # Normalise review comments.
        raw_comments = data.get("review_comments", [])
        review_comments: list[dict[str, Any]] = []
        if isinstance(raw_comments, list):
            for comment in raw_comments:
                if isinstance(comment, dict) and "body" in comment:
                    review_comments.append(
                        {
                            "file_path": str(comment.get("file_path", "")),
                            "line": comment.get("line"),
                            "body": str(comment.get("body", "")),
                        }
                    )

        # Normalise suggested changes.
        raw_suggestions = data.get("suggested_changes", [])
        suggested_changes: list[dict[str, Any]] = []
        if isinstance(raw_suggestions, list):
            for suggestion in raw_suggestions:
                if isinstance(suggestion, dict) and "suggested_code" in suggestion:
                    suggested_changes.append(
                        {
                            "file_path": str(suggestion.get("file_path", "")),
                            "start_line": suggestion.get("start_line", 0),
                            "end_line": suggestion.get("end_line", 0),
                            "suggested_code": str(suggestion.get("suggested_code", "")),
                        }
                    )

        # Normalise security concerns.
        raw_concerns = data.get("security_concerns", [])
        security_concerns: list[str] = []
        if isinstance(raw_concerns, list):
            security_concerns = [str(c) for c in raw_concerns if c]

        return {
            "assessment": assessment,
            "review_comments": review_comments,
            "suggested_changes": suggested_changes,
            "security_concerns": security_concerns,
        }

    def _parse_review_text_fallback(
        self, session_log: AgentSessionLog
    ) -> dict[str, Any]:
        """Parse review output using text heuristics when JSON is unavailable.

        Scans all assistant messages for assessment keywords, file-level
        comment patterns, and security concern patterns.

        Args:
            session_log: The session log containing assistant messages.

        Returns:
            A dictionary with review fields populated from heuristic parsing.
            ``assessment`` defaults to ``"comment"`` if ambiguous.
        """
        assessment = "comment"
        review_comments: list[dict[str, Any]] = []
        security_concerns: list[str] = []

        all_text = "\n".join(
            str(m.get("content", "") or "") for m in session_log.messages
        )

        # Determine assessment from keywords.
        lower_text = all_text.lower()
        if re.search(r"\b(request[_\s]changes|changes?\s+requested)\b", lower_text):
            assessment = "request_changes"
        elif re.search(r"\bapprove[ds]?\b", lower_text):
            # Only set approve if there are no blocking concern indicators.
            if not re.search(r"\b(block|must\s+fix|critical|reject)\b", lower_text):
                assessment = "approve"

        # Extract file-level comments: patterns like "**path/to/file.py**: ..."
        # or "- `path/to/file.py` (line 42): ..."
        file_comment_pattern = re.compile(
            r"(?:\*\*|`)([\w./\\-]+\.(?:py|js|ts|tsx|jsx|go|rs|java|rb|md|yml|yaml|json))"
            r"(?:\*\*|`)"
            r"(?:\s*\(line\s*(\d+)\))?\s*[:—]\s*(.+?)(?:\n|$)",
            re.IGNORECASE,
        )
        for match in file_comment_pattern.finditer(all_text):
            file_path = match.group(1)
            line = int(match.group(2)) if match.group(2) else None
            body = match.group(3).strip()
            if body:
                review_comments.append(
                    {"file_path": file_path, "line": line, "body": body}
                )

        # Extract security concerns.
        security_pattern = re.compile(
            r"(?:security\s*(?:concern|issue|warning|vulnerability|finding)"
            r"|CVE-\d{4}-\d+"
            r"|(?:potential|possible)\s+(?:injection|xss|csrf|ssrf))"
            r"\s*[:—]?\s*(.+?)(?:\n|$)",
            re.IGNORECASE,
        )
        for match in security_pattern.finditer(all_text):
            concern = match.group(0).strip()
            if concern:
                security_concerns.append(concern)

        return {
            "assessment": assessment,
            "review_comments": review_comments,
            "suggested_changes": [],
            "security_concerns": security_concerns,
        }

    # ------------------------------------------------------------------
    # PR review submission
    # ------------------------------------------------------------------

    async def _submit_review(self, review_data: dict[str, Any]) -> str | None:
        """Submit the PR review via the GitHub API.

        Maps the ``assessment`` to a GitHub review event (``APPROVE``,
        ``REQUEST_CHANGES``, or ``COMMENT``) and submits the review with
        a body built from the review comments.

        When the initial submission fails with an ``APPROVE`` or
        ``REQUEST_CHANGES`` event (e.g., GitHub rejects self-approval with
        HTTP 422), the method automatically retries with a ``COMMENT`` event
        so that the review content is still posted to the PR.

        Review submission failure is **non-fatal** — the error is logged
        but the method returns ``None`` instead of raising.

        Args:
            review_data: The parsed review data with ``assessment``,
                ``review_comments``, ``suggested_changes``, and
                ``security_concerns``.

        Returns:
            The review URL if submission succeeds, or ``None`` on failure.
        """
        if not self._repository:
            logger.warning(
                "Cannot submit PR review for run %s — no repository specified",
                self._run_id,
                extra={"run_id": self._run_id},
            )
            return None

        # Map assessment to GitHub review event.
        event = self._map_assessment_to_event(review_data["assessment"])

        # Build review body.
        body = self._build_review_body(review_data)

        url = await self._post_review(event, body)

        # If the primary event failed and was not already COMMENT, retry as
        # COMMENT so that the review content is still posted to the PR.
        if url is None and event != "COMMENT":
            logger.warning(
                "Retrying review submission as COMMENT for run %s, PR #%d "
                "(original event %s was rejected)",
                self._run_id,
                self._pr_number,
                event,
                extra={
                    "run_id": self._run_id,
                    "pr_number": self._pr_number,
                    "original_event": event,
                },
            )
            url = await self._post_review("COMMENT", body)

        return url

    async def _post_review(self, event: str, body: str) -> str | None:
        """Post a single review API call to GitHub.

        Args:
            event: The GitHub review event (``APPROVE``, ``REQUEST_CHANGES``,
                or ``COMMENT``).
            body: The Markdown review body.

        Returns:
            The review URL if submission succeeds, or ``None`` on failure.
        """
        try:
            review_payload: dict[str, Any] = {
                "event": event,
                "body": body,
            }
            payload_json = json.dumps(review_payload)

            output = await self._run_cmd_async(
                "gh",
                "api",
                f"repos/{self._repository}/pulls/{self._pr_number}/reviews",
                "--method",
                "POST",
                "--input",
                "-",
                stdin_input=payload_json,
            )

            # Parse response for review URL.
            try:
                response = json.loads(output.strip())
                review_url = response.get("html_url", "")
                logger.info(
                    "PR review submitted for run %s, PR #%d: %s (event=%s)",
                    self._run_id,
                    self._pr_number,
                    review_url,
                    event,
                    extra={
                        "run_id": self._run_id,
                        "pr_number": self._pr_number,
                        "event": event,
                    },
                )
                return review_url or None
            except json.JSONDecodeError:
                logger.warning(
                    "Could not parse review submission response for run %s",
                    self._run_id,
                    extra={"run_id": self._run_id},
                )
                return None

        except AgentExecutionError as exc:
            logger.error(
                "Failed to submit PR review for run %s, PR #%d: %s",
                self._run_id,
                self._pr_number,
                exc.error_message,
                extra={
                    "run_id": self._run_id,
                    "pr_number": self._pr_number,
                    "error_code": exc.error_code,
                },
            )
            return None
        except Exception as exc:
            logger.error(
                "Unexpected error submitting PR review for run %s, PR #%d: %s",
                self._run_id,
                self._pr_number,
                exc,
                extra={"run_id": self._run_id, "pr_number": self._pr_number},
            )
            return None

    @staticmethod
    def _map_assessment_to_event(assessment: str) -> str:
        """Map the review assessment to a GitHub API review event.

        Args:
            assessment: One of ``"approve"``, ``"request_changes"``,
                ``"comment"``.

        Returns:
            The corresponding GitHub review event string.
        """
        event_map: dict[str, str] = {
            "approve": "APPROVE",
            "request_changes": "REQUEST_CHANGES",
            "comment": "COMMENT",
        }
        return event_map.get(assessment, "COMMENT")

    def _build_review_body(self, review_data: dict[str, Any]) -> str:
        """Build the review body text from the parsed review data.

        Combines review comments, suggested changes, and security concerns
        into a formatted Markdown string suitable for the GitHub review body.

        Args:
            review_data: The parsed review data dictionary.

        Returns:
            A Markdown-formatted review body string.
        """
        sections: list[str] = [
            f"## Copilot Dispatch Code Review (run: {self._run_id})",
            "",
            f"**Assessment**: {review_data['assessment']}",
            "",
        ]

        # Review comments.
        comments = review_data.get("review_comments", [])
        if comments:
            sections.append("### Review Comments")
            sections.append("")
            for comment in comments:
                file_path = comment.get("file_path", "")
                line = comment.get("line")
                body = comment.get("body", "")
                if line:
                    sections.append(f"- **{file_path}** (line {line}): {body}")
                else:
                    sections.append(f"- **{file_path}**: {body}")
            sections.append("")

        # Suggested changes.
        suggestions = review_data.get("suggested_changes", [])
        if suggestions:
            sections.append("### Suggested Changes")
            sections.append("")
            for suggestion in suggestions:
                file_path = suggestion.get("file_path", "")
                start = suggestion.get("start_line", 0)
                end = suggestion.get("end_line", 0)
                code = suggestion.get("suggested_code", "")
                sections.append(f"**{file_path}** (lines {start}–{end}):")
                sections.append("```")
                sections.append(code)
                sections.append("```")
                sections.append("")

        # Security concerns.
        concerns = review_data.get("security_concerns", [])
        if concerns:
            sections.append("### Security Concerns")
            sections.append("")
            for concern in concerns:
                sections.append(f"- {concern}")
            sections.append("")

        sections.append("---")
        sections.append(f"*Generated by Copilot Dispatch (run: {self._run_id})*")

        return "\n".join(sections)

    # ------------------------------------------------------------------
    # PR approval
    # ------------------------------------------------------------------

    async def _approve_pr(self) -> bool:
        """Approve the pull request via ``gh pr review --approve``.

        This is a separate step from :meth:`_submit_review` because
        approval is a distinct GitHub API action that can fail
        independently (e.g., the PAT user may not have approval permissions).

        Returns:
            ``True`` if approval succeeds, ``False`` otherwise.
        """
        try:
            await self._run_cmd_async(
                "gh",
                "pr",
                "review",
                str(self._pr_number),
                "--approve",
            )
            logger.info(
                "PR #%d approved for run %s",
                self._pr_number,
                self._run_id,
                extra={"run_id": self._run_id, "pr_number": self._pr_number},
            )
            return True
        except AgentExecutionError as exc:
            logger.error(
                "Failed to approve PR #%d for run %s: %s",
                self._pr_number,
                self._run_id,
                exc.error_message,
                extra={
                    "run_id": self._run_id,
                    "pr_number": self._pr_number,
                    "error_code": exc.error_code,
                },
            )
            return False
        except Exception as exc:
            logger.error(
                "Unexpected error approving PR #%d for run %s: %s",
                self._pr_number,
                self._run_id,
                exc,
                extra={"run_id": self._run_id, "pr_number": self._pr_number},
            )
            return False

    # ------------------------------------------------------------------
    # System message / skill path helpers
    # ------------------------------------------------------------------

    def build_system_message(self) -> str:
        """Build the fully-rendered system message for the review role.

        Uses :class:`~app.src.agent.prompts.PromptLoader` to load the
        review template and substitute placeholders with
        ``system_instructions`` and repo-level instructions.

        Returns:
            The rendered system message string.
        """
        loader = PromptLoader()
        return loader.build_system_message(
            role="review",
            system_instructions=self._system_instructions,
            repo_path=self._repo_path,
        )

    def resolve_skill_directories(self) -> list[str]:
        """Resolve skill paths to absolute filesystem paths.

        Uses :meth:`PromptLoader.resolve_skill_paths` to resolve relative
        paths against the repo checkout directory.

        Returns:
            List of absolute path strings for valid skill files.
        """
        return PromptLoader.resolve_skill_paths(self._skill_paths, self._repo_path)

    # ------------------------------------------------------------------
    # Command execution helpers
    # ------------------------------------------------------------------

    async def _run_git(self, *args: str) -> str:
        """Run a git command asynchronously in the repository directory.

        Args:
            *args: Git sub-command and arguments (e.g., ``"checkout"``,
                ``"main"``).

        Returns:
            The stdout output of the command.

        Raises:
            AgentExecutionError: On non-zero exit code, with
                ``error_code="PR_PRE_PROCESS_ERROR"``.
        """
        return await self._run_cmd_async("git", *args)

    async def _run_cmd_async(
        self,
        *args: str,
        stdin_input: str | None = None,
    ) -> str:
        """Run a command asynchronously and return stdout.

        Args:
            *args: The command and its arguments.
            stdin_input: Optional string to pass to the process's stdin.

        Returns:
            The stdout output of the command as a string.

        Raises:
            AgentExecutionError: On non-zero exit code, with the command's
                stderr included in ``error_details``.
        """
        cmd_str = " ".join(args)
        logger.debug(
            "Running command for run %s: %s",
            self._run_id,
            cmd_str,
            extra={"run_id": self._run_id},
        )

        stdin_pipe = asyncio.subprocess.PIPE if stdin_input else None
        process = await asyncio.create_subprocess_exec(
            *args,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            stdin=stdin_pipe,
            cwd=str(self._repo_path),
        )

        if stdin_input:
            stdout_bytes, stderr_bytes = await process.communicate(
                input=stdin_input.encode("utf-8")
            )
        else:
            stdout_bytes, stderr_bytes = await process.communicate()

        stdout = stdout_bytes.decode("utf-8", errors="replace")
        stderr = stderr_bytes.decode("utf-8", errors="replace")

        if process.returncode != 0:
            error_msg = f"Command failed with exit code {process.returncode}: {cmd_str}"
            logger.error(
                "%s\nstderr: %s",
                error_msg,
                stderr,
                extra={"run_id": self._run_id},
            )
            # Determine error code based on the command.
            error_code = "PR_PRE_PROCESS_ERROR"
            if args[0] == "gh" and "review" in args:
                error_code = "PR_REVIEW_SUBMIT_ERROR"
            elif args[0] == "gh":
                error_code = "PR_PRE_PROCESS_ERROR"

            raise AgentExecutionError(
                error_message=error_msg,
                error_code=error_code,
                error_details={
                    "command": cmd_str,
                    "exit_code": process.returncode,
                    "stderr": stderr.strip(),
                    "stdout": stdout.strip(),
                },
            )

        return stdout
