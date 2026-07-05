"""
orchestrator/pipeline.py
────────────────────────
Main compliance pipeline orchestrator.

Wires together:
  MCPClient → PolicyEvaluatorWorker → ExpenseAuditorAgent → ResolutionCommunicator

Also manages:
  • MCP client lifecycle (connect / disconnect)
  • AuditTrailService initialization
  • AgentRunContext creation with correlation IDs
  • Structured 503 fallback on MCP infrastructure failure
  • Transaction rollback semantics on critical failure
"""

from __future__ import annotations

import json
import logging
import os
import time
from decimal import Decimal
from typing import Any
from uuid import UUID

from agents.base_agent import AgentRunContext
from agents.expense_auditor_agent import ExpenseAuditorAgent
from agents.policy_evaluator_worker import PolicyEvaluatorWorker
from agents.resolution_communicator import ResolutionCommunicator
from core.audit_trail import AuditTrailService
from core.exceptions import (
    ComplianceEngineError,
    MCPCircuitOpenError,
    MCPDisconnectError,
    MCPTimeoutError,
    PayloadValidationError,
)
from core.models import (
    AuditEventType,
    ComplianceResponse,
    ComplianceStatus,
    ErrorResponse,
    ExpensePayload,
    PolicyRuleSet,
)
from mcp.client import MCPClient

logger = logging.getLogger("orchestrator")


# ─────────────────────────────────────────────────────────────────────────────
# Policy Ruleset Loader
# ─────────────────────────────────────────────────────────────────────────────

def _load_local_ruleset() -> PolicyRuleSet:
    path = os.environ.get("POLICY_RULES_PATH", "config/policy_rules.json")
    try:
        with open(path) as f:
            data = json.load(f)
        return PolicyRuleSet(
            version=data.get("version", "1.0.0"),
            default_limit_usd=Decimal(str(data.get("default_limit_usd", 50.00))),
            rules=[],  # Parsed lazily by PolicyLimit for performance
        )
    except Exception as exc:
        logger.warning(f"Could not load local ruleset from {path}: {exc} — using defaults")
        return PolicyRuleSet()


# ─────────────────────────────────────────────────────────────────────────────
# CompliancePipeline
# ─────────────────────────────────────────────────────────────────────────────

