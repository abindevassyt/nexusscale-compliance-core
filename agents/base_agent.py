"""
agents/base_agent.py
─────────────────────
Abstract base class for all NexusScale compliance agents.

Provides a structured lifecycle with instrumented hooks:
  before_run  → pre-execution setup / validation
  run         → core business logic (abstract)
  after_run   → post-execution cleanup / telemetry
  on_error    → structured error handling

Every concrete agent registers itself with the AgentRegistry
so the orchestrator can discover and invoke agents by name.
"""

from __future__ import annotations

import abc
import logging
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, ClassVar
from uuid import UUID

from core.audit_trail import AuditTrailService
from core.exceptions import AgentInitializationError, ComplianceEngineError
from core.models import AgentRole, AuditEvent, AuditEventType

logger = logging.getLogger("agents")


# ─────────────────────────────────────────────────────────────────────────────
# Agent Registry (Singleton)
# ─────────────────────────────────────────────────────────────────────────────

class AgentRegistry:
    """Global registry of all instantiated agents."""

    _registry: ClassVar[dict[str, "BaseAgent"]] = {}

    @classmethod
    def register(cls, agent: "BaseAgent") -> None:
        cls._registry[agent.name] = agent
        logger.debug(f"Agent registered: {agent.name}")

    @classmethod
    def get(cls, name: str) -> "BaseAgent | None":
        return cls._registry.get(name)

    @classmethod
    def all(cls) -> dict[str, "BaseAgent"]:
        return dict(cls._registry)


# ─────────────────────────────────────────────────────────────────────────────
# Execution Context
# ─────────────────────────────────────────────────────────────────────────────

@dataclass
class AgentRunContext:
    """Carries correlation data and shared state through a single pipeline run."""

    trace_id: UUID
    correlation_id: str
    audit_service: AuditTrailService | None = None
    metadata: dict[str, Any] = field(default_factory=dict)
    start_time: float = field(default_factory=time.monotonic)

    @property
    def elapsed_ms(self) -> float:
        return (time.monotonic() - self.start_time) * 1000


# ─────────────────────────────────────────────────────────────────────────────
# Base Agent
# ─────────────────────────────────────────────────────────────────────────────

class BaseAgent(abc.ABC):
    """
    Abstract base for all compliance pipeline agents.

    Concrete agents must implement:
      - `run(payload, context)` → the core business logic
      - `_validate_inputs(payload, context)` → guard pre-run preconditions
    """

    # Subclasses declare these as class-level attributes
    name: str = "base_agent"
    persona: str = "Generic Compliance Agent"
    role: AgentRole = AgentRole.WORKER
    version: str = "1.0.0"

    def __init__(self) -> None:
        self._logger = logging.getLogger(f"agents.{self.name}")
        self._run_count: int = 0
        self._error_count: int = 0
        self._total_latency_ms: float = 0.0
        AgentRegistry.register(self)
        self._logger.info(
            "Agent initialized",
            extra={
                "agent": self.name,
                "role": self.role,
                "persona": self.persona,
                "version": self.version,
            },
        )

    # ── Public Lifecycle ──────────────────────────────────────────────────────

    async def execute(
        self,
        payload: Any,
        context: AgentRunContext,
    ) -> Any:
        """
        Full agent lifecycle:
          1. before_run hook
          2. _validate_inputs guard
          3. run (abstract — core logic)
          4. after_run hook
          5. on_error hook (if exception)

        Returns the result of `run()`.
        """
        self._run_count += 1
        t0 = time.monotonic()

        self._logger.info(
            "▶ Agent starting",
            extra={
                "agent": self.name,
                "trace_id": str(context.trace_id),
                "run_count": self._run_count,
            },
        )

        try:
            await self.before_run(payload, context)
            self._validate_inputs(payload, context)
            result = await self.run(payload, context)
            await self.after_run(result, context)

            latency = (time.monotonic() - t0) * 1000
            self._total_latency_ms += latency

            self._logger.info(
                "✅ Agent completed",
                extra={
                    "agent": self.name,
                    "trace_id": str(context.trace_id),
                    "latency_ms": round(latency, 2),
                },
            )
            return result

        except ComplianceEngineError:
            # Domain exceptions propagate unmodified
            self._error_count += 1
            await self.on_error(payload, context)
            raise

        except Exception as exc:
            # Wrap unexpected exceptions
            self._error_count += 1
            await self.on_error(payload, context)
            self._logger.exception(
                "💥 Unexpected agent error",
                extra={"agent": self.name, "trace_id": str(context.trace_id)},
            )
            raise ComplianceEngineError(
                message=f"Agent '{self.name}' encountered an unexpected error: {exc}",
                context={"agent": self.name, "error_type": type(exc).__name__},
            ) from exc

    # ── Abstract Methods ──────────────────────────────────────────────────────

    @abc.abstractmethod
    async def run(self, payload: Any, context: AgentRunContext) -> Any:
        """Core agent business logic — must be implemented by subclasses."""
        ...

    @abc.abstractmethod
    def _validate_inputs(self, payload: Any, context: AgentRunContext) -> None:
        """Guard clause — raise ComplianceEngineError if preconditions fail."""
        ...

    # ── Optional Lifecycle Hooks ──────────────────────────────────────────────

    async def before_run(self, payload: Any, context: AgentRunContext) -> None:
        """Pre-execution hook — override for setup logic."""
        pass

    async def after_run(self, result: Any, context: AgentRunContext) -> None:
        """Post-execution hook — override for cleanup / telemetry."""
        pass

    async def on_error(self, payload: Any, context: AgentRunContext) -> None:
        """Error hook — override for custom error handling."""
        pass

    # ── Audit Helper ──────────────────────────────────────────────────────────

    async def _emit_audit(
        self,
        context: AgentRunContext,
        event_type: AuditEventType,
        outcome: str,
        payload_snapshot: dict | None = None,
        error_detail: str | None = None,
        duration_ms: float = 0.0,
    ) -> None:
        """Write an audit event to the trail service if available."""
        if context.audit_service is None:
            return
        event = AuditEvent(
            trace_id=context.trace_id,
            event_type=event_type,
            agent_name=self.name,
            payload_snapshot=payload_snapshot or {},
            outcome=outcome,
            error_detail=error_detail,
            duration_ms=duration_ms,
        )
        await context.audit_service.record(event)

    # ── Metrics ───────────────────────────────────────────────────────────────

    @property
    def metrics(self) -> dict[str, Any]:
        """Return a JSON-serialisable snapshot of this agent's metrics."""
        return {
            "agent": self.name,
            "run_count": self._run_count,
            "error_count": self._error_count,
            "avg_latency_ms": (
                round(self._total_latency_ms / self._run_count, 2)
                if self._run_count > 0
                else 0.0
            ),
        }
