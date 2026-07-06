"""
tests/conftest.py
─────────────────
Shared pytest fixtures for the NexusScale Compliance Engine test suite.

Provides:
  • Mocked MCP client (no real HTTP calls)
  • Pre-built audit trail (in-memory SQLite)
  • Valid session key generator
  • Standard AgentRunContext factory
  • Pre-configured agent instances
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import os
import time
import uuid
from decimal import Decimal
from typing import AsyncGenerator
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
import pytest_asyncio

# ─────────────────────────────────────────────────────────────────────────────
# Environment Setup — MUST happen before any module imports
# ─────────────────────────────────────────────────────────────────────────────

# Set required env vars before test modules are imported
os.environ.setdefault("ENTERPRISE_AGENT_SECRET", "test-enterprise-secret-32chars!!")
os.environ.setdefault("SESSION_HMAC_SECRET",    "test-hmac-secret-key-32chars!!!!")
os.environ.setdefault("SESSION_KEY_TTL_SECONDS", "3600")
os.environ.setdefault("AUDIT_DB_URL", "sqlite+aiosqlite:///./test_audit.db")
os.environ.setdefault("MCP_SERVER_URL", "http://localhost:9000/mcp")
os.environ.setdefault("POLICY_RULES_PATH", "config/policy_rules.json")


# ─────────────────────────────────────────────────────────────────────────────
# Imports (after env setup)
# ─────────────────────────────────────────────────────────────────────────────

from agents.base_agent import AgentRunContext
from agents.expense_auditor_agent import ExpenseAuditorAgent
from agents.policy_evaluator_worker import PolicyEvaluatorWorker
from agents.resolution_communicator import ResolutionCommunicator
from core.audit_trail import AuditTrailService
from core.models import ExpenseCategory, ExpensePayload, PolicyLimit, PolicyRuleSet
from mcp.client import MCPClient


# ─────────────────────────────────────────────────────────────────────────────
# Session Key Helper
# ─────────────────────────────────────────────────────────────────────────────

def make_session_key(employee_id: str = "ENG-001", offset_seconds: int = 0) -> str:
    """Generate a valid HMAC-signed session key for tests."""
    secret = os.environ["SESSION_HMAC_SECRET"].encode()
    ts = int(time.time()) + offset_seconds
    message = f"{employee_id}:{ts}".encode()
    sig = hmac.new(secret, message, hashlib.sha256).hexdigest()
    return f"{ts}.{sig}"


# ─────────────────────────────────────────────────────────────────────────────
# Mock MCP Client Factory
# ─────────────────────────────────────────────────────────────────────────────

def make_mock_mcp_client(
    policy_limit_usd: float = 50.0,
    department: str = "Engineering",
    category: str = "*",
    simulate_timeout: bool = False,
    simulate_disconnect: bool = False,
) -> AsyncMock:
    """
    Return an AsyncMock MCPClient that returns a deterministic PolicyLimit
    without making real HTTP calls.
    """
    from core.exceptions import MCPDisconnectError, MCPTimeoutError

    client = AsyncMock(spec=MCPClient)
    client.circuit_state = {"state": "CLOSED", "failure_count": 0}

    if simulate_timeout:
        client.call_tool.side_effect = MCPTimeoutError(
            message="Simulated MCP timeout",
            timeout_seconds=10.0,
        )
    elif simulate_disconnect:
        client.call_tool.side_effect = MCPDisconnectError(
            message="Simulated MCP disconnect",
        )
    else:
        client.call_tool.return_value = {
            "department": department,
            "category": category,
            "limit_usd": policy_limit_usd,
            "limit_cents": int(policy_limit_usd * 100),
            "escalation_threshold_usd": policy_limit_usd * 4,
            "currency": "USD",
            "description": f"Test policy: ${policy_limit_usd} limit",
            "matched_rule": "TEST_FIXTURE",
        }

    return client


# ─────────────────────────────────────────────────────────────────────────────
# Standard Policy Ruleset
# ─────────────────────────────────────────────────────────────────────────────

STANDARD_RULESET = PolicyRuleSet(
    version="1.0.0",
    default_limit_usd=Decimal("50.00"),
    rules=[
        PolicyLimit(department="Engineering", category="*",  limit_usd=Decimal("50.00"),  escalation_threshold_usd=Decimal("200.00")),
        PolicyLimit(department="Marketing",   category="*",  limit_usd=Decimal("50.00"),  escalation_threshold_usd=Decimal("300.00")),
        PolicyLimit(department="Sales",       category="*",  limit_usd=Decimal("75.00"),  escalation_threshold_usd=Decimal("400.00")),
        PolicyLimit(department="Executive",   category="*",  limit_usd=Decimal("500.00"), escalation_threshold_usd=Decimal("5000.00")),
    ],
)


# ─────────────────────────────────────────────────────────────────────────────
# Fixtures
# ─────────────────────────────────────────────────────────────────────────────

@pytest.fixture(scope="session")
def event_loop():
    """Provide a single event loop for the whole test session."""
    loop = asyncio.new_event_loop()
    yield loop
    loop.close()


@pytest_asyncio.fixture
async def audit_service() -> AsyncGenerator[AuditTrailService, None]:
    """In-memory SQLite audit trail for tests."""
    svc = AuditTrailService("sqlite+aiosqlite:///./test_audit_db.sqlite")
    await svc.initialize()
    yield svc
    await svc.shutdown()


@pytest.fixture
def mock_mcp_client() -> AsyncMock:
    return make_mock_mcp_client()


@pytest.fixture
def run_context(audit_service: AuditTrailService) -> AgentRunContext:
    trace = uuid.uuid4()
    return AgentRunContext(
        trace_id=trace,
        correlation_id=str(trace),
        audit_service=audit_service,
    )


# ── Agent Fixtures ────────────────────────────────────────────────────────────

@pytest.fixture
def resolution_worker_mock() -> ResolutionCommunicator:
    """ResolutionCommunicator with webhook dispatch mocked out."""
    worker = ResolutionCommunicator(slack_enabled=False, teams_enabled=False)
    worker._dispatch_slack = AsyncMock(return_value=None)  # type: ignore
    worker._dispatch_teams = AsyncMock(return_value=None)  # type: ignore
    return worker


@pytest.fixture
def policy_worker(mock_mcp_client: AsyncMock) -> PolicyEvaluatorWorker:
    return PolicyEvaluatorWorker(
        mcp_client=mock_mcp_client,
        local_ruleset=STANDARD_RULESET,
    )


@pytest.fixture
def supervisor(
    policy_worker: PolicyEvaluatorWorker,
    resolution_worker_mock: ResolutionCommunicator,
) -> ExpenseAuditorAgent:
    return ExpenseAuditorAgent(
        policy_worker=policy_worker,
        resolution_worker=resolution_worker_mock,
    )


# ── Payload Factories ─────────────────────────────────────────────────────────

def make_payload(
    department: str = "Engineering",
    amount: float = 42.50,
    employee_id: str = "ENG-001",
    session_key: str | None = None,
    category: ExpenseCategory = ExpenseCategory.MEALS,
    **kwargs,
) -> ExpensePayload:
    return ExpensePayload(
        department=department,
        amount=Decimal(str(amount)),
        employee_id=employee_id,
        employee_email=f"{employee_id.lower()}@nexusscale.io",
        session_key=session_key or make_session_key(employee_id),
        category=category,
        **kwargs,
    )
