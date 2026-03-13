"""Role-specific agent logic for implement, review, and merge roles.

Each role defines pre-processing (repository setup), execution (agent
invocation via :class:`~app.src.agent.runner.AgentRunner`), and
post-processing (result collection, PR creation/review/merge) steps.

The :class:`RoleHandler` protocol specifies the interface every role module
must satisfy. Concrete implementations live in submodules:

- :mod:`~app.src.agent.roles.implement` — branch creation, code changes, PR creation
- :mod:`~app.src.agent.roles.review` — PR diff analysis, structured review, approval
- :mod:`~app.src.agent.roles.merge` — merge attempt, conflict resolution, merge execution
"""

from __future__ import annotations

from typing import Any, Protocol, runtime_checkable

from app.src.agent.runner import AgentRunner, AgentSessionLog


@runtime_checkable
class RoleHandler(Protocol):
    """Protocol defining the interface for role-specific agent logic.

    Every role handler must implement three lifecycle methods that are
    called in sequence by the orchestrator:

    1. :meth:`pre_process` — prepare the repository and environment.
    2. :meth:`execute` — run the Copilot SDK agent via ``AgentRunner``.
    3. :meth:`post_process` — collect results and create artefacts.

    Example::

        role: RoleHandler = ImplementRole(...)
        await role.pre_process()
        session_log = await role.execute(runner)
        result = await role.post_process(session_log)
    """

    async def pre_process(self) -> None:
        """Prepare the repository for the agent run.

        This step handles git operations, branch creation, PR checkout,
        or merge preparation depending on the role.

        Raises:
            AgentExecutionError: If preparation fails (e.g., git errors).
        """
        ...

    async def execute(self, runner: AgentRunner) -> AgentSessionLog:
        """Execute the Copilot SDK agent for this role.

        Builds the system message, enriches instructions, and delegates
        to the :class:`AgentRunner` for session lifecycle management.

        Args:
            runner: A configured :class:`AgentRunner` instance.

        Returns:
            The :class:`AgentSessionLog` produced by the agent session.
        """
        ...

    async def post_process(self, session_log: AgentSessionLog) -> dict[str, Any]:
        """Collect results and create role-specific artefacts.

        After agent execution, this step pushes branches, creates PRs,
        submits reviews, or executes merges depending on the role.

        Args:
            session_log: The session log from the completed agent run.

        Returns:
            A dictionary containing all fields required to construct the
            role-specific result model (e.g., ``ImplementResult``).
        """
        ...


__all__ = ["RoleHandler"]
