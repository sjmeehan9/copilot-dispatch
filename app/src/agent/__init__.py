"""Agent integration package.

Handles the construction and rendering of role-specific system prompts that
are passed to the Copilot SDK session. Prompt templates are loaded from
``app/config/prompts/`` and enriched with runtime variables before dispatch.
"""
