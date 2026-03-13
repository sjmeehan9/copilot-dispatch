---
description: Load these instructions into the AI's context at the commencement of any new session.
# applyTo: '**' # when provided, instructions will automatically be added to the request context when the pattern matches an attached file
---
# Instructions for all Agents

## Preliminaries
- **Language versions:** Python **3.13+**; TypeScript **strict** (Next.js 16+, Node.js 24+).
- **Your role:** MUST provide clean, production-grade, production-ready, high quality code that adheres to the standards below. Do not partially implement anything, you must deliver complete working components, rather than a bunch of place holders or production todo’s. This will be production code, so break down delivery even smaller if it’s too much at once.
- **Local development:** Activate the Python virtual environment with `source .venv/bin/activate` before running any Python code. For TypeScript, ensure you have the necessary dependencies installed and configured.
- **MCP servers:** GitHub MCP enabled.

---

## 0) Quickstart

```bash
# VENV (repo root)
source .venv/bin/activate

# ENV (repo root)
set -o allexport; source .env/.env.local; set +o allexport

# TESTS
pytest -q --cov=app/src --cov-report=term-missing
# or for TypeScript
pnpm test
```

## 1) Application Overview

### Folder Structure

```
copilot-dispatch/
├── .claude/
│   ├── agent-memory/
│   ├── agents/
│   ├── settings.local.json
│   └── skills/
├── .env/
│   ├── .env.local
│   ├── .env.example
│   └── .env.test
├── .github/
│   ├── agents/
│   ├── instructions/
│   │   └── copilot.instructions.md    # this file
│   └── workflows/
├── .venv/
├── app/
│   ├── src/
│   ├── config/
│   └── docs/
├── docs/
├── .gitignore
├── LICENSE
└── README.md
```

### Context Documents

The following documents drive phased implementation and must be kept up to date:

| Document | Location | Purpose |
|----------|----------|---------|
| `brief.md` | `(root-folder-name)/docs/` | Synthesized project brief with problem statement, requirements, constraints |
| `solution-design.md` | `(root-folder-name)/docs/` | Detailed technical solution design document |
| `phase-X-component-breakdown.md` | `(root-folder-name)/docs/` | Complete requirements for every component in a phase |
| `phase-plan.md` | `(root-folder-name)/docs/` | Phase sequencing, dependencies, delivery strategy |
| `implementation-context-phase-X.md` | `(root-folder-name)/docs/` | Running log of implemented components within a phase |
| `phase-summary.md` | `(root-folder-name)/docs/` | Post-completion summary of delivered phases |

## 2) Code Style and Conventions

### Python
- **Version:** Python 3.13+. Enable `from __future__ import annotations` where helpful.
- **Style Guide:** Follow PEP 8 for code style and formatting.
- **Formatting:** Use Black for code formatting and isort for import sorting.
- **Typing:** Comprehensive type hints incl. `TypedDict`, `Protocol`, and `Final`; prefer `|` unions; avoid `Any`.
- **Docstrings:** Use Google style docstrings for all public functions, classes, and modules.
- **Data models:** **Pydantic v2** `BaseModel` for request/response; `dataclasses` for internal state where validation is not required.
- **Imports:** Always use **absolute imports** from the installed package name (e.g., `from app.src.services.auth import AuthService`), never relative imports (`from .services.auth import ...`) and never path-relative scripts-style imports (`from services.auth import ...`). The project must have a `pyproject.toml` (or `setup.py`) at the repo root with the package declared, and the virtual environment must install it in editable mode (`pip install -e .`). This ensures the same import path resolves identically whether code is run via `pytest`, `python -m`, direct script execution, or in production. If a `pyproject.toml` does not yet exist, create one before adding any new modules. Never manipulate `sys.path` at runtime to fix import resolution — that indicates a packaging issue, not an import issue.
- **Error handling:** Use custom exceptions for domain-specific errors; avoid bare `except`.
- **Testing:** Use pytest with fixtures. Only essential unit tests required; aim for 30% coverage. Integration tests and end-to-end tests using provided credentials.
- **Human-gated external system tests:** When an integration test requires a live external system (e.g. broker API, third-party service) that cannot be stubbed, gate execution behind a custom pytest CLI flag and an interactive confirmation fixture. Implementation pattern: (1) register a `--<system>-confirm` flag via `pytest_addoption` in root `conftest.py`; (2) add a `pytest_collection_modifyitems` hook that auto-skips tests marked `requires_<system>` unless the flag is passed; (3) create a session-scoped fixture (e.g. `confirm_<system>_gateway`) that prompts the developer once to verify the external system is running and properly configured, returning connection parameters or skipping if declined; (4) provide a helper to run any event-loop or long-lived client in a daemon thread (e.g. `run_<system>_client_in_thread`); (5) in the test, intercept async callbacks with `threading.Event` for cross-thread coordination and always disconnect/clean up in a `finally` block. Tests must pass deterministically when the external system is available and auto-skip cleanly in CI or when the flag is omitted.
- **Logging:** Use Python's built-in `logging` module with structured logging (e.g., JSON format). Log at appropriate levels (DEBUG, INFO, WARNING, ERROR, CRITICAL). Never log sensitive information such as API keys or provider configs.
- **Config:** Centralise in `config/` (YAML). Never log provider configs or API keys. Support env overrides.

