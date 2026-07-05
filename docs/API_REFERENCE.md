# API_REFERENCE.md — Complete HTTP & WebSocket API Reference

> **Base URL:** `http://localhost:8000`  
> **Interactive Docs:** `http://localhost:8000/docs` (Swagger UI)  
> **Alt Docs:** `http://localhost:8000/redoc`

---

## Table of Contents

1. [Authentication](#authentication)
2. [Common Headers](#common-headers)
3. [Compliance Endpoints](#compliance-endpoints)
   - [POST /submit-expense](#post-submit-expense)
4. [Operations Endpoints](#operations-endpoints)
   - [GET /health](#get-health)
   - [GET /stats](#get-stats)
   - [GET /metrics](#get-metrics)
   - [GET /circuit-state](#get-circuit-state)
5. [Audit Endpoints](#audit-endpoints)
   - [GET /audit/{trace_id}](#get-audittrace_id)
   - [GET /audit/recent](#get-auditrecent)
6. [Policy Endpoints](#policy-endpoints)
   - [GET /policy-rules](#get-policy-rules)
7. [Security Endpoints](#security-endpoints)
   - [GET /generate-session-key](#get-generate-session-key)
8. [Admin Endpoints](#admin-endpoints)
   - [POST /admin/circuit-reset](#post-admincircuit-reset)
9. [GUI & Docs Endpoints](#gui--docs-endpoints)
   - [GET /](#get-)
   - [GET /docs](#get-docs)
10. [WebSocket Endpoints](#websocket-endpoints)
    - [WS /ws/logs](#ws-wslogs)
11. [Error Codes Reference](#error-codes-reference)
12. [HTTP Status Code Guide](#http-status-code-guide)

---

## Authentication

Every request to `POST /submit-expense` requires a valid session key in the request body.

**Session key format:** `<unix_timestamp>.<hmac_sha256_hex>`

Generate one:
```bash
# Via the API (when server is running)
curl "http://localhost:8000/generate-session-key?employee_id=ENG-001"

# Via the CLI launcher
python start.py --genkey ENG-001

# Programmatically (Python)
from core.security import generate_session_key
key = generate_session_key("ENG-001")
```

---

## Common Headers

### Request Headers

| Header | Required | Description |
|--------|:--------:|-------------|
| `Content-Type` | ✅ (POST) | `application/json` |
| `X-Correlation-ID` | — | Client-supplied correlation ID. Auto-generated if absent. |

### Response Headers

| Header | Always Present | Description |
|--------|:--------------:|-------------|
| `X-Correlation-ID` | ✅ | The correlation ID for this request |
| `X-Processing-Time-Ms` | ✅ | Middleware-measured processing time |
| `Content-Type` | ✅ | `application/json` |
| `Retry-After` | 503 only | Seconds before retrying (circuit breaker) |

---

## Compliance Endpoints

### `POST /submit-expense`

**Tags:** Compliance  
**Summary:** Submit an expense payload for compliance evaluation through the full agent pipeline.

#### Pipeline Executed

```
1. Pydantic schema validation         → 422 on failure
2. HMAC session key verification      → 401 on failure
3. Department empty check             → 422 on failure
4. MCP policy fetch (with fallback)   → 503 on MCP failure
5. Integer cents comparison           → APPROVED | FLAGGED
6. Conditional notification dispatch  → (FLAGGED only)
7. Audit trail write                  → (non-fatal)
```

#### Request Body

```json
{
  "department":     "Engineering",
  "amount":         42.50,
  "category":       "meals",
  "employee_id":    "ENG-001",
  "employee_email": "alice@nexusscale.io",
  "session_key":    "1720000000.abcdef1234...",
  "currency":       "USD",
  "description":    "Team lunch"
}
```

| Field | Type | Required | Constraints | Description |
|-------|------|:--------:|-------------|-------------|
| `department` | `string` | ✅ | `min_length=1`, `max_length=128` | Submitting department |
| `amount` | `number` | ✅ | `gt=0`, `lt=1000000`, `decimal_places=2` | Expense amount |
| `category` | `string` | — | One of `ExpenseCategory` values | Default: `miscellaneous` |
| `employee_id` | `string` | — | `min_length=3`, `max_length=64` | Default: `UNKNOWN` |
| `employee_email` | `string` | — | Valid email format if provided | Submitter email |
| `session_key` | `string` | ✅ | `min_length=1`, HMAC format | HMAC-signed session key |
| `currency` | `string` | — | `^[A-Z]{3}$` | Default: `USD` |
| `description` | `string` | — | `max_length=512` | Expense description |

#### Responses

**`200 OK` — APPROVED**
```json
{
  "trace_id":               "a3f2c1d4-...",
  "status":                 "APPROVED",
  "department":             "Engineering",
  "amount_usd":             42.50,
  "limit_usd":              50.00,
  "variance_usd":           0.00,
  "message":                "Expense of $42.50 from Engineering is within the $50.00 policy limit. APPROVED.",
  "requires_escalation":    false,
  "notification_dispatched":false,
  "processing_time_ms":     12.5
}
```

**`200 OK` — FLAGGED**
```json
{
  "trace_id":               "b7e9d2f1-...",
  "status":                 "FLAGGED",
  "department":             "Marketing",
  "amount_usd":             120.00,
  "limit_usd":              50.00,
  "variance_usd":           70.00,
  "message":                "Expense of $120.00 exceeds the $50.00 Marketing limit by $70.00. FLAGGED.",
  "requires_escalation":    false,
  "notification_dispatched":true,
  "processing_time_ms":     48.3
}
```

**`401 Unauthorized` — Session Key Invalid**
```json
{
  "error":          "SESSION_KEY_INVALID",
  "message":        "Session key signature does not match — possible tampering.",
  "http_status":    401,
  "trace_id":       "...",
  "timestamp":      "2026-07-06T...",
  "field_errors":   [],
  "context":        {"employee_id": "ENG-001"}
}
```

**`422 Unprocessable Entity` — Department Empty**
```json
{
  "error":          "DEPARTMENT_EMPTY",
  "message":        "The 'department' field is required and cannot be blank.",
  "http_status":    422,
  "trace_id":       "...",
  "timestamp":      "2026-07-06T...",
  "field_errors":   [{"field": "department", "issue": "blank_or_empty"}],
  "context":        {}
}
```

**`422 Unprocessable Entity` — Schema Invalid**
```json
{
  "error":          "PAYLOAD_SCHEMA_INVALID",
  "message":        "Request payload failed schema validation.",
  "http_status":    422,
  "trace_id":       "...",
  "timestamp":      "2026-07-06T...",
  "field_errors":   [{"type": "greater_than", "loc": ["amount"], "msg": "Input should be greater than 0"}]
}
```

**`503 Service Unavailable` — MCP Circuit Open**
```json
{
  "error":                "MCP_CIRCUIT_OPEN",
  "message":              "Circuit breaker 'mcp-bridge' is OPEN — MCP bridge is unavailable.",
  "http_status":          503,
  "trace_id":             "...",
  "timestamp":            "2026-07-06T...",
  "retry_after_seconds":  28
}
```

#### cURL Examples

```bash
# Step 1: Generate a session key
SESSION_KEY=$(curl -s "http://localhost:8000/generate-session-key?employee_id=ENG-001" | python -c "import sys,json; print(json.load(sys.stdin)['session_key'])")

# Step 2: Submit expense (Test Case A)
curl -s -X POST http://localhost:8000/submit-expense \
  -H "Content-Type: application/json" \
  -d "{
    \"department\": \"Engineering\",
    \"amount\": 42.50,
    \"category\": \"meals\",
    \"employee_id\": \"ENG-001\",
    \"employee_email\": \"alice@nexusscale.io\",
    \"session_key\": \"$SESSION_KEY\"
  }" | python -m json.tool

# Test Case B (FLAGGED)
curl -s -X POST http://localhost:8000/submit-expense \
  -H "Content-Type: application/json" \
  -d "{\"department\":\"Marketing\",\"amount\":120.00,\"employee_id\":\"MKT-002\",\"session_key\":\"$SESSION_KEY\"}"

# Test Case C (422)
curl -s -X POST http://localhost:8000/submit-expense \
  -H "Content-Type: application/json" \
  -d "{\"department\":\"\",\"amount\":75.00,\"session_key\":\"\"}"
```

---

## Operations Endpoints

### `GET /health`

**Tags:** Operations  
**Summary:** Returns a full system health snapshot.

#### Response `200 OK`

```json
{
  "status":       "healthy",
  "timestamp":    "2026-07-06T...",
  "version":      "1.0.0",
  "environment":  "development",
  "pipeline": {
    "initialized":  true,
    "mcp_available": true,
    "mcp_circuit":  {"state": "CLOSED", "failure_count": 0}
  },
  "agents": {
    "ExpenseAuditorAgent":    {"agent": "ExpenseAuditorAgent", "run_count": 10, "error_count": 0, "avg_latency_ms": 14.2},
    "PolicyEvaluatorWorker":  {"agent": "PolicyEvaluatorWorker", "run_count": 10, "error_count": 0, "avg_latency_ms": 8.6},
    "ResolutionCommunicator": {"agent": "ResolutionCommunicator", "run_count": 3, "error_count": 0, "avg_latency_ms": 120.4}
  }
}
```

---

### `GET /stats`

**Tags:** Operations  
**Summary:** Live aggregate session statistics. Polled every 4 seconds by the GUI dashboard.

#### Response `200 OK`

```json
{
  "total_requests":       42,
  "approved":             38,
  "flagged":               3,
  "errors":                1,
  "avg_processing_ms":   16.4,
  "approval_rate_pct":   90.5,
  "started_at":          "2026-07-06T...",
  "recent_requests": [
    {
      "trace_id":              "a3f2c1d4-...",
      "department":            "Engineering",
      "amount":                42.50,
      "limit":                 50.00,
      "variance":               0.00,
      "status":                "APPROVED",
      "http_status":           200,
      "processing_ms":         14.1,
      "timestamp":             "2026-07-06T...",
      "notification_dispatched": false
    }
  ]
}
```

---

### `GET /metrics`

**Tags:** Operations  
**Summary:** Per-agent execution metrics.

#### Response `200 OK`

```json
{
  "agents": {
    "ExpenseAuditorAgent":   {"agent": "ExpenseAuditorAgent",   "run_count": 10, "error_count": 0, "avg_latency_ms": 14.2},
    "PolicyEvaluatorWorker": {"agent": "PolicyEvaluatorWorker", "run_count": 10, "error_count": 0, "avg_latency_ms": 8.6},
    "ResolutionCommunicator":{"agent": "ResolutionCommunicator","run_count":  3, "error_count": 0, "avg_latency_ms": 120.4}
  }
}
```

---

### `GET /circuit-state`

**Tags:** Operations  
**Summary:** MCP circuit breaker state snapshot.

#### Response `200 OK`

```json
{
  "circuit":                  "mcp-bridge",
  "state":                    "CLOSED",
  "failure_count":             0,
  "failure_threshold":         5,
  "recovery_timeout_seconds": 30.0,
  "last_failure_at":          null
}
```

**When MCP not initialized:**
```json
{"circuit": "DISCONNECTED", "mcp_available": false}
```

---

## Audit Endpoints

### `GET /audit/{trace_id}`

**Tags:** Audit  
**Summary:** Returns all audit events for a given trace UUID.

#### Path Parameters

| Parameter | Type | Description |
|-----------|------|-------------|
| `trace_id` | `string` (UUID) | The trace ID returned by `POST /submit-expense` |

#### Response `200 OK`

```json
{
  "trace_id": "a3f2c1d4-...",
  "count": 4,
  "events": [
    {
      "event_id":   "...",
      "trace_id":   "a3f2c1d4-...",
      "event_type": "PAYLOAD_RECEIVED",
      "agent_name": "ExpenseAuditorAgent",
      "outcome":    "Payload received and validated",
      "duration_ms": 1.2,
      "timestamp":  "2026-07-06T..."
    },
    {
      "event_type": "SECURITY_VALIDATED",
      "agent_name": "ExpenseAuditorAgent",
      "outcome":    "Session key verified",
      "duration_ms": 0.8
    },
    {
      "event_type": "POLICY_EVALUATED",
      "agent_name": "PolicyEvaluatorWorker",
      "outcome":    "APPROVED: 4250 cents ≤ 5000 cents",
      "duration_ms": 8.6
    },
    {
      "event_type": "APPROVED",
      "agent_name": "ExpenseAuditorAgent",
      "outcome":    "Expense approved",
      "duration_ms": 0.4
    }
  ]
}
```

**`400 Bad Request`** — invalid UUID format  
**`503 Service Unavailable`** — audit service not initialized

---

### `GET /audit/recent`

**Tags:** Audit  
**Summary:** Returns the most recent processed requests from the in-memory log.

#### Query Parameters

| Parameter | Type | Default | Description |
|-----------|------|---------|-------------|
| `limit` | `integer` | `20` | Maximum number of events to return (max 50) |

#### Response `200 OK`

```json
{
  "count": 5,
  "events": [
    {"trace_id": "...", "department": "Engineering", "amount": 42.50, "status": "APPROVED", ...}
  ]
}
```

---

## Policy Endpoints

### `GET /policy-rules`

**Tags:** Policy  
**Summary:** Returns the full loaded corporate policy ruleset.

#### Response `200 OK`

```json
{
  "version": "1.0.0",
  "default_limit_usd": 50.00,
  "rules": [
    {
      "department": "Engineering",
      "category": "*",
      "limit_usd": 50.00,
      "escalation_threshold_usd": 200.00,
      "description": "Engineering default policy"
    },
    {
      "department": "Engineering",
      "category": "software",
      "limit_usd": 500.00,
      "escalation_threshold_usd": 2000.00,
      "description": "Software purchases"
    }
  ]
}
```

**`404 Not Found`** — `config/policy_rules.json` does not exist

---

## Security Endpoints

### `GET /generate-session-key`

**Tags:** Security  
**Summary:** Generates a fresh HMAC-SHA256 session key for use in `POST /submit-expense`.

#### Query Parameters

| Parameter | Type | Default | Description |
|-----------|------|---------|-------------|
| `employee_id` | `string` | `"ENG-001"` | Employee ID to bind the key to |

#### Response `200 OK`

```json
{
  "employee_id":   "ENG-001",
  "session_key":   "1720000000.abcdef1234567890abcdef1234567890...",
  "generated_at":  "2026-07-06T...",
  "ttl_seconds":   3600
}
```

> ⚠️ **Security note:** This endpoint should be restricted or removed in production. It's provided for development and testing convenience.

---

## Admin Endpoints

### `POST /admin/circuit-reset`

**Tags:** Admin  
**Summary:** Force-transitions the MCP circuit breaker to CLOSED state.

#### Response `200 OK`

```json
{
  "success": true,
  "message": "Circuit breaker reset to CLOSED",
  "state": {
    "circuit": "mcp-bridge",
    "state": "CLOSED",
    "failure_count": 0,
    "failure_threshold": 5,
    "recovery_timeout_seconds": 30.0,
    "last_failure_at": null
  }
}
```

**`503 Service Unavailable`** — MCP client not initialized

---

## GUI & Docs Endpoints

### `GET /`

Serves `dashboard.html` — the full NexusScale Control Panel SPA.  
See [GUI.md](GUI.md) for the full feature guide.

### `GET /docs`

Swagger UI interactive API documentation.

### `GET /redoc`

ReDoc API reference documentation.

---

## WebSocket Endpoints

### `WS /ws/logs`

**Summary:** Live log stream. Pushes structured log entries from all pipeline modules to connected clients.

#### Connection

```javascript
const ws = new WebSocket("ws://localhost:8000/ws/logs");
ws.onmessage = (event) => {
  const log = JSON.parse(event.data);
  console.log(log);
};
```

#### Message Format

Each message is a JSON object:

```json
{
  "t":     "14:32:05",
  "level": "INFO",
  "name":  "agents.PolicyEvaluatorWorker",
  "msg":   "✅ Agent completed"
}
```

| Field | Type | Description |
|-------|------|-------------|
| `t` | `string` | Time (`HH:MM:SS` UTC) |
| `level` | `string` | `DEBUG`, `INFO`, `WARNING`, `ERROR`, `CRITICAL` |
| `name` | `string` | Logger name (module path) |
| `msg` | `string` | Log message |

#### Behaviour

- Up to 500 messages are queued; if the queue fills, new messages are dropped
- Disconnected clients are silently removed from the broadcast set
- The stream runs as a background `asyncio.Task` started during lifespan

---

## Error Codes Reference

| Error Code | HTTP Status | Description |
|-----------|:-----------:|-------------|
| `COMPLIANCE_ENGINE_ERROR` | 500 | Generic unclassified error |
| `SECURITY_VALIDATION_FAILED` | 422 | Security preflight failed |
| `SESSION_KEY_INVALID` | 401 | Session key absent, expired, or tampered |
| `PAYLOAD_VALIDATION_FAILED` | 422 | Schema validation error |
| `DEPARTMENT_EMPTY` | 422 | `department` field is blank |
| `PAYLOAD_SCHEMA_INVALID` | 422 | Pydantic schema rejected the body |
| `MCP_ERROR` | 503 | Generic MCP infrastructure error |
| `MCP_TIMEOUT` | 504 | MCP bridge timed out |
| `MCP_DISCONNECTED` | 503 | MCP bridge connection lost |
| `MCP_CIRCUIT_OPEN` | 503 | Circuit breaker fast-failing |
| `POLICY_LOAD_FAILED` | 500 | Policy rules file unreadable |
| `POLICY_EVALUATION_FAILED` | 500 | Error during integer comparison |
| `WEBHOOK_DISPATCH_FAILED` | 502 | All Slack/Teams retries exhausted |
| `AGENT_INIT_FAILED` | 500 | Agent could not initialise |
| `INTERNAL_SERVER_ERROR` | 500 | Unexpected unhandled exception |

---

## HTTP Status Code Guide

| Status | Meaning | Action |
|--------|---------|--------|
| `200` | Evaluation complete (`APPROVED` or `FLAGGED`) | Read `status` field |
| `401` | Session key invalid | Re-generate session key |
| `422` | Payload rejected | Fix `field_errors` |
| `500` | Internal server error | Contact support |
| `502` | Webhook dispatch failed | Retry later |
| `503` | MCP bridge unavailable | Wait `Retry-After` seconds |
| `504` | MCP bridge timed out | Retry with exponential backoff |
