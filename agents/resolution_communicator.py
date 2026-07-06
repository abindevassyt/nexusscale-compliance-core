"""
agents/resolution_communicator.py
───────────────────────────────────
ResolutionCommunicator — COMMUNICATOR (Sub-Agent 2)

Activated ONLY when PolicyEvaluatorWorker returns FLAGGED status.

Responsibilities:
  1. Build a richly formatted notification payload from the evaluation result
  2. Dispatch to Slack and/or Microsoft Teams via webhook HTTP calls
  3. Retry up to N times with exponential backoff (tenacity)
  4. Log and audit every dispatch attempt
  5. Never crash the pipeline — failure is logged and the audit event is written

Webhook targets are configured via environment variables:
  SLACK_BOT_TOKEN, SLACK_DEFAULT_CHANNEL
  TEAMS_WEBHOOK_URL
"""

from __future__ import annotations

import logging
import os
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any

import httpx
from tenacity import (
    AsyncRetrying,
    retry_if_exception_type,
    stop_after_attempt,
    wait_exponential,
)

from agents.base_agent import AgentRole, AgentRunContext, BaseAgent
from core.exceptions import WebhookDispatchError
from core.models import (
    AuditEventType,
    ComplianceStatus,
    ExpensePayload,
    NotificationChannel,
    NotificationPayload,
    NotificationResult,
    PolicyEvaluationResult,
)

logger = logging.getLogger("agents.resolution_communicator")

_SLACK_MAX_ATTEMPTS: int = 3
_TEAMS_MAX_ATTEMPTS: int = 3
_WEBHOOK_TIMEOUT: float = 10.0


