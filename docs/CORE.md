# CORE.md — Core Module Reference

> **Location:** `core/`  
> **Purpose:** Domain models, security, fault tolerance, audit persistence, and error taxonomy  
> **Total:** 3 classes in audit_trail, 1 in circuit_breaker, 13 exception classes, 15 model classes, 6 security functions

---

## Table of Contents

1. [core/models.py](#coremodels) — Pydantic v2 domain models & enumerations
2. [core/exceptions.py](#coreexceptions) — Exception hierarchy
3. [core/security.py](#coresecurity) — HMAC, preflight abort, session validation
4. [core/circuit_breaker.py](#corecircuit_breaker) — Async circuit breaker
5. [core/audit_trail.py](#coreaudit_trail) — SQLAlchemy async event log

---

## `core/models.py`

**Path:** [`core/models.py`](../core/models.py)

All Pydantic v2 models enforce strict typing. Fields use `Annotated` constraints, `@field_validator`, and `@computed_field` to guarantee payload integrity before any agent receives data.

---

### Enumerations

All enumerations inherit from `(str, Enum)` so their values are JSON-serialisable strings.

---

#### `ComplianceStatus`

```python
class ComplianceStatus(str, Enum)
```

The outcome of a compliance evaluation.

| Member | Value | Description |
|--------|-------|-------------|
| `APPROVED` | `"APPROVED"` | Expense is within the policy limit |
| `FLAGGED` | `"FLAGGED"` | Expense exceeds the policy limit |
| `PENDING` | `"PENDING"` | Evaluation not yet complete |
| `ERROR` | `"ERROR"` | Evaluation encountered an unrecoverable error |

---

#### `ExpenseCategory`

```python
class ExpenseCategory(str, Enum)
```

Valid categories for expense payloads. Used as a discriminator in policy rule resolution.

| Member | Value |
|--------|-------|
| `MEALS` | `"meals"` |
| `TRAVEL` | `"travel"` |
| `ACCOMMODATION` | `"accommodation"` |
| `SOFTWARE` | `"software"` |
| `EQUIPMENT` | `"equipment"` |
| `TRAINING` | `"training"` |
| `ENTERTAINMENT` | `"entertainment"` |
| `MISCELLANEOUS` | `"miscellaneous"` |

---

#### `AgentRole`

```python
class AgentRole(str, Enum)
```

Classifies agents in the Supervisor-Worker hierarchy.

| Member | Value | Agent |
|--------|-------|-------|
| `SUPERVISOR` | `"supervisor"` | `ExpenseAuditorAgent` |
| `WORKER` | `"worker"` | `PolicyEvaluatorWorker` |
| `COMMUNICATOR` | `"communicator"` | `ResolutionCommunicator` |

---

#### `NotificationChannel`

```python
class NotificationChannel(str, Enum)
```

| Member | Value |
|--------|-------|
| `SLACK` | `"slack"` |
| `TEAMS` | `"teams"` |
| `EMAIL` | `"email"` |

---

#### `CircuitState`

```python
class CircuitState(str, Enum)
```

The three states of the MCP circuit breaker.

| Member | Value | Description |
|--------|-------|-------------|
| `CLOSED` | `"CLOSED"` | Normal — all calls go through |
| `OPEN` | `"OPEN"` | Fast-fail — all calls raise `MCPCircuitOpenError` |
| `HALF_OPEN` | `"HALF_OPEN"` | One probe call allowed |

---

#### `AuditEventType`

```python
class AuditEventType(str, Enum)
```

All possible event types recorded in the audit trail.

| Member | Value | When Emitted |
|--------|-------|-------------|
| `PAYLOAD_RECEIVED` | `"PAYLOAD_RECEIVED"` | Inbound request enters the supervisor |
| `SECURITY_VALIDATED` | `"SECURITY_VALIDATED"` | Session key passes HMAC check |
| `SECURITY_REJECTED` | `"SECURITY_REJECTED"` | Security gate rejects the request |
| `POLICY_EVALUATED` | `"POLICY_EVALUATED"` | Policy comparison completes |
| `APPROVED` | `"APPROVED"` | Expense approved |
| `FLAGGED` | `"FLAGGED"` | Expense flagged |
| `NOTIFICATION_SENT` | `"NOTIFICATION_SENT"` | Notification dispatched to Slack/Teams |
| `MCP_ERROR` | `"MCP_ERROR"` | MCP bridge failure with rollback |
| `SYSTEM_ERROR` | `"SYSTEM_ERROR"` | Unhandled exception |

---

### `TimestampedModel`

```python
class TimestampedModel(BaseModel)
```

Base Pydantic model inherited by payload and result models.

**Config:** `str_strip_whitespace=True`, `validate_assignment=True`, `populate_by_name=True`

**Fields:**

| Field | Type | Default | Description |
|-------|------|---------|-------------|
| `created_at` | `datetime` | `datetime.now(timezone.utc)` | UTC timestamp of model creation |

---

### `ExpensePayload`

```python
class ExpensePayload(TimestampedModel)
```

The raw inbound expense object intercepted by `ExpenseAuditorAgent`. Every field is validated before the first agent runs.

**Fields:**

| Field | Type | Constraints | Description |
|-------|------|-------------|-------------|
| `trace_id` | `UUID` | auto-generated | Unique ID for this request |
| `department` | `str` | `min_length=1`, `max_length=128` | Submitting department name |
| `amount` | `Decimal` | `gt=0`, `lt=1000000`, `decimal_places=2` | Expense amount |
| `category` | `ExpenseCategory` | default: `MISCELLANEOUS` | Expense category |
| `employee_id` | `str` | `min_length=3`, `max_length=64` | Submitter's employee ID |
| `employee_email` | `str` | regex email format if non-empty | Submitter's email |
| `description` | `str` | `max_length=512` | Expense description |
| `session_key` | `str` | `min_length=1`, `max_length=512` | HMAC-signed session key |
| `currency` | `str` | regex `^[A-Z]{3}$` | ISO 4217 currency code |
| `submitted_by_ip` | `str` | default: `"0.0.0.0"` | Submitter's IP address |
| `metadata` | `dict[str, Any]` | default: `{}` | Extensible key-value bag |

**Validators:**

---

##### `department_must_not_be_whitespace(cls, v: str) → str` *(field_validator)*

```python
@field_validator("department")
@classmethod
def department_must_not_be_whitespace(cls, v: str) -> str
```

Raises `ValueError` if `v.strip()` is empty. Returns `v.strip().title()` (title-cased, trimmed) on success.

---

##### `validate_email_format(cls, v: str) → str` *(field_validator)*

```python
@field_validator("employee_email")
@classmethod
def validate_email_format(cls, v: str) -> str
```

If `v` is non-empty, validates against `^[^@\s]+@[^@\s]+\.[^@\s]+$`. Returns `v.lower()`.

---

##### `coerce_amount(cls, v: Any) → Decimal` *(field_validator, mode="before")*

```python
@field_validator("amount", mode="before")
@classmethod
def coerce_amount(cls, v: Any) -> Decimal
```

Converts `v` (int, float, str) to `Decimal` quantized to 2 decimal places via `Decimal(str(v)).quantize(Decimal("0.01"))`. Raises `ValueError` on conversion failure.

---

##### `amount_cents → int` *(computed_field, property)*

```python
@computed_field
@property
def amount_cents(self) -> int
```

Returns `int(self.amount * 100)` — the **lossless integer representation** used by `PolicyEvaluatorWorker` for deterministic comparison. **No floating-point arithmetic.**

---

##### `payload_fingerprint → str` *(computed_field, property)*

```python
@computed_field
@property
def payload_fingerprint(self) -> str
```

Returns a SHA-256 hex digest of `f"{department}:{employee_id}:{amount_cents}:{created_at.date()}"`. Used for deduplication detection. Produces the same value for identical requests on the same calendar day.

---

### `PolicyLimit`

```python
class PolicyLimit(BaseModel)
```

A single departmental spending rule loaded from `policy_rules.json`.

**Fields:**

| Field | Type | Default | Description |
|-------|------|---------|-------------|
| `department` | `str` | required | Department name or `"*"` for global |
| `category` | `str` | `"*"` | Category or `"*"` for wildcard |
| `limit_usd` | `Decimal` | required | Maximum allowed amount |
| `currency` | `str` | `"USD"` | Currency of the limit |
| `escalation_threshold_usd` | `Decimal \| None` | `None` | Amount above which `requires_escalation=True` |
| `description` | `str` | `""` | Human-readable rule description |

---

##### `limit_cents → int` *(computed_field)*

```python
@computed_field
@property
def limit_cents(self) -> int
```

Returns `int(self.limit_usd * 100)` — integer limit for comparison with `amount_cents`.

---

##### `escalation_cents → int | None` *(computed_field)*

```python
@computed_field
@property
def escalation_cents(self) -> int | None
```

Returns `int(self.escalation_threshold_usd * 100)` if threshold is set, else `None`.

---

### `PolicyRuleSet`

```python
class PolicyRuleSet(BaseModel)
```

The full corporate policy ruleset loaded from `config/policy_rules.json`.

**Fields:**

| Field | Type | Default | Description |
|-------|------|---------|-------------|
| `version` | `str` | `"1.0.0"` | Ruleset version string |
| `default_limit_usd` | `Decimal` | `50.00` | Global fallback limit |
| `rules` | `list[PolicyLimit]` | `[]` | All explicit rules |

---

##### `resolve_limit(department, category) → PolicyLimit`

```python
def resolve_limit(self, department: str, category: str = "*") -> PolicyLimit
```

Resolves the applicable policy limit using a **3-tier priority lookup**:

1. **Exact match:** `department == dept_lower AND category == cat_lower`
2. **Department wildcard:** `department == dept_lower AND category == "*"`
3. **Global default:** Returns `PolicyLimit(department="*", category="*", limit_usd=default_limit_usd)`

Case-insensitive comparison on both `department` and `category`.

| Parameter | Type | Description |
|-----------|------|-------------|
| `department` | `str` | Department name from the expense payload |
| `category` | `str` | Category name from the expense payload |

**Returns:** The most specific matching `PolicyLimit`.

---

### `PolicyEvaluationResult`

```python
class PolicyEvaluationResult(TimestampedModel)
```

The output of `PolicyEvaluatorWorker.run()`. Pure data — no LLM generation.

**Fields:**

| Field | Type | Description |
|-------|------|-------------|
| `trace_id` | `UUID` | Correlation ID from the original request |
| `department` | `str` | Department from the payload |
| `amount_usd` | `Decimal` | Amount from the payload |
| `limit_usd` | `Decimal` | Resolved policy limit |
| `variance_usd` | `Decimal` | `max(0, amount - limit)` |
| `status` | `ComplianceStatus` | `APPROVED` or `FLAGGED` |
| `applied_rule` | `PolicyLimit` | The rule that was applied |
| `requires_escalation` | `bool` | `True` if amount exceeds escalation threshold |
| `evaluation_latency_ms` | `float` | Time taken for policy fetch + comparison |

---

##### `compute_derived_fields(self) → PolicyEvaluationResult` *(model_validator, mode="after")*

```python
@model_validator(mode="after")
def compute_derived_fields(self) -> "PolicyEvaluationResult"
```

Ensures `variance_usd` is never negative (clamps to `0.00`).

---

### `NotificationPayload`

```python
class NotificationPayload(BaseModel)
```

The structured message dispatched by `ResolutionCommunicator`.

| Field | Type | Default | Description |
|-------|------|---------|-------------|
| `trace_id` | `UUID` | required | Original request trace ID |
| `channel` | `NotificationChannel` | `SLACK` | Target platform |
| `recipient` | `str` | required | Slack user ID or Teams email |
| `subject` | `str` | required | Message subject/title |
| `body` | `str` | required | Message body text |
| `severity` | `str` | `"WARNING"` | `INFO`, `WARNING`, or `CRITICAL` |
| `expense_summary` | `dict[str, Any]` | `{}` | Key expense metrics |
| `sent_at` | `datetime` | `datetime.now(timezone.utc)` | Dispatch timestamp |

---

### `NotificationResult`

```python
class NotificationResult(BaseModel)
```

Result from a single notification dispatch attempt.

| Field | Type | Description |
|-------|------|-------------|
| `trace_id` | `UUID` | Original request trace ID |
| `channel` | `NotificationChannel` | Channel that was attempted |
| `success` | `bool` | Whether dispatch succeeded |
| `attempts` | `int` | Number of tries made |
| `response_code` | `int \| None` | HTTP response code from webhook |
| `error_detail` | `str \| None` | Error message if failed |

---

### `AuditEvent`

```python
class AuditEvent(BaseModel)
```

Immutable audit event. Written to the database for every pipeline state transition.

| Field | Type | Default | Description |
|-------|------|---------|-------------|
| `event_id` | `UUID` | `uuid4()` | Unique event identifier |
| `trace_id` | `UUID` | required | Links to the originating request |
| `event_type` | `AuditEventType` | required | Type of pipeline event |
| `agent_name` | `str` | required | Which agent emitted this event |
| `payload_snapshot` | `dict` | `{}` | Serialised subset of the request |
| `outcome` | `str` | `""` | Human-readable outcome |
| `error_detail` | `str \| None` | `None` | Error info if applicable |
| `duration_ms` | `float` | `0.0` | Processing time for this step |
| `timestamp` | `datetime` | `datetime.now(timezone.utc)` | UTC event timestamp |

---

### `ComplianceResponse`

```python
class ComplianceResponse(BaseModel)
```

Standard success envelope returned by `POST /submit-expense`.

| Field | Type | Description |
|-------|------|-------------|
| `trace_id` | `UUID` | Unique request identifier |
| `status` | `ComplianceStatus` | `APPROVED` or `FLAGGED` |
| `department` | `str` | Department from the request |
| `amount_usd` | `Decimal` | Amount from the request |
| `limit_usd` | `Decimal` | Applied policy limit |
| `variance_usd` | `Decimal` | Amount over limit (0 if approved) |
| `message` | `str` | Human-readable summary |
| `requires_escalation` | `bool` | Whether escalation is required |
| `notification_dispatched` | `bool` | Whether notification was sent |
| `processing_time_ms` | `float` | End-to-end processing time |

---

### `ErrorResponse`

```python
class ErrorResponse(BaseModel)
```

Standard error envelope for all non-2xx responses.

| Field | Type | Description |
|-------|------|-------------|
| `error` | `str` | Machine-readable error code (e.g. `DEPARTMENT_EMPTY`) |
| `message` | `str` | Human-readable error description |
| `http_status` | `int` | HTTP status code (401, 422, 503, 500) |
| `trace_id` | `str` | Correlation ID |
| `timestamp` | `str` | UTC ISO-8601 timestamp |
| `field_errors` | `list[dict]` | Per-field validation errors |
| `context` | `dict` | Additional error context |

---

## `core/exceptions.py`

**Path:** [`core/exceptions.py`](../core/exceptions.py)

All exceptions use `@dataclass` and inherit from `ComplianceEngineError`. Every class carries `error_code`, `http_status`, and `to_dict()` so exception handlers can produce deterministic JSON.

### Exception Hierarchy

```
ComplianceEngineError                     (base)
├── SecurityValidationError               (HTTP 422)
│   └── SessionKeyInvalidError            (HTTP 401)
├── PayloadValidationError                (HTTP 422)
│   └── DepartmentEmptyError             (HTTP 422)
├── MCPError                              (HTTP 503)
│   ├── MCPTimeoutError                  (HTTP 504)
│   ├── MCPDisconnectError               (HTTP 503)
│   └── MCPCircuitOpenError              (HTTP 503)
├── PolicyLoadError                       (HTTP 500)
├── PolicyEvaluationError                 (HTTP 500)
├── WebhookDispatchError                  (HTTP 502)
└── AgentInitializationError              (HTTP 500)
```

---

### `ComplianceEngineError`

```python
@dataclass
class ComplianceEngineError(Exception)
```

Root exception. Every domain exception inherits from this.

**Fields:**

| Field | Type | Default | Description |
|-------|------|---------|-------------|
| `message` | `str` | required | Human-readable description |
| `error_code` | `str` | `"COMPLIANCE_ENGINE_ERROR"` | Machine-readable code |
| `correlation_id` | `str` | `str(uuid4())` | Request correlation ID |
| `timestamp` | `str` | `datetime.now(utc).isoformat()` | Error timestamp |
| `context` | `dict[str, Any]` | `{}` | Extensible error context |

---

##### `__post_init__(self) → None`

Calls `super().__init__(self.message)` to satisfy Python's `Exception` constructor.

---

##### `to_dict(self) → dict[str, Any]`

```python
def to_dict(self) -> dict[str, Any]
```

Returns:
```json
{
  "error": "COMPLIANCE_ENGINE_ERROR",
  "message": "...",
  "correlation_id": "...",
  "timestamp": "...",
  "context": {}
}
```

---

### Security Exceptions

| Class | `error_code` | `http_status` | Extra Fields |
|-------|-------------|---------------|-------------|
| `SecurityValidationError` | `SECURITY_VALIDATION_FAILED` | `422` | — |
| `SessionKeyInvalidError` | `SESSION_KEY_INVALID` | `401` | — |
| `DepartmentEmptyError` | `DEPARTMENT_EMPTY` | `422` | `field_errors: list[dict]` |
| `PayloadValidationError` | `PAYLOAD_VALIDATION_FAILED` | `422` | `field_errors: list[dict]` |

`PayloadValidationError.to_dict()` adds `"http_status"` and `"field_errors"` to the base dict.

---

### MCP Exceptions

| Class | `error_code` | `http_status` | Extra Fields |
|-------|-------------|---------------|-------------|
| `MCPError` | `MCP_ERROR` | `503` | — |
| `MCPTimeoutError` | `MCP_TIMEOUT` | `504` | `timeout_seconds: float` |
| `MCPDisconnectError` | `MCP_DISCONNECTED` | `503` | — |
| `MCPCircuitOpenError` | `MCP_CIRCUIT_OPEN` | `503` | `retry_after_seconds: int` |

`MCPCircuitOpenError.to_dict()` adds `"retry_after_seconds"` to enable clients to implement retry logic.

---

### Other Exceptions

| Class | `error_code` | `http_status` | Extra Fields |
|-------|-------------|---------------|-------------|
| `PolicyLoadError` | `POLICY_LOAD_FAILED` | `500` | — |
| `PolicyEvaluationError` | `POLICY_EVALUATION_FAILED` | `500` | — |
| `WebhookDispatchError` | `WEBHOOK_DISPATCH_FAILED` | `502` | `webhook_target: str`, `attempts: int` |
| `AgentInitializationError` | `AGENT_INIT_FAILED` | `500` | `agent_name: str` |

---

## `core/security.py`

**Path:** [`core/security.py`](../core/security.py)

The runtime security validation layer. Covers process startup, HMAC session keys, and combined payload security gates.

---

### `enforce_enterprise_secret() → str`

```python
def enforce_enterprise_secret() -> str
```

**Phase 1 Security Hook** — the very first function called by `main.py` before any imports or agent creation. Reads `ENTERPRISE_AGENT_SECRET` from the environment and **aborts the process** with `sys.exit(1)` if:

| Condition | Abort Reason |
|-----------|-------------|
| Env var not set | `"absent"` |
| Value is blank / whitespace-only | `"blank"` |
| Value length < 16 characters | `"too_short"` |

**Returns:** The validated secret string (stripped of whitespace).

---

### `_fatal_abort(reason, detail) → None` *(private)*

```python
def _fatal_abort(reason: str, detail: str) -> None
```

Called by `enforce_enterprise_secret()` on failure. Writes a `CRITICAL` log entry and prints directly to `stderr` (in case logging isn't yet wired), then calls `sys.exit(1)`.

| Parameter | Type | Description |
|-----------|------|-------------|
| `reason` | `str` | Short machine-readable reason (`"absent"`, `"blank"`, `"too_short"`) |
| `detail` | `str` | Full human-readable description |

---

### `_get_hmac_secret() → bytes` *(private, cached)*

```python
@lru_cache(maxsize=1)
def _get_hmac_secret() -> bytes
```

Reads `SESSION_HMAC_SECRET` from the environment, encodes to bytes. Falls back to `ENTERPRISE_AGENT_SECRET` if `SESSION_HMAC_SECRET` is not set (logs a warning). Result is **cached** after the first call — the secret is never re-read from the environment at request time.

**Returns:** `bytes` — the HMAC signing key.

---

### `generate_session_key(employee_id, issued_at) → str`

```python
def generate_session_key(employee_id: str, issued_at: int | None = None) -> str
```

Creates a time-bound HMAC-SHA256 session key.

**Algorithm:**
```python
ts      = issued_at or int(time.time())
message = f"{employee_id}:{ts}".encode("utf-8")
sig     = hmac.new(_get_hmac_secret(), message, sha256).hexdigest()
return  f"{ts}.{sig}"
```

**Key format:** `<unix_timestamp>.<64-hex-char HMAC-SHA256>`

| Parameter | Type | Description |
|-----------|------|-------------|
| `employee_id` | `str` | The employee ID to bind the key to |
| `issued_at` | `int \| None` | Unix timestamp override (for testing) |

**Returns:** The session key string.

---

### `verify_session_key(session_key, employee_id, correlation_id) → bool`

```python
def verify_session_key(
    session_key: str,
    employee_id: str,
    correlation_id: str = "",
) -> bool
```

Verifies a session key. Raises `SessionKeyInvalidError` on any failure condition:

| Failure | Error Message |
|---------|-------------|
| Blank key | `"Session key is absent or blank."` |
| No dot separator | `"Session key has invalid format"` |
| Non-integer timestamp | `"Session key timestamp is not a valid integer."` |
| `age > TTL` or `age < 0` | `"Session key expired (age=Xs, ttl=Ys)."` |
| HMAC mismatch | `"Session key signature does not match — possible tampering."` |

Uses **`hmac.compare_digest()`** for constant-time comparison to prevent timing attacks.

**Returns:** `True` if valid (never returns `False` — always raises on invalid).

---

### `validate_inbound_payload_security(department, session_key, employee_id, correlation_id) → None`

```python
def validate_inbound_payload_security(
    department: str,
    session_key: str,
    employee_id: str,
    correlation_id: str = "",
) -> None
```

Combined security gate called by `ExpenseAuditorAgent` at Gate 1. Runs two checks in order:

1. Checks `department` is non-empty and non-whitespace → raises `DepartmentEmptyError` if not
2. Calls `verify_session_key(session_key, employee_id, correlation_id)` → raises `SessionKeyInvalidError` if not valid

| Parameter | Type | Description |
|-----------|------|-------------|
| `department` | `str` | Department from `ExpensePayload` |
| `session_key` | `str` | Session key from `ExpensePayload` |
| `employee_id` | `str` | Employee ID from `ExpensePayload` |
| `correlation_id` | `str` | Request correlation ID for audit logs |

**Returns:** `None` — side-effect only. Raises on failure.

---

## `core/circuit_breaker.py`

**Path:** [`core/circuit_breaker.py`](../core/circuit_breaker.py)

An async-compatible circuit breaker protecting all MCP JSON-RPC calls. Uses `asyncio.Lock` for thread-safe state transitions.

---

### `CircuitBreaker`

```python
@dataclass
class CircuitBreaker
```

#### Constructor Parameters

| Parameter | Type | Default | Description |
|-----------|------|---------|-------------|
| `name` | `str` | required | Identifier for logging (e.g. `"mcp-bridge"`) |
| `failure_threshold` | `int` | `5` | Consecutive failures before CLOSED → OPEN |
| `recovery_timeout_seconds` | `float` | `30.0` | Seconds before OPEN → HALF_OPEN |
| `success_threshold_in_half_open` | `int` | `2` | Successes in HALF_OPEN before → CLOSED |

---

#### `state → CircuitState` *(property)*

```python
@property
def state(self) -> CircuitState
```

Returns the current circuit state: `CLOSED`, `OPEN`, or `HALF_OPEN`.

---

#### `failure_count → int` *(property)*

```python
@property
def failure_count(self) -> int
```

Returns the current consecutive failure count.

---

#### `call(fn, *args, **kwargs) → T` *(async)*

```python
async def call(
    self,
    fn: Callable[..., Awaitable[T]],
    *args: object,
    **kwargs: object,
) -> T
```

Executes `fn(*args, **kwargs)` through the circuit. 

**Before calling `fn`:**
- Calls `_maybe_transition_to_half_open()` (OPEN → HALF_OPEN if timeout elapsed)
- If still OPEN: raises `MCPCircuitOpenError` immediately (fast-fail)

**After `fn` returns:** Calls `_on_success()`  
**If `fn` raises `MCPError`:** Calls `_on_failure(exc)` then re-raises

| Parameter | Type | Description |
|-----------|------|-------------|
| `fn` | `Callable[..., Awaitable[T]]` | The async function to wrap |
| `*args` / `**kwargs` | `Any` | Forwarded to `fn` |

**Returns:** Whatever `fn` returns.  
**Raises:** `MCPCircuitOpenError` (when OPEN), or any exception raised by `fn`.

---

#### `reset() → None` *(async)*

```python
async def reset(self) -> None
```

Force-transitions the circuit to `CLOSED` and resets all counters. Called by `POST /admin/circuit-reset`.

---

#### `snapshot() → dict`

```python
def snapshot(self) -> dict
```

Returns a JSON-serialisable health snapshot:
```json
{
  "circuit": "mcp-bridge",
  "state": "CLOSED",
  "failure_count": 0,
  "failure_threshold": 5,
  "recovery_timeout_seconds": 30.0,
  "last_failure_at": null
}
```

---

#### `_maybe_transition_to_half_open() → None` *(async, internal)*

Checks if the OPEN → HALF_OPEN recovery timeout has elapsed. If yes, transitions state under `asyncio.Lock`.

---

#### `_on_success() → None` *(async, internal)*

- In `HALF_OPEN`: increments `_success_count_in_half_open`. If it reaches `success_threshold_in_half_open`, transitions to `CLOSED`.
- In `CLOSED`: decrements `_failure_count` by 1 (decay on consecutive successes).

---

#### `_on_failure(exc: MCPError) → None` *(async, internal)*

Increments `_failure_count`. Records `_last_failure_time`. If `_failure_count >= failure_threshold`, transitions to `OPEN`.

---

## `core/audit_trail.py`

**Path:** [`core/audit_trail.py`](../core/audit_trail.py)

Append-only event log using SQLAlchemy 2.0 async ORM. Events are **never updated or deleted**.

---

### `AuditEventRecord`

```python
class AuditEventRecord(Base)
```

SQLAlchemy ORM row. Columns mirror the `AuditEvent` Pydantic model.

| Column | SQLAlchemy Type | Index | Description |
|--------|----------------|-------|-------------|
| `event_id` | `String(36)` | PK | UUID string |
| `trace_id` | `String(36)` | Yes | Correlation ID |
| `event_type` | `String(64)` | Yes | Event type |
| `agent_name` | `String(128)` | No | Agent that emitted the event |
| `payload_snapshot` | `Text` | No | JSON-encoded snapshot |
| `outcome` | `String(256)` | No | Human-readable outcome |
| `error_detail` | `Text` | No | Error information |
| `duration_ms` | `Float` | No | Processing time |
| `timestamp` | `DateTime(timezone=True)` | Yes | UTC event time |

---

### `AuditTrailService`

```python
class AuditTrailService
```

The service layer for interacting with the audit event database.

---

#### `__init__(db_url: str) → None`

```python
def __init__(self, db_url: str) -> None
```

Creates an async SQLAlchemy engine and session factory. Does **not** create tables — call `initialize()` first.

| Parameter | Type | Description |
|-----------|------|-------------|
| `db_url` | `str` | Async SQLAlchemy connection string (e.g. `sqlite+aiosqlite:///audit.db`) |

---

#### `initialize() → None` *(async)*

```python
async def initialize(self) -> None
```

Creates all database tables if they do not exist (`CREATE TABLE IF NOT EXISTS`). Called once during `CompliancePipeline.startup()`.

---

#### `record(event: AuditEvent) → None` *(async)*

```python
async def record(self, event: AuditEvent) -> None
```

Inserts one `AuditEventRecord` into the database. **Never raises** — any database error is caught, logged, and swallowed so audit failures cannot crash the compliance pipeline.

| Parameter | Type | Description |
|-----------|------|-------------|
| `event` | `AuditEvent` | The Pydantic event model to persist |

---

#### `query_by_trace(trace_id: UUID) → list[dict]` *(async)*

```python
async def query_by_trace(self, trace_id: UUID) -> list[dict]
```

Returns all audit events for a given trace ID, ordered by `timestamp` ascending.

| Parameter | Type | Description |
|-----------|------|-------------|
| `trace_id` | `UUID` | The correlation UUID to query |

**Returns:** `list[dict]` — each dict contains `event_id`, `trace_id`, `event_type`, `agent_name`, `outcome`, `duration_ms`, `timestamp`.

---

#### `shutdown() → None` *(async)*

```python
async def shutdown(self) -> None
```

Disposes the SQLAlchemy engine connection pool. Called during `CompliancePipeline.shutdown()`.
