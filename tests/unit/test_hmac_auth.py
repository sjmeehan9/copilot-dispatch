"""Unit tests for HMAC-SHA256 authentication utilities."""

import pytest

from app.src.auth.hmac_auth import generate_hmac_signature, verify_hmac_signature


def test_generate_hmac_signature_deterministic():
    """Test that the same payload and secret produce the same signature."""
    payload = b'{"status": "success"}'
    secret = "test-secret-key"

    sig1 = generate_hmac_signature(payload, secret)
    sig2 = generate_hmac_signature(payload, secret)

    assert sig1 == sig2


def test_generate_hmac_signature_known_value():
    """Test signature generation against a known reference value."""
    payload = b"hello world"
    secret = "secret"
    # Expected value generated via:
    # echo -n "hello world" | openssl dgst -sha256 -hmac "secret"
    expected_sig = "734cc62f32841568f45715aeb9f4d7891324e6d948e4c6c60c0621cdac48623a"

    sig = generate_hmac_signature(payload, secret)
    assert sig == expected_sig


def test_generate_hmac_signature_empty_secret():
    """Test that an empty secret raises ValueError."""
    with pytest.raises(ValueError, match="Secret cannot be empty"):
        generate_hmac_signature(b"payload", "")


def test_verify_hmac_signature_valid():
    """Test that a valid signature returns True."""
    payload = b'{"status": "success"}'
    secret = "test-secret-key"
    signature = generate_hmac_signature(payload, secret)

    assert verify_hmac_signature(payload, signature, secret) is True


def test_verify_hmac_signature_tampered_payload():
    """Test that a tampered payload returns False."""
    payload = b'{"status": "success"}'
    secret = "test-secret-key"
    signature = generate_hmac_signature(payload, secret)

    tampered_payload = b'{"status": "failure"}'
    assert verify_hmac_signature(tampered_payload, signature, secret) is False


def test_verify_hmac_signature_tampered_signature():
    """Test that a tampered signature returns False."""
    payload = b'{"status": "success"}'
    secret = "test-secret-key"
    signature = generate_hmac_signature(payload, secret)

    tampered_signature = signature[:-1] + ("0" if signature[-1] != "0" else "1")
    assert verify_hmac_signature(payload, tampered_signature, secret) is False


def test_verify_hmac_signature_empty_secret():
    """Test that an empty secret returns False."""
    payload = b"payload"
    signature = "some-signature"
    assert verify_hmac_signature(payload, signature, "") is False


def test_verify_hmac_signature_empty_payload():
    """Test that an empty payload produces a valid signature."""
    payload = b""
    secret = "test-secret-key"
    signature = generate_hmac_signature(payload, secret)

    assert verify_hmac_signature(payload, signature, secret) is True