class ResolutionCommunicator(BaseAgent):
    """
    Worker 2 — Multi-channel compliance notification dispatcher.

    Dispatches structured alerts to Slack and/or Microsoft Teams
    when a flagged expense event requires human review.
    """

    name = "ResolutionCommunicator"
    persona = "Compliance Notification Dispatcher"
    role = AgentRole.COMMUNICATOR
    version = "1.0.0"

    def __init__(
        self,
        http_client: httpx.AsyncClient | None = None,
        slack_enabled: bool = True,
        teams_enabled: bool = True,
    ) -> None:
        super().__init__()
        self._http = http_client or httpx.AsyncClient(timeout=_WEBHOOK_TIMEOUT)
        self._slack_enabled = slack_enabled and bool(os.environ.get("SLACK_BOT_TOKEN"))
        self._teams_enabled = teams_enabled and bool(os.environ.get("TEAMS_WEBHOOK_URL"))
        self._slack_token = os.environ.get("SLACK_BOT_TOKEN", "")
        self._slack_channel = os.environ.get("SLACK_DEFAULT_CHANNEL", "#compliance-alerts")
        self._teams_url = os.environ.get("TEAMS_WEBHOOK_URL", "")

    # ── Validation ────────────────────────────────────────────────────────────

    def _validate_inputs(self, payload: dict, context: AgentRunContext) -> None:
        if "payload" not in payload or "evaluation" not in payload:
            raise WebhookDispatchError(
                message="ResolutionCommunicator requires keys 'payload' and 'evaluation'",
                correlation_id=context.correlation_id,
            )

    # ── Lifecycle ─────────────────────────────────────────────────────────────

    async def before_run(self, payload: dict, context: AgentRunContext) -> None:
        eval_result: PolicyEvaluationResult = payload["evaluation"]
        logger.warning(
            "🔔 ResolutionCommunicator activated — preparing notifications",
            extra={
                "agent": self.name,
                "trace_id": str(context.trace_id),
                "department": eval_result.department,
                "variance_usd": float(eval_result.variance_usd),
                "requires_escalation": eval_result.requires_escalation,
                "slack_enabled": self._slack_enabled,
                "teams_enabled": self._teams_enabled,
            },
        )

    # ── Core Logic ────────────────────────────────────────────────────────────

    async def run(self, payload: dict, context: AgentRunContext) -> list[NotificationResult]:
        """
        Build notification payload and dispatch to all enabled channels.
        Failure on one channel does NOT block the other.
        """
        expense: ExpensePayload = payload["payload"]
        evaluation: PolicyEvaluationResult = payload["evaluation"]

        notification = self._build_notification(expense, evaluation, context)
        results: list[NotificationResult] = []

        # ── Slack Dispatch ────────────────────────────────────────────────────
        if self._slack_enabled:
            slack_result = await self._dispatch_slack(notification, context)
            results.append(slack_result)
            await self._emit_audit(
                context,
                event_type=AuditEventType.NOTIFICATION_SENT,
                outcome=f"Slack dispatch {'succeeded' if slack_result.success else 'failed'}",
                payload_snapshot={
                    "channel": NotificationChannel.SLACK.value,
                    "success": slack_result.success,
                    "attempts": slack_result.attempts,
                },
            )
        else:
            logger.info("Slack dispatch skipped (not configured)", extra={"trace_id": str(context.trace_id)})

        # ── Teams Dispatch ────────────────────────────────────────────────────
        if self._teams_enabled:
            teams_result = await self._dispatch_teams(notification, context)
            results.append(teams_result)
            await self._emit_audit(
                context,
                event_type=AuditEventType.NOTIFICATION_SENT,
                outcome=f"Teams dispatch {'succeeded' if teams_result.success else 'failed'}",
                payload_snapshot={
                    "channel": NotificationChannel.TEAMS.value,
                    "success": teams_result.success,
                    "attempts": teams_result.attempts,
                },
            )
        else:
            logger.info("Teams dispatch skipped (not configured)", extra={"trace_id": str(context.trace_id)})

        if not self._slack_enabled and not self._teams_enabled:
            # Fallback to local file logging for time being
            file_result = await self._dispatch_local_file(notification, context)
            results.append(file_result)
            await self._emit_audit(
                context,
                event_type=AuditEventType.NOTIFICATION_SENT,
                outcome=f"Local file dispatch {'succeeded' if file_result.success else 'failed'}",
                payload_snapshot={
                    "channel": NotificationChannel.LOCAL_FILE.value,
                    "success": file_result.success,
                    "attempts": file_result.attempts,
                },
            )
            logger.warning(
                "⚠️  No notification channels configured — logged alert to logs/notifications.log",
                extra={
                    "agent": self.name,
                    "trace_id": str(context.trace_id),
                    "department": expense.department,
                    "amount_usd": float(expense.amount),
                },
            )

        return results

    # ── Slack Dispatch ────────────────────────────────────────────────────────

    async def _dispatch_slack(
        self, notification: NotificationPayload, context: AgentRunContext
    ) -> NotificationResult:
        blocks = _build_slack_blocks(notification)
        payload = {
            "channel": self._slack_channel,
            "text": notification.subject,
            "blocks": blocks,
        }
        return await self._dispatch_with_retry(
            channel=NotificationChannel.SLACK,
            url="https://slack.com/api/chat.postMessage",
            headers={
                "Authorization": f"Bearer {self._slack_token}",
                "Content-Type": "application/json",
            },
            payload=payload,
            max_attempts=_SLACK_MAX_ATTEMPTS,
            notification=notification,
            context=context,
        )

    # ── Teams Dispatch ────────────────────────────────────────────────────────

    async def _dispatch_teams(
        self, notification: NotificationPayload, context: AgentRunContext
    ) -> NotificationResult:
        card = _build_teams_adaptive_card(notification)
        return await self._dispatch_with_retry(
            channel=NotificationChannel.TEAMS,
            url=self._teams_url,
            headers={"Content-Type": "application/json"},
            payload=card,
            max_attempts=_TEAMS_MAX_ATTEMPTS,
            notification=notification,
            context=context,
        )

    # ── Generic Retry Dispatcher ──────────────────────────────────────────────

    async def _dispatch_with_retry(
        self,
        channel: NotificationChannel,
        url: str,
        headers: dict,
        payload: dict,
        max_attempts: int,
        notification: NotificationPayload,
        context: AgentRunContext,
    ) -> NotificationResult:
        attempt_count = 0
        last_error: str | None = None
        last_status: int | None = None

        try:
            async for attempt in AsyncRetrying(
                stop=stop_after_attempt(max_attempts),
                wait=wait_exponential(multiplier=1, min=1, max=8),
                retry=retry_if_exception_type(httpx.HTTPStatusError),
                reraise=True,
            ):
                with attempt:
                    attempt_count += 1
                    logger.info(
                        f"→ Dispatching {channel.value} notification (attempt {attempt_count})",
                        extra={"trace_id": str(context.trace_id), "channel": channel.value},
                    )
                    resp = await self._http.post(url, json=payload, headers=headers)
                    last_status = resp.status_code
                    resp.raise_for_status()

                    logger.info(
                        f"✅ {channel.value} notification dispatched",
                        extra={
                            "trace_id": str(context.trace_id),
                            "channel": channel.value,
                            "status_code": last_status,
                            "attempts": attempt_count,
                        },
                    )
                    return NotificationResult(
                        trace_id=notification.trace_id,
                        channel=channel,
                        success=True,
                        attempts=attempt_count,
                        response_code=last_status,
                    )

        except Exception as exc:
            last_error = str(exc)
            logger.error(
                f"✗ {channel.value} webhook dispatch failed after {attempt_count} attempts",
                extra={
                    "trace_id": str(context.trace_id),
                    "channel": channel.value,
                    "error": last_error,
                    "attempts": attempt_count,
                },
            )

        return NotificationResult(
            trace_id=notification.trace_id,
            channel=channel,
            success=False,
            attempts=attempt_count,
            response_code=last_status,
            error_detail=last_error,
        )

    # ── Local File Dispatch (Fallback) ────────────────────────────────────────

    async def _dispatch_local_file(
        self, notification: NotificationPayload, context: AgentRunContext
    ) -> NotificationResult:
        import json
        import os
        import asyncio

        def write_file():
            os.makedirs("logs", exist_ok=True)
            with open("logs/notifications.log", "a", encoding="utf-8") as f:
                record = {
                    "timestamp": datetime.now(timezone.utc).isoformat(),
                    "trace_id": str(notification.trace_id),
                    "severity": notification.severity,
                    "subject": notification.subject,
                    "body": notification.body
                }
                f.write(json.dumps(record) + "\n")
                
        try:
            await asyncio.to_thread(write_file)
            logger.info(
                "✅ LOCAL_FILE notification dispatched",
                extra={"trace_id": str(context.trace_id), "channel": "local_file"},
            )
            return NotificationResult(
                trace_id=notification.trace_id,
                channel=NotificationChannel.LOCAL_FILE,
                success=True,
                attempts=1,
                response_code=200,
            )
        except Exception as exc:
            logger.error(
                f"✗ LOCAL_FILE webhook dispatch failed: {exc}",
                extra={"trace_id": str(context.trace_id), "channel": "local_file", "error": str(exc)},
            )
            return NotificationResult(
                trace_id=notification.trace_id,
                channel=NotificationChannel.LOCAL_FILE,
                success=False,
                attempts=1,
                error_detail=str(exc)
            )

    # ── Notification Builder ──────────────────────────────────────────────────

    def _build_notification(
        self,
        expense: ExpensePayload,
        evaluation: PolicyEvaluationResult,
        context: AgentRunContext,
    ) -> NotificationPayload:
        severity = "CRITICAL" if evaluation.requires_escalation else "WARNING"
        subject = (
            f"🚨 {'CRITICAL: ' if evaluation.requires_escalation else ''}Expense Policy Violation — "
            f"{expense.department} | ${float(expense.amount):.2f} exceeds ${float(evaluation.limit_usd):.2f} limit"
        )
        body = (
            f"A policy violation has been detected:\n\n"
            f"• Employee: {expense.employee_id} ({expense.employee_email or 'unknown'})\n"
            f"• Department: {expense.department}\n"
            f"• Category: {expense.category.value.title()}\n"
            f"• Amount: ${float(expense.amount):.2f} {expense.currency}\n"
            f"• Policy Limit: ${float(evaluation.limit_usd):.2f}\n"
            f"• Overage: ${float(evaluation.variance_usd):.2f}\n"
            f"• Requires Escalation: {'YES' if evaluation.requires_escalation else 'No'}\n"
            f"• Trace ID: {expense.trace_id}\n"
            f"• Submitted: {expense.created_at.strftime('%Y-%m-%d %H:%M UTC')}"
        )
        return NotificationPayload(
            trace_id=expense.trace_id,
            channel=NotificationChannel.SLACK,
            recipient=expense.employee_email or expense.employee_id,
            subject=subject,
            body=body,
            severity=severity,
            expense_summary={
                "department": expense.department,
                "amount_usd": float(expense.amount),
                "limit_usd": float(evaluation.limit_usd),
                "variance_usd": float(evaluation.variance_usd),
                "category": expense.category.value,
                "trace_id": str(expense.trace_id),
            },
        )


