# Solution Design: Copilot Dispatch — Copilot Agent Application

## Executive Summary

Copilot Dispatch is a lightweight API service that wraps the GitHub Copilot SDK, enabling programmatic execution of coding agent tasks (implement, review, merge) against any accessible GitHub repository. The API receives requests, dispatches GitHub Actions workflows that run the Copilot SDK agent, and delivers results asynchronously via webhook with a synchronous polling fallback. The architecture is intentionally lean — a single FastAPI container on AppRunner, a DynamoDB table for run tracking, and a centralised GitHub Actions workflow — designed for rapid deployment by a single operator with a clear path to broader distribution later.

---

## Architecture Overview

### High-Level Architecture

```
┌──────────────┐     POST /agent/run      ┌──────────────────────┐
│              │ ─────────────────────────▶│                      │
│   Caller /   │     202 { run_id }       │   API Service        │
│   Consumer   │ ◀─────────────────────── │   (FastAPI on        │
│              │                          │    AppRunner)         │
│              │     GET /agent/run/{id}  │                      │
│              │ ─────────────────────────▶│         │            │
│              │     200 { result }       │         ▼            │
│              │ ◀─────────────────────── │   ┌──────────┐      │
│              │                          │   │ DynamoDB  │      │
│              │   ◀── webhook POST ───── │   └──────────┘      │
└──────────────┘                          └──────────┬───────────┘
                                                     │
                                          workflow_dispatch
                                                     │
                                                     ▼
                                          ┌──────────────────────┐
                                          │  GitHub Actions      │
                                          │  (agent-executor)    │
                                          │                      │
                                          │  ┌────────────────┐  │
                                          │  │ Copilot SDK    │  │
                                          │  │ Agent Session  │  │
                                          │  └────────────────┘  │
                                          │         │            │
                                          │   POST result back   │
                                          │   to API service     │
                                          └──────────────────────┘
```

- **API Service (FastAPI on AppRunner)**: Receives requests, validates payloads, dispatches GitHub Actions workflows, stores run state in DynamoDB, receives results from completed workflows, and delivers webhooks to callers.
- **Agent Executor (GitHub Actions Workflow)**: A centralised workflow in this repository that checks out the target repo, sets up the environment, runs the Copilot SDK agent for the specified role, and posts the result back to the API.
- **DynamoDB**: Single table storing run state, request parameters, and results. Enables the polling endpoint and provides an audit trail.
- **Secrets Manager**: Stores API key, GitHub PAT, and webhook signing secret.

### Architecture Principles

- **Lean-first**: Minimise AWS services and operational overhead. Single-user optimised with a modular structure that supports future scale.
- **Stateless API, stateful store**: The API service is stateless (restartable, replaceable). All persistent state lives in DynamoDB.
- **Central orchestration**: One workflow in one repo. Target repositories require no modification.
- **Async execution, sync fallback**: Primary result delivery via webhook; polling endpoint as fallback.
- **Configuration over code**: Role prompts, timeouts, and defaults are externalised as configuration files.

---

## Technology Stack

### API Service

- **Framework**: FastAPI — *Rationale: Async-native, lightweight, Pydantic-first, same Python ecosystem as Copilot SDK. Minimal boilerplate for the 4 endpoints needed.*
- **Language**: Python 3.13+
- **ASGI Server**: Uvicorn
- **HTTP Client**: httpx (async) — *Rationale: Async HTTP client for GitHub API calls and webhook delivery. Matches FastAPI's async patterns.*
- **Data Validation**: Pydantic v2 — *Rationale: Required by project standards. FastAPI-native.*
- **AWS SDK**: boto3 — *Rationale: DynamoDB and Secrets Manager access. Standard AWS SDK for Python.*
- **Container Runtime**: Docker (Python 3.13-slim base)

### Agent Executor

- **Language**: Python 3.13+
- **Copilot SDK**: `github-copilot-sdk` (Python package)
- **GitHub CLI**: `gh` — *Rationale: Used by Copilot SDK for auth and by agent runner for PR operations.*
- **GitHub API Client**: PyGithub or `gh` CLI — *Rationale: PR creation, review submission, merge operations. `gh` CLI is already required for Copilot SDK auth, so prefer it to avoid an extra dependency.*

### Database

- **Primary Database**: DynamoDB (on-demand capacity) — *Rationale: Serverless, zero maintenance, free tier covers single-user volume. Simple key-value access pattern is a perfect fit. No connection management needed.*

### Infrastructure

- **IaC**: AWS CDK (Python) — *Rationale: User preference. Python consistency across the stack.*
- **Compute**: AWS AppRunner — *Rationale: Zero-config container hosting with HTTPS, auto-scaling, and scale-to-zero support. Simplest path to a deployed API.*
- **Container Registry**: Amazon ECR — *Rationale: Required for AppRunner source. Private registry within the same AWS account.*
- **Secrets**: AWS Secrets Manager — *Rationale: User preference. Managed rotation, audit trail, native AppRunner integration for environment variable injection.*
- **CI/CD**: GitHub Actions — *Rationale: Already the execution platform for the agent. Natural fit for build/deploy pipeline.*

### DevOps

- **CI/CD**: GitHub Actions (build → test → push to ECR → CDK deploy)
- **Monitoring**: CloudWatch (AppRunner built-in metrics + DynamoDB metrics) — *Rationale: No additional service needed. AppRunner auto-publishes to CloudWatch.*
- **Logging**: CloudWatch Logs (AppRunner auto-streams stdout/stderr) — *Rationale: Zero config. Sufficient for single user.*

---

## System Components

### Component: API Service

