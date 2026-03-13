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
- **AWS CLI v2** — configured with an AWS profile
- **GitHub CLI** — authenticated with a Copilot-enabled account (`gh auth login`)
- **Node.js 22+** — required for CDK CLI (cloud deployment only)
- **AWS CDK CLI** — `npm install -g aws-cdk` (cloud deployment only)
- **GitHub account** — with an active Copilot subscription for the PAT owner

---

## Getting Started — Local Development

### 1. Clone and install

```bash
git clone https://github.com/sjmeehan9/copilot-dispatch.git
cd copilot-dispatch

python3.13 -m venv .venv
source .venv/bin/activate
pip install -e ".[dev]"
```

### 2. Configure environment variables

```bash
cp .env/.env.example .env/.env.local
```

Edit `.env/.env.local` and fill in:

| Variable | Description |
|----------|-------------|
| `DISPATCH_API_KEY` | Secret key for authenticating API requests (generate with `python -c "import secrets; print(secrets.token_hex(32))"`) |
| `DISPATCH_WEBHOOK_SECRET` | HMAC secret for signing webhook payloads (generate the same way) |
| `AWS_ACCESS_KEY_ID` / `AWS_SECRET_ACCESS_KEY` | AWS credentials (only needed for cloud DynamoDB; local uses DynamoDB Local) |
| `GITHUB_PAT` | GitHub classic PAT with `repo` scope from a Copilot-enabled user |
| `GITHUB_OWNER` | GitHub org or user that owns this repo |
| `GITHUB_REPO` | Repository name (default: `copilot-dispatch`) |
| `DYNAMODB_ENDPOINT_URL` | Set to `http://localhost:8100` for local dev (already set in the example) |

### 3. Start the local stack

```bash
bash scripts/dev.sh
```

This starts DynamoDB Local via Docker Compose, creates the `dispatch-runs` table, and launches the FastAPI dev server with hot-reload on port 8000.

### 4. Verify

```bash
curl http://localhost:8000/health
# {"status": "healthy"}
```

The API also serves auto-generated OpenAPI/Swagger documentation at `http://localhost:8000/docs`.

---

## Getting Started — Cloud Deployment (AWS)

Copilot Dispatch is designed to run on **AWS AppRunner** with **DynamoDB** for state storage and **ECR** for container images. The infrastructure is defined as code in `infra/stacks/copilot_dispatch_stack.py` using CDK (Python).

### 1. Bootstrap AWS CDK

```bash
npm install -g aws-cdk
cdk bootstrap aws://<ACCOUNT_ID>/ap-southeast-2 --profile <your-profile>
```

### 2. Create AWS Secrets Manager secrets

The application reads secrets at runtime from Secrets Manager. Create these before deploying:

```bash
aws secretsmanager create-secret --name dispatch/api-key --secret-string "<your-api-key>" --profile <your-profile>
aws secretsmanager create-secret --name dispatch/github-pat --secret-string "<your-pat>" --profile <your-profile>
aws secretsmanager create-secret --name dispatch/webhook-secret --secret-string "<your-secret>" --profile <your-profile>
```

### 3. Create the ECR repository

```bash
aws ecr create-repository --repository-name copilot-dispatch --region ap-southeast-2 --profile <your-profile>
```

### 4. Build and push the Docker image

```bash
ACCOUNT_ID=$(aws sts get-caller-identity --profile <your-profile> --query Account --output text)
ECR_URI="${ACCOUNT_ID}.dkr.ecr.ap-southeast-2.amazonaws.com/copilot-dispatch"

aws ecr get-login-password --region ap-southeast-2 --profile <your-profile> | \
    docker login --username AWS --password-stdin "${ACCOUNT_ID}.dkr.ecr.ap-southeast-2.amazonaws.com"

docker build -t copilot-dispatch:latest .
docker tag copilot-dispatch:latest "${ECR_URI}:latest"
docker push "${ECR_URI}:latest"
```

> **Apple Silicon note:** The Dockerfile pins `--platform=linux/amd64` for AppRunner compatibility. See the [Docker guide](docs/docker-guide.md) for details.

### 5. Deploy the CDK stack

```bash
pip install -r infra/requirements.txt
cdk deploy --app "python infra/app.py" --require-approval never
```

### 6. Configure GitHub repository secrets

The CI/CD and agent-executor workflows require these repository secrets:

| Secret | Description |
|--------|-------------|
| `AWS_ACCOUNT_ID` | Your AWS account ID |
| `AWS_REGION` | AWS region (e.g. `ap-southeast-2`) |
| `AWS_ACCESS_KEY_ID` | IAM access key for CI/CD |
| `AWS_SECRET_ACCESS_KEY` | IAM secret key for CI/CD |
| `APPRUNNER_SERVICE_ARN` | ARN of the deployed AppRunner service |
| `SERVICE_URL` | Public URL of the AppRunner service |

The agent-executor workflow also uses a `copilot` GitHub Actions **environment** with:

| Secret | Description |
|--------|-------------|
| `PAT` | GitHub PAT with `repo` scope (named `PAT`, not `GITHUB_PAT`, because GitHub reserves the `GITHUB_` prefix) |
| `ANTHROPIC_API_KEY` | *(Optional)* Anthropic BYOK key for Claude models |
| `OPENAI_API_KEY` | *(Optional)* OpenAI BYOK key for GPT models |

### 7. Re-enable the deploy workflow

The deploy workflow (`.github/workflows/deploy.yml`) is currently set to **`workflow_dispatch` only** (manual trigger) for public-repo safety. To restore automatic deployments on merge to `main`, update the trigger in `deploy.yml`:

```yaml
# Replace:
on:
  workflow_dispatch:

# With:
on:
  push:
    branches:
      - main
  workflow_dispatch:
```

> **Important:** Do not re-enable automatic deployments until you have configured all repository secrets listed above. The pipeline will fail without them.

---

## API Usage

All endpoints require the `X-API-Key` header. Replace `<url>` with `http://localhost:8000` for local development or your AppRunner service URL for production.

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
| [app/docs/runbook.md](app/docs/runbook.md) | Operational runbook — local dev, API usage, agent execution, deployment, troubleshooting |
| [docs/docker-guide.md](docs/docker-guide.md) | Docker user guide — build, run, push, debugging |

---

## License

See [LICENSE](LICENSE) for details.
