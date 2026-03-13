# Repository Analysis: Autopilot — Public Release Readiness

**Analysed:** 10 March 2026  
**Scope:** Full repository — public release feasibility, naming, secrets, workflow visibility  
**Primary language(s):** Python 3.13, AWS CDK (Python), GitHub Actions YAML  

---

## Executive Summary

Autopilot is a well-structured FastAPI REST API (~9,400 lines of source, ~17,500 lines of tests) that orchestrates GitHub Copilot SDK agent sessions via GitHub Actions workflow dispatch. The codebase is production-grade with comprehensive typing, Google-style docstrings, proper error handling, and a clean layered architecture. It deploys to AWS AppRunner via CDK with DynamoDB, ECR, and Secrets Manager.

**Decision:** Create a public repository named **`copilot-dispatch`** (`sjmeehan9/copilot-dispatch`) with a fresh git history. The private `sjmeehan9/autopilot` repo continues operating unchanged. The rename touches ~680 occurrences across 10 ordered passes. A new AWS profile (`copilot-dispatch`) will deploy a parallel stack on the same account with distinct resource names (`dispatch-runs`, `dispatch-api`, `dispatch/*` secrets). See the **Implementation Plan** section for the full 7-phase walkthrough.

---

## Analysis: Your Three Concerns

### Concern 1: GitHub Actions Workflow Run Visibility

**The problem:** On a public repo, anyone can see the Actions tab — workflow run logs, input parameters (including instructions, repository names, run IDs), and timing data.

**What's exposed in `agent-executor.yml` logs:**
- `run_id`, `target_repository`, `role`, `agent_instructions`, `model` — all passed as `workflow_dispatch` inputs and visible in the Actions UI
- Workflow run durations, success/failure status
- The `copilot` environment name is visible (reveals you use environment-scoped secrets)
- The `PAT`, `ANTHROPIC_API_KEY`, `OPENAI_API_KEY`, `WEBHOOK_SECRET` secret *names* are visible (but values are masked by GitHub)

**What's NOT exposed:**
- Secret *values* — GitHub always masks these in logs
- `.env.local` — never committed (confirmed: not in git history)
- No hardcoded API keys or tokens exist in the committed codebase

**Mitigation options:**

| Option | Effort | Effectiveness |
|--------|--------|---------------|
| **A. Keep the private repo for operations, open-source a separate clean copy** | Medium | Complete — no workflow runs visible |
| **B. Make the repo public but move agent-executor to a separate private repo** | High | Partial — CI/deploy runs still visible |
| **C. Make the repo public, accept that workflow metadata is visible** | Low | Minimal — inputs are visible to anyone |

**Recommendation:** **Option A** is your best path. You keep `sjmeehan9/autopilot` as your private operational repo (with workflow history, Actions runs, and secrets). You create a new public repo under a different name with a clean `git init` — no workflow run history.

---

### Concern 2: Secrets Proximity to Public View

**Current secrets posture (good):**
- `.env/.env.local` is gitignored and was **never committed** to git history ✓
- `.env/.env.example` contains only placeholder values (`your-api-key-here`) ✓
- `.env/.env.test` contains only dummy test values ✓
- Production secrets are in AWS Secrets Manager (prefixed `autopilot/*`) ✓
- GitHub Actions secrets are stored in the repo's encrypted secrets store ✓
- No hardcoded API keys, PATs, or tokens exist in any committed file ✓

**What would be exposed if you simply made this repo public:**
- The **names** of your AWS Secrets Manager secrets: `dispatch/api-key`, `dispatch/github-pat`, `dispatch/webhook-secret`, `dispatch/openai-api-key`
- The **AWS region** (`ap-southeast-2`) hardcoded in `infra/stacks/autopilot_stack.py` and `cdk.json`
- Your **GitHub username** (`sjmeehan9`) hardcoded in:
  - `infra/stacks/autopilot_stack.py:478` (`DISPATCH_GITHUB_OWNER` = `"sjmeehan9"`)
  - `README.md:58` (clone URL)
  - `tests/e2e/conftest.py:30` (default test target)
  - `scripts/smoke_test.py:45` (default test target)
  - `docs/` (various implementation notes)
- Your **AppRunner service URL pattern** in `.env.example` comments

**For the public copy:** These should be parameterised (replaced with `YOUR_GITHUB_OWNER`, `your-org`, or read from config) rather than hardcoded.

---

### Concern 3: The "autopilot" Name is Embedded Everywhere

**Quantified scope of the rename:**

