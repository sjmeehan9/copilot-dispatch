# Copilot Dispatch Agent — Runbook

## Overview

Copilot Dispatch is a GitHub Copilot agent orchestration service. It exposes a REST
API that accepts agent run requests (implement, review, merge) and dispatches
them to a GitHub Actions workflow that executes the Copilot agent against a
target repository. Results are stored in DynamoDB and optionally delivered via
HMAC-signed webhook.

For full context see [docs/brief.md](../../docs/brief.md) and
[docs/solution-design.md](../../docs/solution-design.md).

---

## Prerequisites

| Tool | Minimum Version | Notes |
|------|----------------|-------|
| Python | 3.13 | `python3 --version` |
| Docker | 24+ | Required for DynamoDB Local and container builds |
| AWS CLI | 2.x | Profile `copilot-dispatch` must be configured |
| GitHub CLI | 2.x | `gh auth status` must succeed |
| Node.js | 22+ | Required for CDK CLI (`node --version`) |
| CDK CLI | 2.170+ | `npm install -g aws-cdk` (`cdk --version`) |

Additional requirements:

- Active **GitHub Copilot subscription** for the PAT owner (required for agent
  execution via the Copilot SDK)
- AWS account with **CDK bootstrapped** in `ap-southeast-2`
  (`cdk bootstrap --profile copilot-dispatch`)

---

## Local Development

### 1 — One-command startup

```bash
bash scripts/dev.sh
```

This script:

1. Activates the virtual environment (`.venv/`)
2. Loads environment variables from `.env/.env.local`
3. Starts DynamoDB Local via Docker Compose (port `8100`)
4. Waits for DynamoDB Local to be healthy
5. Creates the `dispatch-runs` table
6. Starts the FastAPI dev server with hot-reload on port `8000`

### 2 — Manual step-by-step setup

```bash
# Activate virtual environment
source .venv/bin/activate

# Install all dependencies (production + dev)
pip install -e ".[dev]"

# Copy and populate the environment file
cp .env/.env.example .env/.env.local
# Edit .env/.env.local — fill in AWS keys, GitHub PAT, etc.

# Start DynamoDB Local
docker compose up -d

# Initialise the DynamoDB table
python scripts/init_dynamodb.py

# Start the API server
uvicorn app.src.main:app --host 0.0.0.0 --port 8000 --reload
```

### 3 — Verify the local stack

```bash
curl http://localhost:8000/health
# {"status": "healthy"}
```

### 4 — Running against DynamoDB Local vs production DynamoDB

By default, the local development stack uses **DynamoDB Local** (port 8100)
via Docker Compose. This is controlled by the `DISPATCH_DYNAMODB_ENDPOINT_URL`
environment variable in `.env/.env.local`:

```bash
# DynamoDB Local (default for local development)
DISPATCH_DYNAMODB_ENDPOINT_URL=http://localhost:8100

# Production DynamoDB (leave empty or unset)
DISPATCH_DYNAMODB_ENDPOINT_URL=
```

When `DISPATCH_DYNAMODB_ENDPOINT_URL` is empty or unset, the application
uses the real AWS DynamoDB endpoint (the boto3 default behaviour). In
production (AppRunner), this variable is deliberately left empty so the
application connects to the DynamoDB table in `ap-southeast-2`.

---

## API Usage

### Create an agent run — implement

```bash
curl -X POST http://localhost:8000/agent/run \
  -H "Content-Type: application/json" \
  -H "X-API-Key: <your-api-key>" \
  -d '{
    "repository": "owner/repo-name",
    "branch": "main",
    "agent_instructions": "Add request validation to the registration endpoint.",
    "model": "gpt-5",
    "role": "implement",
    "system_instructions": "Follow the project coding standards. Run tests before committing.",
    "skill_paths": [".github/skills/python-standards.md"],
    "agent_paths": [".github/agents/security-reviewer.agent.md"],
    "callback_url": "https://example.com/copilot-dispatch-callback",
    "timeout_minutes": 30
  }'
```

Expected response:

```json
{
  "run_id": "a1b2c3d4-e5f6-7890-abcd-ef1234567890",
  "status": "dispatched",
  "created_at": "2026-02-28T10:00:00+00:00"
}
```

> **Note:** `pr_number` must **not** be provided for the `implement` role.
> `system_instructions`, `skill_paths`, and `agent_paths` are optional for all roles.

### Create an agent run — review

```bash
curl -X POST http://localhost:8000/agent/run \
  -H "Content-Type: application/json" \
  -H "X-API-Key: <your-api-key>" \
  -d '{
    "repository": "owner/repo-name",
    "branch": "main",
    "agent_instructions": "Review this PR for security vulnerabilities and correctness.",
    "model": "claude-sonnet-4-20250514",
    "role": "review",
    "pr_number": 42,
    "system_instructions": "Focus on input validation, SQL injection, and auth bypass risks.",
    "skill_paths": [".github/skills/security-review.md"],
    "agent_paths": [".github/agents/security-reviewer.agent.md"],
    "callback_url": "https://example.com/copilot-dispatch-callback",
    "timeout_minutes": 15
  }'
```

Expected response:

