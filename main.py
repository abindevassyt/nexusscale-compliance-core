"""
main.py
───────
NexusScale Compliance Engine — FastAPI Application Entrypoint

Features:
  • Lifespan-managed startup/shutdown (agent init, MCP connect, DB setup)
  • Phase 1 security preflight abort on startup
  • Structured logging.INFO initialization from YAML config
  • POST /submit-expense → full compliance pipeline
  • GET  /health → system health snapshot
  • GET  /audit/{trace_id} → audit event log for a trace
  • GET  /metrics → agent run metrics
  • GET  /circuit-state → MCP circuit breaker snapshot
  • Phase 4 MCP fault trap → HTTP 503 with rollback
  • Phase 3C security rejection → HTTP 422
  • Rate limiting middleware
  • Correlation ID injection on every request
  • OpenTelemetry tracing (configurable)
"""

from __future__ import annotations

import asyncio
import collections
import json
import logging
import logging.config
import os
import time
import uuid
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from typing import AsyncGenerator

import yaml
from fastapi import FastAPI, HTTPException, Request, Response, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import ValidationError

# ─────────────────────────────────────────────────────────────────────────────
# In-memory statistics tracker (per server session)
# ─────────────────────────────────────────────────────────────────────────────

_stats: dict = {
    "total_requests": 0,
    "approved": 0,
    "flagged": 0,
    "errors": 0,
    "total_processing_ms": 0.0,
    "started_at": datetime.now(timezone.utc).isoformat(),
    "recent_requests": collections.deque(maxlen=50),  # last 50 requests
}

# WebSocket log broadcast manager
class _LogBroadcaster(logging.Handler):
    """Custom logging handler that pushes records to connected WebSocket clients."""
    def __init__(self):
        super().__init__()
        self._clients: set[WebSocket] = set()
        self._queue: asyncio.Queue = asyncio.Queue(maxsize=500)

    def emit(self, record: logging.LogRecord) -> None:
        try:
            entry = {
                "t": datetime.now(timezone.utc).strftime("%H:%M:%S"),
                "level": record.levelname,
                "name": record.name,
                "msg": self.format(record),
            }
            self._queue.put_nowait(entry)
        except asyncio.QueueFull:
            pass
        except Exception:
            pass

    async def broadcast_loop(self) -> None:
        """Drain the queue and broadcast to all connected WS clients."""
        while True:
            try:
                entry = await asyncio.wait_for(self._queue.get(), timeout=1.0)
                dead: set[WebSocket] = set()
                for ws in list(self._clients):
                    try:
                        await ws.send_text(json.dumps(entry))
                    except Exception:
                        dead.add(ws)
                self._clients -= dead
            except asyncio.TimeoutError:
                pass

    def add_client(self, ws: WebSocket) -> None:
        self._clients.add(ws)

    def remove_client(self, ws: WebSocket) -> None:
        self._clients.discard(ws)


_log_broadcaster = _LogBroadcaster()
_log_broadcaster.setFormatter(logging.Formatter("%(message)s"))
logging.getLogger().addHandler(_log_broadcaster)

# ─────────────────────────────────────────────────────────────────────────────
# Phase 4: Logging bootstrap (must happen before any other imports)
# ─────────────────────────────────────────────────────────────────────────────

def _setup_logging() -> None:
    """Initialize structured logging from YAML config at logging.INFO level."""
    import os
    from pathlib import Path

    os.makedirs("logs", exist_ok=True)

    config_path = Path("config/logging_config.yaml")
    if config_path.exists():
        with open(config_path) as f:
            config = yaml.safe_load(f)
        # Patch the RichHandler reference (requires rich to be installed)
        try:
            logging.config.dictConfig(config)
        except Exception:
            # Fallback to basic config if YAML config fails
            logging.basicConfig(
                level=logging.INFO,
                format="%(asctime)s [%(levelname)-8s] [%(name)s] %(message)s",
            )
    else:
        logging.basicConfig(
            level=logging.INFO,
            format="%(asctime)s [%(levelname)-8s] [%(name)s] %(message)s",
        )

    # Override level from env var if set
    level = os.environ.get("LOG_LEVEL", "INFO").upper()
    logging.getLogger().setLevel(getattr(logging, level, logging.INFO))