| Category | Occurrences | Rename Difficulty |
|----------|-------------|-------------------|
| **Python env prefix** (`DISPATCH_`) | ~90 | Mechanical find-replace in `config.py` + all env var refs |
| **AWS resource names** (`dispatch-runs`, `dispatch-api`, `autopilot/*`) | ~30 | CDK stack + `cdk.json` + `settings.yaml` |
| **Python class names** (`CopilotDispatchStack`, `AppError`, etc.) | ~15 | Clean refactor, IDE-assisted |
| **Package name** (`pyproject.toml`, imports) | ~5 | `pyproject.toml` name field, egg-info |
| **Logger names** (`autopilot.agent.runner`, etc.) | ~12 | String literals in source |
| **User-facing strings** (`"Autopilot Bot"`, PR titles, review headers) | ~10 | Branding strings |
| **Documentation** (README, docstrings, runbook, phase docs) | ~400+ | Documentation rewrite |
| **Tests** (assertions on config defaults, env vars, strings) | ~100+ | Test fixture updates |
| **Git repo name / GitHub refs** | ~20 | URL, clone path, workflow refs |
| **Docker image tag** (`autopilot:sha`) | ~5 | `deploy.yml` |
| **Total estimated** | **~680+** | |

**Key architectural constraint:** The env var prefix `DISPATCH_` is load-bearing — it's the convention used by `app/src/config.py:29` (`_ENV_PREFIX = "DISPATCH_"`) and flows through to all environment variable resolution. Renaming this prefix changes the interface contract for anyone deploying the application.

---

## Implementation Plan — `copilot-dispatch`

**Chosen name:** `copilot-dispatch`  
**Private repo (unchanged):** `sjmeehan9/autopilot`  
**Public repo (new):** `sjmeehan9/copilot-dispatch`  
**Strategy:** Fresh `git init` with renamed source, no history carried over  

---

### Phase 1: Create the GitHub Repo and Local Workspace

#### Step 1.1 — Create the public repo on GitHub

```bash
# Create an empty public repo (no README, no .gitignore — we'll push everything)
gh repo create sjmeehan9/copilot-dispatch --public --description \
  "REST API that orchestrates GitHub Copilot SDK agent sessions via GitHub Actions workflow dispatch" \
  --clone
```

#### Step 1.2 — Copy source from autopilot into the new workspace

```bash
cd ~/Projects/autopilot-project

# rsync the autopilot source into copilot-dispatch, excluding build artefacts,
# personal configs, and files that should not be in the public repo.
rsync -av \
  --exclude='.git' \
  --exclude='.claude/' \
  --exclude='data/' \
  --exclude='__pycache__/' \
  --exclude='*.egg-info/' \
  --exclude='cdk.out/' \
  --exclude='node_modules/' \
  --exclude='.mypy_cache/' \
  --exclude='.pytest_cache/' \
  --exclude='.DS_Store' \
  --exclude='docs/implementation-context-phase-*.md' \
  --exclude='docs/phase-*-component-breakdown.md' \
  --exclude='docs/phase-plan.md' \
  --exclude='docs/phase-summary.md' \
  --exclude='docs/autopilot-public-release-analysis-*.md' \
  autopilot/ copilot-dispatch/
```

#### Step 1.3 — Verify the copy and clean up

```bash
cd ~/Projects/autopilot-project/copilot-dispatch

# Remove any stray __pycache__ or .pyc files
find . -type d -name __pycache__ -exec rm -rf {} + 2>/dev/null
find . -name "*.pyc" -delete 2>/dev/null

# Verify no .env.local leaked
ls -la .env/
# Should contain: .env.example, .env.test (NOT .env.local)

# Verify .claude/ and .github/agents/ are absent
ls .claude 2>&1      # Should say "No such file or directory"
ls .github/agents 2>&1  # Should say "No such file or directory"
```

---

### Phase 2: Global Rename — `autopilot` → `copilot-dispatch`

The rename is mechanical but touches ~680 occurrences. The replacements must be applied in a specific order to avoid double-replacing. The agent performing Phase 2 should apply these in **ordered passes**.

#### Naming Convention Map

| Context | autopilot form | copilot-dispatch form |
|---------|---------------|----------------------|
| Repo / package name | `autopilot` | `copilot-dispatch` |
| Python importable package | `autopilot` | `copilot_dispatch` (underscored) |
| Env var prefix | `DISPATCH_` | `DISPATCH_` |
| DynamoDB table | `dispatch-runs` | `dispatch-runs` |
| ECR repository | `autopilot` | `copilot-dispatch` |
| AppRunner service | `dispatch-api` | `dispatch-api` |
| Auto-scaling config | `dispatch-scaling` | `dispatch-scaling` |
| Secrets Manager prefix | `autopilot/` | `dispatch/` |
| CDK stack name | `autopilot` | `copilot-dispatch` |
| CDK stack class | `CopilotDispatchStack` | `CopilotDispatchStack` |
| Base exception class | `AppError` | `DispatchError` — **CONFLICT** with existing `DispatchError` |
| Bot identity (git commits) | `Autopilot Bot` | `Copilot Dispatch Bot` |
| Bot email | `autopilot@noreply.github.com` | `copilot-dispatch@noreply.github.com` |
| Logger prefix | `autopilot.` | `dispatch.` |
| Docker image tag | `autopilot:sha` | `copilot-dispatch:sha` |

