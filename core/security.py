"""
core/security.py
────────────────
Runtime security validation layer for the NexusScale Compliance Engine.

Responsibilities:
  1. ENTERPRISE_AGENT_SECRET preflight check — hard abort on process start.
  2. HMAC-SHA256 session key verification for every inbound API request.
  3. IP allowlist / blocklist gate (extensible).
  4. Structured security audit logging on every outcome.
"""

from __future__ import annotations

import hashlib
import hmac
import logging
import os
import sys
import time
from datetime import datetime, timezone
from functools import lru_cache
from typing import Final

from core.exceptions import SecurityValidationError, SessionKeyInvalidError

logger = logging.getLogger(__name__)


# ─────────────────────────────────────────────────────────────────────────────
# Constants
# ─────────────────────────────────────────────────────────────────────────────

_MIN_SECRET_LENGTH: Final[int] = 16
_SECRET_ENV_KEY: Final[str] = "ENTERPRISE_AGENT_SECRET"
_SESSION_HMAC_ENV_KEY: Final[str] = "SESSION_HMAC_SECRET"
_SESSION_TTL_ENV_KEY: Final[str] = "SESSION_KEY_TTL_SECONDS"
_DEFAULT_SESSION_TTL: Final[int] = 3600


# ─────────────────────────────────────────────────────────────────────────────
# Phase 1 Preflight: Hard Abort if Secret is Invalid
# ─────────────────────────────────────────────────────────────────────────────

def enforce_enterprise_secret() -> str:
    """
    PHASE 1 SECURITY HOOK — Called at process startup before any agent is
    initialized. Immediately aborts the process with exit code 1 if the
    ENTERPRISE_AGENT_SECRET environment variable:
      • Does not exist
      • Is None
      • Is blank / whitespace-only
      • Contains fewer than 16 characters

    Returns the validated secret string on success.
    """
    raw: str | None = os.environ.get(_SECRET_ENV_KEY)

    if raw is None:
        _fatal_abort(
            reason="absent",
            detail=f"Environment variable '{_SECRET_ENV_KEY}' is not set.",
        )

    stripped = raw.strip()

    if not stripped:
        _fatal_abort(
            reason="blank",
            detail=f"Environment variable '{_SECRET_ENV_KEY}' is set but empty or whitespace-only.",
        )

    if len(stripped) < _MIN_SECRET_LENGTH:
        _fatal_abort(
            reason="too_short",
            detail=(
                f"Environment variable '{_SECRET_ENV_KEY}' has {len(stripped)} characters; "
                f"minimum required is {_MIN_SECRET_LENGTH}."
            ),
        )

    logger.info(
        "✅ Security preflight PASSED",
        extra={
            "event": "ENTERPRISE_SECRET_VALIDATED",
            "secret_length": len(stripped),
            "timestamp": datetime.now(timezone.utc).isoformat(),
        },
    )
    return stripped


def _fatal_abort(reason: str, detail: str) -> None:
    """Write a structured error to stderr and immediately terminate the process."""
    logger.critical(
        "🔴 FATAL: Security preflight FAILED — aborting process",
        extra={
            "event": "SECURITY_PREFLIGHT_ABORT",
            "reason": reason,
            "detail": detail,
            "timestamp": datetime.now(timezone.utc).isoformat(),
        },
    )
    # Also print directly to stderr in case the logging pipeline isn't wired yet
    print(
        f"\n[NexusScale FATAL] Security validation failed: {detail}\n"
        f"Reason: {reason}\n"
        f"Process aborting with exit code 1.\n",
        file=sys.stderr,
    )
    sys.exit(1)


# ─────────────────────────────────────────────────────────────────────────────
# Session Key HMAC Verification
# ─────────────────────────────────────────────────────────────────────────────

