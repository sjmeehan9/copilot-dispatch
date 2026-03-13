# Copilot Dispatch

Copilot Dispatch is a REST API service that orchestrates GitHub Copilot SDK agent sessions against any accessible GitHub repository. It accepts agent run requests, dispatches GitHub Actions workflows that execute Copilot agent sessions, and delivers structured results asynchronously via webhook with a synchronous polling fallback.

---

## Overview

Copilot Dispatch enables programmatic execution of coding agent tasks through three distinct roles:

| Role | Description |
|------|-------------|
| **implement** | Create a branch, implement a feature or fix based on natural language instructions, run tests, and raise a PR |
| **review** | Review an existing PR, post a structured assessment with file-level feedback, and optionally approve |
| **merge** | Attempt to merge a PR, resolve conflicts using the LLM if needed, and report the outcome |

The system follows an async execution model: the API returns a `202 Accepted` immediately with a `run_id`, dispatches a GitHub Actions workflow to execute the Copilot agent, and delivers the structured result via webhook when complete. Callers can also poll for status at any time.

### Architecture

```
Caller ─── POST /agent/run ──▶ API Service (FastAPI on AppRunner)
  ▲                                  │             │
  │                                  ▼             ▼
  │                            DynamoDB        workflow_dispatch
  │                            (run state)         │
  │                                  ▲             ▼
  │         GET /agent/run/{id} ─────┘     GitHub Actions
  │                                        (agent-executor.yml)
  │                                              │
  │                                              ▼
  │                                        Copilot SDK Agent
  │                                              │
  └──── webhook POST ◀── API ◀── POST result ───┘
```

**Flow**: `POST /agent/run` → API stores a run record in DynamoDB → dispatches `workflow_dispatch` → GitHub Actions runs the Copilot SDK agent → agent executes the role-specific task → workflow POSTs the structured result back to the API → API stores the result and delivers an HMAC-signed webhook to the caller's `callback_url`.

---

## Prerequisites

- **Python 3.13+** — `python3 --version`
- **Docker Desktop** — for DynamoDB Local and container builds
- **AWS CLI v2** — configured with the `copilot-dispatch` profile
- **GitHub CLI** — authenticated with a Copilot-enabled account (`gh auth login`)
- **Node.js 22+** — required for CDK CLI
- **AWS CDK CLI** — `npm install -g aws-cdk`
- **AWS account** — with CDK bootstrapped in `ap-southeast-2`
- **GitHub account** — with an active Copilot subscription for the PAT owner

---

## Quickstart

```bash
# 1. Clone the repository
git clone https://github.com/sjmeehan9/copilot-dispatch.git
cd copilot-dispatch

# 2. Create and activate a Python virtual environment
python3.13 -m venv .venv
source .venv/bin/activate

# 3. Install in editable mode (includes dev dependencies)
pip install -e ".[dev]"

# 4. Configure your local environment
cp .env/.env.example .env/.env.local
# Edit .env/.env.local — fill in your AWS credentials, GitHub PAT, and API key

# 5. Load environment variables
set -o allexport; source .env/.env.local; set +o allexport

# 6. Start the local development stack (DynamoDB Local + FastAPI)
bash scripts/dev.sh

# 7. Verify the API is running
curl http://localhost:8000/health
# {"status": "healthy"}
```

---

## API Usage

All endpoints require the `X-API-Key` header. The production API is served
over HTTPS via AppRunner. Replace `<url>` with `http://localhost:8000` for
local development or the AppRunner service URL for production.

### Implement — create a branch and PR

```bash
curl -X POST https://<url>/agent/run \
  -H "X-API-Key: <key>" \
  -H "Content-Type: application/json" \
  -d '{
    "repository": "owner/repo",
    "branch": "main",
    "role": "implement",
    "agent_instructions": "Add a square(n) function to calculator.py with tests.",
    "model": "claude-sonnet-4-20250514"
  }'
```

### Review — assess an existing PR

```bash
curl -X POST https://<url>/agent/run \
  -H "X-API-Key: <key>" \
  -H "Content-Type: application/json" \
  -d '{
    "repository": "owner/repo",
    "branch": "main",
    "role": "review",
    "pr_number": 42,
    "agent_instructions": "Review for security and correctness.",
    "model": "claude-sonnet-4-20250514"
  }'
```

