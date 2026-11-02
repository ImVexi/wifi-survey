"""Password hashing, signed sessions and node tokens.

Only the standard library is used here so the auth rules are easy to audit.
Passwords are PBKDF2-HMAC-SHA256; the stored form is
``pbkdf2_sha256$<iterations>$<salt-hex>$<hash-hex>``. Node tokens are random
32-byte values and only their SHA-256 digest is stored in the database.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import secrets
import time
from typing import Any

PBKDF2_ITERATIONS = 240_000
PASSWORD_SCHEME = "pbkdf2_sha256"


def hash_password(password: str, *, iterations: int = PBKDF2_ITERATIONS) -> str:
    if not password:
        raise ValueError("password must not be empty")
    salt = secrets.token_bytes(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, iterations)
    return "$".join(
        (PASSWORD_SCHEME, str(iterations), salt.hex(), digest.hex())
    )


def verify_password(password: str, stored: str) -> bool:
    try:
        scheme, raw_iterations, salt_hex, digest_hex = stored.split("$", 3)
        if scheme != PASSWORD_SCHEME:
            return False
        iterations = int(raw_iterations)
        salt = bytes.fromhex(salt_hex)
        expected = bytes.fromhex(digest_hex)
    except (ValueError, AttributeError):
        return False
    got = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, iterations)
    return hmac.compare_digest(got, expected)


def new_node_token() -> str:
    return secrets.token_urlsafe(32)


def hash_node_token(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def new_pairing_code() -> str:
    """Six digits, including leading zeros."""
    return f"{secrets.randbelow(1_000_000):06d}"


def hash_digest(code: str, pairing_secret: str) -> str:
    """Digest of a pairing code bound to the install's pairing secret."""
    return hashlib.sha256((code + ":" + pairing_secret).encode("utf-8")).hexdigest()


def _b64encode(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


def _b64decode(text: str) -> bytes:
    pad = "=" * (-len(text) % 4)
    return base64.urlsafe_b64decode(text + pad)


def make_session(secret: bytes, *, ttl_hours: int, role: str = "viewer") -> str:
    payload = {
        "role": role,
        "iat": int(time.time()),
        "exp": int(time.time()) + int(ttl_hours) * 3600,
        "nonce": secrets.token_hex(8),
    }
    body = _b64encode(json.dumps(payload, separators=(",", ":"), sort_keys=True).encode())
    signature = hmac.new(secret, body.encode("ascii"), hashlib.sha256).digest()
    return f"v1.{body}.{_b64encode(signature)}"


def verify_session(secret: bytes, token: str) -> dict[str, Any] | None:
    try:
        version, body, signature = token.split(".", 2)
    except ValueError:
        return None
    if version != "v1":
        return None
    expected = hmac.new(secret, body.encode("ascii"), hashlib.sha256).digest()
    try:
        supplied = _b64decode(signature)
    except (ValueError, TypeError):
        return None
    if not hmac.compare_digest(expected, supplied):
        return None
    try:
        payload = json.loads(_b64decode(body).decode("utf-8"))
    except (ValueError, UnicodeDecodeError):
        return None
    if not isinstance(payload, dict):
        return None
    if int(payload.get("exp") or 0) < int(time.time()):
        return None
    return payload


def constant_time_equals(left: str, right: str) -> bool:
    return hmac.compare_digest(left.encode("utf-8"), right.encode("utf-8"))