- **Purpose**: HTTP interface for creating agent runs, retrieving results, and ingesting workflow results.
- **Technology**: FastAPI, Uvicorn, boto3, httpx
- **Responsibilities**:
  - Validate incoming request payloads
  - Generate unique `run_id` (UUID v4)
  - Store run record in DynamoDB with status `dispatched`
  - Dispatch GitHub Actions workflow via GitHub REST API
  - Serve run state and results via polling endpoint
  - Receive results from completed workflows (HMAC-verified)
  - Forward results to caller's `callback_url` with retries
- **Interfaces**:
  - **Inputs**: HTTP requests from callers; HTTP POST from workflow with results
  - **Outputs**: HTTP responses; webhook POST to `callback_url`; DynamoDB writes
- **Dependencies**: DynamoDB, Secrets Manager, GitHub API
- **Scaling Strategy**: AppRunner auto-scaling (not needed for single user; configured to 1 min / 1 max instance)

### Component: Agent Executor (GitHub Actions Workflow)

- **Purpose**: Execute the Copilot SDK agent against a target repository for a given role.
- **Technology**: GitHub Actions, Python 3.13, Copilot SDK, `gh` CLI
- **Responsibilities**:
  - Receive workflow dispatch inputs (run_id, repository, branch, role, instructions, model, etc.)
  - Check out the target repository at the specified branch
  - Detect and install language runtimes and project dependencies
  - Initialise the Copilot SDK client and create a session
  - Execute the agent loop with the role-specific system prompt
  - Compile structured result payload
  - POST status update (`running`) to API at workflow startup
  - POST result (or error) back to the API service on completion or failure
  - Execute a failure handler step (`if: failure() || cancelled()`) that POSTs an error payload to the API if the main execution step crashes or the job is cancelled, ensuring the caller is never left without status
- **Interfaces**:
  - **Inputs**: `workflow_dispatch` event with JSON inputs
  - **Outputs**: HTTP POST to API result endpoint (status updates, results, and errors); Git operations on target repo
- **Dependencies**: GitHub API, Copilot SDK, target repository
- **Scaling Strategy**: GitHub Actions handles runner provisioning. Concurrent runs limited by GitHub Actions plan (20 concurrent jobs on free/pro).

### Component: Agent Runner Script

- **Purpose**: Python script executed within the Actions workflow that manages the Copilot SDK session lifecycle.
- **Technology**: Python 3.13, Copilot SDK
- **Responsibilities**:
  - Load role-specific system prompt template and merge with caller's `system_instructions`
  - Initialise `CopilotClient` and start the CLI server
  - Create a session with the specified model, system message, and optional skill directories
  - Send agent instructions and manage the agent loop
  - Subscribe to session events for logging (tool calls, errors, messages)
  - Execute post-agent operations (push branch, create PR, submit review, merge)
  - Run security validation steps (for implement role)
  - Compile the result payload with all role-specific fields
  - Handle timeouts and errors gracefully
- **Interfaces**:
  - **Inputs**: Environment variables and CLI arguments from the workflow
  - **Outputs**: Structured JSON result; Git operations; GitHub API calls
- **Dependencies**: Copilot SDK, `gh` CLI, target repo filesystem

### Component: Actions Visibility Service

- **Purpose**: Provide read-only visibility into GitHub Actions workflows and runs on target repositories, plus the ability to trigger workflow dispatches.
- **Technology**: Python, httpx (async), Pydantic v2
- **Responsibilities**:
  - List available workflows for a given repository
  - Retrieve workflow run history with optional filters (branch, status, actor, event, per_page)
  - Retrieve detailed information for a specific workflow run, including jobs and steps
  - Trigger a workflow dispatch on a target repository
  - Map GitHub REST API responses to typed Pydantic response models
  - Handle GitHub API errors and translate to appropriate HTTP responses
- **Interfaces**:
  - **Inputs**: Repository owner/repo, optional query parameters, workflow ID and dispatch inputs
  - **Outputs**: Typed Pydantic models (workflow list, run list, run detail, dispatch acknowledgement)
- **Dependencies**: GitHub REST API (authenticated via PAT from Secrets Manager)
- **Scaling Strategy**: Stateless; scales with API service. GitHub API rate limits (5,000 req/hr) are the practical constraint.

### Component: Infrastructure Stack (CDK)

- **Purpose**: Define and deploy all AWS resources.
- **Technology**: AWS CDK (Python)
- **Responsibilities**:
  - Provision AppRunner service with ECR source
  - Create DynamoDB table
  - Create Secrets Manager secrets
  - Create ECR repository
  - Configure IAM roles and policies
  - Wire environment variables from Secrets Manager into AppRunner
- **Interfaces**:
  - **Inputs**: CDK context values (account, region, configuration)
  - **Outputs**: CloudFormation stack with all resources
- **Dependencies**: AWS account, CDK bootstrap

---

## Data Model

### DynamoDB Table: `dispatch-runs`

**Table Configuration:**
- Billing mode: On-demand (PAY_PER_REQUEST)
- Partition key: `run_id` (String)
- TTL attribute: `ttl` (auto-delete records after 90 days)
- No GSIs for MVP

**Item Schema:**

| Attribute | Type | Required | Description |
|-----------|------|----------|-------------|
| `run_id` | String (UUID) | PK | Unique identifier for the run |
| `repository` | String | Yes | Target repository (owner/repo) |
| `branch` | String | Yes | Base branch |
| `role` | String | Yes | `implement`, `review`, or `merge` |
| `status` | String | Yes | `dispatched`, `running`, `success`, `failure`, `timeout` |
| `model` | String | Yes | LLM model identifier |
| `agent_instructions` | String | Yes | Natural language task description |
| `system_instructions` | String | No | Additional system-level instructions |
| `pr_number` | Number | No | PR number (required for review/merge roles) |
| `callback_url` | String | No | Webhook delivery URL |
| `timeout_minutes` | Number | Yes | Max execution time (default: 30) |
| `created_at` | String (ISO 8601) | Yes | Run creation timestamp |
| `updated_at` | String (ISO 8601) | Yes | Last update timestamp |
| `result` | Map | No | Full result payload (written on completion) |
| `error` | Map | No | Structured error information (written on failure). Contains `error_code`, `error_message`, and optional `error_details`. |
| `ttl` | Number | Yes | Unix epoch for DynamoDB TTL (created_at + 90 days) |

