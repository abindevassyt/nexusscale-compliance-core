"""
mcp/server.py
─────────────
Local stub MCP server for development and testing.
Implements the JSON-RPC 2.0 MCP protocol surface using FastAPI.

Run standalone:
    uvicorn mcp.server:app --port 9000 --log-level info

This stub server reads policy data from config/policy_rules.json and
simulates the enterprise database bridge without any live credentials.
"""

from __future__ import annotations

import json
import logging
import os
import time
import uuid
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

logger = logging.getLogger("mcp.stub_server")

# ─────────────────────────────────────────────────────────────────────────────
# Load policy data at startup
# ─────────────────────────────────────────────────────────────────────────────

_POLICY_PATH = Path(os.environ.get("POLICY_RULES_PATH", "config/policy_rules.json"))

def _load_policy_rules() -> dict:
    if _POLICY_PATH.exists():
        with open(_POLICY_PATH) as f:
            return json.load(f)
    return {"default_limit_usd": 50.00, "rules": []}

_POLICY_DATA = _load_policy_rules()

# In-memory audit event log (stub only)
_AUDIT_LOG: list[dict] = []

# Stub employee profiles
_EMPLOYEE_PROFILES = {
    "ENG-001": {"name": "Alice Chen",    "email": "alice@nexusscale.io",  "slack_id": "U01ALICE",  "department": "Engineering"},
    "MKT-002": {"name": "Bob Martinez",  "email": "bob@nexusscale.io",    "slack_id": "U02BOB",    "department": "Marketing"},
    "FIN-003": {"name": "Carol Singh",   "email": "carol@nexusscale.io",  "slack_id": "U03CAROL",  "department": "Finance"},
    "UNKNOWN": {"name": "Unknown User",  "email": "",                      "slack_id": "",          "department": "Unknown"},
}

# ─────────────────────────────────────────────────────────────────────────────
# FastAPI App
# ─────────────────────────────────────────────────────────────────────────────

app = FastAPI(title="NexusScale MCP Stub Server", version="1.0.0")


@app.get("/health")
async def health() -> JSONResponse:
    return JSONResponse({"status": "ok", "timestamp": datetime.now(timezone.utc).isoformat()})


@app.post("/rpc")
async def rpc_dispatch(request: Request) -> JSONResponse:
    """Main JSON-RPC 2.0 dispatcher."""
    body: dict = await request.json()
    rpc_id = body.get("id", str(uuid.uuid4()))
    method = body.get("method")
    params = body.get("params", {})

    if method != "tools/call":
        return _rpc_error(rpc_id, -32601, f"Method not found: {method}")

    tool_name = params.get("name")
    arguments = params.get("arguments", {})

    logger.info(f"[MCP Stub] tool={tool_name} args={arguments}")

    # Simulate slow responses / failures for testing
    simulate = os.environ.get("MCP_STUB_SIMULATE", "")
    if simulate == "timeout":
        await _async_sleep(60)  # will be killed by client timeout
    elif simulate == "disconnect":
        return JSONResponse(status_code=503, content={"error": "Service Unavailable"})

    # Dispatch
    handlers = {
        "fetch_corporate_policy": _handle_fetch_corporate_policy,
        "write_audit_event":      _handle_write_audit_event,
        "fetch_employee_profile": _handle_fetch_employee_profile,
    }

    handler = handlers.get(tool_name)
    if handler is None:
        return _rpc_error(rpc_id, -32602, f"Unknown tool: {tool_name}")

    try:
        result = await handler(arguments)
        return JSONResponse({"jsonrpc": "2.0", "id": rpc_id, "result": result})
    except Exception as exc:
        logger.exception(f"[MCP Stub] Error in tool {tool_name}")
        return _rpc_error(rpc_id, -32000, str(exc))


# ─────────────────────────────────────────────────────────────────────────────
# Tool Handlers
# ─────────────────────────────────────────────────────────────────────────────

async def _handle_fetch_corporate_policy(args: dict) -> dict[str, Any]:
    """Resolve the applicable policy rule for the given department + category."""
    department = args.get("department", "").strip().title()
    category = args.get("category", "*").strip().lower()

    rules = _POLICY_DATA.get("rules", [])
    default_limit = float(_POLICY_DATA.get("default_limit_usd", 50.00))

    # Priority 1: exact dept + category
    for rule in rules:
        if rule["department"].title() == department and rule.get("category", "*") == category:
            return _format_rule(rule)

    # Priority 2: dept wildcard
    for rule in rules:
        if rule["department"].title() == department and rule.get("category", "*") == "*":
            return _format_rule(rule)

    # Priority 3: global default
    return {
        "department": department,
        "category": "*",
        "limit_usd": default_limit,
        "limit_cents": int(default_limit * 100),
        "escalation_threshold_usd": default_limit * 4,
        "currency": "USD",
        "description": "Global default policy (no specific rule matched)",
        "matched_rule": "DEFAULT",
    }


def _format_rule(rule: dict) -> dict[str, Any]:
    limit = float(rule["limit_usd"])
    escalation = float(rule.get("escalation_threshold_usd", limit * 4))
    return {
        "department": rule["department"],
        "category": rule.get("category", "*"),
        "limit_usd": limit,
        "limit_cents": int(limit * 100),
        "escalation_threshold_usd": escalation,
        "currency": rule.get("currency", "USD"),
        "description": rule.get("description", ""),
        "matched_rule": "EXPLICIT",
    }


async def _handle_write_audit_event(args: dict) -> dict[str, Any]:
    """Persist an audit event to the in-memory log (stub)."""
    event = {**args, "server_timestamp": datetime.now(timezone.utc).isoformat()}
    _AUDIT_LOG.append(event)
    return {"written": True, "log_index": len(_AUDIT_LOG)}


async def _handle_fetch_employee_profile(args: dict) -> dict[str, Any]:
    """Return employee profile by ID."""
    emp_id = args.get("employee_id", "UNKNOWN")
    profile = _EMPLOYEE_PROFILES.get(emp_id, _EMPLOYEE_PROFILES["UNKNOWN"])
    return {**profile, "employee_id": emp_id}


# ─────────────────────────────────────────────────────────────────────────────
# Utilities
# ─────────────────────────────────────────────────────────────────────────────

def _rpc_error(rpc_id: str, code: int, message: str) -> JSONResponse:
    return JSONResponse({
        "jsonrpc": "2.0",
        "id": rpc_id,
        "error": {"code": code, "message": message},
    })


async def _async_sleep(seconds: float) -> None:
    import asyncio
    await asyncio.sleep(seconds)


@app.get("/audit-log")
async def get_audit_log() -> JSONResponse:
    """Debug endpoint — returns all stub audit events."""
    return JSONResponse({"count": len(_AUDIT_LOG), "events": _AUDIT_LOG})