### TypeScript
- **Version:** TypeScript strict mode (Next.js 16+, Node.js 24+).
- **Style Guide:** Follow Airbnb TypeScript style guide.
- **Formatting:** Use Prettier for code formatting and ESLint for linting.
- **Typing:** Strict typing with interfaces and types; avoid `any`.
- **Docstrings:** Use TSDoc for all public functions, classes, and modules.
- **Testing:** Use Jest with React Testing Library for unit tests; Cypress for end-to-end tests. Only essential unit tests required; aim for 30% coverage.
- **Config:** Centralise in `config/` (YAML or JSON). Never log provider configs or API keys.

### Completion Standards (All Languages)
- **No placeholders:** Code must never contain `pass`, `...`, `# TODO`, `// TODO`, `FIXME`, `NotImplementedError`, or `throw new Error('not implemented')` in delivered components.
- **No partial files:** Every file must be syntactically valid and functionally complete for its stated scope.
- **No deferred work:** If a function is declared, it must be implemented. If a dependency is imported, it must be used.
- **Edge cases:** All public functions must handle expected edge cases (null/undefined inputs, empty collections, boundary values) explicitly.
- **Fail loudly:** Errors must be raised or logged with actionable context — never silently swallowed.

### Integration Standards
- **Backward compatibility:** New code must not break existing tests or functionality unless a spec explicitly requires a breaking change.
- **Import from, don't duplicate:** Reuse existing modules and utilities. Do not rewrite functionality that already exists in the codebase.
- **Consistent patterns:** Follow the conventions established by previously implemented components (naming, file structure, error handling patterns, config patterns).
- **Dependency hygiene:** Any new dependency must be added to the appropriate manifest (`requirements.txt` / `pyproject.toml` / `package.json`) with a brief comment explaining why it was added.

---

## 3) CI/CD, Evals, and Security
- **Pipeline (Python 3.13):** Black, custom `scripts/evals.py` (docstrings present, no TODO/FIXME).
- **Web pipeline (Node 24, pnpm):** build + type check; lint.
- **Security pipeline:** Gitleaks; CodeQL as configured.
- **Blocking policy:** CI must be green; evals must pass for PR merge.

### Pre-Commit Validation
All agents and contributors must run the following validation sequence before considering work complete:

```bash
# Python
source .venv/bin/activate
black --check app/src/
isort --check-only app/src/
pytest -q --cov=app/src --cov-report=term-missing
python scripts/evals.py

# TypeScript
pnpm build
pnpm lint
pnpm test
```

Evals (`scripts/evals.py`) enforce: all public functions have docstrings, no TODO/FIXME comments remain in delivered code, and any project-specific quality gates pass. CI must be green before merge.

---

## 4) Environment & Config Matrix
- **Local development:** Use `.env/.env.local` (never commit real secrets); `.env/.env.example` for template.
- **Testing:** Use `.env/.env.test` with test credentials.
- **Production:** Use environment variables or secret management (e.g., AWS Secrets Manager); never commit real secrets.