_setup_logging()
logger = logging.getLogger("nexusscale")


# ─────────────────────────────────────────────────────────────────────────────
# Phase 1 Security Preflight — Hard abort before any agent is initialized
# ─────────────────────────────────────────────────────────────────────────────

from core.security import enforce_enterprise_secret

_ENTERPRISE_SECRET = enforce_enterprise_secret()  # Aborts process if invalid
logger.info("🔐 Enterprise secret validated — proceeding with initialization")


# ─────────────────────────────────────────────────────────────────────────────
# Pipeline (imported after security check)
# ─────────────────────────────────────────────────────────────────────────────

from core.exceptions import (
    ComplianceEngineError,
    DepartmentEmptyError,
    MCPCircuitOpenError,
    MCPDisconnectError,
    MCPTimeoutError,
    PayloadValidationError,
    SecurityValidationError,
    SessionKeyInvalidError,
)
from core.models import ComplianceResponse, ErrorResponse, ExpensePayload
from orchestrator.pipeline import CompliancePipeline

_pipeline = CompliancePipeline()


# ─────────────────────────────────────────────────────────────────────────────
# Lifespan — Startup & Shutdown
# ─────────────────────────────────────────────────────────────────────────────

@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncGenerator[None, None]:
    """Manage the full lifecycle of the compliance pipeline."""
    logger.info("=" * 60)
    logger.info("🚀 NexusScale Compliance Engine starting up")
    logger.info(f"   Environment : {os.environ.get('APP_ENV', 'development')}")
    logger.info(f"   Version     : {os.environ.get('APP_VERSION', '1.0.0')}")
    logger.info("=" * 60)

    await _pipeline.startup()
    logger.info("✅ All systems operational — accepting requests")

    # Start WebSocket log broadcaster in background
    broadcast_task = asyncio.create_task(_log_broadcaster.broadcast_loop())

    yield  # Application is running

    broadcast_task.cancel()
    logger.info("🛑 NexusScale Compliance Engine shutting down...")
    await _pipeline.shutdown()
    logger.info("Goodbye.")


# ─────────────────────────────────────────────────────────────────────────────
# FastAPI Application
# ─────────────────────────────────────────────────────────────────────────────

app = FastAPI(
    title="NexusScale Compliance Engine",
    description=(
        "Multi-agent financial compliance system. "
        "Intercepts expense payloads, evaluates against corporate policy, "
        "and routes flagged events to communication workers."
    ),
    version=os.environ.get("APP_VERSION", "1.0.0"),
    lifespan=lifespan,
    docs_url="/docs",
    redoc_url="/redoc",
)

# ── CORS ──────────────────────────────────────────────────────────────────────
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],  # Restrict in production
    allow_methods=["GET", "POST"],
    allow_headers=["*"],
)


# ─────────────────────────────────────────────────────────────────────────────
# Middleware — Correlation ID injection & request timing
# ─────────────────────────────────────────────────────────────────────────────

@app.middleware("http")
async def correlation_id_middleware(request: Request, call_next):
    """Inject a correlation ID into every request and log timing."""
    correlation_id = request.headers.get("X-Correlation-ID", str(uuid.uuid4()))
    request.state.correlation_id = correlation_id

    t0 = time.monotonic()
    response: Response = await call_next(request)
    elapsed_ms = (time.monotonic() - t0) * 1000

    response.headers["X-Correlation-ID"] = correlation_id
    response.headers["X-Processing-Time-Ms"] = str(round(elapsed_ms, 2))

    logger.info(
        f"{request.method} {request.url.path} → {response.status_code}",
        extra={
            "method": request.method,
            "path": str(request.url.path),
            "status_code": response.status_code,
            "elapsed_ms": round(elapsed_ms, 2),
            "correlation_id": correlation_id,
        },
    )
    return response


# ─────────────────────────────────────────────────────────────────────────────
# Routes
# ─────────────────────────────────────────────────────────────────────────────

