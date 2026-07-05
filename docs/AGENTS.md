# AGENTS.md — Agent Module Reference

> **Location:** `agents/`  
> **Pattern:** Supervisor-Worker with dependency-injected sub-agents  
> **Base:** All agents extend `BaseAgent` and register with `AgentRegistry`

---

## Table of Contents

1. [base_agent.py](#base_agentpy)
   - [AgentRegistry](#agentregistry)
   - [AgentRunContext](#agentruncontext)
   - [BaseAgent](#baseagent)
2. [expense_auditor_agent.py](#expense_auditor_agentpy)
   - [ExpenseAuditorAgent](#expenseauditoragent)
3. [policy_evaluator_worker.py](#policy_evaluator_workerpy)
   - [PolicyEvaluatorWorker](#policyevaluatorworker)
4. [resolution_communicator.py](#resolution_communicatorpy)
   - [ResolutionCommunicator](#resolutioncommunicator)
   - [Helper Functions](#resolution-helper-functions)

---

## `base_agent.py`

**Path:** [`agents/base_agent.py`](../agents/base_agent.py)

Provides the abstract foundation for every compliance agent. Defines the execution lifecycle, audit emission, metrics tracking, and the global agent registry.

---

### `AgentRegistry`

```python
class AgentRegistry
```

A **class-level singleton** dictionary mapping agent names to their instances. Agents self-register in `BaseAgent.__init__()`. Used by the `/health`, `/metrics` endpoints and the orchestrator to discover agents by name.

#### Methods

---

##### `register(agent: BaseAgent) → None` *(classmethod)*

```python
@classmethod
def register(cls, agent: BaseAgent) -> None
```

Adds `agent` to the internal `_registry` dict keyed by `agent.name`. Called automatically in `BaseAgent.__init__()` — you never need to call this manually.

| Parameter | Type | Description |
|-----------|------|-------------|
| `agent` | `BaseAgent` | The agent instance to register |

---

##### `get(name: str) → BaseAgent | None` *(classmethod)*

```python
@classmethod
def get(cls, name: str) -> BaseAgent | None
```

Retrieves an agent instance by name. Returns `None` if the name is not found.

| Parameter | Type | Description |
|-----------|------|-------------|
| `name` | `str` | Agent name (e.g. `"ExpenseAuditorAgent"`) |

**Returns:** The registered `BaseAgent` instance or `None`.

---

##### `all() → dict[str, BaseAgent]` *(classmethod)*

```python
@classmethod
def all(cls) -> dict[str, BaseAgent]
```

Returns a **copy** of the full registry dict. Used by `/health` and `/metrics` endpoints to enumerate all running agents.

**Returns:** `dict[str, BaseAgent]` — shallow copy of the registry.

---

### `AgentRunContext`

```python
@dataclass
class AgentRunContext
```

A **context object** that flows through the entire pipeline for a single request. Carries correlation data and shared references (audit service) so every agent writes to the same trace.

#### Fields

| Field | Type | Default | Description |
|-------|------|---------|-------------|
| `trace_id` | `UUID` | required | Auto-generated UUID for this request |
| `correlation_id` | `str` | required | HTTP `X-Correlation-ID` header value |
| `audit_service` | `AuditTrailService \| None` | `None` | Injected by the orchestrator |
| `metadata` | `dict[str, Any]` | `{}` | Extensible bag for pipeline-specific state |
| `start_time` | `float` | `time.monotonic()` | Monotonic clock reading at context creation |

#### Properties

---

##### `elapsed_ms → float`

```python
@property
def elapsed_ms(self) -> float
```

Returns the milliseconds elapsed since the context was created (`time.monotonic()` difference). Used by agents to measure end-to-end pipeline latency.

---

### `BaseAgent`

```python
class BaseAgent(abc.ABC)
```

Abstract base class for all NexusScale compliance agents. Provides a structured **5-step lifecycle**, built-in metrics tracking, and an audit emission helper.

#### Class Attributes

| Attribute | Type | Description |
|-----------|------|-------------|
| `name` | `str` | Unique agent identifier (override in subclass) |
| `persona` | `str` | Human-readable description of the agent's role |
| `role` | `AgentRole` | `SUPERVISOR`, `WORKER`, or `COMMUNICATOR` |
| `version` | `str` | Semver string for this agent's implementation |

#### `__init__(self) → None`

Initialises private counters, creates a namespaced logger (`agents.<name>`), and calls `AgentRegistry.register(self)`.

---

#### `execute(payload, context) → Any` *(async)*

```python
async def execute(self, payload: Any, context: AgentRunContext) -> Any
```

**The public entry point.** Runs the full lifecycle in this order:

```
before_run(payload, context)
  └─ _validate_inputs(payload, context)      ← abstract guard
       └─ run(payload, context)              ← abstract core logic
            └─ after_run(result, context)   ← optional cleanup
```

If any step raises a `ComplianceEngineError`, `on_error()` is called and the exception propagates unmodified.  
If any step raises a generic `Exception`, it is wrapped in `ComplianceEngineError` before propagating.

| Parameter | Type | Description |
|-----------|------|-------------|
| `payload` | `Any` | Input to this agent (e.g. `ExpensePayload`) |
| `context` | `AgentRunContext` | Pipeline-wide correlation context |

**Returns:** Whatever `run()` returns.  
**Raises:** `ComplianceEngineError` (or subclass) on any failure.

---

#### `run(payload, context) → Any` *(abstract, async)*

```python
@abc.abstractmethod
async def run(self, payload: Any, context: AgentRunContext) -> Any
```

**Must be implemented by every concrete agent.** Contains the core business logic.

---

#### `_validate_inputs(payload, context) → None` *(abstract)*

```python
@abc.abstractmethod
def _validate_inputs(self, payload: Any, context: AgentRunContext) -> None
```

**Must be implemented by every concrete agent.** Called synchronously before `run()`. Should raise a `ComplianceEngineError` subclass if preconditions are not met.

---

#### `before_run(payload, context) → None` *(optional, async)*

```python
async def before_run(self, payload: Any, context: AgentRunContext) -> None
```

Pre-execution hook. Default is a no-op. Override for setup logic such as acquiring resources or logging entry.

---

#### `after_run(result, context) → None` *(optional, async)*

```python
async def after_run(self, result: Any, context: AgentRunContext) -> None
```

Post-execution hook. Default is a no-op. Override for cleanup, metrics emission, or telemetry.

---

#### `on_error(payload, context) → None` *(optional, async)*

```python
async def on_error(self, payload: Any, context: AgentRunContext) -> None
```

Error hook called when any lifecycle step raises. Default is a no-op. Override to write error audit events or trigger alerts.

---

#### `_emit_audit(context, event_type, outcome, ...) → None` *(async)*

```python
async def _emit_audit(
    self,
    context: AgentRunContext,
    event_type: AuditEventType,
    outcome: str,
    payload_snapshot: dict | None = None,
    error_detail: str | None = None,
    duration_ms: float = 0.0,
) -> None
```

Writes a structured `AuditEvent` to the `AuditTrailService`. If `context.audit_service` is `None` (e.g. in unit tests without a DB), this is a silent no-op.

| Parameter | Type | Description |
|-----------|------|-------------|
| `context` | `AgentRunContext` | Provides `trace_id` and `audit_service` |
| `event_type` | `AuditEventType` | One of `APPROVED`, `FLAGGED`, `SECURITY_REJECTED`, etc. |
| `outcome` | `str` | Human-readable outcome string |
| `payload_snapshot` | `dict \| None` | Serialised request data for the audit record |
| `error_detail` | `str \| None` | Error message if this is an error event |
| `duration_ms` | `float` | Processing time for this step |

---

#### `metrics → dict[str, Any]` *(property)*

```python
@property
def metrics(self) -> dict[str, Any]
```

Returns a JSON-serialisable metrics snapshot:
```json
{
  "agent": "ExpenseAuditorAgent",
  "run_count": 42,
  "error_count": 1,
  "avg_latency_ms": 15.8
}
```

---

## `expense_auditor_agent.py`

**Path:** [`agents/expense_auditor_agent.py`](../agents/expense_auditor_agent.py)

The **Supervisor agent**. Entry point for every inbound expense. Orchestrates the 4-gate compliance pipeline.

---

### `ExpenseAuditorAgent`

```python
class ExpenseAuditorAgent(BaseAgent)
```

| Attribute | Value |
|-----------|-------|
| `name` | `"ExpenseAuditorAgent"` |
| `persona` | `"Financial Ingress Supervisor"` |
| `role` | `AgentRole.SUPERVISOR` |

#### Constructor

```python
def __init__(
    self,
    policy_worker: PolicyEvaluatorWorker,
    resolution_worker: ResolutionCommunicator,
) -> None
```

Accepts both worker agents via **dependency injection** for testability. Both workers are stored as `_policy_worker` and `_resolution_worker`.

---

#### `run(payload, context) → ComplianceResponse` *(async)*

Executes the 4-gate compliance pipeline:

```
Gate 1 — Security Validation
  └─ validate_inbound_payload_security(department, session_key, employee_id)
  └─ Emits: SECURITY_VALIDATED or SECURITY_REJECTED audit event

Gate 2 — Policy Evaluation
  └─ await policy_worker.execute(payload, context)
  └─ Emits: POLICY_EVALUATED audit event

Gate 3 — Conditional Notification
  └─ if status == FLAGGED:
       └─ await resolution_worker.execute({payload, evaluation}, context)
  └─ Emits: NOTIFICATION_SENT audit event

Gate 4 — Response Construction
  └─ Build ComplianceResponse from evaluation result
  └─ Emits: APPROVED or FLAGGED audit event
```

**Returns:** `ComplianceResponse`

---

#### `_validate_inputs(payload, context) → None`

Checks that `payload` is an `ExpensePayload` instance and that `context.trace_id` is set. Raises `AgentInitializationError` if not.

---

## `policy_evaluator_worker.py`

**Path:** [`agents/policy_evaluator_worker.py`](../agents/policy_evaluator_worker.py)

**Worker 1**. Fetches the corporate spending limit via MCP and performs a **pure integer comparison** (no floats) to determine compliance status.

---

### `PolicyEvaluatorWorker`

```python
class PolicyEvaluatorWorker(BaseAgent)
```

| Attribute | Value |
|-----------|-------|
| `name` | `"PolicyEvaluatorWorker"` |
| `persona` | `"Corporate Policy Compliance Evaluator"` |
| `role` | `AgentRole.WORKER` |

#### Constructor

```python
def __init__(
    self,
    mcp_client: MCPClient,
    local_ruleset: PolicyRuleSet,
) -> None
```

| Parameter | Type | Description |
|-----------|------|-------------|
| `mcp_client` | `MCPClient` | MCP client for fetching live policy from the bridge |
| `local_ruleset` | `PolicyRuleSet` | Local fallback loaded from `policy_rules.json` |

---

#### `run(payload, context) → PolicyEvaluationResult` *(async)*

1. Calls `_fetch_policy(department, category)` → `PolicyLimit`
2. Performs integer arithmetic:
   ```python
   amount_cents = payload.amount_cents         # e.g. 4250
   limit_cents  = policy.limit_cents           # e.g. 5000
   variance_cents = max(0, amount_cents - limit_cents)
   status = APPROVED if amount_cents <= limit_cents else FLAGGED
   ```
3. Checks escalation threshold if `requires_escalation` is configured
4. Returns `PolicyEvaluationResult`

**No floating-point arithmetic is used in the comparison — all calculations are integer cents.**

**Returns:** `PolicyEvaluationResult`

---

#### `_fetch_policy(department, category) → PolicyLimit` *(async, private)*

Attempts to fetch the policy limit via `mcp_client.call_tool("fetch_corporate_policy", ...)`.  
On `MCPError` / `MCPCircuitOpenError`, falls back to `local_ruleset.resolve_limit(department, category)`.

---

#### `_validate_inputs(payload, context) → None`

Verifies `payload` is an `ExpensePayload` with `amount_cents > 0`.

---

## `resolution_communicator.py`

**Path:** [`agents/resolution_communicator.py`](../agents/resolution_communicator.py)

**Worker 2**. Only activated when the Supervisor determines `status == FLAGGED`. Dispatches formatted notifications to Slack and/or Teams. **Never crashes the pipeline** — all webhook errors are swallowed and logged.

---

### `ResolutionCommunicator`

```python
class ResolutionCommunicator(BaseAgent)
```

| Attribute | Value |
|-----------|-------|
| `name` | `"ResolutionCommunicator"` |
| `persona` | `"Compliance Notification Dispatcher"` |
| `role` | `AgentRole.COMMUNICATOR` |

#### Constructor

```python
def __init__(
    self,
    slack_enabled: bool = True,
    teams_enabled: bool = True,
) -> None
```

Reads `SLACK_BOT_TOKEN` and `TEAMS_WEBHOOK_URL` from the environment. Sets `slack_enabled` and `teams_enabled` based on both the constructor arg and whether the env var is present.

---

#### `run(data, context) → list[NotificationResult]` *(async)*

Accepts a dict `{"payload": ExpensePayload, "evaluation": PolicyEvaluationResult}`.

1. Builds a `NotificationPayload` with severity based on `requires_escalation`
2. Dispatches to Slack if enabled (via `_dispatch_slack`)
3. Dispatches to Teams if enabled (via `_dispatch_teams`)
4. Returns a list of `NotificationResult` — one per channel attempted

**Returns:** `list[NotificationResult]`  
**Never raises** — all exceptions are caught, logged, and returned as failed `NotificationResult` objects.

---

#### `_dispatch_slack(notification) → NotificationResult` *(async, private)*

Sends a Slack Block Kit message via the Slack Web API `chat.postMessage` endpoint.  
Uses `tenacity` with exponential backoff (3 retries, 1s/2s/4s delays).

---

#### `_dispatch_teams(notification) → NotificationResult` *(async, private)*

Posts a Teams Adaptive Card to the configured incoming webhook URL.  
Uses `tenacity` with exponential backoff (3 retries).

---

### Resolution Helper Functions

These module-level functions build the platform-specific message payloads:

---

#### `_build_slack_blocks(notification: NotificationPayload) → list[dict]`

```python
def _build_slack_blocks(notification: NotificationPayload) -> list[dict]
```

Builds a Slack [Block Kit](https://api.slack.com/block-kit) message structure for a flagged expense event.

**Returns a list of blocks containing:**
- `header` block with alert icon and subject line
- `section` block with department, amount, limit, and variance fields
- `divider` block
- `actions` block with "Review Expense" and "Acknowledge" buttons

| Parameter | Type | Description |
|-----------|------|-------------|
| `notification` | `NotificationPayload` | The structured notification to render |

**Returns:** `list[dict]` — Slack Block Kit-compatible block array.

---

#### `_build_teams_adaptive_card(notification: NotificationPayload) → dict`

```python
def _build_teams_adaptive_card(notification: NotificationPayload) -> dict
```

Builds a Microsoft Teams Adaptive Card payload structured as an `application/vnd.microsoft.card.adaptive` attachment.

**Returns a dict with:**
- `type: "message"`
- `attachments[0].contentType: "application/vnd.microsoft.card.adaptive"`
- `body` containing `TextBlock` and `FactSet` elements
- `actions` for review and escalation

| Parameter | Type | Description |
|-----------|------|-------------|
| `notification` | `NotificationPayload` | The structured notification to render |

**Returns:** `dict` — Teams Adaptive Card-compatible message payload.