# ─────────────────────────────────────────────────────────────────────────────
# Slack Block Kit Builder
# ─────────────────────────────────────────────────────────────────────────────

def _build_slack_blocks(n: NotificationPayload) -> list[dict]:
    color = "#FF0000" if n.severity == "CRITICAL" else "#FFA500"
    summary = n.expense_summary
    return [
        {"type": "header", "text": {"type": "plain_text", "text": n.subject, "emoji": True}},
        {"type": "divider"},
        {
            "type": "section",
            "fields": [
                {"type": "mrkdwn", "text": f"*Department:*\n{summary.get('department','N/A')}"},
                {"type": "mrkdwn", "text": f"*Category:*\n{summary.get('category','N/A').title()}"},
                {"type": "mrkdwn", "text": f"*Amount:*\n${summary.get('amount_usd',0):.2f}"},
                {"type": "mrkdwn", "text": f"*Policy Limit:*\n${summary.get('limit_usd',0):.2f}"},
                {"type": "mrkdwn", "text": f"*Overage:*\n${summary.get('variance_usd',0):.2f}"},
                {"type": "mrkdwn", "text": f"*Severity:*\n{n.severity}"},
            ],
        },
        {"type": "divider"},
        {"type": "section", "text": {"type": "mrkdwn", "text": f"*Trace ID:* `{summary.get('trace_id','')}`"}},
        {
            "type": "actions",
            "elements": [
                {"type": "button", "text": {"type": "plain_text", "text": "✅ Approve"}, "style": "primary", "value": f"approve:{summary.get('trace_id','')}"},
                {"type": "button", "text": {"type": "plain_text", "text": "❌ Reject"},  "style": "danger",   "value": f"reject:{summary.get('trace_id','')}"},
                {"type": "button", "text": {"type": "plain_text", "text": "🔍 View Audit"}, "value": f"audit:{summary.get('trace_id','')}"},
            ],
        },
    ]