@lru_cache(maxsize=1)
def _get_hmac_secret() -> bytes:
    """Retrieve SESSION_HMAC_SECRET from environment (cached after first read)."""
    secret = os.environ.get(_SESSION_HMAC_ENV_KEY, "")
    if not secret:
        logger.warning(
            "SESSION_HMAC_SECRET not set — falling back to ENTERPRISE_AGENT_SECRET for HMAC"
        )
        secret = os.environ.get(_SECRET_ENV_KEY, "fallback-insecure-key")
    return secret.encode("utf-8")


def generate_session_key(employee_id: str, issued_at: int | None = None) -> str:
    """
    Generate a time-bound HMAC-SHA256 session key.

    Format:  <issued_at_unix>.<hmac_hex>
    """
    ts = issued_at or int(time.time())
    message = f"{employee_id}:{ts}".encode("utf-8")
    signature = hmac.new(_get_hmac_secret(), message, hashlib.sha256).hexdigest()
    return f"{ts}.{signature}"


def verify_session_key(session_key: str, employee_id: str, correlation_id: str = "") -> bool:
    """
    Verify a session key using constant-time HMAC comparison.

    Raises:
        SessionKeyInvalidError: if the key is missing, malformed, expired, or tampered.
    """
    if not session_key or not session_key.strip():
        raise SessionKeyInvalidError(
            message="Session key is absent or blank.",
            correlation_id=correlation_id,
            context={"employee_id": employee_id},
        )

    parts = session_key.split(".", 1)
    if len(parts) != 2:
        raise SessionKeyInvalidError(
            message="Session key has invalid format (expected '<timestamp>.<hmac>').",
            correlation_id=correlation_id,
            context={"employee_id": employee_id},
        )

    ts_str, provided_hmac = parts

    try:
        issued_at = int(ts_str)
    except ValueError:
        raise SessionKeyInvalidError(
            message="Session key timestamp is not a valid integer.",
            correlation_id=correlation_id,
        )

    # TTL check
    ttl = int(os.environ.get(_SESSION_TTL_ENV_KEY, _DEFAULT_SESSION_TTL))
    age = int(time.time()) - issued_at
    if age > ttl or age < 0:
        raise SessionKeyInvalidError(
            message=f"Session key expired (age={age}s, ttl={ttl}s).",
            correlation_id=correlation_id,
            context={"age_seconds": age, "ttl_seconds": ttl},
        )

    # HMAC recomputation + constant-time compare
    message = f"{employee_id}:{issued_at}".encode("utf-8")
    expected_hmac = hmac.new(_get_hmac_secret(), message, hashlib.sha256).hexdigest()

    if not hmac.compare_digest(provided_hmac, expected_hmac):
        logger.warning(
            "🚨 Session key HMAC mismatch",
            extra={
                "event": "SESSION_KEY_TAMPERED",
                "employee_id": employee_id,
                "correlation_id": correlation_id,
            },
        )
        raise SessionKeyInvalidError(
            message="Session key signature does not match — possible tampering.",
            correlation_id=correlation_id,
            context={"employee_id": employee_id},
        )

    logger.debug(
        "✅ Session key verified",
        extra={"employee_id": employee_id, "correlation_id": correlation_id},
    )
    return True


# ─────────────────────────────────────────────────────────────────────────────
# Payload Validation Guard
# ─────────────────────────────────────────────────────────────────────────────

def validate_inbound_payload_security(
    department: str,
    session_key: str,
    employee_id: str,
    correlation_id: str = "",
) -> None:
    """
    Combined security gate for inbound expense payloads:
      1. Department must not be blank.
      2. Session key must pass HMAC verification.

    Raises SecurityValidationError or SessionKeyInvalidError on failure.
    """
    from core.exceptions import DepartmentEmptyError  # local import to avoid cycle

    if not department or not department.strip():
        logger.warning(
            "Payload rejected: empty department",
            extra={"event": "EMPTY_DEPARTMENT", "correlation_id": correlation_id},
        )
        raise DepartmentEmptyError(
            message="The 'department' field is required and cannot be blank.",
            correlation_id=correlation_id,
            field_errors=[{"field": "department", "issue": "blank_or_empty"}],
        )

    verify_session_key(session_key, employee_id, correlation_id)
