"""
mcp/tools.py
────────────
Typed MCP tool binding layer.

Each function wraps a raw MCPClient.call_tool() invocation with:
  • Typed input/output conversion
  • Structured logging
  • Domain exception mapping
"""

from __future__ import annotations

import logging
from decimal import Decimal
from typing import TYPE_CHECKING

from core.exceptions import MCPError, PolicyLoadError
from core.models import PolicyLimit

if TYPE_CHECKING:
    from mcp.client import MCPClient

logger = logging.getLogger("mcp.tools")


async def fetch_corporate_policy(
    client: "MCPClient",
    department: str,
    category: str = "*",
    correlation_id: str = "",
) -> PolicyLimit:
    """
    Typed binding for the `fetch_corporate_policy` MCP tool.

    Args:
        client: Connected MCPClient instance.
        department: The department name to look up.
        category: Expense category; defaults to wildcard "*".
        correlation_id: Trace ID for logging correlation.

    Returns:
        A fully populated PolicyLimit domain model.

    Raises:
        PolicyLoadError: If the MCP response is malformed.
        MCPError / MCPTimeoutError / MCPCircuitOpenError: On transport failures.
    """
    logger.info(
        "Fetching corporate policy",
        extra={
            "tool": "fetch_corporate_policy",
            "department": department,
            "category": category,
            "correlation_id": correlation_id,
        },
    )

    raw = await client.call_tool(
        "fetch_corporate_policy",
        {"department": department, "category": category},
        correlation_id=correlation_id,
    )

    try:
        # Extract the inner content list if wrapped by MCP SDK envelope
        content = raw.get("content", [raw])
        if isinstance(content, list) and content:
            data = content[0].get("text", raw) if isinstance(content[0], dict) else raw
            if isinstance(data, str):
                import json
                data = json.loads(data)
        else:
            data = raw

        policy = PolicyLimit(
            department=data.get("department", department),
            category=data.get("category", "*"),
            limit_usd=Decimal(str(data.get("limit_usd", 50.00))),
            escalation_threshold_usd=(
                Decimal(str(data["escalation_threshold_usd"]))
                if data.get("escalation_threshold_usd") is not None
                else None
            ),
            currency=data.get("currency", "USD"),
            description=data.get("description", ""),
        )

        logger.info(
            "Policy fetched",
            extra={
                "department": department,
                "limit_usd": float(policy.limit_usd),
                "matched_rule": data.get("matched_rule", "UNKNOWN"),
                "correlation_id": correlation_id,
            },
        )
        return policy

    except (KeyError, ValueError, TypeError) as exc:
        raise PolicyLoadError(
            message=f"Failed to parse MCP policy response: {exc}",
            correlation_id=correlation_id,
            context={"raw_response": raw},
        ) from exc


async def write_audit_event_remote(
    client: "MCPClient",
    trace_id: str,
    event_type: str,
    agent_name: str,
    outcome: str,
    payload: dict | None = None,
    correlation_id: str = "",
) -> bool:
    """
    Write a compliance audit event to the enterprise audit log via MCP.
    Returns True on success; logs and returns False on MCP failure (non-crashing).
    """
    try:
        await client.call_tool(
            "write_audit_event",
            {
                "trace_id": trace_id,
                "event_type": event_type,
                "agent_name": agent_name,
                "outcome": outcome,
                "payload": payload or {},
            },
            correlation_id=correlation_id,
        )
        return True
    except MCPError as exc:
        logger.warning(
            "Remote audit event write failed (non-fatal)",
            extra={"error": exc.error_code, "correlation_id": correlation_id},
        )
        return False


async def fetch_employee_profile(
    client: "MCPClient",
    employee_id: str,
    correlation_id: str = "",
) -> dict:
    """
    Retrieve employee details for notification routing.
    Returns a safe fallback dict on any MCP error.
    """
    try:
        raw = await client.call_tool(
            "fetch_employee_profile",
            {"employee_id": employee_id},
            correlation_id=correlation_id,
        )
        content = raw.get("content", [raw])
        if isinstance(content, list) and content:
            data = content[0].get("text", raw) if isinstance(content[0], dict) else raw
            if isinstance(data, str):
                import json
                data = json.loads(data)
        else:
            data = raw
        return data
    except MCPError:
        return {"employee_id": employee_id, "email": "", "slack_id": "", "name": "Unknown"}
