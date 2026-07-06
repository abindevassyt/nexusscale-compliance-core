"""
core/audit_trail.py
────────────────────
Immutable append-only audit event log using SQLAlchemy async ORM.

Every state transition in the compliance pipeline writes one AuditEvent row.
Rows are never updated or deleted — only inserted. This provides a full
forensic record of every expense payload's lifecycle.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from uuid import UUID

from sqlalchemy import Column, DateTime, Float, String, Text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.orm import DeclarativeBase

from core.models import AuditEvent, AuditEventType

logger = logging.getLogger(__name__)


# ─────────────────────────────────────────────────────────────────────────────
# ORM Models
# ─────────────────────────────────────────────────────────────────────────────

class Base(DeclarativeBase):
    pass


class AuditEventRecord(Base):
    """SQLAlchemy ORM row — mirrors AuditEvent Pydantic model."""

    __tablename__ = "audit_events"

    event_id = Column(String(36), primary_key=True)
    trace_id = Column(String(36), nullable=False, index=True)
    event_type = Column(String(64), nullable=False, index=True)
    agent_name = Column(String(128), nullable=False)
    payload_snapshot = Column(Text, nullable=True)  # JSON string
    outcome = Column(String(256), nullable=True)
    error_detail = Column(Text, nullable=True)
    duration_ms = Column(Float, nullable=True)
    timestamp = Column(DateTime(timezone=True), nullable=False, index=True)


# ─────────────────────────────────────────────────────────────────────────────
# Audit Trail Service
# ─────────────────────────────────────────────────────────────────────────────

class AuditTrailService:
    """
    Async service for writing compliance audit events.

    Usage:
        service = AuditTrailService("sqlite+aiosqlite:///./audit_trail.db")
        await service.initialize()
        await service.record(event)
    """

    def __init__(self, db_url: str) -> None:
        self._engine = create_async_engine(db_url, echo=False)
        self._session_factory = async_sessionmaker(
            self._engine, expire_on_commit=False
        )

    async def initialize(self) -> None:
        """Create tables if they do not exist."""
        async with self._engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        logger.info("✅ Audit trail database initialized")

    async def record(self, event: AuditEvent) -> None:
        """Append one immutable audit event to the log."""
        import json

        record = AuditEventRecord(
            event_id=str(event.event_id),
            trace_id=str(event.trace_id),
            event_type=event.event_type.value,
            agent_name=event.agent_name,
            payload_snapshot=json.dumps(event.payload_snapshot, default=str),
            outcome=event.outcome,
            error_detail=event.error_detail,
            duration_ms=event.duration_ms,
            timestamp=event.timestamp,
        )
        try:
            async with self._session_factory() as session:
                async with session.begin():
                    session.add(record)
            logger.debug(
                "📝 Audit event recorded",
                extra={
                    "event_id": record.event_id,
                    "trace_id": record.trace_id,
                    "event_type": record.event_type,
                },
            )
        except Exception as exc:
            # Audit failures must NEVER crash the compliance pipeline
            logger.error(
                "⚠️  Failed to write audit event (non-fatal)",
                extra={"error": str(exc), "event_type": event.event_type.value},
            )

    async def query_by_trace(self, trace_id: UUID) -> list[dict]:
        """Retrieve all events for a given trace/correlation ID."""
        from sqlalchemy import select

        async with self._session_factory() as session:
            result = await session.execute(
                select(AuditEventRecord)
                .where(AuditEventRecord.trace_id == str(trace_id))
                .order_by(AuditEventRecord.timestamp)
            )
            rows = result.scalars().all()
            return [
                {
                    "event_id": r.event_id,
                    "trace_id": r.trace_id,
                    "event_type": r.event_type,
                    "agent_name": r.agent_name,
                    "outcome": r.outcome,
                    "duration_ms": r.duration_ms,
                    "timestamp": r.timestamp.isoformat() if r.timestamp else None,
                }
                for r in rows
            ]

    async def query_recent_events(self, limit: int = 100) -> list[dict]:
        """Retrieve the most recent audit events across all traces."""
        from sqlalchemy import select

        async with self._session_factory() as session:
            result = await session.execute(
                select(AuditEventRecord)
                .order_by(AuditEventRecord.timestamp.desc())
                .limit(limit)
            )
            rows = result.scalars().all()
            return [
                {
                    "event_id": r.event_id,
                    "trace_id": r.trace_id,
                    "event_type": r.event_type,
                    "agent_name": r.agent_name,
                    "outcome": r.outcome,
                    "duration_ms": r.duration_ms,
                    "timestamp": r.timestamp.isoformat() if r.timestamp else None,
                }
                for r in rows
            ]

    async def shutdown(self) -> None:
        await self._engine.dispose()
        logger.info("Audit trail database connection closed")
