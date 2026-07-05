"""
mcp/client.py
─────────────
Async JSON-RPC 2.0 MCP client for the NexusScale enterprise database bridge.

Features:
  • HTTP transport using httpx async client with connection pooling
  • Automatic retry with exponential backoff (via tenacity)
  • Circuit breaker integration
  • Structured request/response logging with redaction of sensitive fields
  • Graceful 503 fallback on timeout / disconnect
"""

from __future__ import annotations

import json
import logging
import os
import time
import uuid
from typing import Any

import httpx
from tenacity import (
    AsyncRetrying,
    retry_if_exception_type,
    stop_after_attempt,
    wait_exponential,
)

from core.circuit_breaker import CircuitBreaker
from core.exceptions import MCPDisconnectError, MCPError, MCPTimeoutError

logger = logging.getLogger("mcp.client")

# Redact these keys from request/response logs
_REDACTED_FIELDS = {"session_key", "employee_email", "password", "secret"}


def _redact(obj: dict) -> dict:
    """Return a shallow copy of `obj` with sensitive keys replaced."""
    return {k: ("***REDACTED***" if k in _REDACTED_FIELDS else v) for k, v in obj.items()}


# ─────────────────────────────────────────────────────────────────────────────
# MCP Client
# ─────────────────────────────────────────────────────────────────────────────