**Example Item:**

```json
{
  "run_id": "a1b2c3d4-e5f6-7890-abcd-ef1234567890",
  "repository": "myorg/my-app",
  "branch": "main",
  "role": "implement",
  "status": "success",
  "model": "gpt-4.1",
  "agent_instructions": "Add input validation to the user registration endpoint",
  "callback_url": "https://my-system.example.com/webhooks/copilot-dispatch",
  "timeout_minutes": 30,
  "created_at": "2026-02-26T10:00:00Z",
  "updated_at": "2026-02-26T10:12:34Z",
  "result": {
    "pr_url": "https://github.com/myorg/my-app/pull/42",
    "pr_number": 42,
    "branch": "feature/a1b2c3d4",
    "commits": [{"sha": "abc123", "message": "Add validation to registration"}],
    "files_changed": ["src/routes/register.py", "tests/test_register.py"],
    "test_results": {"passed": 15, "failed": 0, "skipped": 2},
    "security_findings": [],
    "session_summary": "Added Pydantic validation models..."
  },
  "ttl": 1685000000
}
```

---

## API Design

### Authentication

All client-facing endpoints require `X-API-Key` header. The workflow result endpoint uses HMAC-SHA256 signature verification via `X-Webhook-Signature` header.

### Endpoint: Health Check

#### `GET /health`
- **Purpose**: AppRunner health check and liveness probe
- **Authentication**: None
- **Response**: `200 OK`
```json
{
  "status": "healthy"
}
```

### Endpoint Group: Agent Runs

#### `POST /agent/run`
- **Purpose**: Create a new agent run, dispatch the GitHub Actions workflow
- **Authentication**: API key required
- **Request Body**:
```json
{
  "repository": "owner/repo-name",
  "branch": "main",
  "agent_instructions": "Add input validation to the user registration endpoint",
  "model": "gpt-4.1",
  "role": "implement",
  "pr_number": null,
  "system_instructions": "Follow the project's existing Pydantic patterns. Ensure 80% test coverage.",
  "skill_paths": [".github/copilot-skills/backend.md"],
  "agent_paths": [".github/agents/security-reviewer.agent.md"],
  "callback_url": "https://my-system.example.com/webhooks/copilot-dispatch",
  "timeout_minutes": 30
}
```
- **Validation Rules**:
  - `repository`: Required. Must match `owner/repo` format.
  - `branch`: Required. Non-empty string.
  - `agent_instructions`: Required. Non-empty string.
  - `model`: Required. Non-empty string.
  - `role`: Required. One of `implement`, `review`, `merge`.
  - `pr_number`: Required when role is `review` or `merge`.
  - `agent_paths`: Optional list of markdown custom agent definition paths relative to the target repo root.
  - `timeout_minutes`: Optional. Default 30, min 1, max 60.

Custom agent markdown files in the target repository (for example
`.github/agents/*.agent.md`) are parsed by Copilot Dispatch and translated into SDK
`custom_agents` dictionaries before session creation.
- **Response**: `202 Accepted`
```json
{
  "run_id": "a1b2c3d4-e5f6-7890-abcd-ef1234567890",
  "status": "dispatched",
  "created_at": "2026-02-26T10:00:00Z"
}
```
- **Error Responses**:
  - `400 Bad Request` — Invalid payload (missing fields, invalid role, etc.)
  - `401 Unauthorized` — Missing or invalid API key
  - `422 Unprocessable Entity` — Validation error (e.g., `pr_number` missing for review role)
  - `500 Internal Server Error` — Workflow dispatch failure (GitHub API error)

#### `GET /agent/run/{run_id}`
- **Purpose**: Retrieve the current state and result of an agent run
- **Authentication**: API key required
- **Stale Run Detection**: If the run status is `dispatched` or `running` and `created_at + timeout_minutes` has elapsed, the API automatically updates the status to `timeout`, writes an error object (`error_code: STALE_RUN_TIMEOUT`, `error_message` describing the condition), and returns the updated record. This ensures callers are never left polling indefinitely for a run that silently failed.
- **Response (success)**: `200 OK`
```json
{
  "run_id": "a1b2c3d4-e5f6-7890-abcd-ef1234567890",
  "repository": "owner/repo-name",
  "branch": "main",
  "role": "implement",
  "status": "success",
  "model": "gpt-4.1",
  "created_at": "2026-02-26T10:00:00Z",
  "updated_at": "2026-02-26T10:12:34Z",
  "result": { ... },
  "error": null
}
```
- **Response (failure/error)**: `200 OK`
```json
{
  "run_id": "a1b2c3d4-e5f6-7890-abcd-ef1234567890",
  "repository": "owner/repo-name",
  "branch": "main",
  "role": "implement",
  "status": "failure",
  "model": "gpt-4.1",
  "created_at": "2026-02-26T10:00:00Z",
  "updated_at": "2026-02-26T10:05:12Z",
  "result": null,
  "error": {
    "error_code": "AGENT_CRASH",
    "error_message": "Copilot SDK session terminated unexpectedly.",
    "error_details": {
      "phase": "agent_execution",
      "last_event": "session.error",
      "agent_log_url": "https://github.com/owner/copilot-dispatch/actions/runs/12345"
    }
  }
}
```
- **Response (stale run detected)**: `200 OK`
```json
{
  "run_id": "a1b2c3d4-e5f6-7890-abcd-ef1234567890",
  "repository": "owner/repo-name",
  "branch": "main",
  "role": "implement",
  "status": "timeout",
  "model": "gpt-4.1",
  "created_at": "2026-02-26T10:00:00Z",
  "updated_at": "2026-02-26T10:35:00Z",
  "result": null,
  "error": {
    "error_code": "STALE_RUN_TIMEOUT",
    "error_message": "Run exceeded timeout of 30 minutes without reporting a result. The workflow may have crashed or been cancelled.",
    "error_details": {
      "timeout_minutes": 30,
      "detected_at": "2026-02-26T10:35:00Z"
    }
  }
}
```
- **Error Responses**:
  - `401 Unauthorized` — Missing or invalid API key
  - `404 Not Found` — Run ID does not exist