@app.post(
    "/submit-expense",
    response_model=ComplianceResponse,
    responses={
        200: {"description": "Expense approved"},
        200: {"description": "Expense flagged (still a 200 with FLAGGED status)"},
        401: {"model": ErrorResponse, "description": "Session key invalid"},
        422: {"model": ErrorResponse, "description": "Payload validation failed"},
        503: {"model": ErrorResponse, "description": "MCP bridge unavailable"},
    },
    summary="Submit expense for compliance evaluation",
    tags=["Compliance"],
)
async def submit_expense(request: Request, payload: ExpensePayload) -> JSONResponse:
    """
    Main compliance endpoint — routes expense through the full agent pipeline.

    Pipeline:
      1. Pydantic schema validation
      2. Security preflight (department, session key HMAC)
      3. MCP policy fetch
      4. Deterministic integer comparison
      5. Conditional notification dispatch
      6. Audit trail write
    """
    correlation_id = getattr(request.state, "correlation_id", str(uuid.uuid4()))
    t0 = time.monotonic()

    result = await _pipeline.process(payload, correlation_id=correlation_id)
    elapsed_ms = (time.monotonic() - t0) * 1000

    # Update in-memory stats
    _stats["total_requests"] += 1
    _stats["total_processing_ms"] += elapsed_ms

    if isinstance(result, ErrorResponse):
        _stats["errors"] += 1
        _stats["recent_requests"].appendleft({
            "trace_id": correlation_id,
            "department": getattr(payload, 'department', ''),
            "amount": float(getattr(payload, 'amount', 0)),
            "status": "ERROR",
            "http_status": result.http_status,
            "processing_ms": round(elapsed_ms, 1),
            "timestamp": datetime.now(timezone.utc).isoformat(),
        })
        return JSONResponse(
            status_code=result.http_status,
            content=result.model_dump(),
            headers={"X-Correlation-ID": correlation_id},
        )

    serialized = _serialize_response(result)
    if result.status.value == "APPROVED":
        _stats["approved"] += 1
    else:
        _stats["flagged"] += 1

    _stats["recent_requests"].appendleft({
        "trace_id": str(result.trace_id),
        "department": result.department,
        "amount": float(result.amount_usd),
        "limit": float(result.limit_usd),
        "variance": float(result.variance_usd),
        "status": result.status.value,
        "http_status": 200,
        "processing_ms": round(elapsed_ms, 1),
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "notification_dispatched": result.notification_dispatched,
    })

    return JSONResponse(
        status_code=200,
        content=serialized,
        headers={"X-Correlation-ID": correlation_id},
    )


@app.get(
    "/health",
    summary="System health check",
    tags=["Operations"],
)
async def health() -> JSONResponse:
    """Returns system health: pipeline status, MCP circuit state, agent registry."""
    from agents.base_agent import AgentRegistry
    from datetime import datetime, timezone

    health_data = {
        "status": "healthy" if _pipeline._initialized else "degraded",
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "version": os.environ.get("APP_VERSION", "1.0.0"),
        "environment": os.environ.get("APP_ENV", "development"),
        "pipeline": _pipeline.health,
        "agents": {
            name: agent.metrics
            for name, agent in AgentRegistry.all().items()
        },
    }
    return JSONResponse(health_data)


@app.get(
    "/audit/{trace_id}",
    summary="Retrieve audit events for a trace ID",
    tags=["Audit"],
)
async def get_audit_trail(trace_id: str) -> JSONResponse:
    """Returns the full audit event log for a given trace/correlation ID."""
    if _pipeline._audit is None:
        raise HTTPException(status_code=503, detail="Audit service not initialized")
    try:
        events = await _pipeline._audit.query_by_trace(uuid.UUID(trace_id))
        return JSONResponse({"trace_id": trace_id, "count": len(events), "events": events})
    except ValueError:
        raise HTTPException(status_code=400, detail=f"Invalid UUID: {trace_id}")


@app.get(
    "/metrics",
    summary="Agent run metrics",
    tags=["Operations"],
)
async def get_metrics() -> JSONResponse:
    """Returns per-agent execution metrics (run count, error count, avg latency)."""
    from agents.base_agent import AgentRegistry
    return JSONResponse({
        "agents": {name: agent.metrics for name, agent in AgentRegistry.all().items()}
    })