```json
{
  "run_id": "b2c3d4e5-f6a7-8901-bcde-f12345678901",
  "status": "dispatched",
  "created_at": "2026-02-28T10:05:00+00:00"
}
```

> **Note:** `pr_number` is **required** for the `review` role.

### Create an agent run — merge

```bash
curl -X POST http://localhost:8000/agent/run \
  -H "Content-Type: application/json" \
  -H "X-API-Key: <your-api-key>" \
  -d '{
    "repository": "owner/repo-name",
    "branch": "main",
    "agent_instructions": "Merge this PR. Resolve any conflicts preserving the feature branch changes.",
    "model": "gpt-5",
    "role": "merge",
    "pr_number": 42,
    "callback_url": "https://example.com/copilot-dispatch-callback",
    "timeout_minutes": 20
  }'
```

Expected response:

```json
{
  "run_id": "c3d4e5f6-a7b8-9012-cdef-123456789012",
  "status": "dispatched",
  "created_at": "2026-02-28T10:10:00+00:00"
}
```

> **Note:** `pr_number` is **required** for the `merge` role.

### Request payload reference

| Field | Type | Required | Default | Notes |
|-------|------|----------|---------|-------|
| `repository` | string | Yes | — | GitHub repository in `owner/repo` format |
| `branch` | string | Yes | — | Target branch for the agent |
| `agent_instructions` | string | Yes | — | Instructions for the agent to follow |
| `model` | string | Yes | — | AI model identifier (see Agent Runner docs) |
| `role` | string | Yes | — | One of `implement`, `review`, `merge` |
| `pr_number` | int | Conditional | `null` | Required for `review` and `merge`; must be omitted for `implement` |
| `system_instructions` | string | No | `null` | Additional system-level instructions merged into the prompt |
| `skill_paths` | list[string] | No | `null` | Paths to custom skill files relative to the repo root |
| `agent_paths` | list[string] | No | `null` | Paths to Markdown custom agent files (for example `.github/agents/security-reviewer.agent.md`) relative to the repo root |
| `callback_url` | string (URL) | No | `null` | URL to receive an HMAC-signed webhook on run completion |
| `timeout_minutes` | int | No | `30` | Maximum execution time in minutes (1–60) |

### Check run status

```bash
curl -X GET http://localhost:8000/agent/run/<run_id> \
  -H "X-API-Key: <your-api-key>"
```

Possible statuses:

- `dispatched`: request accepted and workflow dispatch attempted
- `running`: workflow started and posted a status update
- `success`: terminal success with role-specific `result`
- `failure`: terminal failure with structured `error`
- `timeout`: stale run auto-transitioned by polling endpoint

### Result ingestion endpoint (internal)

`POST /agent/run/{run_id}/result` is intended for the GitHub Actions workflow,
not external callers. It is authenticated via `X-Webhook-Signature` HMAC and
accepts:

- status-only updates (for `running`)
- terminal success payloads (role-specific result fields)
- terminal failure payloads (error-only or result+error)

### Webhook payload shape

Webhook callbacks contain the full run record from DynamoDB, including `result`
and `error` fields.

Implement result excerpt:

```json
{
  "run_id": "...",
  "role": "implement",
  "status": "success",
  "result": {
    "pr_url": "https://github.com/owner/repo/pull/42",
    "pr_number": 42,
    "branch": "feature/run-id",
    "commits": [{"sha": "abc123", "message": "feat: add validation"}],
    "files_changed": ["app/src/api/routes.py"],
    "test_results": {"passed": 20, "failed": 0, "skipped": 1},
    "security_findings": []
  },
  "error": null
}
```

Review result excerpt:

```json
{
  "run_id": "...",
  "role": "review",
  "status": "success",
  "result": {
    "assessment": "approve",
    "review_comments": [{"file_path": "app/src/main.py", "body": "Looks good"}],
    "suggested_changes": [],
    "security_concerns": [],
    "pr_approved": true
  },
  "error": null
}
```

Merge result excerpt:

```json
{
  "run_id": "...",
  "role": "merge",
  "status": "success",
  "result": {
    "merge_status": "merged",
    "merge_sha": "def456",
    "conflict_files": [],
    "conflict_resolutions": [],
    "test_results": {"passed": 22, "failed": 0, "skipped": 0}
  },
  "error": null
}
```

### Local Webhook Testing