#### `POST /agent/run/{run_id}/result`
- **Purpose**: Receive execution results and status updates from the GitHub Actions workflow. Accepts both full result payloads (on success/failure completion) and lightweight status-only updates (to report `running` status at workflow startup or error-only payloads on crash).
- **Authentication**: HMAC-SHA256 signature (`X-Webhook-Signature` header)
- **Request Body (full result — success)**: Role-specific result payload (see Section 9.1 of brief for full schema)
```json
{
  "run_id": "a1b2c3d4-e5f6-7890-abcd-ef1234567890",
  "role": "implement",
  "status": "success",
  "model_used": "gpt-4.1",
  "duration_seconds": 734,
  "pr_url": "https://github.com/owner/repo/pull/42",
  "pr_number": 42,
  "branch": "feature/a1b2c3d4",
  "commits": [{"sha": "abc123", "message": "Add validation"}],
  "files_changed": ["src/routes/register.py"],
  "test_results": {"passed": 15, "failed": 0, "skipped": 2},
  "security_findings": [],
  "agent_log_url": "https://github.com/owner/copilot-dispatch/actions/runs/12345",
  "session_summary": "Added Pydantic validation..."
}
```
- **Request Body (status-only — running)**: Lightweight update sent by the workflow immediately after startup, before agent execution begins.
```json
{
  "run_id": "a1b2c3d4-e5f6-7890-abcd-ef1234567890",
  "status": "running"
}
```
- **Request Body (error — failure)**: Sent by the workflow failure handler when the agent runner crashes, the workflow step fails, or the job is cancelled. Contains error details without a full result.
```json
{
  "run_id": "a1b2c3d4-e5f6-7890-abcd-ef1234567890",
  "status": "failure",
  "error": {
    "error_code": "WORKFLOW_STEP_FAILURE",
    "error_message": "Agent runner exited with code 1: CopilotClient failed to start.",
    "error_details": {
      "phase": "sdk_initialisation",
      "exit_code": 1,
      "agent_log_url": "https://github.com/owner/copilot-dispatch/actions/runs/12345"
    }
  }
}
```
- **Behaviour**:
  1. Verify HMAC signature
  2. Update DynamoDB record: set `status` and `updated_at`; write `result` if present; write `error` if present
  3. If status is a terminal state (`success`, `failure`, `timeout`) and `callback_url` exists on the run record, forward the full record (including any `error`) as a webhook POST (with HMAC signature, up to 3 retries with exponential backoff). Status-only updates (`running`) do not trigger webhook delivery.
  4. Idempotency: If the run is already in a terminal state, ignore subsequent updates (prevents duplicate webhook delivery).
- **Response**: `200 OK`
- **Error Responses**:
  - `401 Unauthorized` — Invalid HMAC signature
  - `404 Not Found` — Run ID does not exist
  - `409 Conflict` — Run is already in a terminal state (result previously accepted)

---

### Endpoint Group: Actions Visibility

#### `GET /actions/{owner}/{repo}/workflows`
- **Purpose**: List all GitHub Actions workflows defined in the target repository
- **Authentication**: API key required
- **Path Parameters**:
  - `owner`: Repository owner (user or organisation)
  - `repo`: Repository name
- **Response**: `200 OK`
```json
{
  "total_count": 3,
  "workflows": [
    {
      "id": 12345,
      "name": "CI Pipeline",
      "path": ".github/workflows/ci.yml",
      "state": "active",
      "created_at": "2026-01-15T08:00:00Z",
      "updated_at": "2026-02-20T14:30:00Z",
      "html_url": "https://github.com/owner/repo/actions/workflows/ci.yml"
    }
  ]
}
```
- **Error Responses**:
  - `401 Unauthorized` — Missing or invalid API key
  - `404 Not Found` — Repository not found or not accessible with the configured PAT
  - `502 Bad Gateway` — GitHub API error

#### `GET /actions/{owner}/{repo}/runs`
- **Purpose**: List workflow runs for a repository with optional filters
- **Authentication**: API key required
- **Path Parameters**:
  - `owner`: Repository owner
  - `repo`: Repository name
- **Query Parameters** (all optional):
  - `branch`: Filter by branch name
  - `status`: Filter by status (`queued`, `in_progress`, `completed`, `action_required`, `cancelled`, `failure`, `neutral`, `skipped`, `stale`, `success`, `timed_out`, `waiting`)
  - `actor`: Filter by the user who triggered the run
  - `event`: Filter by event type (e.g., `push`, `pull_request`, `workflow_dispatch`)
  - `per_page`: Results per page (1-100, default 30)
- **Response**: `200 OK`
```json
{
  "total_count": 42,
  "workflow_runs": [
    {
      "id": 67890,
      "name": "CI Pipeline",
      "workflow_id": 12345,
      "head_branch": "main",
      "head_sha": "abc123def456",
      "status": "completed",
      "conclusion": "success",
      "event": "push",
      "actor": "sjmeehan9",
      "run_number": 42,
      "run_attempt": 1,
      "created_at": "2026-02-28T10:00:00Z",
      "updated_at": "2026-02-28T10:05:30Z",
      "html_url": "https://github.com/owner/repo/actions/runs/67890"
    }
  ]
}
```
- **Error Responses**:
  - `401 Unauthorized` — Missing or invalid API key
  - `404 Not Found` — Repository not found or not accessible
  - `502 Bad Gateway` — GitHub API error

