#!/usr/bin/env python3
"""Local webhook receiver for testing Copilot Dispatch callback delivery.

Starts a simple HTTP server on a configurable port (default 9000) that:
  - Accepts POST requests on any path
  - Optionally verifies the HMAC-SHA256 signature via X-Webhook-Signature
  - Pretty-prints the JSON payload
  - Returns 200 OK so the WebhookService records delivery as successful

Usage:
    # Basic (no signature verification):
    python scripts/webhook_receiver.py

    # With HMAC verification (reads DISPATCH_WEBHOOK_SECRET from env):
    source .venv/bin/activate
    set -o allexport; source .env/.env.local; set +o allexport
    python scripts/webhook_receiver.py --verify

    # Custom port:
    python scripts/webhook_receiver.py --port 8888
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, HTTPServer

# ---------------------------------------------------------------------------
# Allow importing from the project when running from the repo root.
# ---------------------------------------------------------------------------
REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)


def _verify_signature(body: bytes, header_value: str, secret: str) -> bool:
    """Verify the HMAC-SHA256 signature from the X-Webhook-Signature header.

    Args:
        body: Raw request body bytes.
        header_value: Full header value, expected format ``sha256=<hex>``.
        secret: Shared HMAC secret.

    Returns:
        True when the signature is valid.
    """
    from app.src.auth.hmac_auth import verify_hmac_signature

    if not header_value.startswith("sha256="):
        return False
    signature_hex = header_value[len("sha256=") :]
    return verify_hmac_signature(body, signature_hex, secret)


class WebhookHandler(BaseHTTPRequestHandler):
    """HTTP request handler that accepts webhook POST payloads."""

    # Set by the factory function below.
    verify: bool = False
    secret: str = ""

    def do_POST(self) -> None:  # noqa: N802 — stdlib naming convention
        """Handle incoming POST requests."""
        content_length = int(self.headers.get("Content-Length", 0))
        body = self.rfile.read(content_length)

        timestamp = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
        sig_header = self.headers.get("X-Webhook-Signature", "")

        print(f"\n{'=' * 72}")
        print(f"  Webhook received at {timestamp}")
        print(f"  Path: {self.path}")
        print(f"  Content-Length: {content_length}")
        print(f"  X-Webhook-Signature: {sig_header or '(not present)'}")

        # Signature verification (optional).
        if self.verify:
            if not sig_header:
                print("  Signature verification: SKIPPED (no header)")
            elif not self.secret:
                print("  Signature verification: SKIPPED (no secret configured)")
            else:
                valid = _verify_signature(body, sig_header, self.secret)
                status = "\033[92mVALID\033[0m" if valid else "\033[91mINVALID\033[0m"
                print(f"  Signature verification: {status}")

        # Pretty-print JSON payload.
        print(f"{'=' * 72}")
        try:
            payload = json.loads(body)
            print(json.dumps(payload, indent=2, default=str))
        except (json.JSONDecodeError, UnicodeDecodeError):
            print(body.decode("utf-8", errors="replace"))
        print(f"{'=' * 72}\n")

        # Respond 200 OK so the sender considers delivery successful.
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(b'{"status":"received"}')

    def do_GET(self) -> None:  # noqa: N802
        """Respond to GET requests with a simple health message."""
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(b'{"status":"webhook_receiver_ready"}')

    def log_message(self, format: str, *args: object) -> None:
        """Suppress default stderr access logging to keep output clean."""
        pass


def _build_handler(verify: bool, secret: str) -> type[WebhookHandler]:
    """Build a handler class with the given configuration baked in.

    Args:
        verify: Whether to verify HMAC signatures.
        secret: The HMAC shared secret.

    Returns:
        A configured BaseHTTPRequestHandler subclass.
    """

    class ConfiguredHandler(WebhookHandler):
        pass

    ConfiguredHandler.verify = verify
    ConfiguredHandler.secret = secret
    return ConfiguredHandler


def main() -> None:
    """Parse arguments and start the webhook receiver server."""
    parser = argparse.ArgumentParser(
        description="Local webhook receiver for Copilot Dispatch callback testing."
    )
    parser.add_argument(
        "--port",
        type=int,
        default=9000,
        help="Port to listen on (default: 9000).",
    )
    parser.add_argument(
        "--verify",
        action="store_true",
        default=False,
        help="Verify HMAC-SHA256 signatures using DISPATCH_WEBHOOK_SECRET.",
    )
    args = parser.parse_args()

    secret = os.environ.get("DISPATCH_WEBHOOK_SECRET", "")
    if args.verify and not secret:
        print(
            "WARNING: --verify flag set but DISPATCH_WEBHOOK_SECRET is not set. "
            "Signature verification will be skipped.\n"
            "Hint: set -o allexport; source .env/.env.local; set +o allexport"
        )

    handler_cls = _build_handler(verify=args.verify, secret=secret)
    server = HTTPServer(("0.0.0.0", args.port), handler_cls)

    print(f"Webhook receiver listening on http://0.0.0.0:{args.port}")
    print(f"HMAC verification: {'enabled' if args.verify and secret else 'disabled'}")
    print("Press Ctrl+C to stop.\n")

    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nShutting down.")
        server.server_close()


if __name__ == "__main__":
    main()