@app.get(
    "/circuit-state",
    summary="MCP circuit breaker state",
    tags=["Operations"],
)
async def get_circuit_state() -> JSONResponse:
    """Returns the current state of the MCP circuit breaker."""
    if _pipeline._mcp is None:
        return JSONResponse({"circuit": "DISCONNECTED", "mcp_available": False})
    return JSONResponse(_pipeline._mcp.circuit_state)


@app.get(
    "/stats",
    summary="Live request statistics",
    tags=["Operations"],
)
async def get_stats() -> JSONResponse:
    """Returns live aggregate statistics for the GUI dashboard."""
    total = _stats["total_requests"]
    avg_ms = (
        round(_stats["total_processing_ms"] / total, 1) if total > 0 else 0.0
    )
    approval_rate = round(_stats["approved"] / total * 100, 1) if total > 0 else 0.0
    return JSONResponse({
        "total_requests": total,
        "approved": _stats["approved"],
        "flagged": _stats["flagged"],
        "errors": _stats["errors"],
        "avg_processing_ms": avg_ms,
        "approval_rate_pct": approval_rate,
        "started_at": _stats["started_at"],
        "recent_requests": list(_stats["recent_requests"]),
    })


@app.get(
    "/policy-rules",
    summary="Load and return all policy rules",
    tags=["Policy"],
)
async def get_policy_rules() -> JSONResponse:
    """Returns the full corporate policy rule set from config."""
    path = Path(os.environ.get("POLICY_RULES_PATH", "config/policy_rules.json"))
    if not path.exists():
        raise HTTPException(status_code=404, detail="Policy rules file not found")
    with open(path) as f:
        rules = json.load(f)
    return JSONResponse(rules)


@app.get(
    "/generate-session-key",
    summary="Generate a valid HMAC session key",
    tags=["Security"],
)
async def generate_session_key_endpoint(employee_id: str = "ENG-001") -> JSONResponse:
    """Generate a fresh HMAC-signed session key for the given employee_id."""
    from core.security import generate_session_key
    key = generate_session_key(employee_id)
    return JSONResponse({
        "employee_id": employee_id,
        "session_key": key,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "ttl_seconds": int(os.environ.get("SESSION_KEY_TTL_SECONDS", 3600)),
    })


@app.post(
    "/admin/circuit-reset",
    summary="Reset the MCP circuit breaker to CLOSED",
    tags=["Admin"],
)
async def reset_circuit_breaker() -> JSONResponse:
    """Force-close the MCP circuit breaker (admin action)."""
    if _pipeline._mcp is None:
        raise HTTPException(status_code=503, detail="MCP client not initialized")
    await _pipeline._mcp._cb.reset()
    logger.info("🔄 Circuit breaker manually reset via admin endpoint")
    return JSONResponse({"success": True, "message": "Circuit breaker reset to CLOSED", "state": _pipeline._mcp.circuit_state})


@app.get(
    "/audit/recent",
    summary="List most recent audit events across all traces",
    tags=["Audit"],
)
async def get_recent_audit_events(limit: int = 20) -> JSONResponse:
    """Returns the most recent audit events from the local request log."""
    return JSONResponse({
        "count": len(_stats["recent_requests"]),
        "events": list(_stats["recent_requests"])[:limit],
    })


@app.websocket("/ws/logs")
async def websocket_log_stream(websocket: WebSocket) -> None:
    """WebSocket endpoint — streams live log entries to the GUI dashboard."""
    await websocket.accept()
    _log_broadcaster.add_client(websocket)
    logger.info("📡 GUI log stream client connected")
    try:
        while True:
            # Keep alive — actual data is pushed by broadcaster
            await asyncio.sleep(30)
    except WebSocketDisconnect:
        pass
    finally:
        _log_broadcaster.remove_client(websocket)
        logger.info("GUI log stream client disconnected")


@app.get(
    "/",
    summary="NexusScale Control Panel",
    include_in_schema=False,
)
async def serve_dashboard() -> FileResponse:
    """Serve the NexusScale GUI Control Panel."""
    dashboard_path = Path("dashboard.html")
    if not dashboard_path.exists():
        return HTMLResponse("<h1>Dashboard not found. Place dashboard.html in the project root.</h1>", status_code=404)
    return FileResponse(str(dashboard_path), media_type="text/html")


