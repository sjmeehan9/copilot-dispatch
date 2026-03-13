"""Integration test package.

Tests that exercise the application against live local infrastructure:
DynamoDB Local (docker-compose) and the FastAPI test client. External GitHub
API calls remain mocked unless the ``--github-confirm`` pytest flag is passed.
"""
