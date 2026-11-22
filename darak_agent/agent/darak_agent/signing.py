"""Shared, byte-exact HMAC contract for the agent and hub (stdlib only)."""

import hashlib
import hmac
import re

REQUEST_LABEL = "darak-agent-request-v1"
RESPONSE_LABEL = "darak-agent-response-v1"
MAX_SKEW_SECONDS = 300


def body_digest(body: bytes) -> str:
    return hashlib.sha256(body).hexdigest()


def request_string(home_id, method, raw_path, timestamp, operation, body) -> bytes:
    return "\n".join((REQUEST_LABEL, home_id, method.upper(), raw_path, timestamp,
                      operation, body_digest(body))).encode("utf-8")


def response_string(home_id, operation, status, body) -> bytes:
    return "\n".join((RESPONSE_LABEL, home_id, operation or "", str(status),
                      body_digest(body))).encode("utf-8")


def sign(key: bytes, message: bytes) -> str:
    return "v1=" + hmac.new(key, message, hashlib.sha256).hexdigest()


def verify(key: bytes, message: bytes, signature: str) -> bool:
    if not isinstance(key, bytes) or not isinstance(message, bytes):
        return False
    # Malformed signatures still incur the same HMAC and fixed-length comparison.
    valid = isinstance(signature, str) and re.fullmatch(r"v1=[0-9a-f]{64}", signature) is not None
    try:
        expected = sign(key, message)[3:]
    except (TypeError, ValueError):
        return False
    matches = hmac.compare_digest(expected, signature[3:] if valid else "0" * 64)
    return matches and valid
