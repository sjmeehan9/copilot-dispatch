"""End-to-end test package for Copilot Dispatch.

These tests validate complete system journeys — from API request through
agent workflow dispatch, execution, and result delivery. They require live
external infrastructure (GitHub, running API service, agent executor) and
are gated behind the ``--e2e-confirm`` pytest flag.

Run with::

    pytest tests/e2e/ --e2e-confirm

"""
