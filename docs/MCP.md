# MCP.md — Model Context Protocol Layer Reference

> **Location:** `mcp/`  
> **Protocol:** JSON-RPC 2.0 over HTTP  
> **Pattern:** Client → Stub Server (dev) or Enterprise DB Bridge (prod)

---

## Table of Contents

1. [Overview](#overview)
2. [mcp/client.py — MCPClient](#mcpclientpy)
3. [mcp/tools.py — Typed Tool Bindings](#mcptoolspy)
4. [mcp/server.py — Stub Server](#mcpserverpy)
5. [JSON-RPC Protocol](#json-rpc-protocol)
6. [Tool Definitions](#tool-definitions)

---

## Overview

The MCP (Model Context Protocol) layer abstracts communication between the compliance agents and the enterprise database bridge. In development, `mcp/server.py` acts as a local stub that returns policy data from `config/policy_rules.json`. In production, replace the `MCP_SERVER_URL` env var to point at your real bridge.

```
PolicyEvaluatorWorker
  └─ MCPClient.call_tool("fetch_corporate_policy", {department, category})
       └─ CircuitBreaker.call(MCPClient._raw_call, ...)
            └─ POST {MCP_SERVER_URL}/rpc  (JSON-RPC 2.0)
                 └─ MCP Stub Server (or enterprise bridge)
                      └─ Returns: {limit_usd, escalation_threshold_usd, ...}
```

---

## `mcp/client.py`

**Path:** [`mcp/client.py`](../mcp/client.py)

The async JSON-RPC 2.0 client. All MCP calls are routed through the circuit breaker. Retries use `tenacity` with exponential backoff.

---

### `MCPClient`

```python
class MCPClient
```

#### Constructor

```python
def __init__(
    self,
    base_url: str,
    timeout_seconds: float = 10.0,
    max_retries: int = 3,
    circuit_breaker: CircuitBreaker | None = None,
) -> None
```

| Parameter | Type | Default | Description |
|-----------|------|---------|-------------|
| `base_url` | `str` | required | MCP server base URL (e.g. `http://localhost:9000`) |
| `timeout_seconds` | `float` | `10.0` | Per-call HTTP timeout |
| `max_retries` | `int` | `3` | Maximum retry attempts on retriable errors |
| `circuit_breaker` | `CircuitBreaker \| None` | `None` | Injected circuit breaker; auto-created from env if `None` |

If `circuit_breaker` is `None`, one is created using env vars:
- `failure_threshold` ← `MCP_CIRCUIT_BREAKER_THRESHOLD` (default: `5`)
- `recovery_timeout_seconds` ← `MCP_CIRCUIT_RECOVERY_SECONDS` (default: `30`)

---

#### `from_config(config_path) → MCPClient` *(classmethod)*

```python
@classmethod
def from_config(cls, config_path: str = "config/mcp_config.json") -> "MCPClient"
```

Factory method that reads `mcp_config.json` and constructs an `MCPClient` with the settings from the config file. Used by `CompliancePipeline.startup()`.

| Parameter | Type | Description |
|-----------|------|-------------|
| `config_path` | `str` | Path to the MCP client configuration JSON |

**Returns:** Configured `MCPClient` instance.

---

#### `connect() → None` *(async)*

```python
async def connect(self) -> None
```

Creates an `httpx.AsyncClient` session with configured timeouts and headers. Performs an initial `health_check()` to verify the MCP server is reachable. Called during `CompliancePipeline.startup()`.

**Raises:** `MCPDisconnectError` if the server is unreachable.

---

#### `disconnect() → None` *(async)*

```python
async def disconnect(self) -> None
```

Closes the `httpx.AsyncClient` session. Called during `CompliancePipeline.shutdown()`.

---

#### `call_tool(tool_name, arguments, correlation_id) → dict[str, Any]` *(async)*

```python
async def call_tool(
    self,
    tool_name: str,
    arguments: dict[str, Any],
    correlation_id: str | None = None,
) -> dict[str, Any]
```

The **primary public API** for calling MCP tools. Routes the call through the circuit breaker and retry logic.

| Parameter | Type | Description |
|-----------|------|-------------|
| `tool_name` | `str` | Name of the MCP tool (e.g. `"fetch_corporate_policy"`) |
| `arguments` | `dict[str, Any]` | Tool arguments |
| `correlation_id` | `str \| None` | Correlation ID for logging |

**Returns:** `dict[str, Any]` — the `"result"` field from the JSON-RPC response.  
**Raises:** `MCPTimeoutError`, `MCPDisconnectError`, `MCPCircuitOpenError`

---

#### `_call_with_retry(tool_name, arguments, correlation_id) → dict[str, Any]` *(async, internal)*

```python
async def _call_with_retry(
    self,
    tool_name: str,
    arguments: dict[str, Any],
    correlation_id: str,
) -> dict[str, Any]
```

Wraps `_raw_call()` with `tenacity` retry logic:
- `stop_after_attempt(max_retries)`
- `wait_exponential(multiplier=1, min=1, max=8)`
- Retries on `MCPTimeoutError` and `MCPDisconnectError`
- Does **not** retry on `MCPCircuitOpenError`

---

#### `_raw_call(tool_name, arguments, correlation_id) → dict[str, Any]` *(async, internal)*

```python
async def _raw_call(
    self,
    tool_name: str,
    arguments: dict[str, Any],
    correlation_id: str,
) -> dict[str, Any]
```

Performs the actual HTTP POST with a JSON-RPC 2.0 envelope:

```json
{
  "jsonrpc": "2.0",
  "id": "<uuid>",
  "method": "tools/call",
  "params": {
    "name": "<tool_name>",
    "arguments": {...},
    "correlation_id": "<correlation_id>"
  }
}
```

Translates HTTP/network errors into typed `MCPError` exceptions:
- `httpx.TimeoutException` → `MCPTimeoutError`
- `httpx.ConnectError` → `MCPDisconnectError`
- JSON-RPC `"error"` field → `MCPError` with the RPC error message

---

#### `health_check() → dict[str, Any]` *(async)*

```python
async def health_check(self) -> dict[str, Any]
```

Performs `GET /health` on the MCP server. Returns the health response dict. Raises `MCPDisconnectError` if unreachable.

---

#### `circuit_state → dict[str, Any]` *(property)*

```python
@property
def circuit_state(self) -> dict[str, Any]
```

Returns the circuit breaker's `snapshot()` dict. Exposed via `GET /circuit-state`.

---

### `_redact(obj: dict) → dict` *(module-level, private)*

```python
def _redact(obj: dict) -> dict
```

Returns a copy of `obj` with sensitive keys (`"session_key"`, `"password"`, `"token"`, `"secret"`) replaced with `"[REDACTED]"`. Used before logging request arguments to prevent secret leakage in log files.

---

## `mcp/tools.py`

**Path:** [`mcp/tools.py`](../mcp/tools.py)

Typed wrapper functions that call `MCPClient.call_tool()` and deserialise the response into domain models.

---

### `fetch_corporate_policy(client, department, category, correlation_id) → PolicyLimit` *(async)*

```python
async def fetch_corporate_policy(
    client: "MCPClient",
    department: str,
    category: str = "*",
    correlation_id: str = "",
) -> PolicyLimit
```

Fetches the corporate spending policy limit for a given department and category via MCP. Deserialises the result into a `PolicyLimit` Pydantic model.

**On `MCPError`:** Falls back to a default `PolicyLimit` with `limit_usd=50.00` and logs a warning. This ensures policy evaluation never completely blocks on MCP failures.

| Parameter | Type | Description |
|-----------|------|-------------|
| `client` | `MCPClient` | The MCP client instance |
| `department` | `str` | Department name to look up |
| `category` | `str` | Category to look up (default `"*"` for wildcard) |
| `correlation_id` | `str` | Correlation ID for log correlation |

**Returns:** `PolicyLimit` — the resolved spending limit.

---

### `write_audit_event_remote(client, trace_id, event_type, agent_name, outcome, payload, correlation_id) → bool` *(async)*

```python
async def write_audit_event_remote(
    client: "MCPClient",
    trace_id: str,
    event_type: str,
    agent_name: str,
    outcome: str,
    payload: dict | None = None,
    correlation_id: str = "",
) -> bool
```

Writes an audit event to the remote MCP bridge's event store (in addition to the local SQLite audit trail). Returns `True` on success, `False` on any `MCPError` (non-fatal).

| Parameter | Type | Description |
|-----------|------|-------------|
| `client` | `MCPClient` | The MCP client instance |
| `trace_id` | `str` | Correlation UUID string |
| `event_type` | `str` | `AuditEventType` value string |
| `agent_name` | `str` | Name of the emitting agent |
| `outcome` | `str` | Human-readable outcome |
| `payload` | `dict \| None` | Optional payload snapshot |
| `correlation_id` | `str` | Correlation ID for log correlation |

**Returns:** `bool` — `True` if the remote write succeeded.

---

### `fetch_employee_profile(client, employee_id, correlation_id) → dict` *(async)*

```python
async def fetch_employee_profile(
    client: "MCPClient",
    employee_id: str,
    correlation_id: str = "",
) -> dict
```

Fetches an employee profile from the enterprise database bridge via MCP. Returns a safe fallback dict `{"employee_id": employee_id, "found": False}` on any `MCPError`.

| Parameter | Type | Description |
|-----------|------|-------------|
| `client` | `MCPClient` | The MCP client instance |
| `employee_id` | `str` | The employee ID to look up |
| `correlation_id` | `str` | Correlation ID |

**Returns:** Employee profile dict or fallback.

---

## `mcp/server.py`

**Path:** [`mcp/server.py`](../mcp/server.py)

A local FastAPI stub server that simulates the enterprise MCP database bridge. Used in development and tests. Serves requests on `:9000`.

Start with: `uvicorn mcp.server:app --port 9000`

---

### `_load_policy_rules() → dict` *(module-level)*

```python
def _load_policy_rules() -> dict
```

Loads `config/policy_rules.json` at server startup. Returns the parsed dict. Falls back to `{"rules": [], "default_limit_usd": 50.0}` if the file is missing.

---

### `health() → JSONResponse` *(async, GET /health)*

```python
@app.get("/health")
async def health() -> JSONResponse
```

Returns `{"status": "healthy", "server": "mcp-stub", "timestamp": "..."}`. Used by `MCPClient.connect()` to verify reachability.

---

### `rpc_dispatch(request: Request) → JSONResponse` *(async, POST /rpc)*

```python
@app.post("/rpc")
async def rpc_dispatch(request: Request) -> JSONResponse
```

Main JSON-RPC 2.0 dispatcher. Reads the request body, validates the `jsonrpc` and `method` fields, and dispatches to the appropriate handler:

| Method | Handler |
|--------|---------|
| `tools/call` with `name="fetch_corporate_policy"` | `_handle_fetch_corporate_policy` |
| `tools/call` with `name="write_audit_event"` | `_handle_write_audit_event` |
| `tools/call` with `name="fetch_employee_profile"` | `_handle_fetch_employee_profile` |

Returns `_rpc_error(id, -32601, "Method not found")` for unknown methods.

---

### `_handle_fetch_corporate_policy(args: dict) → dict[str, Any]` *(async, internal)*

```python
async def _handle_fetch_corporate_policy(args: dict) -> dict[str, Any]
```

Resolves the policy limit for `args["department"]` and `args["category"]` against the loaded ruleset using the same 3-tier priority logic as `PolicyRuleSet.resolve_limit()`.

**Returns:** `{"policy": {limit_usd, escalation_threshold_usd, department, category, description}}`

---

### `_format_rule(rule: dict) → dict[str, Any]`

```python
def _format_rule(rule: dict) -> dict[str, Any]
```

Converts a raw policy rule dict from JSON into the wire format expected by `MCPClient`: includes `limit_usd`, `escalation_threshold_usd`, `department`, `category`, `description`.

---

### `_handle_write_audit_event(args: dict) → dict[str, Any]` *(async, internal)*

Appends the audit event to an in-memory list `_audit_log`. Returns `{"written": True}`. The in-memory log is accessible at `GET /audit-log`.

---

### `_handle_fetch_employee_profile(args: dict) → dict[str, Any]` *(async, internal)*

Returns a stub employee profile for development:
```json
{
  "employee_id": "...",
  "name": "Test Employee",
  "department": "...",
  "cost_centre": "CC-0000",
  "manager": "manager@nexusscale.io",
  "found": true
}
```

---

### `_rpc_error(rpc_id, code, message) → JSONResponse`

```python
def _rpc_error(rpc_id: str, code: int, message: str) -> JSONResponse
```

Builds a JSON-RPC 2.0 error response:
```json
{
  "jsonrpc": "2.0",
  "id": "<rpc_id>",
  "error": {"code": -32601, "message": "Method not found"}
}
```

---

### `_async_sleep(seconds: float) → None` *(async)*

Helper used by the `MCP_STUB_SIMULATE=timeout` mode to simulate bridge latency.

---

### `get_audit_log() → JSONResponse` *(async, GET /audit-log)*

```python
@app.get("/audit-log")
async def get_audit_log() -> JSONResponse
```

Returns all events written via `_handle_write_audit_event`. Useful for debugging.

---

## JSON-RPC Protocol

### Request Format

```json
{
  "jsonrpc": "2.0",
  "id":      "<uuid-string>",
  "method":  "tools/call",
  "params": {
    "name":           "<tool_name>",
    "arguments":      {...},
    "correlation_id": "<string>"
  }
}
```

### Success Response

```json
{
  "jsonrpc": "2.0",
  "id":      "<uuid-string>",
  "result":  {...}
}
```

### Error Response

```json
{
  "jsonrpc": "2.0",
  "id":      "<uuid-string>",
  "error": {
    "code":    -32601,
    "message": "Method not found"
  }
}
```

---

## Tool Definitions

### `fetch_corporate_policy`

```
Input:
  department: string  — department name
  category:   string  — expense category (default: "*")

Output:
  policy:
    department:                string
    category:                  string
    limit_usd:                 number
    escalation_threshold_usd:  number | null
    description:               string
```

### `write_audit_event`

```
Input:
  trace_id:         string
  event_type:       string
  agent_name:       string
  outcome:          string
  payload_snapshot: object (optional)

Output:
  written: boolean
```

### `fetch_employee_profile`

```
Input:
  employee_id: string

Output:
  employee_id:  string
  name:         string
  department:   string
  cost_centre:  string
  manager:      string
  found:        boolean
```