#### `GET /actions/{owner}/{repo}/runs/{run_id}`
- **Purpose**: Get detailed information about a specific workflow run, including its jobs and steps
- **Authentication**: API key required
- **Path Parameters**:
  - `owner`: Repository owner
  - `repo`: Repository name
  - `run_id`: GitHub Actions workflow run ID
- **Response**: `200 OK`
```json
{
  "id": 67890,
  "name": "CI Pipeline",
  "workflow_id": 12345,
  "head_branch": "main",
  "head_sha": "abc123def456",
  "status": "completed",
  "conclusion": "success",
  "event": "push",
  "actor": "sjmeehan9",
  "run_number": 42,
  "run_attempt": 1,
  "created_at": "2026-02-28T10:00:00Z",
  "updated_at": "2026-02-28T10:05:30Z",
  "html_url": "https://github.com/owner/repo/actions/runs/67890",
  "jobs": [
    {
      "id": 11111,
      "name": "build",
      "status": "completed",
      "conclusion": "success",
      "started_at": "2026-02-28T10:00:15Z",
      "completed_at": "2026-02-28T10:03:45Z",
      "steps": [
        {
          "name": "Checkout code",
          "status": "completed",
          "conclusion": "success",
          "number": 1,
          "started_at": "2026-02-28T10:00:16Z",
          "completed_at": "2026-02-28T10:00:20Z"
        }
      ]
    }
  ]
}
```
- **Error Responses**:
  - `401 Unauthorized` — Missing or invalid API key
  - `404 Not Found` — Repository or run ID not found
  - `502 Bad Gateway` — GitHub API error

#### `POST /actions/{owner}/{repo}/workflows/{workflow_id}/dispatch`
- **Purpose**: Trigger a workflow dispatch event on the target repository
- **Authentication**: API key required
- **Path Parameters**:
  - `owner`: Repository owner
  - `repo`: Repository name
  - `workflow_id`: Workflow ID or workflow filename (e.g., `ci.yml`)
- **Request Body**:
```json
{
  "ref": "main",
  "inputs": {
    "environment": "staging",
    "dry_run": "true"
  }
}
```
- **Validation Rules**:
  - `ref`: Required. Non-empty string (branch, tag, or commit SHA).
  - `inputs`: Optional. Key-value pairs (string values only, per GitHub API requirement).
- **Response**: `204 No Content`
- **Error Responses**:
  - `400 Bad Request` — Invalid request body
  - `401 Unauthorized` — Missing or invalid API key
  - `404 Not Found` — Repository or workflow not found
  - `422 Unprocessable Entity` — Invalid ref or inputs
  - `502 Bad Gateway` — GitHub API error

---

## Security Design

### Authentication & Authorisation

| Boundary | Mechanism | Details |
|----------|-----------|---------|
| Caller → API | API key (`X-API-Key` header) | Single API key stored in Secrets Manager. Validated on every client-facing request. |
| API → GitHub | Personal Access Token (PAT) | Classic PAT with `repo` scope. Used for workflow dispatch. Stored in Secrets Manager (for API) and as a GitHub Actions secret (for workflow). |
| Workflow → API | HMAC-SHA256 signature | Shared webhook secret. Workflow signs the result payload; API verifies before accepting. |
| API → Callback URL | HMAC-SHA256 signature | Same or separate webhook secret. Allows the receiving system to verify payload authenticity. |
| Copilot SDK → GitHub | GitHub PAT via `GITHUB_TOKEN` | The PAT authenticates Copilot SDK sessions. Must belong to a user with an active Copilot subscription. |
| BYOK (optional) | Provider API keys | Stored as GitHub Actions environment secrets (e.g., `ANTHROPIC_API_KEY`). Only used when explicitly configured. |

### PAT Requirements

The GitHub PAT (classic) requires:
- `repo` scope (full repository access for clone, push, PR operations)
- The PAT owner must have a GitHub Copilot subscription (Individual, Business, or Enterprise)
- The PAT must have access to all target repositories

### Data Protection

- **Encryption at rest**: DynamoDB encryption enabled by default (AWS-managed key). Secrets Manager encrypts all secrets with KMS.
- **Encryption in transit**: TLS 1.2+ enforced on all connections (AppRunner HTTPS, DynamoDB HTTPS endpoint, GitHub API HTTPS).
- **Secrets management**: All secrets stored in AWS Secrets Manager. Never logged, never included in API responses, never committed to the repository.
- **Sensitive data in DynamoDB**: Agent instructions and results may contain code snippets. The 90-day TTL ensures automatic cleanup. No PII is expected in payloads.

### Security Controls

- **Rate limiting**: Not implemented for MVP (single user). AppRunner supports request throttling if needed later.
- **CORS**: Disabled (API is backend-to-backend only, no browser access).
- **Input validation**: Pydantic models enforce strict typing and constraints on all request payloads.
- **Agent sandboxing**: The Copilot SDK agent runs within a GitHub Actions runner (ephemeral VM). No persistent access to infrastructure.

---

## Performance & Scalability

### Current Target (Single User)

- **Concurrent runs**: 1-3 simultaneous agent executions
- **API requests**: < 100/day
- **DynamoDB**: Well within free tier (< 25 reads and writes per second)
- **AppRunner**: Single instance (1 vCPU, 2 GB RAM), scale to zero when idle

### Scaling Strategy (Future)

- **API**: AppRunner auto-scales horizontally (configure max instances). FastAPI with async handlers supports high concurrency per instance.
- **Database**: DynamoDB on-demand scales automatically. Add a GSI on `status` + `created_at` if query patterns require it.
- **Agent execution**: GitHub Actions concurrent job limits depend on the plan (20 for free/pro, 180 for enterprise). For higher throughput, consider self-hosted runners or multiple orchestration repos.
- **Caching**: Not needed for MVP. Add Redis/ElastiCache only if API response times become an issue under load.