> **Exception naming conflict:** The codebase already has an `app/src/exceptions.py::DispatchError` (for workflow dispatch failures). Renaming the base `AppError` → `DispatchError` would collide. Instead, rename the base to **`AppError`** — it's generic, short, and every subclass already extends it implicitly. The existing `DispatchError` subclass keeps its name unchanged.

#### Pass 1 — Env var prefix (most pervasive, do first)

This pass renames every occurrence of `DISPATCH_` as an environment variable prefix to `DISPATCH_`. This touches `config.py`, `settings.yaml`, `.env.example`, `.env.test`, `conftest.py`, the CDK stack, and all test files.

**Files affected:**
- `app/src/config.py`: `_ENV_PREFIX = "DISPATCH_"` → `_ENV_PREFIX = "DISPATCH_"`
- `app/config/settings.yaml`: all `DISPATCH_` references in comments
- `.env/.env.example`: all `DISPATCH_` env var names → `DISPATCH_`
- `.env/.env.test`: all `DISPATCH_` env var names → `DISPATCH_`
- `tests/conftest.py`: all `monkeypatch.setenv("DISPATCH_*")` → `DISPATCH_*`
- `tests/unit/test_config.py`: all `DISPATCH_` assertions and env vars
- `tests/unit/test_api_key.py`: all `DISPATCH_` env vars
- `tests/unit/test_health.py`: `DISPATCH_WEBHOOK_SECRET`
- `tests/unit/test_result_endpoint.py`: `DISPATCH_WEBHOOK_SECRET`
- `tests/unit/test_component_1_2_scaffolding.py`: `DISPATCH_API_KEY`, `DISPATCH_WEBHOOK_SECRET`
- `tests/unit/test_runner.py`: `DISPATCH_DISABLE_EXPLICIT_GITHUB_TOKEN`
- `tests/unit/test_routes.py`: `DISPATCH_SERVICE_URL`
- `tests/unit/test_cdk_stack.py`: all `DISPATCH_` env var names in assertions
- `tests/integration/test_full_api_flow.py`: all `DISPATCH_` env vars
- `tests/e2e/conftest.py`: `DISPATCH_E2E_*` → `DISPATCH_E2E_*`
- `app/src/api/routes.py`: `DISPATCH_SERVICE_URL` warning messages
- `app/src/agent/runner.py`: `DISPATCH_DISABLE_EXPLICIT_GITHUB_TOKEN`
- `infra/stacks/autopilot_stack.py`: all `DISPATCH_` env var injections
- `.github/workflows/deploy.yml`: (none — uses `secrets.*` not `DISPATCH_`)

**Exact transformation:** `DISPATCH_` → `DISPATCH_` (case-sensitive prefix replacement)

#### Pass 2 — AWS resource names

| Find (exact) | Replace | Files |
|--------------|---------|-------|
| `"dispatch-runs"` | `"dispatch-runs"` | `config.py`, `settings.yaml`, `cdk.json`, CDK stack, tests |
| `"dispatch-api"` | `"dispatch-api"` | CDK stack, `test_cdk_stack.py` |
| `"dispatch-scaling"` | `"dispatch-scaling"` | CDK stack |
| `"dispatch/api-key"` | `"dispatch/api-key"` | `api_key.py`, `test_api_key.py`, CDK stack |
| `"dispatch/github-pat"` | `"dispatch/github-pat"` | `dispatcher.py`, `actions.py`, `test_dispatcher.py`, CDK stack |
| `"dispatch/webhook-secret"` | `"dispatch/webhook-secret"` | `routes.py`, CDK stack, `test_secret.py` |
| `"dispatch/openai-api-key"` | `"dispatch/openai-api-key"` | CDK stack |
| `secret:dispatch/*` | `secret:dispatch/*` | CDK stack (IAM policy) |

#### Pass 3 — Python class names