To receive and inspect webhook callbacks locally (e.g. when testing against the
production AppRunner deployment), use the bundled webhook receiver script and
[ngrok](https://ngrok.com/) to expose a public URL.

#### 1 — Install and configure ngrok

```bash
# macOS
brew install ngrok

# Authenticate (one-time)
ngrok config add-authtoken <YOUR_NGROK_AUTH_TOKEN>
```

#### 2 — Start the webhook receiver

```bash
source .venv/bin/activate
set -o allexport; source .env/.env.local; set +o allexport

# --verify enables HMAC-SHA256 signature checking (uses DISPATCH_WEBHOOK_SECRET)
python scripts/webhook_receiver.py --verify
```

The receiver listens on port 9000 by default. Use `--port <N>` to change it.

#### 3 — Start ngrok

In a separate terminal:

```bash
# Ephemeral URL (changes each session)
ngrok http 9000

# Or with a static/custom domain
ngrok http --url=your-subdomain.ngrok-free.app 9000
```

Copy the public `https://...ngrok-free.app` URL from the ngrok output.

#### 4 — Use the ngrok URL as callback_url

Pass the ngrok URL in your `POST /agent/run` request:

```json
{
  "repository": "owner/repo",
  "branch": "main",
  "agent_instructions": "Add a square function.",
  "model": "gpt-5.1",
  "role": "implement",
  "callback_url": "https://your-subdomain.ngrok-free.app",
  "timeout_minutes": 10
}
```

When the run reaches a terminal state (`success`, `failure`, `timeout`), the API
delivers the webhook to your ngrok URL. The receiver prints the full payload and
HMAC verification result to the terminal.

> **Tip:** If ngrok shows `502 Bad Gateway`, the webhook receiver is not running
> or is listening on a different port.

### Error scenarios

- Stale run detection: polling `GET /agent/run/{run_id}` can auto-transition
  overdue `dispatched`/`running` runs to `timeout` with `STALE_RUN_TIMEOUT`.
- Error payload propagation: failure callbacks include structured `error` with
  `error_code`, `error_message`, and optional `error_details`.
- Polling fallback: if webhook delivery fails after retries, callers can always
  retrieve terminal state via `GET /agent/run/{run_id}`.

---

## Agent Execution

The agent executor runs Copilot SDK sessions to perform implement, review,
and merge operations on target repositories.  It can be invoked locally
(for development/testing) or via the GitHub Actions workflow (production).

### Local Agent Execution

Run the agent runner script directly against a locally cloned repository:

```bash
# 1. Clone the target repository
gh repo clone owner/target-repo ./target-repo

# 2. Activate venv and load environment
source .venv/bin/activate
set -o allexport; source .env/.env.local; set +o allexport

# 3. Run the agent runner
python -m app.src.agent.runner \
  --run-id test-123 \
  --role implement \
  --model claude-sonnet-4-20250514 \
  --instructions "Add a square(n) function to src/calculator.py that returns n*n. Add a test for it." \
  --repo-path ./target-repo \
  --timeout 10
```

#### Implement role example

```bash
python -m app.src.agent.runner \
  --run-id impl-001 \
  --role implement \
  --model claude-sonnet-4-20250514 \
  --instructions "Add request validation to the registration endpoint." \
  --repo-path ./target-repo \
  --timeout 30
```

#### Review role example

```bash
python -m app.src.agent.runner \
  --run-id review-001 \
  --role review \
  --model claude-sonnet-4-20250514 \
  --instructions "Review this PR for security and correctness." \
  --repo-path ./target-repo \
  --timeout 15 \
  --pr-number 42
```

#### Merge role example

```bash
python -m app.src.agent.runner \
  --run-id merge-001 \
  --role merge \
  --model claude-sonnet-4-20250514 \
  --instructions "Merge this PR safely." \
  --repo-path ./target-repo \
  --timeout 20 \
  --pr-number 42
```

### Agent Runner Arguments

| Argument | Required | Description |
|----------|----------|-------------|
| `--run-id` | Yes | Unique identifier for the agent run |
| `--role` | Yes | Agent role: `implement`, `review`, or `merge` |
| `--model` | Yes | LLM model identifier (e.g., `claude-sonnet-4-20250514`) |
| `--instructions` | Yes | Agent instructions (plain text or `@filepath` to read from file) |
| `--repo-path` | Yes | Absolute or relative path to the checked-out target repository |
| `--timeout` | No | Maximum session duration in minutes (default: 30) |
| `--system-instructions` | No | Additional system-level instructions to merge into the prompt |
| `--pr-number` | No | Pull request number (required for `review` and `merge` roles) |
| `--skill-paths` | No | Comma-separated list of skill file paths relative to the repo root |
| `--agent-paths` | No | Comma-separated list of Markdown custom agent definition paths relative to the repo root |
| `--api-result-url` | No | URL to POST the result payload (used by the workflow) |
| `--webhook-secret` | No | HMAC signing secret for result posting (used by the workflow) |

### Markdown Custom Agent Format

Custom agent files are expected inside the target repository at `.github/agents/`
and use Markdown plus YAML frontmatter. Example:

```markdown
---
name: Security Reviewer
description: Reviews code for security vulnerabilities.
tools: ["grep", "glob", "view"]
infer: true
---

# Agent: Security Reviewer

Review this change for auth bypass, injection issues, and secret leakage.
```

Required frontmatter keys:

- `name` (string)
- `description` (string)

Optional keys:

- `display_name` (string)
- `machine_name` (string)
- `tools` (list of strings)
- `infer` (boolean)

The markdown body is passed through as the SDK custom agent `prompt`.

The `--instructions` argument supports `@filepath` syntax: if the value
starts with `@`, the remaining string is treated as a file path whose
contents are read as the instruction text.

#### Finding Valid Model Identifiers

The `--model` value is passed directly to the Copilot SDK `create_session`
call without validation. Which strings are accepted depends on the
authentication path in use:

- **GitHub Copilot (default — GitHub token auth):** Available models are
  governed by your Copilot subscription plan. Use the SDK's `list_models()`
  method at runtime, or consult the GitHub docs:
  <https://docs.github.com/en/copilot/reference/ai-models/supported-models>
- **Anthropic BYOK (`ANTHROPIC_API_KEY`):** Use Anthropic API model IDs
  (e.g., `claude-opus-4-6`, `claude-sonnet-4-6`). See the full list at:
  <https://docs.anthropic.com/en/docs/about-claude/models>
- **OpenAI BYOK (`OPENAI_API_KEY`):** Use OpenAI API model IDs (e.g.,
  `gpt-5.4`, `gpt-4.1`). See the full list at:
  <https://platform.openai.com/docs/models>

> **Note:** Model identifier formats differ between providers. For example,
> Anthropic uses `claude-sonnet-4-6` while GitHub Copilot may expose the
> same model as `claude-sonnet-4.6`. Always verify against the relevant
> source above.

### Environment Variables

| Variable | Required | Description |
|----------|----------|-------------|
| `GITHUB_TOKEN` | Yes | GitHub PAT with `repo` scope for `gh` CLI operations |
| `COPILOT_API_KEY` | No | BYOK API key for the model provider (if not using Copilot subscription) |
| `ANTHROPIC_API_KEY` | No | Anthropic API key (when using Claude models via BYOK) |
| `OPENAI_API_KEY` | No | OpenAI API key (when using GPT models via BYOK) |

When using a Copilot subscription (not BYOK), authentication is handled
automatically via the GitHub PAT. BYOK keys are only needed when configured
in the `copilot` GitHub Actions environment.

### Troubleshooting Agent Issues

| Problem | Likely Cause | Resolution |
|---------|-------------|------------|
| `copilot CLI not found` | Copilot SDK not installed | `pip install github-copilot-sdk` or install `@anthropic-ai/copilot` globally |
| `SDK_INIT_ERROR` on startup | CLI binary not on PATH | Verify `which copilot` returns a path; check installation |
| `SESSION_CREATE_ERROR` | Invalid model or auth failure | Verify PAT has Copilot access; check model identifier is valid |
| `AGENT_TIMEOUT` | Agent exceeded time limit | Increase `--timeout`; simplify instructions for faster completion |
| `GIT_PUSH_ERROR` | PAT lacks push permission | Ensure PAT has `repo` scope on the target repository |
| `PR_CREATE_ERROR` | `gh` CLI auth issue | Run `gh auth status` to verify authentication |
| Agent produces no output | Session ended without messages | Check system prompt and instructions for clarity |
| Test suite not detected | No `pytest`/`npm test` in repo | Agent notes missing test suite in session summary |

### Workflow Execution

In production, the agent executor runs as a GitHub Actions workflow
(`agent-executor.yml`) triggered via `workflow_dispatch` by the API's
dispatch service.

**Flow**: `POST /agent/run` → API stores record in DynamoDB → API triggers
`workflow_dispatch` → GitHub Actions runs `agent-executor.yml` → workflow
reports `running` status → agent executes → workflow POSTs result back to
API → API stores result and delivers webhook.

The workflow can also be triggered manually from the GitHub Actions UI for
testing. Navigate to **Actions → Agent Executor → Run workflow** and fill
in the required inputs (`run_id`, `target_repository`, `target_branch`,
`role`, `agent_instructions`, `model`, `api_result_url`).

The workflow includes a failure handler step (`if: failure() || cancelled()`)
that guarantees an error payload is posted back to the API even if the agent
runner itself crashes. This ensures callers always receive a terminal status.

---

## GitHub Actions Visibility

The Actions endpoints provide read-only visibility into GitHub Actions
workflows and runs on target repositories, plus the ability to trigger
workflow dispatches.  All endpoints require the `X-API-Key` header.

### List workflows

```bash
curl -X GET http://localhost:8000/actions/{owner}/{repo}/workflows \
  -H "X-API-Key: <your-api-key>"
```

Response (200):

```json
{
  "total_count": 2,
  "workflows": [
    {
      "id": 12345,
      "name": "CI Pipeline",
      "path": ".github/workflows/ci.yml",
      "state": "active",
      "created_at": "2025-01-15T08:00:00Z",
      "updated_at": "2026-02-20T14:30:00Z",
      "html_url": "https://github.com/owner/repo/actions/workflows/ci.yml"
    }
  ]
}
```

### List workflow runs

Supports optional query parameters: `branch`, `status`, `actor`, `event`,
`per_page` (1–100, default 30).

```bash
# All runs
curl -X GET http://localhost:8000/actions/{owner}/{repo}/runs \
  -H "X-API-Key: <your-api-key>"

# Filtered by branch and status
curl -X GET "http://localhost:8000/actions/{owner}/{repo}/runs?branch=main&status=completed&per_page=10" \
  -H "X-API-Key: <your-api-key>"
```

Response (200):

```json
{
  "total_count": 1,
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
      "actor": "octocat",
      "run_number": 42,
      "run_attempt": 1,
      "created_at": "2026-02-28T10:00:00Z",
      "updated_at": "2026-02-28T10:05:30Z",
      "html_url": "https://github.com/owner/repo/actions/runs/67890"
    }
  ]
}
```

### Get workflow run detail

Returns run metadata with embedded jobs and steps.

```bash
curl -X GET http://localhost:8000/actions/{owner}/{repo}/runs/{run_id} \
  -H "X-API-Key: <your-api-key>"
```

Response (200):

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
  "actor": "octocat",
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

### Dispatch a workflow

Triggers a `workflow_dispatch` event on the specified workflow.

```bash
curl -X POST http://localhost:8000/actions/{owner}/{repo}/workflows/{workflow_id}/dispatch \
  -H "Content-Type: application/json" \
  -H "X-API-Key: <your-api-key>" \
  -d '{
    "ref": "main",
    "inputs": {
      "environment": "staging",
      "dry_run": "true"
    }
  }'
```

Response: `204 No Content` (empty body on success).

### Actions error responses

| HTTP Status | Error Code | Cause |
|-------------|-----------|-------|
| 401 | `AUTHENTICATION_ERROR` | Missing or invalid `X-API-Key` header |
| 403 | `ACTIONS_PERMISSION_DENIED` | GitHub PAT lacks permission to access the repository |
| 404 | `ACTIONS_NOT_FOUND` | Repository, workflow, or run not found |
| 422 | `ACTIONS_VALIDATION_ERROR` | GitHub rejected the dispatch payload (e.g. missing `workflow_dispatch` trigger) |
| 502 | `ACTIONS_GITHUB_ERROR` | GitHub API returned a 5xx server error |

All error responses follow the standard `ErrorResponse` shape:

```json
{
  "error_code": "ACTIONS_NOT_FOUND",
  "error_message": "Repository owner/missing not found",
  "error_details": null
}
```

### Actions troubleshooting

| Problem | Likely Cause | Resolution |
|---------|-------------|------------|
| 403 on Actions endpoints | GitHub PAT missing `actions` scope | Regenerate PAT with `repo` + `actions` scopes |
| 404 on valid repository | Repository is private and PAT lacks access | Ensure PAT has access to the target repo |
| 422 on dispatch | Workflow does not define `workflow_dispatch` trigger | Add `on: workflow_dispatch` to the workflow YAML |
| 502 on any Actions endpoint | GitHub API experiencing issues | Check [githubstatus.com](https://githubstatus.com); retry after a delay |
| Empty workflows list | No `.github/workflows/*.yml` files in the repo | Add workflow YAML files to the repository |

---

## Running Tests

### Unit & integration tests (no external services required)

```bash
# Activate venv first
source .venv/bin/activate

# Run all non-E2E tests with coverage
pytest -q --cov=app/src --cov-report=term-missing
```

### Quality gates

```bash
# Formatting
black --check app/src/ tests/ scripts/

# Import ordering
isort --check-only app/src/ tests/ scripts/

# Docstring and TODO/FIXME gate
python scripts/evals.py
```

Run the full validation sequence in one go:

```bash
black --check app/src/ tests/ scripts/ \
  && isort --check-only app/src/ tests/ scripts/ \
  && pytest -q --cov=app/src --cov-report=term-missing \
  && python scripts/evals.py
```

---

## E2E Testing

End-to-end tests live in `tests/e2e/` and cover all three agent roles plus
Actions visibility:

| Test file | Scenario | Tests |
|-----------|----------|-------|
| `tests/e2e/test_implement_flow.py` | Implement: branch + PR creation | 3 |
| `tests/e2e/test_review_flow.py` | Review: structured assessment + GitHub review | 2 |
| `tests/e2e/test_merge_flow.py` | Merge: clean merge and status reporting | 2 |
| `tests/e2e/test_actions_flow.py` | Actions visibility and workflow dispatch | 5 |

### Prerequisites

Before running E2E tests, ensure:

1. **Production deployment is running** — `curl https://<apprunner-url>/health`
   returns `200`
2. **`autopilot-test-target` repository** is populated with the Python + Node.js
   test project
3. **Environment variables** are set (in `.env/.env.local` or shell):

| Variable | Description |
|----------|-------------|
| `DISPATCH_E2E_API_URL` | Production AppRunner URL (e.g., `https://rdduzmr9sk.ap-southeast-2.awsapprunner.com`) |
| `DISPATCH_E2E_API_KEY` | API key matching the production Secrets Manager value |
| `DISPATCH_E2E_REPOSITORY` | Target repo (default: `sjmeehan9/autopilot-test-target`) |

### Gating mechanism

All E2E tests carry the `@pytest.mark.requires_e2e` marker. Without the
`--e2e-confirm` flag they are **automatically skipped** — they will never
fail in CI or normal local test runs.

```bash
# E2E tests skipped (default — safe for CI)
pytest -q

# E2E tests run (requires live infrastructure)
set -o allexport; source .env/.env.local; set +o allexport
pytest --e2e-confirm tests/e2e/ -v
```

### Expected duration

A full E2E run across all four flows takes approximately **10–15 minutes**.
The implement and review flows are the slowest as they require GitHub Actions
workflow execution + Copilot SDK agent sessions. The Actions visibility
tests complete in under 30 seconds.

### Interpreting results

- **Pass**: The flow completed end-to-end — API accepted the request,
  workflow dispatched, agent executed, result was stored and retrievable.
- **Fail**: A specific assertion failed. Check the test output for details
  on which step failed and the actual vs expected values.
- **Flaky/Timeout**: Agent execution is non-deterministic. The LLM may
  produce different results across runs. If a test times out (600s default),
  check the GitHub Actions workflow run logs for the target repository.

---

## Troubleshooting

| # | Symptom | Cause | Resolution |
|---|---------|-------|------------|
| 1 | AppRunner service not starting | ECR image missing or invalid | Check ECR repository for the `latest` tag; check CloudWatch logs for startup errors; verify image was built with `--platform linux/amd64` |
| 2 | `GET /health` returns 502/503 | Service is starting or has crashed | Wait 30s for health check to pass; if persistent, check CloudWatch logs for application errors; verify secrets are correctly injected |
| 3 | `POST /agent/run` returns 401 | API key mismatch | Verify the `X-API-Key` header value matches the `dispatch/api-key` secret in Secrets Manager; check `SecretsProvider` cache (5-minute TTL) |
| 4 | `POST /agent/run` returns 500 with dispatch error | GitHub PAT invalid or lacks permissions | Verify PAT is valid: `gh auth status`; ensure PAT has `repo` scope; check the PAT hasn't expired |
| 5 | Workflow not dispatching | Workflow file missing or PAT issue | Confirm `agent-executor.yml` exists on the `main` branch of the orchestration repo; check PAT permissions; verify `DISPATCH_GITHUB_OWNER` and `DISPATCH_GITHUB_REPO` env vars in AppRunner |
| 6 | Workflow fails immediately | Copilot SDK installation or auth failure | Check GitHub Actions run logs; verify `copilot` environment has the correct BYOK keys; ensure `GITHUB_TOKEN` is passed to the agent step |
| 7 | Agent timeout (`AGENT_TIMEOUT`) | Agent exceeded the `timeout_minutes` limit | Increase `timeout_minutes` in the request payload (max 60); simplify agent instructions; check target repo complexity |
| 8 | Stale run detected (`STALE_RUN_TIMEOUT`) | Workflow crashed silently or never started | Check GitHub Actions run logs for the workflow; the workflow failure handler may have failed to POST; manually check DynamoDB record status |
| 9 | Webhook not delivered | Callback URL unreachable or HMAC mismatch | Verify `callback_url` is publicly accessible; check CloudWatch logs for webhook delivery attempts and errors; webhook retries 3 times with exponential backoff |
| 10 | DynamoDB access denied in production | IAM instance role misconfigured | Verify the AppRunner instance role has `dynamodb:*` on the `dispatch-runs` table ARN; check `DISPATCH_APP_ENV` is set to `production` (not `local`) |
| 11 | `ImportError: No module named 'app'` | Package not installed in editable mode | Run `pip install -e ".[dev]"` from repo root; ensure venv is activated |
| 12 | `cdk deploy` fails | AWS credentials or bootstrap issue | Verify `aws sts get-caller-identity --profile copilot-dispatch` succeeds; ensure CDK is bootstrapped in the target region: `cdk bootstrap --profile copilot-dispatch`; install CDK deps: `pip install -r infra/requirements.txt` |
| 13 | Docker image crashes on AppRunner (no logs) | Architecture mismatch (arm64 vs amd64) | Rebuild with `docker build --platform linux/amd64`; AppRunner only supports x86_64 images |
| 14 | `POST /agent/run/{run_id}/result` returns 409 | Run already has a terminal status | The result endpoint rejects duplicate terminal state updates; check DynamoDB for the existing status; this is expected behaviour for idempotency |
| 15 | DynamoDB Local not starting | Port 8100 in use or Docker not running | Run `docker ps` to check for port conflicts; ensure Docker Desktop is running; try `docker compose down && docker compose up -d` |
| 16 | `black --check` or `isort --check-only` fails | Unformatted code | Run `black app/src/ tests/ scripts/` and `isort app/src/ tests/ scripts/` to auto-format; commit the changes |

---

## Deployment

### First-time deployment

Deploy the full AWS infrastructure stack using CDK:

```bash
# Activate venv and install CDK dependencies
source .venv/bin/activate
pip install -r infra/requirements.txt
pip install -e .

# Verify AWS credentials
aws sts get-caller-identity --profile copilot-dispatch

# Bootstrap CDK (once per account/region)
cdk bootstrap --profile copilot-dispatch

# Synthesise the CloudFormation template (dry run)
cdk synth --app "python infra/app.py" --profile copilot-dispatch

# Deploy the stack
cdk deploy --app "python infra/app.py" --profile copilot-dispatch --require-approval never
```

After CDK deploy completes, note the CloudFormation outputs:
- `AppRunnerServiceUrl` — the HTTPS URL for the production API
- `DynamoDbTableName` — the DynamoDB table name
- `EcrRepositoryUri` — the ECR repository URI for Docker push

Then build and push the Docker image (see Docker Image & ECR Push below),
and verify the deployment with the smoke test:

```bash
python scripts/smoke_test.py \
  --url https://<apprunner-url> \
  --api-key <your-api-key> \
  --skip-dispatch
```

### Subsequent deployments (automated)

The CI/CD pipeline (`.github/workflows/deploy.yml`) triggers automatically
on merge to `main`:

1. **test** — runs Black, isort, pytest, evals
2. **build-and-push** — builds Docker image, tags with git SHA + `latest`,
   pushes to ECR
3. **deploy** — runs `cdk deploy --require-approval never`

AppRunner is also configured with `auto_deployments_enabled: True`, so it
automatically pulls new images pushed to the `latest` ECR tag.

### Manual deployment

For ad-hoc deployments outside the CI/CD pipeline:

```bash
# Build, tag, and push Docker image
ACCOUNT_ID=$(aws sts get-caller-identity --profile copilot-dispatch --query Account --output text)
ECR_URI="${ACCOUNT_ID}.dkr.ecr.ap-southeast-2.amazonaws.com/copilot-dispatch"

aws ecr get-login-password --region ap-southeast-2 --profile copilot-dispatch | \
    docker login --username AWS --password-stdin "${ACCOUNT_ID}.dkr.ecr.ap-southeast-2.amazonaws.com"

docker build --platform linux/amd64 -t copilot-dispatch:latest .
COMMIT_SHA=$(git rev-parse --short HEAD)
docker tag copilot-dispatch:latest "${ECR_URI}:${COMMIT_SHA}"
docker tag copilot-dispatch:latest "${ECR_URI}:latest"
docker push "${ECR_URI}:${COMMIT_SHA}"
docker push "${ECR_URI}:latest"

# Deploy CDK stack (if infrastructure changes)
cdk deploy --app "python infra/app.py" --profile copilot-dispatch --require-approval never
```

### Monitoring

- **CloudWatch Logs**: AppRunner automatically streams stdout/stderr to
  CloudWatch. Navigate to **AWS Console → CloudWatch → Log groups →
  /aws/apprunner/dispatch-api** to view application logs.
- **AppRunner Console**: **AWS Console → App Runner → dispatch-api** shows
  service status, deployment history, metrics (request count, latency, errors).
- **DynamoDB Console**: **AWS Console → DynamoDB → Tables → dispatch-runs**
  to inspect run records, monitor item count, and check consumed capacity.

### Rollback

To roll back to a previous version:

1. **Re-deploy a previous image**: Tag a known-good image as `latest` and
   push to ECR. AppRunner auto-deploys the new `latest` tag.
   ```bash
   docker pull "${ECR_URI}:<previous-sha>"
   docker tag "${ECR_URI}:<previous-sha>" "${ECR_URI}:latest"
   docker push "${ECR_URI}:latest"
   ```
2. **CDK rollback**: Check out a previous commit and re-deploy:
   ```bash
   git checkout <commit-sha>
   cdk deploy --app "python infra/app.py" --profile copilot-dispatch --require-approval never
   ```
3. **AppRunner console**: Use the AppRunner console to pause/resume the
   service if needed for emergency maintenance.

---

## Docker Image & ECR Push

### Building the production image

The production Docker image uses a multi-stage build with a non-root user.
Build it locally to verify before pushing:

```bash
# Build the image
docker build -t copilot-dispatch:latest .

# Verify it starts and responds to health checks
docker run --rm -d -p 8000:8000 --name copilot-dispatch-test copilot-dispatch:latest
curl -s http://localhost:8000/health
# {"status": "healthy"}

# Verify it runs as non-root
docker exec copilot-dispatch-test whoami
# appuser

# Clean up
docker stop copilot-dispatch-test
```

### Pushing to ECR

Authenticate Docker with ECR, then build, tag, and push:

```bash
# Set your AWS account ID (replace with your actual account ID)
ACCOUNT_ID=$(aws sts get-caller-identity --profile copilot-dispatch --query Account --output text)
ECR_URI="${ACCOUNT_ID}.dkr.ecr.ap-southeast-2.amazonaws.com/copilot-dispatch"

# Authenticate Docker with ECR
aws ecr get-login-password --region ap-southeast-2 --profile copilot-dispatch | \
    docker login --username AWS --password-stdin "${ACCOUNT_ID}.dkr.ecr.ap-southeast-2.amazonaws.com"

# Build the image
docker build -t copilot-dispatch:latest .

# Tag with ECR URI — both git SHA (traceability) and latest (AppRunner auto-deploy)
COMMIT_SHA=$(git rev-parse --short HEAD)
docker tag copilot-dispatch:latest "${ECR_URI}:${COMMIT_SHA}"
docker tag copilot-dispatch:latest "${ECR_URI}:latest"

# Push to ECR
docker push "${ECR_URI}:${COMMIT_SHA}"
docker push "${ECR_URI}:latest"
```

AppRunner is configured with `auto_deployments_enabled: True`, so it
automatically pulls and deploys new images pushed to the `latest` tag.

### Verifying AppRunner deployment

After pushing an image to ECR, verify AppRunner picks it up:

1. Check AppRunner console: **AWS Console → App Runner → dispatch-api**
2. Wait for service status to change from "Operation in progress" to "Running"
3. Test the health endpoint:

```bash
# Get the AppRunner URL from CDK outputs or the console
curl https://<apprunner-url>/health
# {"status": "healthy"}
```

### Docker troubleshooting

| Problem | Likely Cause | Resolution |
|---------|-------------|------------|
| Build fails on `pip install` | Missing dependency or network issue | Check `pyproject.toml` dependencies; retry with `--no-cache` |
| Container exits immediately | Application crash on startup | Run `docker logs copilot-dispatch-test` to see the error |
| Health check fails in container | Port mismatch or slow startup | Verify `EXPOSE 8000` and `--port 8000` match; increase `--start-period` |
| ECR push `denied` error | Docker not authenticated with ECR | Re-run `aws ecr get-login-password` command |
| ECR push `repository not found` | ECR repo not yet created by CDK | Deploy the CDK stack first: `cd infra && cdk deploy` |
| AppRunner not updating after push | Image tag didn't change | Always push the `latest` tag for auto-deployment |
| Image too large (> 300 MB) | Build context includes unnecessary files | Verify `.dockerignore` excludes `.venv/`, `.git/`, `tests/`, etc. |

---

## Secret Rotation

### API Key Rotation

The API key authenticates external callers to the Copilot Dispatch API.

1. **Generate a new key**:
   ```bash
   NEW_KEY=$(openssl rand -hex 32)
   echo "New API key: $NEW_KEY"
   ```
2. **Update Secrets Manager**:
   ```bash
   aws secretsmanager update-secret-value \
     --secret-id dispatch/api-key \
     --secret-string "$NEW_KEY" \
     --profile copilot-dispatch
   ```
3. **Update GitHub Actions secret**: Go to **GitHub → Repository Settings →
   Secrets and variables → Actions** and update the `API_KEY` secret with
   the new value.
4. **Restart AppRunner** (or wait for next deployment): AppRunner caches
   Secrets Manager values. Force a refresh by triggering a new deployment
   or manually restarting the service from the AppRunner console.
5. **Notify callers**: All API consumers must update their `X-API-Key`
   header value to the new key. The old key stops working immediately after
   the AppRunner service picks up the new secret.
6. **Update local env**: If developing locally, update `DISPATCH_API_KEY`
   in `.env/.env.local`.

### GitHub Token Rotation

The GitHub token (stored in secrets named `PAT` for historical reasons) is
actually a **GitHub OAuth token** that authenticates the API's workflow
dispatch and the agent's git/GitHub operations.

1. **Generate a new OAuth token**: Use the GitHub CLI device flow to
   obtain a fresh OAuth token:
   ```bash
   gh auth login --web
   gh auth token
   ```
   Ensure the authenticated account has an active Copilot subscription and
   `repo` scope access.
2. **Update Secrets Manager**:
   ```bash
   aws secretsmanager update-secret-value \
     --secret-id dispatch/github-pat \
     --secret-string "<new-oauth-token>" \
     --profile copilot-dispatch
   ```
   Note: The secret ID retains the `/github-pat` name for backward
   compatibility despite storing an OAuth token.
3. **Update GitHub Actions secret**: Update the `PAT` repository secret
   (not `GITHUB_PAT` — GitHub reserves the `GITHUB_` prefix). The secret
   name is kept as `PAT` for backward compatibility with existing workflows.
4. **Restart AppRunner**: Trigger a new deployment or restart from the
   AppRunner console.
5. **Verify**: Run `gh auth status` to confirm the new token works.
   Test a `POST /agent/run` request to verify workflow dispatch succeeds.
6. **Revoke the old token**: If the previous token was a classic PAT,
   revoke it from **GitHub → Settings → Developer settings → Personal
   access tokens**. If it was an OAuth token, use `gh auth logout` on
   the old session or revoke it from **GitHub → Settings → Applications →
   Authorized OAuth Apps**.

### Webhook Secret Rotation

The webhook secret is used for HMAC-SHA256 signing of both inbound result
payloads (from the workflow) and outbound webhook deliveries (to callers).

1. **Generate a new secret**:
   ```bash
   NEW_SECRET=$(openssl rand -hex 32)
   echo "New webhook secret: $NEW_SECRET"
   ```
2. **Update Secrets Manager**:
   ```bash
   aws secretsmanager update-secret-value \
     --secret-id dispatch/webhook-secret \
     --secret-string "$NEW_SECRET" \
     --profile copilot-dispatch
   ```
3. **Update GitHub Actions secret**: Update the `WEBHOOK_SECRET` repository
   secret so the agent-executor workflow can sign result payloads.
4. **Restart AppRunner**: Trigger a new deployment or restart from the
   AppRunner console.
5. **Update webhook receivers**: Any system receiving Copilot Dispatch webhooks
   must update their HMAC verification secret to the new value. Coordinate
   this change with downstream consumers before rotating.
6. **Update local env**: If developing locally, update
   `DISPATCH_WEBHOOK_SECRET` in `.env/.env.local`.

> **Important**: The webhook secret must be updated in three places
> simultaneously — Secrets Manager (for the API), GitHub Actions secrets
> (for the workflow), and any webhook receivers. A mismatch causes HMAC
> verification failures (401 on result ingestion, failed webhook delivery).