### Capacity Planning

| Metric | MVP (Single User) | Future (10 Users) |
|--------|-------------------|---------------------|
| Agent runs/day | 5-20 | 50-200 |
| API requests/day | 20-80 | 200-800 |
| DynamoDB WCUs | < 1 avg | < 5 avg |
| DynamoDB storage | < 100 MB/year | < 1 GB/year |
| AppRunner instances | 0-1 | 1-3 |

---

## Resilience & Reliability

### Availability Target

- **SLA**: Best-effort for MVP. AppRunner provides 99.95% uptime SLA. DynamoDB provides 99.99%.
- **Maintenance windows**: None required. AppRunner performs rolling deployments.
- **Deployment strategy**: AppRunner auto-deploys new container images from ECR. Rolling deployment with health check validation.

### Failure Modes

| Failure | Impact | Mitigation |
|---------|--------|------------|
| AppRunner instance crash | API temporarily unavailable | Auto-restart by AppRunner. In-flight result delivery retried by workflow. |
| DynamoDB throttling | Write/read failures | On-demand capacity auto-scales. Boto3 built-in retries. |
| GitHub API outage | Cannot dispatch workflows or create PRs | Return 503 to caller. Agent gracefully fails on GitHub operations. |
| Workflow timeout | Agent exceeds time limit | GitHub Actions cancels the workflow. Workflow failure handler (`if: failure() || cancelled()`) POSTs error to API with `error_code: WORKFLOW_TIMEOUT`. If the failure handler itself cannot fire (hard kill), stale run detection on the polling endpoint catches it. |
| Webhook delivery failure | Caller doesn't receive result | 3 retries with exponential backoff (1s, 4s, 16s). Result always available via polling endpoint. |
| Copilot SDK error | Agent session crashes | `session.error` event caught. Error details compiled into result with `status: failure`. Error object written to DynamoDB. |
| Orphaned run (no result reported) | Caller polls `dispatched` indefinitely | Stale run detection: when `GET /agent/run/{run_id}` is called and `created_at + timeout_minutes` has elapsed while status is still `dispatched` or `running`, the API auto-transitions status to `timeout` and writes an `error` object (`STALE_RUN_TIMEOUT`). Webhook is delivered if `callback_url` exists. |

### Monitoring & Alerting

- **Health checks**: AppRunner pings `GET /health` every 10 seconds.
- **Key metrics** (CloudWatch):
  - AppRunner: Request count, latency (p50/p99), 4xx/5xx error rate, active instances
  - DynamoDB: Read/write capacity consumed, throttled requests
- **Alerting**: Not configured for MVP (single user monitors manually). Add CloudWatch Alarms for error rate spikes if distributing to more users.
- **Logs**: All API requests and agent execution events are logged to CloudWatch Logs via AppRunner's built-in log streaming.

---

## Integration Points

### External System: GitHub API

- **Purpose**: Dispatch workflows, create branches/PRs, submit reviews, merge PRs, list workflows, query workflow runs, trigger workflow dispatches
- **Integration Type**: REST API (HTTPS)
- **Authentication**: Personal Access Token (Bearer token)
- **Error Handling**: Retry transient errors (5xx, rate limits) with exponential backoff. Fail fast on 4xx (auth errors, not found).
- **Rate Limits**: 5,000 requests/hour for authenticated requests. Single-user usage is well within this.
- **Dependencies**: If GitHub API is down, no workflows can be dispatched and no PR operations succeed. The API returns 503.

### External System: GitHub Copilot SDK / LLM Provider

- **Purpose**: Run the AI coding agent within the Actions workflow
- **Integration Type**: Copilot SDK (JSON-RPC over stdio to Copilot CLI)
- **Authentication**: GitHub PAT (authenticates to Copilot service via GitHub CLI). For BYOK, provider API keys passed as environment variables.
- **Error Handling**: SDK surfaces errors as `session.error` events. Agent runner catches and includes in result payload.
- **Rate Limits**: Copilot premium request quota varies by subscription plan. Monitor usage via GitHub billing.
- **Dependencies**: If Copilot service is unavailable, agent sessions fail. Result is reported as `status: failure`.

### External System: Caller's Webhook Endpoint

- **Purpose**: Deliver agent execution results to the calling system
- **Integration Type**: HTTP POST (webhook)
- **Authentication**: HMAC-SHA256 signature header (`X-Webhook-Signature`)
- **Error Handling**: 3 retries with exponential backoff. Failed deliveries are logged. Result remains available via polling.
- **Rate Limits**: Caller-dependent. Copilot Dispatch sends at most 1 webhook per run.
- **Dependencies**: If the callback URL is unreachable, the caller can poll `GET /agent/run/{run_id}` instead.

---

## Development & Deployment

### Project Structure

