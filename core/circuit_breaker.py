"""
core/circuit_breaker.py
───────────────────────
Thread-safe, async-compatible circuit breaker for MCP bridge calls.

States:
  CLOSED   → Normal operation. Failures are counted.
  OPEN     → Fast-fail mode. All calls raise MCPCircuitOpenError immediately.
  HALF_OPEN → Probe mode. One trial call is allowed; success → CLOSED, failure → OPEN.

Configuration is driven by environment variables (see .env.example).
"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Awaitable, Callable, TypeVar

from core.exceptions import MCPCircuitOpenError, MCPError
from core.models import CircuitState

logger = logging.getLogger(__name__)

T = TypeVar("T")


# ─────────────────────────────────────────────────────────────────────────────
# Circuit Breaker
# ─────────────────────────────────────────────────────────────────────────────

@dataclass
class CircuitBreaker:
    """
    Async circuit breaker protecting the MCP JSON-RPC bridge.

    Usage:
        cb = CircuitBreaker(name="mcp-bridge", failure_threshold=5)
        result = await cb.call(some_async_function, arg1, arg2)
    """

    name: str
    failure_threshold: int = 5
    recovery_timeout_seconds: float = 30.0
    success_threshold_in_half_open: int = 2  # successes needed to close again

    # Internal state (not part of constructor kwargs for external use)
    _state: CircuitState = field(default=CircuitState.CLOSED, init=False, repr=False)
    _failure_count: int = field(default=0, init=False, repr=False)
    _success_count_in_half_open: int = field(default=0, init=False, repr=False)
    _last_failure_time: float = field(default=0.0, init=False, repr=False)
    _lock: asyncio.Lock = field(init=False, repr=False)

    def __post_init__(self) -> None:
        self._lock = asyncio.Lock()

    # ── Public API ────────────────────────────────────────────────────────────

    @property
    def state(self) -> CircuitState:
        return self._state

    @property
    def failure_count(self) -> int:
        return self._failure_count

    async def call(
        self,
        fn: Callable[..., Awaitable[T]],
        *args: object,
        **kwargs: object,
    ) -> T:
        """
        Execute `fn(*args, **kwargs)` through the circuit.
        Raises MCPCircuitOpenError when the circuit is OPEN.
        """
        await self._maybe_transition_to_half_open()

        if self._state == CircuitState.OPEN:
            retry_after = max(
                0,
                int(self.recovery_timeout_seconds - (time.monotonic() - self._last_failure_time)),
            )
            logger.warning(
                "⚡ Circuit OPEN — fast-failing MCP call",
                extra={
                    "circuit": self.name,
                    "state": self._state,
                    "retry_after_seconds": retry_after,
                },
            )
            raise MCPCircuitOpenError(
                message=f"Circuit breaker '{self.name}' is OPEN — MCP bridge is unavailable.",
                retry_after_seconds=retry_after,
            )

        try:
            result = await fn(*args, **kwargs)
            await self._on_success()
            return result
        except MCPError as exc:
            await self._on_failure(exc)
            raise

    async def reset(self) -> None:
        """Manually force circuit CLOSED (for testing or admin actions)."""
        async with self._lock:
            self._state = CircuitState.CLOSED
            self._failure_count = 0
            self._success_count_in_half_open = 0
            logger.info(
                "🔄 Circuit manually reset to CLOSED",
                extra={"circuit": self.name},
            )

    def snapshot(self) -> dict:
        """Return a JSON-serialisable health snapshot."""
        return {
            "circuit": self.name,
            "state": self._state.value,
            "failure_count": self._failure_count,
            "failure_threshold": self.failure_threshold,
            "recovery_timeout_seconds": self.recovery_timeout_seconds,
            "last_failure_at": (
                datetime.fromtimestamp(self._last_failure_time, tz=timezone.utc).isoformat()
                if self._last_failure_time
                else None
            ),
        }

    # ── Internal Transition Logic ─────────────────────────────────────────────

    async def _maybe_transition_to_half_open(self) -> None:
        """Move from OPEN → HALF_OPEN if the recovery timeout has elapsed."""
        if self._state == CircuitState.OPEN:
            async with self._lock:
                elapsed = time.monotonic() - self._last_failure_time
                if elapsed >= self.recovery_timeout_seconds:
                    self._state = CircuitState.HALF_OPEN
                    self._success_count_in_half_open = 0
                    logger.info(
                        "🟡 Circuit transitioning OPEN → HALF_OPEN",
                        extra={"circuit": self.name, "elapsed_seconds": elapsed},
                    )

    async def _on_success(self) -> None:
        async with self._lock:
            if self._state == CircuitState.HALF_OPEN:
                self._success_count_in_half_open += 1
                if self._success_count_in_half_open >= self.success_threshold_in_half_open:
                    self._state = CircuitState.CLOSED
                    self._failure_count = 0
                    logger.info(
                        "✅ Circuit transitioning HALF_OPEN → CLOSED",
                        extra={"circuit": self.name},
                    )
            elif self._state == CircuitState.CLOSED:
                # Decay failure count on consecutive successes
                if self._failure_count > 0:
                    self._failure_count = max(0, self._failure_count - 1)

    async def _on_failure(self, exc: MCPError) -> None:
        async with self._lock:
            self._failure_count += 1
            self._last_failure_time = time.monotonic()

            logger.warning(
                "⚠️  MCP call failure recorded",
                extra={
                    "circuit": self.name,
                    "failure_count": self._failure_count,
                    "threshold": self.failure_threshold,
                    "error": exc.error_code,
                },
            )

            if self._failure_count >= self.failure_threshold:
                if self._state != CircuitState.OPEN:
                    self._state = CircuitState.OPEN
                    logger.error(
                        "🔴 Circuit transitioning → OPEN (threshold breached)",
                        extra={
                            "circuit": self.name,
                            "failure_count": self._failure_count,
                        },
                    )