### Merge — merge or resolve conflicts on a PR

```bash
curl -X POST https://<url>/agent/run \
  -H "X-API-Key: <key>" \
  -H "Content-Type: application/json" \
  -d '{
    "repository": "owner/repo",
    "branch": "main",
    "role": "merge",
    "pr_number": 42,
    "agent_instructions": "Merge if all tests pass.",
    "model": "claude-sonnet-4-20250514"
  }'
```

### Poll for result

```bash
curl https://<url>/agent/run/<run_id> \
  -H "X-API-Key: <key>"
```

Response statuses: `dispatched` → `running` → `success` | `failure` | `timeout`

### Optional fields

All `POST /agent/run` requests also accept these optional fields:

| Field | Description |
|-------|-------------|
| `system_instructions` | Additional system-level instructions merged into the agent prompt |
| `skill_paths` | Paths to custom skill files relative to the target repo root |
| `agent_paths` | Paths to Markdown custom agent definitions (e.g. `.github/agents/security-reviewer.agent.md`) |
| `callback_url` | URL to receive an HMAC-signed webhook on run completion |
| `timeout_minutes` | Maximum execution time in minutes (default: 30, range: 1–60) |

See the [agent runbook](app/docs/runbook.md) for complete API documentation
including all request/response fields, webhook payload shapes, custom agent
format, and error codes.

---

## Deployment

The CI/CD pipeline (`.github/workflows/deploy.yml`) automatically builds,
tests, pushes to ECR, and deploys via CDK on every merge to `main`.

For first-time setup or manual deployment, see the
[Deployment section](app/docs/runbook.md#deployment) in the agent runbook.

### Infrastructure

The AWS stack is defined in `infra/stacks/copilot_dispatch_stack.py` using CDK (Python):

| Resource | Configuration |
|----------|--------------|
| **AppRunner** | 1 vCPU, 2 GB RAM, scale-to-zero (min 0, max 1), HTTPS, health check on `/health` |
| **DynamoDB** | `dispatch-runs` table, on-demand billing, TTL enabled, point-in-time recovery |
| **ECR** | `copilot-dispatch` repository, image scan on push, lifecycle rules |
| **Secrets Manager** | `dispatch/api-key`, `dispatch/github-pat`, `dispatch/webhook-secret` (pre-existing) |
| **IAM** | Instance role for DynamoDB + Secrets Manager access; ECR access role for AppRunner |

---

## Development

### Daily workflow

```bash
source .venv/bin/activate
set -o allexport; source .env/.env.local; set +o allexport
docker compose up -d
uvicorn app.src.main:app --reload --port 8000
```

### Running tests

```bash
# All tests (unit + integration, E2E auto-skipped)
pytest -q --cov=app/src --cov-report=term-missing

# Quality gates
black --check app/src/ tests/ scripts/
isort --check-only app/src/ tests/ scripts/
python scripts/evals.py

# E2E tests (requires production deployment)
pytest --e2e-confirm tests/e2e/ -v
```

### Adding dependencies

Add to `pyproject.toml` under `[project.dependencies]` (production) or
`[project.optional-dependencies] dev` (development), then reinstall:

```bash
pip install -e ".[dev]"
```

---

## Documentation

| Document | Description |
|----------|-------------|
| [app/docs/runbook.md](app/docs/runbook.md) | Operational runbook — local dev, API usage, agent execution, deployment, troubleshooting, secret rotation |
| [docs/brief.md](docs/brief.md) | Project brief — problem statement, goals, requirements, constraints |
| [docs/solution-design.md](docs/solution-design.md) | Technical solution design — architecture, data model, API contracts |
| [docs/phase-plan.md](docs/phase-plan.md) | Phase delivery plan — 5 phases, 28 components |
| [docs/phase-summary.md](docs/phase-summary.md) | Completed phase summaries with key decisions and outcomes |
| [docs/docker-guide.md](docs/docker-guide.md) | Docker user guide — build, run, push workflows |

The API also serves auto-generated OpenAPI/Swagger documentation at `/docs`
on both local and production deployments.

---

## License

See [LICENSE](LICENSE) for details.
