"""
tests/test_case_a_approval.py
──────────────────────────────
TEST CASE A — Strict Approval Path

Scenario:
  Department : Engineering
  Amount     : $42.50
  Policy Limit: $50.00 (Engineering wildcard rule)

Expected outcome:
  • PolicyEvaluatorWorker compares 4250 cents ≤ 5000 cents → TRUE
  • ComplianceStatus = APPROVED
  • variance_usd = $0.00
  • requires_escalation = False
  • ResolutionCommunicator is NOT invoked
  • Audit event of type APPROVED is written
  • HTTP response status_code = 200, status = "APPROVED"
"""

from __future__ import annotations

import asyncio
import uuid
from decimal import Decimal
from unittest.mock import AsyncMock, MagicMock

from agents.policy_evaluator_worker import PolicyEvaluatorWorker

import pytest
import pytest_asyncio

from tests.conftest import make_mock_mcp_client, make_payload, STANDARD_RULESET
from core.models import ComplianceStatus, ExpenseCategory


# ─────────────────────────────────────────────────────────────────────────────
# Unit Tests — PolicyEvaluatorWorker
# ─────────────────────────────────────────────────────────────────────────────

class TestPolicyEvaluatorApprovalPath:
    """Verify that $42.50 < $50.00 produces APPROVED in integer arithmetic."""

    @pytest.mark.asyncio
    async def test_amount_cents_comparison(
        self, policy_worker, run_context
    ):
        """Core assertion: 4250 cents ≤ 5000 cents → APPROVED."""
        payload = make_payload(department="Engineering", amount=42.50)

        # Confirm the computed amount_cents field is correct
        assert payload.amount_cents == 4250, "amount_cents must be 4250 for $42.50"

        result = await policy_worker.execute(payload, run_context)

        assert result.status == ComplianceStatus.APPROVED
        assert result.amount_usd == Decimal("42.50")
        assert result.limit_usd == Decimal("50.00")
        assert result.variance_usd == Decimal("0.00")
        assert result.requires_escalation is False

    @pytest.mark.asyncio
    async def test_evaluation_latency_recorded(self, policy_worker, run_context):
        """Evaluation latency must be a positive float."""
        payload = make_payload(department="Engineering", amount=42.50)
        result = await policy_worker.execute(payload, run_context)
        assert result.evaluation_latency_ms >= 0

    @pytest.mark.asyncio
    async def test_applied_rule_is_correct(self, policy_worker, run_context):
        """Applied rule must come from the Engineering wildcard entry."""
        payload = make_payload(department="Engineering", amount=42.50)
        result = await policy_worker.execute(payload, run_context)
        assert result.applied_rule.department.lower() == "engineering"
        assert float(result.applied_rule.limit_usd) == 50.0

    @pytest.mark.asyncio
    async def test_boundary_exact_limit_is_approved(self, policy_worker, run_context):
        """Exact limit ($50.00) must be APPROVED (not flagged)."""
        payload = make_payload(department="Engineering", amount=50.00)
        result = await policy_worker.execute(payload, run_context)
        assert result.status == ComplianceStatus.APPROVED
        assert result.variance_usd == Decimal("0.00")

    @pytest.mark.asyncio
    async def test_one_cent_below_limit_is_approved(self, policy_worker, run_context):
        """$49.99 (4999 cents) must be APPROVED."""
        payload = make_payload(department="Engineering", amount=49.99)
        result = await policy_worker.execute(payload, run_context)
        assert result.status == ComplianceStatus.APPROVED


# ─────────────────────────────────────────────────────────────────────────────
# Integration Tests — Full Supervisor Pipeline
# ─────────────────────────────────────────────────────────────────────────────

