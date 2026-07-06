"""
core/models.py
──────────────
Pydantic v2 data models and domain enumerations for the NexusScale
Compliance Engine. All models enforce strict typing and field-level
validators to guarantee payload integrity before agents touch the data.
"""

from __future__ import annotations

import hashlib
import hmac
import re
from datetime import datetime, timezone
from decimal import Decimal
from enum import Enum
from typing import Annotated, Any
from uuid import UUID, uuid4

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    computed_field,
    field_validator,
    model_validator,
)


# ─────────────────────────────────────────────────────────────────────────────
# Enumerations
# ─────────────────────────────────────────────────────────────────────────────

class ComplianceStatus(str, Enum):
    APPROVED = "APPROVED"
    FLAGGED = "FLAGGED"
    PENDING = "PENDING"
    ERROR = "ERROR"


class ExpenseCategory(str, Enum):
    MEALS = "meals"
    TRAVEL = "travel"
    ACCOMMODATION = "accommodation"
    SOFTWARE = "software"
    EQUIPMENT = "equipment"
    TRAINING = "training"
    ENTERTAINMENT = "entertainment"
    MISCELLANEOUS = "miscellaneous"


class AgentRole(str, Enum):
    SUPERVISOR = "supervisor"
    WORKER = "worker"
    COMMUNICATOR = "communicator"


class NotificationChannel(str, Enum):
    SLACK = "slack"
    TEAMS = "teams"
    EMAIL = "email"
    LOCAL_FILE = "local_file"


class CircuitState(str, Enum):
    CLOSED = "CLOSED"
    OPEN = "OPEN"
    HALF_OPEN = "HALF_OPEN"


class AuditEventType(str, Enum):
    PAYLOAD_RECEIVED = "PAYLOAD_RECEIVED"
    SECURITY_VALIDATED = "SECURITY_VALIDATED"
    SECURITY_REJECTED = "SECURITY_REJECTED"
    POLICY_EVALUATED = "POLICY_EVALUATED"
    APPROVED = "APPROVED"
    FLAGGED = "FLAGGED"
    NOTIFICATION_SENT = "NOTIFICATION_SENT"
    MCP_ERROR = "MCP_ERROR"
    SYSTEM_ERROR = "SYSTEM_ERROR"


# ─────────────────────────────────────────────────────────────────────────────
# Shared Base
# ─────────────────────────────────────────────────────────────────────────────

class TimestampedModel(BaseModel):
    model_config = ConfigDict(
        str_strip_whitespace=True,
        validate_assignment=True,
        populate_by_name=True,
    )

    created_at: datetime = Field(
        default_factory=lambda: datetime.now(timezone.utc)
    )


# ─────────────────────────────────────────────────────────────────────────────
# Inbound Payload
# ─────────────────────────────────────────────────────────────────────────────

class ExpensePayload(TimestampedModel):
    """
    Raw inbound expense object intercepted by ExpenseAuditorAgent.
    Strict validators fire before any agent touches the data.
    """

    trace_id: UUID = Field(default_factory=uuid4, description="Auto-generated correlation UUID")
    department: Annotated[str, Field(min_length=1, max_length=128, description="Submitting department name")]
    amount: Annotated[Decimal, Field(gt=Decimal("0"), lt=Decimal("1000000"), decimal_places=2)]
    category: ExpenseCategory = ExpenseCategory.MISCELLANEOUS
    employee_id: Annotated[str, Field(min_length=3, max_length=64)] = "UNKNOWN"
    employee_email: str = ""
    description: str = Field(default="", max_length=512)
    session_key: Annotated[str, Field(min_length=1, max_length=512)]
    currency: str = Field(default="USD", pattern=r"^[A-Z]{3}$")
    submitted_by_ip: str = Field(default="0.0.0.0")
    metadata: dict[str, Any] = Field(default_factory=dict)

    @field_validator("department")
    @classmethod
    def department_must_not_be_whitespace(cls, v: str) -> str:
        if not v.strip():
            raise ValueError("department cannot be blank or whitespace-only")
        return v.strip().title()

    @field_validator("employee_email")
    @classmethod
    def validate_email_format(cls, v: str) -> str:
        if v and not re.match(r"^[^@\s]+@[^@\s]+\.[^@\s]+$", v):
            raise ValueError(f"Invalid email format: {v!r}")
        return v.lower()

    @field_validator("amount", mode="before")
    @classmethod
    def coerce_amount(cls, v: Any) -> Decimal:
        try:
            return Decimal(str(v)).quantize(Decimal("0.01"))
        except Exception:
            raise ValueError(f"amount must be a valid decimal number, got {v!r}")

    @computed_field
    @property
    def amount_cents(self) -> int:
        """Lossless integer representation for deterministic policy comparison."""
        return int(self.amount * 100)

    @computed_field
    @property
    def payload_fingerprint(self) -> str:
        """SHA-256 fingerprint of deterministic payload fields for dedup."""
        raw = f"{self.department}:{self.employee_id}:{self.amount_cents}:{self.created_at.date()}"
        return hashlib.sha256(raw.encode()).hexdigest()


