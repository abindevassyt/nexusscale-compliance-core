"""
agents/expense_auditor_agent.py
────────────────────────────────
ExpenseAuditorAgent — SUPERVISOR

Persona: "Financial Ingress Supervisor"
Role:    Intercepts raw unstructured expense metadata, validates payload
         integrity and security, then orchestrates the downstream worker
         pipeline (PolicyEvaluatorWorker → ResolutionCommunicator).

Design Pattern: Supervisor-Worker
  1. Receive raw ExpensePayload
  2. Run preflight security validation (department, session key)
  3. Dispatch to PolicyEvaluatorWorker → get PolicyEvaluationResult
  4. If FLAGGED → dispatch to ResolutionCommunicator
  5. Emit comprehensive audit events at each gate
  6. Return a ComplianceResponse envelope
"""

from __future__ import annotations

import logging
import time
from decimal import Decimal

from agents.base_agent import AgentRole, AgentRunContext, BaseAgent
from core.audit_trail import AuditTrailService
from core.exceptions import (
    ComplianceEngineError,
    DepartmentEmptyError,
    PayloadValidationError,
    SecurityValidationError,
)
from core.models import (
    AuditEventType,
    ComplianceResponse,
    ComplianceStatus,
    ExpensePayload,
    PolicyEvaluationResult,
)
from core.security import validate_inbound_payload_security

logger = logging.getLogger("agents.expense_auditor")


