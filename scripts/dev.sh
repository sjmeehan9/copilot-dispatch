#!/usr/bin/env bash
set -euo pipefail

echo "Starting local development environment..."

# 1. Activate virtual environment
if [ -f ".venv/bin/activate" ]; then
    echo "Activating virtual environment..."
    source .venv/bin/activate
else
    echo "Error: Virtual environment not found at .venv/bin/activate"
    exit 1
fi

# 2. Load environment variables
if [ -f ".env/.env.local" ]; then
    echo "Loading environment variables from .env/.env.local..."
    set -o allexport
    source .env/.env.local
    set +o allexport
else
    echo "Warning: .env/.env.local not found. Using existing environment variables."
fi

# 3. Start DynamoDB Local
echo "Starting DynamoDB Local via Docker Compose..."
docker compose up -d

# 4. Wait for DynamoDB Local to be healthy
echo "Waiting for DynamoDB Local to be healthy..."
# Wait up to 30 seconds
for i in {1..30}; do
    if curl -sS http://localhost:8100 > /dev/null; then
        echo "DynamoDB Local is up!"
        break
    fi
    if [ "$i" -eq 30 ]; then
        echo "Error: DynamoDB Local failed to start."
        exit 1
    fi
    sleep 1
done

# 5. Initialise the DynamoDB table
echo "Initialising DynamoDB table..."
python scripts/init_dynamodb.py

# 6. Start uvicorn in reload mode
echo "Starting uvicorn dev server..."
uvicorn app.src.main:app --host 0.0.0.0 --port 8000 --reload
