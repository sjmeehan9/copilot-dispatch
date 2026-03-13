"""Services package.

Business-logic services used by the API layer:

- GitHub service — workflow dispatch and PR management via the GitHub REST API
- DynamoDB service — run state persistence and retrieval
- Webhook service — result delivery with HMAC signing and retry logic
"""