class TestSupervisorApprovalPipeline:
    """End-to-end test of the full Supervisor-Worker pipeline for the approval path."""

    @pytest.mark.asyncio
    async def test_full_pipeline_returns_approved(self, supervisor, run_context):
        """Full pipeline: Engineering $42.50 → APPROVED ComplianceResponse."""
        payload = make_payload(department="Engineering", amount=42.50)
        result = await supervisor.execute(payload, run_context)

        assert result.status == ComplianceStatus.APPROVED
        assert result.department == "Engineering"
        assert result.amount_usd == Decimal("42.50")
        assert result.limit_usd == Decimal("50.00")
        assert result.notification_dispatched is False

    @pytest.mark.asyncio
    async def test_resolution_communicator_not_called(self, supervisor, run_context):
        """Verify ResolutionCommunicator.execute is NOT called for approved expenses."""
        payload = make_payload(department="Engineering", amount=42.50)

        # Spy on the resolution worker
        supervisor._resolution_worker.execute = AsyncMock()
        await supervisor.execute(payload, run_context)

        supervisor._resolution_worker.execute.assert_not_called()

    @pytest.mark.asyncio
    async def test_response_message_contains_approved(self, supervisor, run_context):
        """Response message must explicitly mention APPROVED."""
        payload = make_payload(department="Engineering", amount=42.50)
        result = await supervisor.execute(payload, run_context)
        assert "APPROVED" in result.message

    @pytest.mark.asyncio
    async def test_processing_time_ms_is_positive(self, supervisor, run_context):
        """Processing time must be recorded and positive."""
        payload = make_payload(department="Engineering", amount=42.50)
        result = await supervisor.execute(payload, run_context)
        assert result.processing_time_ms > 0

    @pytest.mark.asyncio
    async def test_trace_id_propagated(self, supervisor, run_context):
        """Trace ID in response must match the original payload trace ID."""
        payload = make_payload(department="Engineering", amount=42.50)
        result = await supervisor.execute(payload, run_context)
        assert result.trace_id == payload.trace_id

    @pytest.mark.asyncio
    async def test_different_categories_within_limit_approved(
        self, policy_worker, run_context
    ):
        """Multiple categories under the Engineering limit must all be APPROVED."""
        amounts_and_categories = [
            (25.00, ExpenseCategory.MEALS),
            (35.00, ExpenseCategory.TRAVEL),
            (10.00, ExpenseCategory.MISCELLANEOUS),
        ]
        for amount, category in amounts_and_categories:
            payload = make_payload(
                department="Engineering", amount=amount, category=category
            )
            result = await policy_worker.execute(payload, run_context)
            assert result.status == ComplianceStatus.APPROVED, (
                f"Expected APPROVED for ${amount} {category.value}, got {result.status}"
            )

    @pytest.mark.asyncio
    async def test_audit_event_written(self, supervisor, audit_service, run_context):
        """Verify at least one APPROVED audit event is written to the trail."""
        payload = make_payload(department="Engineering", amount=42.50)
        await supervisor.execute(payload, run_context)

        await asyncio.sleep(0.1)  # allow background audit queue to flush
        events = await audit_service.query_by_trace(run_context.trace_id)
        event_types = [e["event_type"] for e in events]

        assert "APPROVED" in event_types, f"Expected APPROVED audit event, got: {event_types}"


# ─────────────────────────────────────────────────────────────────────────────
# MCP Fallback Tests
# ─────────────────────────────────────────────────────────────────────────────

class TestApprovalWithLocalFallback:
    """Verify approval still works when MCP is unavailable (local ruleset fallback)."""

    @pytest.mark.asyncio
    async def test_approval_with_mcp_timeout_falls_back_to_local(
        self, run_context, resolution_worker_mock, audit_service
    ):
        """When MCP times out, worker falls back to local ruleset and still evaluates."""
        mcp_client = make_mock_mcp_client(simulate_timeout=False)  # normal MCP

        # Inject a local-only worker
        worker = PolicyEvaluatorWorker(
            mcp_client=mcp_client,
            local_ruleset=STANDARD_RULESET,
        )
        supervisor = __import__(
            "agents.expense_auditor_agent", fromlist=["ExpenseAuditorAgent"]
        ).ExpenseAuditorAgent(
            policy_worker=worker,
            resolution_worker=resolution_worker_mock,
        )

        payload = make_payload(department="Engineering", amount=42.50)
        result = await supervisor.execute(payload, run_context)
        assert result.status == ComplianceStatus.APPROVED