class MCPClient:
    """
    Async MCP JSON-RPC 2.0 client.

    Lifecycle:
        client = MCPClient.from_config()
        await client.connect()
        result = await client.call_tool("fetch_corporate_policy", {"department": "Engineering"})
        await client.disconnect()
    """

    def __init__(
        self,
        base_url: str,
        timeout_seconds: float = 10.0,
        max_retries: int = 3,
        circuit_breaker: CircuitBreaker | None = None,
    ) -> None:
        self._base_url = base_url.rstrip("/")
        self._timeout = httpx.Timeout(timeout_seconds)
        self._max_retries = max_retries
        self._cb = circuit_breaker or CircuitBreaker(
            name="mcp-bridge",
            failure_threshold=int(os.environ.get("MCP_CIRCUIT_BREAKER_THRESHOLD", 5)),
            recovery_timeout_seconds=float(
                os.environ.get("MCP_CIRCUIT_BREAKER_RECOVERY_SECONDS", 30)
            ),
        )
        self._http: httpx.AsyncClient | None = None

    # ── Factory ───────────────────────────────────────────────────────────────

    @classmethod
    def from_config(cls, config_path: str = "config/mcp_config.json") -> "MCPClient":
        """Instantiate from the JSON configuration manifest."""
        with open(config_path) as f:
            cfg = json.load(f)

        server = cfg["server"]["primary"]
        cb_cfg = cfg.get("circuit_breaker", {})
        pool_cfg = cfg.get("connection_pool", {})

        # Override with env vars if set
        url = os.environ.get("MCP_SERVER_URL", server["url"])
        timeout = float(os.environ.get("MCP_TIMEOUT_SECONDS", server["timeout_seconds"]))
        retries = int(os.environ.get("MCP_MAX_RETRIES", server.get("max_retries", 3)))

        cb = CircuitBreaker(
            name="mcp-bridge",
            failure_threshold=int(os.environ.get("MCP_CIRCUIT_BREAKER_THRESHOLD", cb_cfg.get("failure_threshold", 5))),
            recovery_timeout_seconds=float(
                os.environ.get("MCP_CIRCUIT_BREAKER_RECOVERY_SECONDS", cb_cfg.get("recovery_timeout_seconds", 30))
            ),
            success_threshold_in_half_open=int(cb_cfg.get("success_threshold_in_half_open", 2)),
        )

        instance = cls(base_url=url, timeout_seconds=timeout, max_retries=retries, circuit_breaker=cb)

        limits = httpx.Limits(
            max_connections=pool_cfg.get("max_connections", 10),
            max_keepalive_connections=pool_cfg.get("max_keepalive_connections", 5),
            keepalive_expiry=pool_cfg.get("keepalive_expiry_seconds", 30),
        )
        instance._http = httpx.AsyncClient(limits=limits, timeout=instance._timeout)
        return instance

    # ── Connection Lifecycle ──────────────────────────────────────────────────

    async def connect(self) -> None:
        """Initialize the underlying HTTP client if not already done."""
        if self._http is None:
            self._http = httpx.AsyncClient(timeout=self._timeout)
        logger.info("MCP client connected", extra={"url": self._base_url})

    async def disconnect(self) -> None:
        """Close the HTTP connection pool."""
        if self._http is not None:
            await self._http.aclose()
            self._http = None
        logger.info("MCP client disconnected")

    # ── Core Tool Call ────────────────────────────────────────────────────────

    async def call_tool(
        self,
        tool_name: str,
        arguments: dict[str, Any],
        correlation_id: str | None = None,
    ) -> dict[str, Any]:
        """
        Execute a JSON-RPC 2.0 tool call against the MCP bridge.
        Wraps the raw call with circuit breaker protection and retries.
        """
        return await self._cb.call(
            self._call_with_retry,
            tool_name,
            arguments,
            correlation_id or str(uuid.uuid4()),
        )

    async def _call_with_retry(
        self,
        tool_name: str,
        arguments: dict[str, Any],
        correlation_id: str,
    ) -> dict[str, Any]:
        """Inner retry loop using tenacity exponential backoff."""
        async for attempt in AsyncRetrying(
            stop=stop_after_attempt(self._max_retries),
            wait=wait_exponential(multiplier=1, min=1, max=8),
            retry=retry_if_exception_type(MCPTimeoutError),
            reraise=True,
        ):
            with attempt:
                return await self._raw_call(tool_name, arguments, correlation_id)
        raise MCPDisconnectError(  # unreachable but satisfies type checker
            message="All retry attempts exhausted",
            correlation_id=correlation_id,
        )

    async def _raw_call(
        self,
        tool_name: str,
        arguments: dict[str, Any],
        correlation_id: str,
    ) -> dict[str, Any]:
        """Build, dispatch, and parse a single JSON-RPC 2.0 request."""
        rpc_id = str(uuid.uuid4())
        body = {
            "jsonrpc": "2.0",
            "method": "tools/call",
            "id": rpc_id,
            "params": {
                "name": tool_name,
                "arguments": arguments,
            },
        }

        logger.info(
            "→ MCP request",
            extra={
                "rpc_id": rpc_id,
                "tool": tool_name,
                "args": _redact(arguments),
                "correlation_id": correlation_id,
            },
        )

        t0 = time.monotonic()
        try:
            assert self._http is not None, "MCPClient not connected — call await client.connect() first"
            response = await self._http.post(
                f"{self._base_url}/rpc",
                json=body,
                headers={
                    "Content-Type": "application/json",
                    "X-Correlation-ID": correlation_id,
                    "X-NexusScale-Client": "nexusscale-compliance/1.0",
                },
            )
            latency_ms = (time.monotonic() - t0) * 1000
            response.raise_for_status()

        except httpx.TimeoutException as exc:
            latency_ms = (time.monotonic() - t0) * 1000
            timeout_s = self._timeout.read or 10.0
            logger.error(
                "✗ MCP timeout",
                extra={
                    "tool": tool_name,
                    "latency_ms": latency_ms,
                    "correlation_id": correlation_id,
                },
            )
            raise MCPTimeoutError(
                message=f"MCP bridge timed out after {timeout_s}s for tool '{tool_name}'",
                correlation_id=correlation_id,
                timeout_seconds=timeout_s,
                context={"tool": tool_name},
            ) from exc

        except (httpx.ConnectError, httpx.RemoteProtocolError) as exc:
            latency_ms = (time.monotonic() - t0) * 1000
            logger.error(
                "✗ MCP connection error",
                extra={
                    "tool": tool_name,
                    "error": str(exc),
                    "correlation_id": correlation_id,
                },
            )
            raise MCPDisconnectError(
                message=f"Lost connection to MCP bridge: {exc}",
                correlation_id=correlation_id,
                context={"tool": tool_name, "url": self._base_url},
            ) from exc

        data = response.json()
        latency_ms = (time.monotonic() - t0) * 1000

        if "error" in data:
            err = data["error"]
            raise MCPError(
                message=f"MCP bridge returned RPC error: {err.get('message', 'unknown')}",
                error_code=f"MCP_RPC_{err.get('code', 'UNKNOWN')}",
                correlation_id=correlation_id,
                context={"rpc_error": err},
            )

        result = data.get("result", {})
        logger.info(
            "← MCP response",
            extra={
                "rpc_id": rpc_id,
                "tool": tool_name,
                "latency_ms": round(latency_ms, 2),
                "correlation_id": correlation_id,
            },
        )
        return result

    # ── Health ────────────────────────────────────────────────────────────────

    async def health_check(self) -> dict[str, Any]:
        """Ping the MCP bridge health endpoint."""
        try:
            assert self._http is not None
            resp = await self._http.get(f"{self._base_url}/health", timeout=5.0)
            return {"healthy": resp.status_code == 200, "status_code": resp.status_code}
        except Exception as exc:
            return {"healthy": False, "error": str(exc)}

    @property
    def circuit_state(self) -> dict[str, Any]:
        return self._cb.snapshot()
