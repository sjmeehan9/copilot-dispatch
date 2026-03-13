# Docker Guide for Copilot Dispatch

A practical guide to Docker in the context of this project. Covers the core
concepts, walks through our Dockerfile line by line, and documents the
real deployment issues we hit so you can avoid them.

---

## Table of Contents

1. [Core Docker Concepts](#core-docker-concepts)
2. [Our Dockerfile Explained](#our-dockerfile-explained)
3. [Building Images](#building-images)
4. [Running Containers Locally](#running-containers-locally)
5. [Pushing to AWS ECR](#pushing-to-aws-ecr)
6. [Inspecting & Debugging Containers](#inspecting--debugging-containers)
7. [Common Commands Cheat Sheet](#common-commands-cheat-sheet)
8. [Lessons Learned — Real Deployment Issues](#lessons-learned--real-deployment-issues)
9. [Glossary](#glossary)

---

## Core Docker Concepts

### What is Docker?

Docker packages your application and all its dependencies into a single unit
called a **container**. A container is a lightweight, isolated process that
runs the same way on any machine — your laptop, a CI server, or AWS
AppRunner. This eliminates "works on my machine" problems.

### Images vs Containers

| Concept | Analogy | Description |
|---------|---------|-------------|
| **Image** | A recipe | A read-only blueprint that defines what's inside the container: OS, Python, your code, dependencies. Built once, reused many times. |
| **Container** | A dish made from the recipe | A running instance of an image. You can start, stop, and delete containers without affecting the image. |

You **build** an image from a `Dockerfile`, then **run** a container from
that image.

### Layers & Caching

Each instruction in a Dockerfile (`FROM`, `RUN`, `COPY`, etc.) creates a
**layer**. Docker caches layers — if nothing changed in a layer, Docker reuses
the cached version instead of rebuilding it. This is why we copy
`pyproject.toml` *before* the application code: dependencies only reinstall
when the manifest changes, not on every code edit.

### Multi-Stage Builds

A multi-stage build uses multiple `FROM` statements. Each stage starts fresh.
You install build tools (compilers, headers) in an early stage and copy only
the built artefacts into a final slim stage. The build tools never appear in
the production image, keeping it small and secure.

### The Build Context

When you run `docker build .`, Docker sends everything in the current directory
(the **build context**) to the Docker daemon. A `.dockerignore` file excludes
unnecessary files (`.venv/`, `.git/`, `tests/`) to keep the context small and
builds fast.

---

## Our Dockerfile Explained

Here's the project's production Dockerfile with annotations:

```dockerfile
# ──────────────────────────────────────────────────────────────
# STAGE 1: BUILDER — install dependencies
# ──────────────────────────────────────────────────────────────

# Start from a slim Python 3.13 base image.
# --platform=linux/amd64  ← CRITICAL: forces x86_64 architecture.
# Without this, building on Apple Silicon (M1/M2/M3) produces an arm64
# image that won't run on AWS AppRunner (which only supports x86_64).
FROM --platform=linux/amd64 python:3.13-slim AS builder

WORKDIR /build

# Install gcc — needed to compile some Python packages with C extensions.
# This stays in the builder stage only; the final image won't have it.
RUN apt-get update && apt-get install -y --no-install-recommends \
    gcc \
    && rm -rf /var/lib/apt/lists/*

# Copy the dependency manifest first.
# Docker caches this layer — dependencies only reinstall when pyproject.toml
# changes, not when you edit application code.
COPY pyproject.toml ./

# Copy the application package for the editable install to resolve.
COPY app/ ./app/

# Install all production dependencies into a clean /install prefix.
# --no-cache-dir prevents pip's cache from being stored in the layer.
RUN pip install --no-cache-dir --prefix=/install .

# ──────────────────────────────────────────────────────────────
# STAGE 2: RUNNER — minimal production image
# ──────────────────────────────────────────────────────────────

# Fresh slim base — no gcc, no build tools.
FROM --platform=linux/amd64 python:3.13-slim AS runner

# Create a non-root user. Running as root in production is a security risk.
RUN groupadd -r appuser && useradd -r -g appuser -d /app -s /sbin/nologin appuser

WORKDIR /app

# Copy only the installed Python packages from the builder stage.
# The compiler and build tools are left behind.
COPY --from=builder /install /usr/local

# Copy application source code.
COPY app/ ./app/

# Give the non-root user ownership of the app directory.
RUN chown -R appuser:appuser /app

# Switch to the non-root user for all subsequent commands and runtime.
USER appuser

# Document the port the application listens on.
EXPOSE 8000

# Built-in health check — Docker (and services like ECS) use this to
# verify the container is alive. Uses Python stdlib to avoid installing curl.
HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://localhost:8000/health')" || exit 1

# Start the application server.
CMD ["uvicorn", "app.src.main:app", "--host", "0.0.0.0", "--port", "8000"]
```

### Why `CMD` Instead of `ENTRYPOINT`?

`CMD` can be overridden at `docker run` time, making debugging easier:

```bash
# Normal run — uses the default CMD
docker run copilot-dispatch:latest

# Debug run — override CMD to get a shell inside the container
docker run -it copilot-dispatch:latest /bin/bash
```

`ENTRYPOINT` is harder to override and is better suited when you want the
container to *always* run a specific binary.

---

## Building Images

### Standard build (for deployment)

```bash
# From the project root
docker build -t copilot-dispatch:latest .
```

The `--platform=linux/amd64` directive in the Dockerfile ensures the correct
architecture regardless of your host machine.

### Verify the architecture

Always check the architecture after building, especially on Apple Silicon:

```bash
docker inspect copilot-dispatch:latest | grep Architecture
# Expected output: "Architecture": "amd64"
```

If you see `arm64` instead, the `--platform` flag isn't taking effect — check
your Dockerfile.

### Force a clean rebuild (no cache)

If something seems stale, rebuild without the layer cache:

```bash
docker build --no-cache -t copilot-dispatch:latest .
```

---

## Running Containers Locally

### Quick start

```bash
docker run -d \
  --name copilot-dispatch-test \
  -p 8000:8000 \
  -e DISPATCH_APP_ENV=local \
  -e GITHUB_PAT=your-pat-here \
  -e DISPATCH_API_KEY=your-key-here \
  -e DISPATCH_WEBHOOK_SECRET=your-secret-here \
  copilot-dispatch:latest
```

Flags explained:

| Flag | Meaning |
|------|---------|
| `-d` | Run in the background (detached mode) |
| `--name copilot-dispatch-test` | Give the container a human-readable name |
| `-p 8000:8000` | Map host port 8000 → container port 8000 |
| `-e KEY=VALUE` | Set an environment variable inside the container |

### Test the health endpoint

```bash
curl http://localhost:8000/health
# {"status":"healthy"}
```

### Stop and remove the container

```bash
docker stop copilot-dispatch-test
docker rm copilot-dispatch-test
```

Or in one command:

```bash
docker rm -f copilot-dispatch-test
```

---

## Pushing to AWS ECR

ECR (Elastic Container Registry) is AWS's Docker image registry. AppRunner
pulls images from here.

### Step-by-step

```bash
# 1. Set variables
ACCOUNT_ID=$(aws sts get-caller-identity --profile copilot-dispatch --query Account --output text)
ECR_URI="${ACCOUNT_ID}.dkr.ecr.ap-southeast-2.amazonaws.com/copilot-dispatch"

# 2. Authenticate Docker with ECR (token expires after 12 hours)
aws ecr get-login-password --region ap-southeast-2 --profile copilot-dispatch | \
    docker login --username AWS --password-stdin \
    "${ACCOUNT_ID}.dkr.ecr.ap-southeast-2.amazonaws.com"

# 3. Build the image
docker build -t copilot-dispatch:latest .

# 4. Tag for ECR — both a version tag and "latest"
COMMIT_SHA=$(git rev-parse --short HEAD)
docker tag copilot-dispatch:latest "${ECR_URI}:${COMMIT_SHA}"
docker tag copilot-dispatch:latest "${ECR_URI}:latest"

# 5. Push to ECR
docker push "${ECR_URI}:${COMMIT_SHA}"
docker push "${ECR_URI}:latest"
```

AppRunner auto-deploys when a new `latest` tag is pushed, so the service
will update automatically after step 5.

### Verify the ECR image architecture

After pushing, confirm the image in ECR is the correct architecture:

```bash
aws ecr batch-get-image \
    --profile copilot-dispatch \
    --region ap-southeast-2 \
    --repository-name copilot-dispatch \
    --image-ids imageTag=latest \
    --output json | grep architecture
```

You should see `"architecture": "amd64"`. If you see `arm64`, the Dockerfile
platform directive isn't working — see the [Lessons Learned](#lessons-learned--real-deployment-issues)
section.

---

## Inspecting & Debugging Containers

### View container logs

```bash
# Live-follow logs
docker logs -f copilot-dispatch-test

# Last 50 lines only
docker logs --tail 50 copilot-dispatch-test
```

### Get a shell inside a running container

```bash
docker exec -it copilot-dispatch-test /bin/bash
```

This drops you into the container's filesystem. You can run Python, check
files, or test network connectivity.

### Check container status

```bash
# All running containers
docker ps

# All containers (including stopped)
docker ps -a

# Detailed info about a container
docker inspect copilot-dispatch-test
```

### Check image details

```bash
# List images
docker images

# Check architecture and size
docker inspect copilot-dispatch:latest | grep -E "Architecture|Size"
```

---

## Common Commands Cheat Sheet

| Task | Command |
|------|---------|
| Build an image | `docker build -t copilot-dispatch:latest .` |
| Run a container | `docker run -d --name test -p 8000:8000 copilot-dispatch:latest` |
| Stop a container | `docker stop test` |
| Remove a container | `docker rm test` |
| Force remove (running) | `docker rm -f test` |
| View logs | `docker logs test` |
| Shell into container | `docker exec -it test /bin/bash` |
| List running containers | `docker ps` |
| List all containers | `docker ps -a` |
| List images | `docker images` |
| Remove an image | `docker rmi copilot-dispatch:latest` |
| Remove all stopped containers | `docker container prune` |
| Remove all unused images | `docker image prune -a` |
| Check image architecture | `docker inspect copilot-dispatch:latest \| grep Architecture` |
| Rebuild without cache | `docker build --no-cache -t copilot-dispatch:latest .` |

---

## Lessons Learned — Real Deployment Issues

These are real bugs encountered during the Copilot Dispatch deployment to AWS
AppRunner. Each one caused silent deployment failures that took significant
investigation to diagnose.

### Issue 1: Architecture Mismatch (arm64 vs amd64)

**Symptom:** AppRunner health checks fail. No application logs appear in
CloudWatch. The service event log shows "Health check failed" with no further
detail.

**Root Cause:** Docker on Apple Silicon (M1/M2/M3 Macs) builds `arm64`
images by default. AWS AppRunner only supports `x86_64` (`amd64`). When
AppRunner tries to run an `arm64` image, the container crashes immediately
with an exec format error — but no application logs are produced because the
process never starts.

**Why it's hard to diagnose:** The failure is completely silent at the
application level. AppRunner reports "health check failed," which looks like
an application bug. Only by inspecting the Docker image manifest do you
discover the architecture mismatch.

**Fix:** Add `--platform=linux/amd64` to every `FROM` directive in the Dockerfile:

```dockerfile
FROM --platform=linux/amd64 python:3.13-slim AS builder
# ...
FROM --platform=linux/amd64 python:3.13-slim AS runner
```

**Verification:**

```bash
# After building, confirm the architecture
docker inspect copilot-dispatch:latest | grep Architecture
# Must show: "Architecture": "amd64"

# Also verify in ECR after pushing
aws ecr batch-get-image --repository-name copilot-dispatch \
    --image-ids imageTag=latest --output json | grep architecture
# Must show: "architecture": "amd64"
```

**Key Takeaway:** If you're on Apple Silicon and deploying to any x86_64
cloud service (AppRunner, ECS, Lambda, etc.), always pin the platform in
your Dockerfile. Never rely on the default.

---

### Issue 2: Secrets Manager Partial ARN

**Symptom:** AppRunner service event log shows:

```
ResourceInitializationError: unable to pull secrets or registry auth:
failed to fetch secret arn:aws:secretsmanager:...:secret:dispatch/webhook-secret
ResourceNotFoundException: Secrets Manager can't find the specified secret.
```

**Root Cause:** AWS Secrets Manager appends a 6-character random suffix to
every secret ARN (e.g., `dispatch/webhook-secret-MMxWvl`). CDK's
`Secret.from_secret_name_v2()` method resolves to a partial ARN *without*
this suffix. While most AWS services accept the partial ARN, AppRunner's
`RuntimeEnvironmentSecrets` requires the **full** ARN with the suffix.

**Fix:** The CDK stack uses a `_get_secret_arn()` helper that calls
`boto3.describe_secret()` at synthesis time to resolve the full ARN. It
falls back to a partial ARN for offline tests:

```python
# In the CDK stack
secret = secretsmanager.Secret.from_secret_complete_arn(
    self, "MySecret",
    self._get_secret_arn("dispatch/webhook-secret", aws_region)
)
```

**Key Takeaway:** When working with AppRunner secrets, always verify the
full ARN (including the random suffix) is being used. You can look up any
secret's full ARN with:

```bash
aws secretsmanager describe-secret --secret-id dispatch/webhook-secret \
    --query ARN --output text
```

---

### Issue 3: Environment Variable Naming Mismatch

**Symptom:** The application starts in production but behaves incorrectly —
for example, trying to read secrets from environment variables instead of
AWS Secrets Manager (as if it thinks it's running locally).

**Root Cause:** The application config system (`app/src/config.py`) reads
settings from `app/config/settings.yaml` and applies environment variable
overrides using the `DISPATCH_` prefix. For example, the `app_env` setting
is overridden by `DISPATCH_APP_ENV`. The CDK stack was originally injecting
env vars *without* the prefix (e.g., `APP_ENV=production`), so the config
system never saw them and defaulted to `app_env=local`.

**Fix:** Renamed all AppRunner environment variables to use the `DISPATCH_`
prefix:

| Before (incorrect) | After (correct) |
|--------------------|-----------------|
| `APP_ENV` | `DISPATCH_APP_ENV` |
| `DYNAMODB_TABLE_NAME` | `DISPATCH_DYNAMODB_TABLE_NAME` |
| `GITHUB_OWNER` | `DISPATCH_GITHUB_OWNER` |
| `GITHUB_REPO` | `DISPATCH_GITHUB_REPO` |
| `GITHUB_WORKFLOW_ID` | `DISPATCH_GITHUB_WORKFLOW_ID` |
| `LOG_LEVEL` | `DISPATCH_LOG_LEVEL` |

**Key Takeaway:** Always check how your application reads config before
setting environment variables. If the app uses a prefix convention, *every*
injected env var must use that prefix.

---

### Debugging Methodology

When a cloud deployment fails silently, follow this sequence:

1. **Check CloudWatch service logs** — AppRunner writes deployment events to
   `/aws/apprunner/<service-name>/<id>/service`. These tell you which phase
   failed (image pull, secret resolution, health check).

2. **Check CloudWatch application logs** — If the application log group
   exists but is empty, the container crashed before producing output. This
   points to an infrastructure issue (wrong architecture, missing library),
   not an application bug.

3. **Reproduce locally with identical env vars** — Run the same Docker image
   with the same environment variables that AppRunner would use. If it works
   locally, the problem is environmental (architecture, permissions, networking).

4. **Inspect the image manifest** — Check the architecture of the image in
   ECR. This catches the arm64/amd64 mismatch that causes silent failures.

5. **Check the CloudFormation template** — Run `cdk synth` and inspect the
   generated JSON. Verify that secret ARNs, env var names, and port numbers
   match what the application expects.

---

## Glossary

| Term | Definition |
|------|-----------|
| **Image** | A read-only template containing the OS, runtime, dependencies, and your application code. |
| **Container** | A running instance of an image — an isolated process with its own filesystem and network. |
| **Dockerfile** | A text file with instructions for building a Docker image. Each instruction creates a layer. |
| **Layer** | A cached, read-only filesystem diff produced by a single Dockerfile instruction. |
| **Multi-stage build** | A Dockerfile with multiple `FROM` statements; intermediate stages provide build tools, the final stage contains only runtime artefacts. |
| **Build context** | The set of files Docker sends to the daemon when building. Controlled by `.dockerignore`. |
| **Registry** | A remote storage service for Docker images (e.g., Docker Hub, AWS ECR). |
| **ECR** | Elastic Container Registry — AWS's managed Docker image registry. |
| **Tag** | A label attached to an image version (e.g., `latest`, `v1.2.3`, `abc123f`). |
| **Platform** | The CPU architecture an image is built for (`linux/amd64`, `linux/arm64`). |
| **`EXPOSE`** | Documents which port the container listens on. Does *not* publish the port — you need `-p` at `docker run` time. |
| **`CMD`** | The default command a container runs on start. Can be overridden at `docker run` time. |
| **`ENTRYPOINT`** | Like `CMD` but harder to override. Used when the container should always run a specific process. |
| **`HEALTHCHECK`** | An instruction that tells Docker how to test if the container is working. |
| **AppRunner** | An AWS service that runs containers from ECR with automatic scaling, TLS, and load balancing. |