# ─────────────────────────────────────────────────────────────────────────────
# Policy Models
# ─────────────────────────────────────────────────────────────────────────────

class PolicyLimit(BaseModel):
    """A single departmental spending rule loaded from policy_rules.json."""

    department: str
    category: str = "*"  # "*" = applies to all categories
    limit_usd: Decimal
    currency: str = "USD"
    escalation_threshold_usd: Decimal | None = None  # auto-escalate beyond this
    description: str = ""

    @computed_field
    @property
    def limit_cents(self) -> int:
        return int(self.limit_usd * 100)

    @computed_field
    @property
    def escalation_cents(self) -> int | None:
        if self.escalation_threshold_usd is not None:
            return int(self.escalation_threshold_usd * 100)
        return None


class PolicyRuleSet(BaseModel):
    """Full ruleset loaded at startup from config/policy_rules.json."""

    version: str = "1.0.0"
    default_limit_usd: Decimal = Decimal("50.00")
    rules: list[PolicyLimit] = Field(default_factory=list)

    def resolve_limit(self, department: str, category: str = "*") -> PolicyLimit:
        """
        Deterministic policy resolution:
        1. Exact department + category match
        2. Department + wildcard category match
        3. Global default
        """
        dept_lower = department.lower()
        cat_lower = category.lower()

        # Priority 1 — exact match
        for rule in self.rules:
            if rule.department.lower() == dept_lower and rule.category.lower() == cat_lower:
                return rule

        # Priority 2 — department wildcard
        for rule in self.rules:
            if rule.department.lower() == dept_lower and rule.category == "*":
                return rule

        # Priority 3 — global default
        return PolicyLimit(
            department="*",
            category="*",
            limit_usd=self.default_limit_usd,
            description="Global default policy limit",
        )


# ─────────────────────────────────────────────────────────────────────────────
# Evaluation Results
# ─────────────────────────────────────────────────────────────────────────────

class PolicyEvaluationResult(TimestampedModel):
    """Output from PolicyEvaluatorWorker — pure data, no LLM text."""

    trace_id: UUID
    department: str
    amount_usd: Decimal
    limit_usd: Decimal
    variance_usd: Decimal
    status: ComplianceStatus
    applied_rule: PolicyLimit
    requires_escalation: bool = False
    evaluation_latency_ms: float = 0.0

    @model_validator(mode="after")
    def compute_derived_fields(self) -> "PolicyEvaluationResult":
        # Ensure variance is always positive
        if self.variance_usd < 0:
            self.variance_usd = Decimal("0.00")
        return self


# ─────────────────────────────────────────────────────────────────────────────
# Resolution / Notification Models
# ─────────────────────────────────────────────────────────────────────────────

class NotificationPayload(BaseModel):
    """Structured message dispatched by ResolutionCommunicator."""

    trace_id: UUID
    channel: NotificationChannel = NotificationChannel.SLACK
    recipient: str  # Slack user ID or Teams email
    subject: str
    body: str
    severity: str = "WARNING"  # INFO | WARNING | CRITICAL
    expense_summary: dict[str, Any] = Field(default_factory=dict)
    sent_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))


class NotificationResult(BaseModel):
    trace_id: UUID
    channel: NotificationChannel
    success: bool
    attempts: int
    response_code: int | None = None
    error_detail: str | None = None


# ─────────────────────────────────────────────────────────────────────────────
# Audit Event
# ─────────────────────────────────────────────────────────────────────────────

class AuditEvent(BaseModel):
    """Immutable audit event written to the event log on every state transition."""

    event_id: UUID = Field(default_factory=uuid4)
    trace_id: UUID
    event_type: AuditEventType
    agent_name: str
    payload_snapshot: dict[str, Any] = Field(default_factory=dict)
    outcome: str = ""
    error_detail: str | None = None
    duration_ms: float = 0.0
    timestamp: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))


# ─────────────────────────────────────────────────────────────────────────────
# API Response Envelopes
# ─────────────────────────────────────────────────────────────────────────────

class ComplianceResponse(BaseModel):
    """Standard success envelope returned by POST /submit-expense."""

    trace_id: UUID
    status: ComplianceStatus
    department: str
    amount_usd: Decimal
    limit_usd: Decimal
    variance_usd: Decimal
    message: str
    requires_escalation: bool = False
    notification_dispatched: bool = False
    processing_time_ms: float = 0.0


class ErrorResponse(BaseModel):
    """Standard error envelope — always returned for any non-2xx response."""

    error: str
    message: str
    http_status: int
    trace_id: str
    timestamp: str = Field(
        default_factory=lambda: datetime.now(timezone.utc).isoformat()
    )
    field_errors: list[dict[str, Any]] = Field(default_factory=list)
    context: dict[str, Any] = Field(default_factory=dict)