class ExpenseAuditorAgent(BaseAgent):
    """
    Financial Ingress Supervisor — the top-level orchestrator.

    Injected dependencies:
        policy_worker: PolicyEvaluatorWorker instance
        resolution_worker: ResolutionCommunicator instance
    """

    name = "ExpenseAuditorAgent"
    persona = "Financial Ingress Supervisor"
    role = AgentRole.SUPERVISOR
    version = "1.0.0"

    def __init__(
        self,
        policy_worker: "BaseAgent",
        resolution_worker: "BaseAgent",
    ) -> None:
        super().__init__()
        self._policy_worker = policy_worker
        self._resolution_worker = resolution_worker

    # ── Validation Guard ──────────────────────────────────────────────────────

    def _validate_inputs(self, payload: ExpensePayload, context: AgentRunContext) -> None:
        """
        Enforce security preconditions before any processing begins.
        An empty department or invalid session key causes an immediate abort
        with a structured 422 error — no policy evaluation, no DB write.
        """
        try:
            validate_inbound_payload_security(
                department=payload.department,
                session_key=payload.session_key,
                employee_id=payload.employee_id,
                correlation_id=context.correlation_id,
            )
        except (DepartmentEmptyError, SecurityValidationError):
            raise  # Let domain exceptions propagate with their structured metadata
        except Exception as exc:
            raise PayloadValidationError(
                message=f"Payload security validation failed: {exc}",
                correlation_id=context.correlation_id,
            ) from exc

    # ── Lifecycle Hooks ───────────────────────────────────────────────────────

    async def before_run(self, payload: ExpensePayload, context: AgentRunContext) -> None:
        await self._emit_audit(
            context,
            event_type=AuditEventType.PAYLOAD_RECEIVED,
            outcome="Payload received and queued for security validation",
            payload_snapshot={
                "trace_id": str(payload.trace_id),
                "department": payload.department,
                "amount_usd": float(payload.amount),
                "employee_id": payload.employee_id,
                "category": payload.category.value,
                "payload_fingerprint": payload.payload_fingerprint,
            },
        )
        logger.info(
            "📥 Expense payload received",
            extra={
                "agent": self.name,
                "trace_id": str(payload.trace_id),
                "department": payload.department,
                "amount_usd": float(payload.amount),
                "category": payload.category.value,
                "employee_id": payload.employee_id,
                "fingerprint": payload.payload_fingerprint[:12] + "...",
            },
        )

    async def after_run(
        self, result: ComplianceResponse, context: AgentRunContext
    ) -> None:
        logger.info(
            "📤 Supervisor pipeline complete",
            extra={
                "agent": self.name,
                "trace_id": str(result.trace_id),
                "status": result.status.value,
                "processing_time_ms": round(result.processing_time_ms, 2),
            },
        )

    async def on_error(self, payload: ExpensePayload, context: AgentRunContext) -> None:
        await self._emit_audit(
            context,
            event_type=AuditEventType.SYSTEM_ERROR,
            outcome="Supervisor pipeline failed",
            error_detail=f"Error in {self.name}",
        )

    # ── Core Logic ────────────────────────────────────────────────────────────

    async def run(
        self, payload: ExpensePayload, context: AgentRunContext
    ) -> ComplianceResponse:
        """
        Supervisor orchestration pipeline:
          Gate 1 → Security validated (by _validate_inputs before this runs)
          Gate 2 → Delegate to PolicyEvaluatorWorker
          Gate 3 → Route to ResolutionCommunicator if FLAGGED
          Gate 4 → Return ComplianceResponse
        """
        pipeline_start = time.monotonic()

        # ── Gate 1: Security Passed ──────────────────────────────────────────
        await self._emit_audit(
            context,
            event_type=AuditEventType.SECURITY_VALIDATED,
            outcome=f"Session key and department validated for employee {payload.employee_id}",
            payload_snapshot={"employee_id": payload.employee_id, "department": payload.department},
        )
        logger.info(
            "🔐 Security validation PASSED",
            extra={"agent": self.name, "trace_id": str(context.trace_id)},
        )

        # ── Gate 2: Policy Evaluation ────────────────────────────────────────
        logger.info(
            "🔄 Dispatching to PolicyEvaluatorWorker",
            extra={
                "agent": self.name,
                "trace_id": str(context.trace_id),
                "department": payload.department,
                "amount_usd": float(payload.amount),
            },
        )

        eval_result: PolicyEvaluationResult = await self._policy_worker.execute(
            payload, context
        )

        # ── Gate 3: Conditional Resolution Dispatch ──────────────────────────
        notification_dispatched = False
        if eval_result.status == ComplianceStatus.FLAGGED:
            logger.warning(
                "🚨 Expense FLAGGED — dispatching ResolutionCommunicator",
                extra={
                    "agent": self.name,
                    "trace_id": str(context.trace_id),
                    "amount_usd": float(payload.amount),
                    "limit_usd": float(eval_result.limit_usd),
                    "variance_usd": float(eval_result.variance_usd),
                    "requires_escalation": eval_result.requires_escalation,
                },
            )
            await self._resolution_worker.execute(
                {"payload": payload, "evaluation": eval_result},
                context,
            )
            notification_dispatched = True
        else:
            logger.info(
                "✅ Expense APPROVED — no notification required",
                extra={
                    "agent": self.name,
                    "trace_id": str(context.trace_id),
                    "amount_usd": float(payload.amount),
                    "limit_usd": float(eval_result.limit_usd),
                },
            )

        # ── Gate 4: Build Response ───────────────────────────────────────────
        total_ms = (time.monotonic() - pipeline_start) * 1000

        response = ComplianceResponse(
            trace_id=payload.trace_id,
            status=eval_result.status,
            department=payload.department,
            amount_usd=payload.amount,
            limit_usd=eval_result.limit_usd,
            variance_usd=eval_result.variance_usd,
            message=_build_message(eval_result, payload),
            requires_escalation=eval_result.requires_escalation,
            notification_dispatched=notification_dispatched,
            processing_time_ms=round(total_ms, 2),
        )

        audit_event_type = (
            AuditEventType.APPROVED
            if eval_result.status == ComplianceStatus.APPROVED
            else AuditEventType.FLAGGED
        )
        await self._emit_audit(
            context,
            event_type=audit_event_type,
            outcome=response.message,
            payload_snapshot={
                "status": eval_result.status.value,
                "amount_usd": float(payload.amount),
                "limit_usd": float(eval_result.limit_usd),
                "variance_usd": float(eval_result.variance_usd),
                "notification_dispatched": notification_dispatched,
                "processing_time_ms": total_ms,
            },
            duration_ms=total_ms,
        )

        return response


# ─────────────────────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────────────────────

def _build_message(result: PolicyEvaluationResult, payload: ExpensePayload) -> str:
    if result.status == ComplianceStatus.APPROVED:
        return (
            f"Expense of ${payload.amount:.2f} from {payload.department} department "
            f"is within the ${result.limit_usd:.2f} policy limit. APPROVED."
        )
    escalation_note = " ESCALATION REQUIRED." if result.requires_escalation else ""
    return (
        f"Expense of ${payload.amount:.2f} from {payload.department} department "
        f"exceeds the ${result.limit_usd:.2f} policy limit by ${result.variance_usd:.2f}. "
        f"FLAGGED for review.{escalation_note}"
    )
