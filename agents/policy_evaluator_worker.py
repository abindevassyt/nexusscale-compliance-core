"""
agents/policy_evaluator_worker.py
──────────────────────────────────
PolicyEvaluatorWorker — WORKER (Sub-Agent 1)

Bound to MCP tool: fetch_corporate_policy

This worker performs DETERMINISTIC INTEGER COMPARISON of expense amounts
against policy limits. No LLM inference is involved — this is pure
arithmetic against fetched policy data. Outcomes are enum values:
APPROVED or FLAGGED.

Evaluation algorithm:
  1. Call MCP tool `fetch_corporate_policy(department, category)`
  2. Compare payload.amount_cents vs policy.limit_cents (integer arithmetic)
  3. If amount_cents > limit_cents → FLAGGED
  4. If amount_cents > escalation_cents → requires_escalation = True
  5. Return typed PolicyEvaluationResult
"""

from __future__ import annotations

import logging
import time
from decimal import Decimal

from agents.base_agent import AgentRole, AgentRunContext, BaseAgent
from core.exceptions import PolicyEvaluationError
from core.models import (
    AuditEventType,
    ComplianceStatus,
    ExpensePayload,
    PolicyEvaluationResult,
    PolicyLimit,
    PolicyRuleSet,
)
from mcp.client import MCPClient
from mcp.tools import fetch_corporate_policy

logger = logging.getLogger("agents.policy_evaluator")