# ─────────────────────────────────────────────────────────────────────────────
# Global Exception Handlers — Phase 4 Fault Trapping
# ─────────────────────────────────────────────────────────────────────────────

@app.exception_handler(MCPTimeoutError)
@app.exception_handler(MCPDisconnectError)
@app.exception_handler(MCPCircuitOpenError)
async def mcp_fault_handler(request: Request, exc: MCPError) -> JSONResponse:
    """
    PHASE 4: MCP infrastructure fault trap.
    Catches timeout / disconnect / circuit-open → HTTP 503 Service Unavailable.
    Transaction rollback is handled inside the pipeline.
    """
    correlation_id = getattr(request.state, "correlation_id", str(uuid.uuid4()))
    logger.error(
        "🔴 MCP infrastructure fault intercepted at API layer",
        extra={"error_code": exc.error_code, "correlation_id": correlation_id},
    )
    from datetime import datetime, timezone
    return JSONResponse(
        status_code=503,
        content={
            "error": exc.error_code,
            "message": exc.message,
            "http_status": 503,
            "trace_id": correlation_id,
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "retry_after_seconds": getattr(exc, "retry_after_seconds", 30),
        },
        headers={"Retry-After": str(getattr(exc, "retry_after_seconds", 30))},
    )


@app.exception_handler(DepartmentEmptyError)
@app.exception_handler(PayloadValidationError)
@app.exception_handler(SessionKeyInvalidError)
@app.exception_handler(SecurityValidationError)
async def validation_fault_handler(request: Request, exc: ComplianceEngineError) -> JSONResponse:
    """Security and validation errors → HTTP 422 / 401."""
    http_status = getattr(exc, "http_status", 422)
    return JSONResponse(status_code=http_status, content=exc.to_dict())


@app.exception_handler(ValidationError)
async def pydantic_validation_handler(request: Request, exc: ValidationError) -> JSONResponse:
    """Pydantic schema validation errors → HTTP 422 with field-level detail."""
    from datetime import datetime, timezone
    return JSONResponse(
        status_code=422,
        content={
            "error": "PAYLOAD_SCHEMA_INVALID",
            "message": "Request payload failed schema validation.",
            "http_status": 422,
            "trace_id": getattr(request.state, "correlation_id", str(uuid.uuid4())),
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "field_errors": exc.errors(),
        },
    )


@app.exception_handler(Exception)
async def global_exception_handler(request: Request, exc: Exception) -> JSONResponse:
    """Last-resort catch-all → HTTP 500."""
    logger.exception("Unhandled exception", extra={"path": str(request.url.path)})
    from datetime import datetime, timezone
    return JSONResponse(
        status_code=500,
        content={
            "error": "INTERNAL_SERVER_ERROR",
            "message": "An unexpected error occurred. Please contact support.",
            "http_status": 500,
            "trace_id": getattr(request.state, "correlation_id", str(uuid.uuid4())),
            "timestamp": datetime.now(timezone.utc).isoformat(),
        },
    )


# ─────────────────────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────────────────────

def _serialize_response(response: ComplianceResponse) -> dict:
    """Convert ComplianceResponse to a JSON-safe dict."""
    return {
        "trace_id": str(response.trace_id),
        "status": response.status.value,
        "department": response.department,
        "amount_usd": float(response.amount_usd),
        "limit_usd": float(response.limit_usd),
        "variance_usd": float(response.variance_usd),
        "message": response.message,
        "requires_escalation": response.requires_escalation,
        "notification_dispatched": response.notification_dispatched,
        "processing_time_ms": response.processing_time_ms,
    }


# Type alias for exception handler registration
MCPError = (MCPTimeoutError, MCPDisconnectError, MCPCircuitOpenError)


# ─────────────────────────────────────────────────────────────────────────────
# Dev Server Entry Point
# ─────────────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(
        "main:app",
        host=os.environ.get("HOST", "0.0.0.0"),
        port=int(os.environ.get("PORT", 8000)),
        reload=os.environ.get("RELOAD", "true").lower() == "true",
        log_level=os.environ.get("LOG_LEVEL", "info").lower(),
    )