# ─────────────────────────────────────────────────────────────────────────────
# Teams Adaptive Card Builder
# ─────────────────────────────────────────────────────────────────────────────

def _build_teams_adaptive_card(n: NotificationPayload) -> dict:
    summary = n.expense_summary
    color = "attention" if n.severity == "CRITICAL" else "warning"
    return {
        "type": "message",
        "attachments": [
            {
                "contentType": "application/vnd.microsoft.card.adaptive",
                "content": {
                    "$schema": "http://adaptivecards.io/schemas/adaptive-card.json",
                    "type": "AdaptiveCard",
                    "version": "1.5",
                    "body": [
                        {
                            "type": "TextBlock",
                            "text": n.subject,
                            "weight": "Bolder",
                            "size": "Medium",
                            "color": color,
                            "wrap": True,
                        },
                        {
                            "type": "FactSet",
                            "facts": [
                                {"title": "Department", "value": summary.get("department", "N/A")},
                                {"title": "Category",   "value": summary.get("category", "N/A").title()},
                                {"title": "Amount",     "value": f"${summary.get('amount_usd', 0):.2f}"},
                                {"title": "Limit",      "value": f"${summary.get('limit_usd', 0):.2f}"},
                                {"title": "Overage",    "value": f"${summary.get('variance_usd', 0):.2f}"},
                                {"title": "Severity",   "value": n.severity},
                                {"title": "Trace ID",   "value": str(summary.get("trace_id", ""))},
                            ],
                        },
                    ],
                    "actions": [
                        {"type": "Action.Submit", "title": "Approve", "data": {"action": "approve", "trace_id": str(summary.get("trace_id", ""))}},
                        {"type": "Action.Submit", "title": "Reject",  "data": {"action": "reject",  "trace_id": str(summary.get("trace_id", ""))}},
                    ],
                },
            }
        ],
    }