class PolicyEvaluatorWorker(BaseAgent):
    """
    Worker 1 — Deterministic Policy Evaluation Engine.

    Can operate in two modes:
      MCP mode:    Fetches policy from enterprise bridge via fetch_corporate_policy tool.
      Local mode:  Falls back to in-process PolicyRuleSet if MCP is unavailable.

    The evaluation result is ALWAYS a deterministic enum — no LLM text involved.
    """

    name = "PolicyEvaluatorWorker"
    persona = "Corporate Policy Compliance Evaluator"
    role = AgentRole.WORKER
    version = "1.0.0"

    def __init__(
        self,
        mcp_client: MCPClient,
        local_ruleset: PolicyRuleSet | None = None,
    ) -> None:
        super().__init__()
        self._mcp = mcp_client
        self._local_ruleset = local_ruleset  # fallback if MCP fails
        self._policy_cache: dict[tuple[str, str], dict] = {}
        self._cache_ttl = 60.0  # seconds

    # ── Validation ────────────────────────────────────────────────────────────

    def _validate_inputs(self, payload: ExpensePayload, context: AgentRunContext) -> None:
        if not isinstance(payload, ExpensePayload):
            raise PolicyEvaluationError(
                message=f"PolicyEvaluatorWorker expected ExpensePayload, got {type(payload).__name__}",
                correlation_id=context.correlation_id,
            )
        if payload.amount_cents <= 0:
            raise PolicyEvaluationError(
                message="Expense amount must be positive",
                correlation_id=context.correlation_id,
                context={"amount_cents": payload.amount_cents},
            )

    # ── Lifecycle ─────────────────────────────────────────────────────────────

    async def before_run(self, payload: ExpensePayload, context: AgentRunContext) -> None:
        logger.info(
            "PolicyEvaluator starting",
            extra={
                "agent": self.name,
                "trace_id": str(context.trace_id),
                "department": payload.department,
                "amount_cents": payload.amount_cents,
                "category": payload.category.value,
            },
        )

    async def after_run(
        self, result: PolicyEvaluationResult, context: AgentRunContext
    ) -> None:
        await self._emit_audit(
            context,
            event_type=AuditEventType.POLICY_EVALUATED,
            outcome=f"Policy evaluation: {result.status.value}",
            payload_snapshot={
                "department": result.department,
                "amount_usd": float(result.amount_usd),
                "limit_usd": float(result.limit_usd),
                "variance_usd": float(result.variance_usd),
                "status": result.status.value,
                "requires_escalation": result.requires_escalation,
                "evaluation_latency_ms": round(result.evaluation_latency_ms, 2),
            },
            duration_ms=result.evaluation_latency_ms,
        )

    # ── Core Logic ────────────────────────────────────────────────────────────

    async def run(
        self, payload: ExpensePayload, context: AgentRunContext
    ) -> PolicyEvaluationResult:
        """
        Deterministic expense-vs-policy evaluation.

        Step 1: Fetch applicable policy (MCP → local fallback)
        Step 2: Integer comparison of amount_cents vs limit_cents
        Step 3: Check escalation threshold
        Step 4: Emit structured result
        """
        eval_start = time.monotonic()

        # ── Step 1: Policy Fetch ─────────────────────────────────────────────
        policy = await self._fetch_policy(payload, context)

        logger.info(
            "Policy resolved",
            extra={
                "agent": self.name,
                "trace_id": str(context.trace_id),
                "department": payload.department,
                "policy_limit_cents": policy.limit_cents,
                "policy_limit_usd": float(policy.limit_usd),
                "applied_category": policy.category,
            },
        )

        # ── Step 2: Deterministic Integer Comparison ─────────────────────────
        #   Integer cents arithmetic eliminates all floating-point hazards.
        #   This is a pure comparison — NO LLM text generation involved.
        amount_cents: int = payload.amount_cents
        limit_cents: int = policy.limit_cents
        over_limit: bool = amount_cents > limit_cents
        variance_cents: int = max(0, amount_cents - limit_cents)

        logger.debug(
            "Integer policy comparison",
            extra={
                "amount_cents": amount_cents,
                "limit_cents": limit_cents,
                "over_limit": over_limit,
                "variance_cents": variance_cents,
            },
        )

        # ── Step 3: Escalation Check ─────────────────────────────────────────
        requires_escalation = False
        if over_limit and policy.escalation_cents is not None:
            requires_escalation = amount_cents > policy.escalation_cents

        # ── Step 4: Build Result ──────────────────────────────────────────────
        status = ComplianceStatus.FLAGGED if over_limit else ComplianceStatus.APPROVED
        evaluation_latency_ms = (time.monotonic() - eval_start) * 1000

        result = PolicyEvaluationResult(
            trace_id=payload.trace_id,
            department=payload.department,
            amount_usd=payload.amount,
            limit_usd=policy.limit_usd,
            variance_usd=Decimal(str(variance_cents / 100)).quantize(Decimal("0.01")),
            status=status,
            applied_rule=policy,
            requires_escalation=requires_escalation,
            evaluation_latency_ms=evaluation_latency_ms,
        )

        _log_evaluation_result(result, context)
        return result

    # ── Policy Fetch with Local Fallback ──────────────────────────────────────

    async def _fetch_policy(
        self, payload: ExpensePayload, context: AgentRunContext
    ) -> PolicyLimit:
        """
        Primary: MCP tool fetch_corporate_policy
        Fallback: local PolicyRuleSet.resolve_limit()
        """
        from core.exceptions import MCPError

        cache_key = (payload.department, payload.category.value)
        cached = self._policy_cache.get(cache_key)
        if cached and (time.monotonic() - cached["timestamp"]) < self._cache_ttl:
            return cached["policy"]

        try:
            policy = await fetch_corporate_policy(
                client=self._mcp,
                department=payload.department,
                category=payload.category.value,
                correlation_id=context.correlation_id,
            )
            self._policy_cache[cache_key] = {"policy": policy, "timestamp": time.monotonic()}
            return policy
        except MCPError as exc:
            logger.warning(
                "MCP policy fetch failed — falling back to local ruleset",
                extra={
                    "agent": self.name,
                    "error": exc.error_code,
                    "trace_id": str(context.trace_id),
                },
            )
            if self._local_ruleset is not None:
                return self._local_ruleset.resolve_limit(
                    payload.department, payload.category.value
                )
            # No local fallback — re-raise to let circuit breaker handle it
            raise


# ─────────────────────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────────────────────

def _log_evaluation_result(result: PolicyEvaluationResult, context: AgentRunContext) -> None:
    log_fn = logger.warning if result.status == ComplianceStatus.FLAGGED else logger.info
    log_fn(
        f"{'🚨 FLAGGED' if result.status == ComplianceStatus.FLAGGED else '✅ APPROVED'}: "
        f"${float(result.amount_usd):.2f} vs ${float(result.limit_usd):.2f} limit",
        extra={
            "agent": "PolicyEvaluatorWorker",
            "trace_id": str(context.trace_id),
            "department": result.department,
            "status": result.status.value,
            "variance_usd": float(result.variance_usd),
            "requires_escalation": result.requires_escalation,
            "eval_latency_ms": round(result.evaluation_latency_ms, 2),
        },
    )