| Find | Replace | Files |
|------|---------|-------|
| `CopilotDispatchStack` | `CopilotDispatchStack` | `infra/stacks/autopilot_stack.py`, `infra/app.py`, `tests/unit/test_cdk_stack.py` |
| `AppError` | `AppError` | `app/src/exceptions.py` (class + all references), all test files importing it |
| `app_error_handler` | `app_error_handler` | `app/src/exceptions.py` |
| `autopilot_stack` (module name) | `copilot_dispatch_stack` | **Rename the file** `infra/stacks/autopilot_stack.py` → `infra/stacks/copilot_dispatch_stack.py`, update imports |

#### Pass 4 — Logger names, branding, bot identity

| Find | Replace | Files |
|------|---------|-------|
| `"autopilot.` (logger prefix) | `"dispatch.` | `main.py`, `middleware.py`, `routes.py`, `runner.py`, `prompts.py`, `result.py`, `implement.py`, `review.py`, `merge.py` |
| `"Autopilot Bot"` | `"Copilot Dispatch Bot"` | `implement.py`, `merge.py`, tests |
| `"autopilot@noreply.github.com"` | `"copilot-dispatch@noreply.github.com"` | `implement.py`, `merge.py`, tests |
| `"Autopilot Code Review"` | `"Copilot Dispatch Code Review"` | `review.py`, tests |
| `"Autopilot: "` (PR title prefix) | `"Dispatch: "` | `implement.py`, tests |
| `"*Created by Autopilot"` | `"*Created by Copilot Dispatch"` | `implement.py` |
| `"*Generated by Autopilot"` | `"*Generated by Copilot Dispatch"` | `review.py` |
| `title="Autopilot"` | `title="Copilot Dispatch"` | `main.py` |
| `"Starting Autopilot API"` | `"Starting Copilot Dispatch API"` | `main.py` |
| `"Shutting down Autopilot API"` | `"Shutting down Copilot Dispatch API"` | `main.py` |
| `"autopilot-e2e"` (git author in e2e) | `"dispatch-e2e"` | `tests/e2e/helpers.py` |

#### Pass 5 — Package name and pyproject.toml

| Find | Replace | Files |
|------|---------|-------|
| `name = "autopilot"` | `name = "copilot-dispatch"` | `pyproject.toml` |
| `"Autopilot —` (description) | `"Copilot Dispatch —` | `pyproject.toml` |
| `name = "Autopilot Team"` | `name = "Copilot Dispatch Contributors"` | `pyproject.toml` |

> **Note:** The `pyproject.toml` package name changes but the `[tool.setuptools.packages.find]` section uses `include = ["app*", "infra*", "tests*"]` — the top-level folder names (`app/`, `infra/`, `tests/`) do **not** change so imports (`from app.src.services...`) remain identical.

#### Pass 6 — CDK context and infrastructure config

**`infra/cdk.json`** — update context values:
```json
{
  "app": "python app.py",
  "context": {
    "aws_region": "ap-southeast-2",
    "stack_name": "copilot-dispatch",
    "dynamodb_table_name": "dispatch-runs",
    "ecr_repository_name": "copilot-dispatch",
    "github_owner": "sjmeehan9",
    "github_repo": "copilot-dispatch",
    "apprunner_cpu": "1024",
    "apprunner_memory": "2048",
    "apprunner_min_size": "1",
    "apprunner_max_size": "1"
  }
}
```

**`infra/stacks/copilot_dispatch_stack.py`** — make `github_owner` and `github_repo` config-driven:
```python
# In __init__, add these to the context reads:
github_owner: str = (
    self.node.try_get_context("github_owner") or "sjmeehan9"
)
github_repo: str = (
    self.node.try_get_context("github_repo") or "copilot-dispatch"
)
```

Then in `_create_apprunner_service`, replace the hardcoded values:
```python
apprunner.CfnService.KeyValuePairProperty(
    name="DISPATCH_GITHUB_OWNER",
    value=github_owner,     # was hardcoded "sjmeehan9"
),
apprunner.CfnService.KeyValuePairProperty(
    name="DISPATCH_GITHUB_REPO",
    value=github_repo,      # was hardcoded "autopilot"
),
```

> Pass these as parameters to `_create_apprunner_service()` (add `github_owner: str` and `github_repo: str` to its signature and the call site).

**`app/config/settings.yaml`** — update defaults:
```yaml
github_repo: "copilot-dispatch"    # was "autopilot"
```

**`app/src/config.py`** — update defaults:
```python
github_repo: str = "copilot-dispatch"    # was "autopilot"
```

#### Pass 7 — Docker and deploy workflow

**`.github/workflows/deploy.yml`:**
```yaml
env:
  ECR_REPOSITORY: copilot-dispatch   # was: autopilot
```
And all `docker build -t copilot-dispatch:` → `docker build -t copilot-dispatch:` and `docker tag copilot-dispatch:` → `docker tag copilot-dispatch:`.