```
copilot-dispatch/
├── .github/
│   ├── workflows/
│   │   ├── agent-executor.yml       # Agent execution workflow (workflow_dispatch)
│   │   └── deploy.yml               # CI/CD pipeline (build, test, deploy)
│   ├── agents/
│   └── instructions/
│       └── copilot.instructions.md
├── .env/
│   ├── .env.local                   # Local dev secrets (gitignored)
│   ├── .env.example                 # Template for required env vars
│   └── .env.test                    # Test configuration
├── app/
│   ├── src/
│   │   ├── __init__.py
│   │   ├── main.py                  # FastAPI app factory and startup
│   │   ├── api/
│   │   │   ├── __init__.py
│   │   │   ├── routes.py            # Route definitions (POST /agent/run, etc.)
│   │   │   ├── actions_routes.py    # Route definitions for Actions visibility endpoints
│   │   │   ├── actions_models.py    # Pydantic models for Actions visibility
│   │   │   ├── dependencies.py      # FastAPI dependencies (auth, services)
│   │   │   └── models.py            # Pydantic request/response models
│   │   ├── services/
│   │   │   ├── __init__.py
│   │   │   ├── dispatcher.py        # GitHub Actions workflow dispatch
│   │   │   ├── actions.py           # GitHub Actions visibility (workflows, runs, dispatch)
│   │   │   ├── webhook.py           # Webhook delivery with HMAC + retries
│   │   │   └── run_store.py         # DynamoDB run state CRUD
│   │   ├── auth/
│   │   │   ├── __init__.py
│   │   │   └── api_key.py           # API key validation dependency
│   │   └── agent/
│   │       ├── __init__.py
│   │       ├── runner.py            # Agent runner entry point (used in Actions)
│   │       ├── roles.py             # Role-specific pre/post processing
│   │       ├── prompts.py           # System prompt template loading and merging
│   │       └── result.py            # Result payload compilation
│   ├── config/
│   │   ├── settings.yaml            # App configuration (defaults, limits)
│   │   └── prompts/
│   │       ├── implement.md         # Implement role system prompt template
│   │       ├── review.md            # Review role system prompt template
│   │       └── merge.md             # Merge role system prompt template
│   └── docs/
├── infra/
│   ├── app.py                       # CDK app entrypoint
│   ├── stacks/
│   │   └── copilot-dispatch_stack.py       # CDK stack definition
│   ├── requirements.txt             # CDK Python dependencies
│   └── cdk.json                     # CDK configuration
├── tests/
│   ├── conftest.py
│   ├── unit/
│   │   ├── test_routes.py
│   │   ├── test_dispatcher.py
│   │   ├── test_webhook.py
│   │   ├── test_run_store.py
│   │   └── test_models.py
│   └── integration/
│       └── test_api_e2e.py
├── Dockerfile                       # API service container image
├── pyproject.toml                   # Python project config (deps, build)
├── docs/
│   ├── brief.md
│   ├── requirements.md
│   └── solution-design.md           # This document
└── README.md
```

### Testing Scenarios

Four end-to-end system journeys that validate core business flows:

1. **Implement flow**: Caller sends a `POST /agent/run` with role `implement` → API creates DynamoDB record, dispatches workflow → workflow clones target repo, runs Copilot agent, creates branch with changes, runs tests, creates PR → workflow POSTs result to API → API updates DynamoDB, delivers webhook to `callback_url` → Caller retrieves result via `GET /agent/run/{run_id}` confirming PR URL and status `success`.

2. **Review flow**: Caller sends a `POST /agent/run` with role `review` and a `pr_number` → API dispatches workflow → workflow checks out PR branch, runs Copilot agent for review, submits PR review via GitHub API → workflow POSTs result to API → API delivers webhook → Result contains `assessment` (approve/request_changes), `review_comments`, and `pr_approved` boolean.

3. **Merge flow**: Caller sends a `POST /agent/run` with role `merge` and a `pr_number` → API dispatches workflow → workflow fetches PR branch, attempts merge, runs tests on merged result → if clean merge with passing tests, merges via GitHub API; if conflicts, agent attempts resolution → workflow POSTs result to API → Result contains `merge_status` and either `merge_sha` or `conflict_resolutions`.

4. **Actions visibility flow**: Caller sends `GET /actions/{owner}/{repo}/workflows` → API returns list of available workflows. Caller sends `GET /actions/{owner}/{repo}/runs?branch=main&status=completed` → API returns filtered run history. Caller sends `GET /actions/{owner}/{repo}/runs/{run_id}` → API returns run details with jobs and steps. Caller sends `POST /actions/{owner}/{repo}/workflows/{workflow_id}/dispatch` with ref and inputs → API triggers the workflow and returns 204.

### Development Workflow

- **Version Control**: GitHub flow (main + feature branches)
- **Branching Strategy**: `main` is production. Feature branches (`feature/description`) for all changes. No `develop` branch needed for single-user project.
- **Code Review**: Optional for single user. Enforce when distributing more widely.
- **Testing Requirements**: Unit tests for core services (30% coverage target per project standards). Integration tests for the API endpoints. E2E tests gated behind `--e2e-confirm` flag for live GitHub operations.

### CI/CD Pipeline

**Build & Test** (on every push/PR):
1. Lint: `black --check` + `isort --check-only`
2. Type check: `mypy` (optional, recommended)
3. Unit tests: `pytest -q --cov=app/src`
4. Evals: `python scripts/evals.py`

**Deploy** (on merge to `main`):
1. Build Docker image
2. Push to ECR
3. Run `cdk deploy` (AppRunner auto-picks up new image)

### Environment Strategy

| Environment | Infrastructure | Configuration |
|-------------|---------------|---------------|
| **Local** | Docker Compose or `uvicorn` directly. DynamoDB Local for testing. | `.env/.env.local` with local overrides. |
| **Production** | AppRunner + DynamoDB + Secrets Manager via CDK | Secrets Manager for secrets. `settings.yaml` baked into container. |

No staging environment for MVP. The CDK stack can be replicated with a different stack name if staging is needed later.

---

## Risks & Technical Debt

### Technology Risks

| Risk | Likelihood | Impact | Mitigation |
|------|-----------|--------|------------|
| **Copilot SDK stability** | Medium | High | SDK is under active development. Pin SDK version. Wrap SDK calls with error handling. Monitor GitHub changelog. |
| **Copilot SDK auth issues** | Medium | High | Classic PAT with `repo` scope is the most reliable method currently. Fine-grained tokens have known issues. Document PAT requirements clearly. |
| **GitHub Actions workflow_dispatch correlation** | Low | Medium | No run ID returned on dispatch. Mitigated by passing `run_id` as workflow input and having workflow self-report back. |
| **Agent non-determinism** | High | Medium | LLMs produce variable output. System prompts constrain behaviour but can't guarantee identical results. Accept variability; log everything for debugging. |
| **Large repository handling** | Medium | Medium | Context window limits may prevent agent from reading full codebase. Mitigate with `system_instructions` scoping agent focus to specific directories and with Copilot SDK's built-in context management. |

