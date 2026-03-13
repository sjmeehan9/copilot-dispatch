"""HMAC-SHA256 authentication utilities.

Provides functions for generating and verifying HMAC-SHA256 signatures
for inbound result verification and outbound webhook signing.
"""

from __future__ import annotations

import hashlib
import hmac


def generate_hmac_signature(payload: bytes, secret: str) -> str:
    """Generate an HMAC-SHA256 signature for a payload.

    Args:
        payload: The raw bytes of the payload to sign.
        secret: The secret key used for signing.

    Returns:
        The hex-encoded HMAC-SHA256 signature string.

    Raises:
        ValueError: If the secret is empty.
    """
    if not secret:
        raise ValueError("Secret cannot be empty.")

    mac = hmac.new(
        key=secret.encode("utf-8"),
        msg=payload,
        digestmod=hashlib.sha256,
    )
    return mac.hexdigest()


def verify_hmac_signature(payload: bytes, signature: str, secret: str) -> bool:
    """Verify an HMAC-SHA256 signature for a payload.

    Uses a constant-time comparison to prevent timing attacks.

    Args:
        payload: The raw bytes of the payload that was signed.
        signature: The hex-encoded signature to verify.
        secret: The secret key used for signing.

    Returns:
        True if the signature is valid, False otherwise.
    """
    if not secret:
        return False

    try:
        expected_signature = generate_hmac_signature(payload, secret)
    except ValueError:
        return False

    return hmac.compare_digest(
        signature.encode("utf-8"),
        expected_signature.encode("utf-8"),
    )
