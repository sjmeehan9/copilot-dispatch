"""API layer package.

Defines all FastAPI routers, request/response schemas, and endpoint handlers
for the Copilot Dispatch service. The primary entry points are:

- ``POST /agent/run`` — dispatch a Copilot agent workflow
- ``GET  /agent/run/{run_id}`` — poll the result of a dispatched run
- ``GET  /health`` — liveness and readiness probe
"""
