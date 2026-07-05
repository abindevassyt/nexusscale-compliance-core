"""
core/exceptions.py
──────────────────
Hierarchical exception taxonomy for the NexusScale Compliance Engine.
All custom exceptions carry structured metadata so that upstream handlers
can produce deterministic, machine-readable error responses.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any
from uuid import uuid4


# ─────────────────────────────────────────────────────────────────────────────
# Base Exception
# ─────────────────────────────────────────────────────────────────────────────

@dataclass
class ComplianceEngineError(Exception):
    """Root exception for every domain-specific error in NexusScale."""

    message: str
    error_code: str = "COMPLIANCE_ENGINE_ERROR"
    correlation_id: str = field(default_factory=lambda: str(uuid4()))
    timestamp: str = field(
        default_factory=lambda: datetime.now(timezone.utc).isoformat()
    )
    context: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        super().__init__(self.message)

    def to_dict(self) -> dict[str, Any]:
        return {
            "error": self.error_code,
            "message": self.message,
            "correlation_id": self.correlation_id,
            "timestamp": self.timestamp,
            "context": self.context,
        }


# ─────────────────────────────────────────────────────────────────────────────
# Security & Validation Exceptions  (→ HTTP 422 / 401 / 403)
# ─────────────────────────────────────────────────────────────────────────────

@dataclass
class SecurityValidationError(ComplianceEngineError):
    """Raised when ENTERPRISE_AGENT_SECRET or session key fails validation."""

    error_code: str = "SECURITY_VALIDATION_FAILED"
    http_status: int = 422

    def to_dict(self) -> dict[str, Any]:
        base = super().to_dict()
        base["http_status"] = self.http_status
        return base


@dataclass
class SessionKeyInvalidError(SecurityValidationError):
    """Raised when an inbound request carries an absent or tampered session key."""

    error_code: str = "SESSION_KEY_INVALID"
    http_status: int = 401


@dataclass
class PayloadValidationError(ComplianceEngineError):
    """Raised when the inbound expense payload fails Pydantic schema validation."""

    error_code: str = "PAYLOAD_VALIDATION_FAILED"
    http_status: int = 422
    field_errors: list[dict[str, Any]] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        base = super().to_dict()
        base["http_status"] = self.http_status
        base["field_errors"] = self.field_errors
        return base


@dataclass
class DepartmentEmptyError(PayloadValidationError):
    """Raised when the `department` field is blank or whitespace-only."""

    error_code: str = "DEPARTMENT_EMPTY"
    http_status: int = 422


# ─────────────────────────────────────────────────────────────────────────────
# MCP / Infrastructure Exceptions  (→ HTTP 503 / 504)
# ─────────────────────────────────────────────────────────────────────────────

@dataclass
class MCPError(ComplianceEngineError):
    """Base for all Model Context Protocol transport errors."""

    error_code: str = "MCP_ERROR"
    http_status: int = 503


@dataclass
class MCPTimeoutError(MCPError):
    """Raised when the MCP bridge does not respond within the configured timeout."""

    error_code: str = "MCP_TIMEOUT"
    http_status: int = 504
    timeout_seconds: float = 0.0

    def to_dict(self) -> dict[str, Any]:
        base = super().to_dict()
        base["timeout_seconds"] = self.timeout_seconds
        return base


@dataclass
class MCPDisconnectError(MCPError):
    """Raised when the MCP bridge TCP/WebSocket connection is lost mid-flight."""

    error_code: str = "MCP_DISCONNECTED"
    http_status: int = 503


@dataclass
class MCPCircuitOpenError(MCPError):
    """Raised when the circuit breaker is in the OPEN state — fast-fail."""

    error_code: str = "MCP_CIRCUIT_OPEN"
    http_status: int = 503
    retry_after_seconds: int = 30

    def to_dict(self) -> dict[str, Any]:
        base = super().to_dict()
        base["retry_after_seconds"] = self.retry_after_seconds
        return base


# ─────────────────────────────────────────────────────────────────────────────
# Policy Engine Exceptions
# ─────────────────────────────────────────────────────────────────────────────

@dataclass
class PolicyLoadError(ComplianceEngineError):
    """Raised when policy_rules.json cannot be parsed or a required rule is missing."""

    error_code: str = "POLICY_LOAD_FAILED"
    http_status: int = 500


@dataclass
class PolicyEvaluationError(ComplianceEngineError):
    """Raised on unexpected errors during deterministic policy comparison."""

    error_code: str = "POLICY_EVALUATION_FAILED"
    http_status: int = 500


# ─────────────────────────────────────────────────────────────────────────────
# Resolution / Notification Exceptions
# ─────────────────────────────────────────────────────────────────────────────

@dataclass
class WebhookDispatchError(ComplianceEngineError):
    """Raised when all retry attempts to Slack/Teams webhook fail."""

    error_code: str = "WEBHOOK_DISPATCH_FAILED"
    http_status: int = 502
    webhook_target: str = ""
    attempts: int = 0

    def to_dict(self) -> dict[str, Any]:
        base = super().to_dict()
        base["webhook_target"] = self.webhook_target
        base["attempts"] = self.attempts
        return base


# ─────────────────────────────────────────────────────────────────────────────
# Agent Lifecycle Exceptions
# ─────────────────────────────────────────────────────────────────────────────

@dataclass
class AgentInitializationError(ComplianceEngineError):
    """Raised when an agent fails its startup preflight checks."""

    error_code: str = "AGENT_INIT_FAILED"
    agent_name: str = ""

    def to_dict(self) -> dict[str, Any]:
        base = super().to_dict()
        base["agent_name"] = self.agent_name
        return base