**`Dockerfile`** — update header comment only (no functional change).

#### Pass 8 — Env template files

**`.env/.env.example`:**
- All `DISPATCH_` env var names → `DISPATCH_`
- `DYNAMODB_TABLE_NAME=dispatch-runs` → `DYNAMODB_TABLE_NAME=dispatch-runs`
- `GITHUB_REPO=copilot-dispatch` → `GITHUB_REPO=copilot-dispatch`
- Header comment: `# Autopilot —` → `# Copilot Dispatch —`

> **Note:** The non-prefixed env var names in `.env.example` (`DYNAMODB_TABLE_NAME`, `GITHUB_PAT`, `GITHUB_OWNER`, `GITHUB_REPO`, `AWS_REGION`) are for local development convenience — they are NOT the canonical config mechanism (that's `DISPATCH_*` prefix via `config.py`). Keep these as-is for discoverability but update their comments and defaults.

**`.env/.env.test`:**
- All `DISPATCH_` env var names → `DISPATCH_`
- `DYNAMODB_TABLE_NAME=dispatch-runs-test` → `DYNAMODB_TABLE_NAME=dispatch-runs-test`

#### Pass 9 — Docstrings, comments, and remaining string references

Every docstring and comment that says "Autopilot" → "Copilot Dispatch". This is the largest pass by volume (~300+ occurrences) but is purely cosmetic and low-risk. It covers:

- Module-level docstrings in all `__init__.py` files
- Class and function docstrings referencing "Autopilot"
- Inline comments
- `.gitignore` section headers

#### Pass 10 — sjmeehan9 and autopilot-test-target references

`sjmeehan9` is kept as the **actual value** but made config-driven where it was previously hardcoded:

| Location | Current | Change |
|----------|---------|--------|
| CDK stack `DISPATCH_GITHUB_OWNER` | Hardcoded `"sjmeehan9"` | Read from CDK context (default: `"sjmeehan9"`) |
| CDK stack `DISPATCH_GITHUB_REPO` | Hardcoded `"autopilot"` | Read from CDK context (default: `"copilot-dispatch"`) |
| `app/config/settings.yaml` `github_owner` | `null` | Stays `null` (set via env var) ✓ already config-driven |
| `tests/e2e/conftest.py` | `_DEFAULT_REPOSITORY = "sjmeehan9/autopilot-test-target"` | Keep as-is — this is a real test target |
| `scripts/smoke_test.py` | `_DEFAULT_REPOSITORY = "sjmeehan9/autopilot-test-target"` | Keep as-is — this is a real test target |
| `tests/unit/test_actions_models.py` | `"actor": {"login": "sjmeehan9", ...}` | Keep as-is — test fixture data |
| `README.md` | `git clone https://github.com/sjmeehan9/autopilot.git` | → `git clone https://github.com/sjmeehan9/copilot-dispatch.git` |

---

### Phase 3: AWS Setup for copilot-dispatch

Since you're using the same AWS account with a new profile, the `copilot-dispatch` stack will create **separate resources** coexisting alongside the existing `autopilot` stack.

#### Step 3.1 — Create a new AWS CLI profile

```bash
aws configure --profile copilot-dispatch
# AWS Access Key ID: (same account, can be same or different IAM user)
# AWS Secret Access Key: ...
# Default region: ap-southeast-2
# Default output format: json
```

#### Step 3.2 — Create Secrets Manager secrets for the new prefix

The new stack expects secrets under the `dispatch/` prefix. These are separate from your existing `autopilot/*` secrets:

```bash
# You can use the same secret values as autopilot, or generate new ones
aws secretsmanager create-secret \
  --name "dispatch/api-key" \
  --secret-string "$(python -c 'import secrets; print(secrets.token_hex(32))')" \
  --region ap-southeast-2 \
  --profile copilot-dispatch

aws secretsmanager create-secret \
  --name "dispatch/webhook-secret" \
  --secret-string "$(python -c 'import secrets; print(secrets.token_hex(32))')" \
  --region ap-southeast-2 \
  --profile copilot-dispatch

aws secretsmanager create-secret \
  --name "dispatch/github-pat" \
  --secret-string "YOUR_GITHUB_PAT_VALUE" \
  --region ap-southeast-2 \
  --profile copilot-dispatch

aws secretsmanager create-secret \
  --name "dispatch/openai-api-key" \
  --secret-string "YOUR_OPENAI_KEY_VALUE" \
  --region ap-southeast-2 \
  --profile copilot-dispatch
```

#### Step 3.3 — Bootstrap CDK (if not already done for this profile)

```bash
cd ~/Projects/autopilot-project/copilot-dispatch/infra
npx cdk bootstrap aws://ACCOUNT_ID/ap-southeast-2 --profile copilot-dispatch
```

#### Step 3.4 — Resource coexistence check

The following resources will be created **alongside** your existing autopilot resources:

| Resource | autopilot (existing) | copilot-dispatch (new) | Conflict? |
|----------|---------------------|----------------------|-----------|
| DynamoDB table | `dispatch-runs` | `dispatch-runs` | No |
| ECR repository | `autopilot` | `copilot-dispatch` | No |
| AppRunner service | `dispatch-api` | `dispatch-api` | No |
| Secrets Manager | `autopilot/*` | `dispatch/*` | No |
| CloudFormation stack | `autopilot` | `copilot-dispatch` | No |
| Auto-scaling config | `dispatch-scaling` | `dispatch-scaling` | No |

No conflicts — all resource names are distinct by design.

---

### Phase 4: Test Locally

#### Step 4.1 — Set up the Python environment

```bash
cd ~/Projects/autopilot-project/copilot-dispatch

python3.13 -m venv .venv
source .venv/bin/activate
pip install -e ".[dev]"
```

#### Step 4.2 — Run the test suite

```bash
# Load env
set -o allexport; source .env/.env.test; set +o allexport

# Run unit tests (should pass without any AWS/GitHub connectivity)
pytest tests/unit/ -q --cov=app/src --cov-report=term-missing

# If all pass, the rename is clean.
# If failures, they will be in string assertions — fix the expected values.
```

#### Step 4.3 — Run local development stack

```bash
# Load real local env
set -o allexport; source .env/.env.local; set +o allexport

# Start DynamoDB Local + API server
bash scripts/dev.sh

# Health check
curl http://localhost:8000/health
```

---

### Phase 5: Configure GitHub Actions Secrets on the Public Repo

The `copilot-dispatch` repo needs its own GitHub Actions secrets for CI, deploy, and agent-executor workflows.

#### Step 5.1 — Repository secrets

```bash
# Set secrets on the copilot-dispatch repo
gh secret set PAT --repo sjmeehan9/copilot-dispatch
gh secret set AWS_ACCESS_KEY_ID --repo sjmeehan9/copilot-dispatch
gh secret set AWS_SECRET_ACCESS_KEY --repo sjmeehan9/copilot-dispatch
gh secret set AWS_REGION --body "ap-southeast-2" --repo sjmeehan9/copilot-dispatch
gh secret set AWS_ACCOUNT_ID --repo sjmeehan9/copilot-dispatch
gh secret set SERVICE_URL --repo sjmeehan9/copilot-dispatch
gh secret set APPRUNNER_SERVICE_ARN --repo sjmeehan9/copilot-dispatch
```

#### Step 5.2 — `copilot` environment secrets (for agent-executor)

```bash
# Create the "copilot" environment on the repo first (via GitHub UI or API)
# Then set environment-scoped secrets:
gh secret set ANTHROPIC_API_KEY --repo sjmeehan9/copilot-dispatch --env copilot
gh secret set OPENAI_API_KEY --repo sjmeehan9/copilot-dispatch --env copilot
gh secret set PAT --repo sjmeehan9/copilot-dispatch --env copilot
gh secret set WEBHOOK_SECRET --repo sjmeehan9/copilot-dispatch --env copilot
```

---

### Phase 6: Deploy and Verify

#### Step 6.1 — Initial CDK deploy (ECR only, no AppRunner)

```bash
cd ~/Projects/autopilot-project/copilot-dispatch/infra

# First deploy: ECR + DynamoDB only (AppRunner needs an image first)
AWS_PROFILE=copilot-dispatch cdk deploy \
  --app "python app.py" \
  -c deploy_apprunner=false \
  --require-approval never
```

#### Step 6.2 — Build and push Docker image

```bash
cd ~/Projects/autopilot-project/copilot-dispatch

# Login to ECR
aws ecr get-login-password --region ap-southeast-2 --profile copilot-dispatch \
  | docker login --username AWS --password-stdin ACCOUNT_ID.dkr.ecr.ap-southeast-2.amazonaws.com

# Build
docker build -t copilot-dispatch:latest .

# Tag and push
docker tag copilot-dispatch:latest ACCOUNT_ID.dkr.ecr.ap-southeast-2.amazonaws.com/copilot-dispatch:latest
docker push ACCOUNT_ID.dkr.ecr.ap-southeast-2.amazonaws.com/copilot-dispatch:latest
```

#### Step 6.3 — Full CDK deploy (with AppRunner)

```bash
cd ~/Projects/autopilot-project/copilot-dispatch/infra

# Now deploy with AppRunner
AWS_PROFILE=copilot-dispatch cdk deploy \
  --app "python app.py" \
  -c service_url=https://<apprunner-url-from-output> \
  --require-approval never
```

#### Step 6.4 — Smoke test the deployed service

```bash
# Use the smoke test script pointed at the new deployment
python scripts/smoke_test.py \
  --api-url https://<new-apprunner-url> \
  --api-key <new-api-key> \
  --repository sjmeehan9/autopilot-test-target
```

---

### Phase 7: Commit and Push to Public Repo

Only after all tests pass locally and the deployment is verified:

```bash
cd ~/Projects/autopilot-project/copilot-dispatch

git add -A
git commit -m "Initial release — Copilot Dispatch API

REST API service that orchestrates GitHub Copilot SDK agent sessions
via GitHub Actions workflow dispatch. Accepts run requests, dispatches
workflows, and delivers structured results via webhook."

git push origin main
```

---

### Phase Summary Checklist

- [ ] **Phase 1:** GitHub repo created, source copied with exclusions
- [ ] **Phase 2:** All 10 rename passes applied, no `autopilot` references remain (except `autopilot-test-target` test fixtures)
- [ ] **Phase 3:** AWS profile created, Secrets Manager secrets provisioned under `dispatch/` prefix
- [ ] **Phase 4:** Unit tests pass, local dev stack runs, health check responds
- [ ] **Phase 5:** GitHub Actions secrets configured on `copilot-dispatch` repo
- [ ] **Phase 6:** CDK deployed, Docker image pushed, AppRunner service healthy, smoke test passes
- [ ] **Phase 7:** Committed and pushed to public repo
- [ ] **Post-push:** CI workflow runs green on the public repo
- [ ] **Post-push:** Verify Actions tab shows only the CI run (no historical agent-executor runs)

### Execution Status (14 March 2026)

- [x] **Phase 3.3:** CDK bootstrap completed for profile `copilot-dispatch` (`python -m infra.app` app entrypoint).
- [x] **Phase 3.4:** Resource coexistence check completed (`autopilot` and `copilot-dispatch` stacks verified in CloudFormation context).
- [x] **Phase 4:** Environment setup and unit test validation completed; local `/health` endpoint confirmed healthy.
- [x] **Phase 5:** GitHub repo/environment secrets configured, including `PAT`, AWS deploy secrets, `SERVICE_URL`, `APPRUNNER_SERVICE_ARN`, and `copilot` environment secrets (`OPENAI_API_KEY`, `PAT`, `WEBHOOK_SECRET`).
- [x] **Phase 6:** Base and full CDK deployments completed, image pushed to ECR (`linux/amd64`), AppRunner service is `RUNNING`, and production smoke test passed (`5/5`).
- [x] **Phase 7:** Initial release committed and pushed to `origin/main` with fresh public history.
- [x] **Post-push CI:** Latest CI run completed successfully on `main`.
- [ ] **Post-push Actions visibility note:** Actions currently include `CI`, `Deploy`, and `Agent Executor` runs (not CI-only).

---

## Appendix A: Files Requiring Modification During Rename

### Source Code (must rename)
| File | Pass | Changes Required |
|------|------|-----------------|
| `app/src/config.py` | 1, 2, 9 | `_ENV_PREFIX` → `DISPATCH_`, `dynamodb_table_name` default, `github_repo` default, docstrings |
| `app/src/main.py` | 4, 9 | Logger name `dispatch.lifespan`, `title="Copilot Dispatch"`, startup/shutdown messages |
| `app/src/exceptions.py` | 3, 9 | `AppError` → `AppError`, handler function name, all subclass bases, docstrings |
| `app/src/auth/api_key.py` | 2 | `"dispatch/api-key"` → `"dispatch/api-key"` |
| `app/src/auth/secrets.py` | 9 | Docstrings only |
| `app/src/api/routes.py` | 1, 2, 4, 9 | Secret name, logger, `DISPATCH_SERVICE_URL` refs, docstring |
| `app/src/api/models.py` | 9 | Docstring |
| `app/src/api/middleware.py` | 4, 9 | Logger name, docstring |
| `app/src/api/dependencies.py` | 9 | Docstring |
| `app/src/api/actions_routes.py` | 9 | Review for name refs |
| `app/src/api/actions_models.py` | 9 | Review for name refs |
| `app/src/services/dispatcher.py` | 2 | `"dispatch/github-pat"` → `"dispatch/github-pat"` |
| `app/src/services/actions.py` | 2 | `"dispatch/github-pat"` → `"dispatch/github-pat"` |
| `app/src/services/run_store.py` | 9 | Review |
| `app/src/services/webhook.py` | 9 | Review |
| `app/src/agent/runner.py` | 1, 4, 9 | `DISPATCH_DISABLE_EXPLICIT_GITHUB_TOKEN`, logger, CLI description |
| `app/src/agent/prompts.py` | 4 | Logger name |
| `app/src/agent/result.py` | 4 | Logger name |
| `app/src/agent/roles/implement.py` | 4 | `_BOT_NAME`, `_BOT_EMAIL`, PR title/body, logger |
| `app/src/agent/roles/review.py` | 4 | Review header, generated-by footer, logger |
| `app/src/agent/roles/merge.py` | 4 | `_BOT_NAME`, `_BOT_EMAIL`, logger |
| `app/config/settings.yaml` | 1, 2, 6, 9 | All comments, default values |
| `app/config/prompts/*.md` | 9 | Review for branding |

### Infrastructure (must rename)
| File | Pass | Changes Required |
|------|------|-----------------|
| `infra/stacks/autopilot_stack.py` | 2, 3, 6 | **Rename file** → `copilot_dispatch_stack.py`, class → `CopilotDispatchStack`, resource names, secret prefixes, `github_owner`/`github_repo` from CDK context |
| `infra/app.py` | 3, 6, 9 | Import + class instantiation + default stack name |
| `infra/cdk.json` | 6 | Add `github_owner`, `github_repo` context; update `stack_name`, `dynamodb_table_name`, `ecr_repository_name` |
| `infra/__init__.py` | 9 | Docstring |
| `infra/stacks/__init__.py` | 9 | Docstring |
| `infra/test_boto3.py` | 2 | Secret name |
| `infra/test_secret.py` | 2 | Secret name |
| `infra/requirements.txt` | 9 | Comment |

### Workflows (must rename)
| File | Pass | Changes Required |
|------|------|-----------------|
| `.github/workflows/deploy.yml` | 7 | `ECR_REPOSITORY`, docker tag names |
| `.github/workflows/agent-executor.yml` | 9 | Comment about package install |
| `.github/workflows/ci.yml` | — | None |

### Config Files (must rename)
| File | Pass | Changes Required |
|------|------|-----------------|
| `pyproject.toml` | 5 | Package name, description, authors |
| `Dockerfile` | 9 | Header comment |
| `.env/.env.example` | 1, 8 | All `DISPATCH_` → `DISPATCH_`, defaults, comments |
| `.env/.env.test` | 1, 8 | All `DISPATCH_` → `DISPATCH_`, table name |
| `.gitignore` | 9 | Comment |
| `README.md` | 10, 9 | Complete rewrite for public audience |

### Tests (must rename — all)
All 30+ test files reference `DISPATCH_` env vars, `dispatch-runs` table names, `AppError`, or other branded strings. Each needs corresponding updates across passes 1-4 and 9.

### Key test files with heavy changes:
| File | Pass | Key Changes |
|------|------|-------------|
| `tests/conftest.py` | 1 | All `DISPATCH_` env vars → `DISPATCH_` |
| `tests/unit/test_config.py` | 1, 2, 9 | Env var names, default assertions, docstrings |
| `tests/unit/test_cdk_stack.py` | 1, 2, 3 | Class name, resource names, env var assertions |
| `tests/unit/test_exceptions.py` | 3 | `AppError` → `AppError` |
| `tests/unit/test_implement_role.py` | 4 | Bot name, email, PR title assertions |
| `tests/unit/test_review_role.py` | 4 | Review header assertion |
| `tests/unit/test_api_key.py` | 1, 2 | Env vars, secret name |
| `tests/unit/test_dispatcher.py` | 2 | Secret name |
| `tests/e2e/conftest.py` | 1 | `DISPATCH_E2E_*` env vars |
| `tests/e2e/helpers.py` | 4 | `"dispatch-e2e"` git author |
| `tests/e2e/test_actions_flow.py` | 10 | Repo name references |

---

## Appendix B: Files Excluded from Public Repo

| File/Directory | Reason |
|---------------|--------|
| `.claude/` | Personal agent memory and configurations |
| `.github/agents/` | Personal Copilot agent definitions |
| `.github/instructions/` | Personal Copilot instructions (can recreate for public if desired) |
| `docs/implementation-context-phase-*.md` | Implementation journal with personal details |
| `docs/phase-*-component-breakdown.md` | Internal specifications |
| `docs/phase-plan.md` | Internal planning |
| `docs/phase-summary.md` | Contains personal username correction history |
| `docs/autopilot-public-release-analysis-*.md` | This analysis document |
| `app/docs/runbook.md` | Operational procedures with personal infra details |
| `data/` | Local DynamoDB data |