### Technical Debt (Accepted for MVP)

| Debt Item | Reason | Resolution Path |
|-----------|--------|-----------------|
| No rate limiting on API | Single user, no abuse risk | Add API Gateway or middleware rate limiting when distributing |
| No multi-tenancy | Single user | Add tenant isolation (API keys per tenant, DynamoDB GSI on tenant) |
| No staging environment | Speed to deployment | Add CDK stack parameter for environment name |
| No automated PAT rotation | Low secret count, single user | Add Secrets Manager rotation Lambda when scaling |
| Webhook delivery from API only | Simpler architecture | Could add SQS for reliable async delivery if needed |
| No structured observability | CloudWatch sufficient for single user | Add Datadog/Grafana when operational complexity warrants it |

---

## Cost Estimation

### Infrastructure Costs (Monthly — Single User)

| Service | Specification | Estimated Cost |
|---------|--------------|----------------|
| **AppRunner** | 1 vCPU, 2 GB RAM, scale-to-zero when idle, ~5-20 active minutes/day | $3–8 |
| **DynamoDB** | On-demand, < 25 RCU/WCU, < 1 GB storage | $0 (free tier) |
| **Secrets Manager** | 3 secrets (API key, GitHub PAT, webhook secret) | $1.20 |
| **ECR** | ~500 MB container image | $0.05 |
| **CloudWatch Logs** | < 1 GB/month ingestion | $0 (free tier) |
| **GitHub Actions** | ~100-500 minutes/month on `ubuntu-latest` | $0 (within free tier for private repos: 2,000 min/month) |
| **Total** | | **~$5–10/month** |

### Scaling Costs

| Scale | Estimated Monthly Cost |
|-------|----------------------|
| 2x load (2 users, ~40 runs/day) | ~$12–18 |
| 5x load (5 users, ~100 runs/day) | ~$25–40 |
| 10x load (10 users, ~200 runs/day) | ~$50–80 |

> Note: The dominant cost at scale will be GitHub Actions minutes (private repo: $0.008/min) and Copilot premium request usage, not AWS infrastructure.

---

## Assumptions & Decisions

### Key Assumptions

- The GitHub PAT owner has an active GitHub Copilot subscription with sufficient premium request quota for expected usage.
- Target repositories are accessible with the configured PAT (the PAT owner has at least write access).
- Target repositories have standard project structures that allow dependency detection (e.g., `package.json`, `requirements.txt`, `go.mod`).
- The Copilot SDK Python package is publicly installable via pip at the time of implementation.
- GitHub Actions `workflow_dispatch` API remains available and stable.
- Agent runs complete within the 60-minute GitHub Actions job timeout.

### Design Decisions & Rationale

1. **Centralised orchestration repo (this repo) rather than per-repo workflows**
   - *Alternatives considered*: Deploy workflow file to each target repo; use a GitHub App to inject workflows.
   - *Tradeoffs*: Centralised means one place to update the workflow, but requires the PAT to have access to all target repos. Per-repo gives more isolation but creates maintenance burden.
   - *Decision*: Centralised wins for single-user simplicity. The workflow checks out target repos using the PAT.

2. **Workflow reports results back to API rather than writing directly to DynamoDB**
   - *Alternatives considered*: Workflow writes to DynamoDB using AWS credentials stored as GitHub secrets; workflow stores result as workflow artifact.
   - *Tradeoffs*: API-mediated approach keeps AWS credentials out of the GitHub environment and centralises webhook delivery logic. Adds dependency on API being available when workflow completes.
   - *Decision*: API-mediated. Keeps the workflow simple (just HTTP POST) and the API as the single point of control for result storage and webhook delivery. AppRunner's quick cold-start makes availability acceptable.

3. **FastAPI over Lambda + API Gateway**
   - *Alternatives considered*: API Gateway + Lambda (truly serverless, pay-per-invocation).
   - *Tradeoffs*: Lambda would be cheaper for very low traffic but adds packaging complexity (layers, cold starts, API Gateway config). AppRunner gives a more traditional development experience and simpler Docker-based deployment.
   - *Decision*: AppRunner for development velocity and simplicity. Cost difference is negligible at single-user scale (~$5/month either way).

4. **GitHub Copilot subscription as primary model access (not BYOK)**
   - *Alternatives considered*: BYOK as primary with direct provider API keys.
   - *Tradeoffs*: Copilot subscription simplifies auth (everything flows through the PAT) and billing (single GitHub invoice). BYOK offers model flexibility and potentially lower cost at high volume.
   - *Decision*: Copilot subscription primary. BYOK support baked in as optional (environment secrets in GitHub Actions) for future flexibility.

5. **DynamoDB over no-datastore approach**
   - *Alternatives considered*: Fully stateless API with webhook-only delivery; store results as GitHub Actions artifacts.
   - *Tradeoffs*: DynamoDB adds a small cost ($0/month in free tier) and an AWS service, but enables polling, audit trail, and run history. Artifacts are harder to query from the API.
   - *Decision*: DynamoDB included. The polling endpoint provides a reliable fallback when webhooks fail and supports operational visibility.

6. **Single API key authentication (not OAuth2/JWT)**
   - *Alternatives considered*: OAuth2 with JWT tokens; GitHub App installation tokens.
   - *Tradeoffs*: API key is the simplest auth mechanism. Insufficient for multi-tenant, but perfect for single-user MVP.
   - *Decision*: API key for MVP. Upgrade to JWT/OAuth2 when multi-tenancy is needed.