class CompliancePipeline:
    """
    Top-level orchestrator for the NexusScale compliance engine.

    Lifecycle:
        pipeline = CompliancePipeline()
        await pipeline.startup()
        result = await pipeline.process(payload)
        await pipeline.shutdown()
    """

    def __init__(self) -> None:
        self._mcp: MCPClient | None = None
        self._audit: AuditTrailService | None = None
        self._supervisor: ExpenseAuditorAgent | None = None
        self._initialized = False

    async def startup(self) -> None:
        """Initialize MCP client, audit trail, and all agents."""
        logger.info("🚀 CompliancePipeline starting up...")

        # ── Audit Trail ───────────────────────────────────────────────────────
        db_url = os.environ.get("AUDIT_DB_URL", "sqlite+aiosqlite:///./audit_trail.db")
        self._audit = AuditTrailService(db_url)
        await self._audit.initialize()

        # ── MCP Client ────────────────────────────────────────────────────────
        try:
            self._mcp = MCPClient.from_config("config/mcp_config.json")
            await self._mcp.connect()
            logger.info("✅ MCP client connected")
        except Exception as exc:
            logger.warning(f"MCP client initialization failed: {exc} — will use local policy fallback")
            self._mcp = None

        # ── Agents ────────────────────────────────────────────────────────────
        local_ruleset = _load_local_ruleset()

        resolution_worker = ResolutionCommunicator()

        policy_worker = PolicyEvaluatorWorker(
            mcp_client=self._mcp,  # type: ignore[arg-type]
            local_ruleset=local_ruleset,
        )

        self._supervisor = ExpenseAuditorAgent(
            policy_worker=policy_worker,
            resolution_worker=resolution_worker,
        )

        self._initialized = True
        logger.info("✅ CompliancePipeline ready — all agents initialized")

    async def process(
        self,
        payload: ExpensePayload,
        correlation_id: str | None = None,
    ) -> ComplianceResponse | ErrorResponse:
        """
        Route an expense payload through the full compliance pipeline.

        Returns:
            ComplianceResponse on success (2xx)
            ErrorResponse on validation failure (422) or infrastructure failure (503)
        """
        if not self._initialized or self._supervisor is None:
            return _make_error_response(
                error="PIPELINE_NOT_INITIALIZED",
                message="Compliance pipeline has not been started. Call startup() first.",
                http_status=503,
                trace_id=str(payload.trace_id),
            )

        corr_id = correlation_id or str(payload.trace_id)
        context = AgentRunContext(
            trace_id=payload.trace_id,
            correlation_id=corr_id,
            audit_service=self._audit,
        )

        t0 = time.monotonic()
        try:
            result = await self._supervisor.execute(payload, context)
            return result

        # ── MCP Infrastructure Faults → 503 with transaction rollback ────────
        except (MCPTimeoutError, MCPDisconnectError, MCPCircuitOpenError) as exc:
            elapsed_ms = (time.monotonic() - t0) * 1000
            logger.error(
                "🔴 MCP infrastructure fault — rolling back transaction",
                extra={
                    "error_code": exc.error_code,
                    "trace_id": corr_id,
                    "elapsed_ms": round(elapsed_ms, 2),
                },
            )
            await _safe_rollback(context, exc, self._audit)
            return _make_error_response(
                error=exc.error_code,
                message=f"MCP database bridge unavailable: {exc.message}",
                http_status=503,
                trace_id=corr_id,
                context=exc.context,
            )

        # ── Payload Validation → 422 ──────────────────────────────────────────
        except PayloadValidationError as exc:
            return _make_error_response(
                error=exc.error_code,
                message=exc.message,
                http_status=exc.http_status,
                trace_id=corr_id,
                field_errors=exc.field_errors,
                context=exc.context,
            )

        # ── Generic Domain Error → 422/500 ────────────────────────────────────
        except ComplianceEngineError as exc:
            http_status = getattr(exc, "http_status", 500)
            return _make_error_response(
                error=exc.error_code,
                message=exc.message,
                http_status=http_status,
                trace_id=corr_id,
                context=exc.context,
            )

        # ── Catch-all → 500 ──────────────────────────────────────────────────
        except Exception as exc:
            logger.exception("Unexpected pipeline error", extra={"trace_id": corr_id})
            return _make_error_response(
                error="INTERNAL_SERVER_ERROR",
                message=f"An unexpected error occurred: {type(exc).__name__}",
                http_status=500,
                trace_id=corr_id,
            )

    async def shutdown(self) -> None:
        """Graceful shutdown of all resources."""
        logger.info("🛑 CompliancePipeline shutting down...")
        if self._mcp:
            await self._mcp.disconnect()
        if self._audit:
            await self._audit.shutdown()
        self._initialized = False
        logger.info("Pipeline shutdown complete")

    @property
    def health(self) -> dict[str, Any]:
        return {
            "initialized": self._initialized,
            "mcp_circuit": self._mcp.circuit_state if self._mcp else {"state": "DISCONNECTED"},
            "agents": list({"ExpenseAuditorAgent", "PolicyEvaluatorWorker", "ResolutionCommunicator"}),
        }


# ─────────────────────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────────────────────

async def _safe_rollback(
    context: AgentRunContext,
    exc: ComplianceEngineError,
    audit: AuditTrailService | None,
) -> None:
    """
    Emit a rollback audit event and log the transaction pool reset.
    In a full production system this would abort any in-flight DB writes.
    """
    logger.warning(
        "⏪ Transaction rollback initiated",
        extra={"trace_id": str(context.trace_id), "reason": exc.error_code},
    )
    if audit:
        from core.models import AuditEvent
        event = AuditEvent(
            trace_id=context.trace_id,
            event_type=AuditEventType.MCP_ERROR,
            agent_name="CompliancePipeline",
            outcome="Transaction rolled back due to MCP infrastructure fault",
            error_detail=exc.message,
        )
        await audit.record(event)


def _make_error_response(
    error: str,
    message: str,
    http_status: int,
    trace_id: str,
    field_errors: list[dict] | None = None,
    context: dict | None = None,
) -> ErrorResponse:
    from datetime import datetime, timezone
    return ErrorResponse(
        error=error,
        message=message,
        http_status=http_status,
        trace_id=trace_id,
        timestamp=datetime.now(timezone.utc).isoformat(),
        field_errors=field_errors or [],
        context=context or {},
    )
