# NexusScale Compliance Engine

<div align="center">

```
╔══════════════════════════════════════════════════════════════╗
║          NexusScale Compliance Engine  v1.0.0                ║
║     Production-Grade Multi-Agent Financial Compliance        ║
╚══════════════════════════════════════════════════════════════╝
```

[![Python 3.11+](https://img.shields.io/badge/python-3.11%2B-blue)](https://www.python.org)
[![FastAPI](https://img.shields.io/badge/FastAPI-0.111-green)](https://fastapi.tiangolo.com)
[![Pydantic v2](https://img.shields.io/badge/Pydantic-v2-red)](https://docs.pydantic.dev)
[![SQLAlchemy](https://img.shields.io/badge/SQLAlchemy-2.0-orange)](https://www.sqlalchemy.org)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow)](LICENSE)

</div>

---

## What Is This?

**NexusScale Compliance Engine** is a production-grade, multi-agent financial compliance system that intercepts corporate expense payloads, evaluates them against tiered departmental policy limits using **deterministic integer arithmetic**, routes flagged events to notification workers, and records every state transition in an append-only audit trail — all without LLM inference in the critical decision path.

Built on a **Supervisor-Worker** agent design pattern with:

- 🔐 **Phase 1 Hard-Abort Security** — process terminates immediately if `ENTERPRISE_AGENT_SECRET` is missing or weak
- ⚡ **Circuit Breaker Fault Tolerance** — MCP bridge failures never cascade into total outages
- 📋 **Append-Only Audit Trail** — every state transition recorded via SQLAlchemy async
- 🌐 **GUI Control Panel** — full-featured dark-mode dashboard at `http://localhost:8000/`
- 🧪 **40+ Test Assertions** — covering approval, flagging, HMAC tampering, and escalation paths

---

## Table of Contents

1. [Architecture](#architecture)
2. [Quick Start](#quick-start)
3. [Project Structure](#project-structure)
4. [Configuration Reference](#configuration-reference)
5. [API Reference](#api-reference)
6. [Test Vectors](#test-vectors)
7. [GUI Control Panel](#gui-control-panel)
8. [Security Model](#security-model)
9. [Policy Rules](#policy-rules)
10. [Detailed Documentation](#detailed-documentation)

---

## Architecture

```
┌──────────────────────────────────────────────────────────────────────┐
│                   FastAPI API Layer  (main.py)                        │
│  POST /submit-expense  │ GET /health │ GET /stats │ WS /ws/logs       │
│  Middleware: CorrelationID • CORS • Global Exception Handlers         │
└───────────────────────────────┬──────────────────────────────────────┘
                                │
                                ▼
┌──────────────────────────────────────────────────────────────────────┐
│              CompliancePipeline  (orchestrator/pipeline.py)           │
│  MCP lifecycle • Audit init • Agent DI • 503 rollback trap            │
└────────────────┬─────────────────────────────────────────────────────┘
                 │
                 ▼
┌──────────────────────────────────────────────┐
│       ExpenseAuditorAgent  [SUPERVISOR]       │
│  Gate 1: Security — HMAC + department check   │
│  Gate 2: Delegate → PolicyEvaluatorWorker     │
│  Gate 3: Route    → ResolutionCommunicator    │
│  Gate 4: Build    → ComplianceResponse        │
└──────────┬──────────────────┬────────────────┘
           │                  │
           ▼                  ▼
┌──────────────────┐  ┌───────────────────────┐
│ PolicyEvaluator  │  │  ResolutionCommunicator│
│ Worker           │  │  (only on FLAGGED)     │
│                  │  │                        │
│ MCP tool call →  │  │  Slack Block Kit       │
│ fetch_corporate  │  │  Teams Adaptive Card   │
│ _policy          │  │  Tenacity retry 3×     │
│                  │  │                        │
│ Integer cents    │  └───────────────────────┘
│ comparison:      │
│ amount_cents     │
│ vs limit_cents   │
│                  │
│ → APPROVED|FLAGGED
└──────┬───────────┘
       │
       ▼
┌──────────────────────────────────────────────┐
│     MCP Client (JSON-RPC 2.0)                │
│  CircuitBreaker: CLOSED → OPEN → HALF_OPEN   │
│  Retry: tenacity exponential backoff         │
│  Fallback: local PolicyRuleSet               │
└──────────────────────────────────────────────┘
       │
       ▼
┌──────────────────────────────────────────────┐
│     MCP Stub Server  (mcp/server.py :9000)   │
│     Swap for enterprise DB bridge in prod    │
└──────────────────────────────────────────────┘
       ↓ every state transition
┌──────────────────────────────────────────────┐
│  AuditTrailService — SQLAlchemy async        │
│  Append-only. Never updated. Never deleted.  │
└──────────────────────────────────────────────┘
```

### Agent Pattern: Supervisor-Worker

| Agent | Role | Trigger |
|-------|------|---------|
| `ExpenseAuditorAgent` | **Supervisor** | Every inbound request |
| `PolicyEvaluatorWorker` | **Worker** | Always (policy fetch + comparison) |
| `ResolutionCommunicator` | **Communicator** | Only when `status == FLAGGED` |

---

## Quick Start

### Prerequisites

- Python 3.11+
- pip

### 1. Clone & Configure

```bash
git clone <repo-url>
cd nexusscale-compliance-core

# Copy environment template
cp .env.example .env
```

Edit `.env` and set **at minimum**:

```bash
ENTERPRISE_AGENT_SECRET=your-minimum-16-char-secret-here
SESSION_HMAC_SECRET=your-hmac-signing-secret-32-chars
```

> ⚠️ **If `ENTERPRISE_AGENT_SECRET` is absent, blank, or shorter than 16 characters, the process aborts immediately with `exit(1)` before any agent initialises.**

### 2. Install Dependencies

```bash
python -m venv .venv

# Windows
.venv\Scripts\activate
# macOS/Linux
source .venv/bin/activate

pip install -r requirements.txt
```

### 3. One-Command Launch

```bash
python start.py
```

This:
1. Validates your environment
2. Starts the MCP stub server on `:9000`
3. Starts the Compliance API on `:8000`
4. Opens the GUI Control Panel in your browser

### 4. Manual Launch (two terminals)

```bash
# Terminal 1 — MCP stub
uvicorn mcp.server:app --port 9000 --log-level info

# Terminal 2 — Compliance API
python main.py
```

---

## Project Structure

```
nexusscale-compliance-core/
│
├── main.py                        # FastAPI app — lifespan, routes, middleware
├── start.py                       # One-command launcher with env check
├── dashboard.html                 # GUI Control Panel (dark-mode SPA)
├── pytest.ini                     # Test runner configuration
├── requirements.txt               # All Python dependencies
├── .env.example                   # Environment variable template
├── README.md                      # This file
│
├── agents/                        # All agent implementations
│   ├── __init__.py
│   ├── base_agent.py              # Abstract base + AgentRegistry + AgentRunContext
│   ├── expense_auditor_agent.py   # Supervisor — 4-gate pipeline
│   ├── policy_evaluator_worker.py # Worker 1 — MCP + integer comparison
│   └── resolution_communicator.py # Worker 2 — Slack/Teams dispatch
│
├── core/                          # Domain models and infrastructure
│   ├── __init__.py
│   ├── models.py                  # Pydantic v2 domain models and enums
│   ├── exceptions.py              # Structured exception hierarchy
│   ├── security.py                # HMAC, preflight abort, session validation
│   ├── circuit_breaker.py         # Async 3-state circuit breaker
│   └── audit_trail.py             # SQLAlchemy async event log
│
├── mcp/                           # Model Context Protocol layer
│   ├── __init__.py
│   ├── client.py                  # Async JSON-RPC 2.0 MCP client
│   ├── server.py                  # Local stub server (dev/test)
│   └── tools.py                   # Typed tool definitions
│
├── orchestrator/
│   ├── __init__.py
│   └── pipeline.py                # Agent wiring + startup/shutdown lifecycle
│
├── tests/
│   ├── __init__.py
│   ├── conftest.py                # Shared fixtures
│   ├── test_case_a_approval.py    # Engineering $42.50 → APPROVED
│   ├── test_case_b_flagging.py    # Marketing $120.00 → FLAGGED
│   └── test_case_c_security.py    # Empty dept / bad key → 422/401
│
├── config/
│   ├── mcp_config.json            # MCP client manifest + circuit breaker config
│   ├── policy_rules.json          # Tiered departmental spending limits
│   └── logging_config.yaml        # logging.INFO hierarchy for all modules
│
└── docs/                          # Detailed per-module documentation
    ├── ARCHITECTURE.md            # Deep-dive system design
    ├── AGENTS.md                  # All agent functions & lifecycle
    ├── CORE.md                    # core/ module function reference
    ├── MCP.md                     # MCP client/server/tools reference
    ├── API_REFERENCE.md           # All HTTP endpoints + WebSocket
    ├── SECURITY.md                # Security model deep-dive
    ├── TESTING.md                 # Test suite guide
    ├── GUI.md                     # Control Panel user guide
    └── CONFIGURATION.md           # All env vars and config files
```

---

## Configuration Reference

| Variable | Required | Default | Description |
|----------|:--------:|---------|-------------|
| `ENTERPRISE_AGENT_SECRET` | ✅ | — | Runtime security secret (≥16 chars). **Process aborts if missing.** |
| `SESSION_HMAC_SECRET` | ✅ | — | HMAC-SHA256 signing key for session tokens |
| `MCP_SERVER_URL` | — | `http://localhost:9000/mcp` | MCP bridge endpoint |
| `MCP_TIMEOUT_SECONDS` | — | `10` | MCP JSON-RPC call timeout |
| `MCP_CIRCUIT_BREAKER_THRESHOLD` | — | `5` | Consecutive failures before circuit opens |
| `MCP_CIRCUIT_RECOVERY_SECONDS` | — | `30` | Seconds before OPEN → HALF_OPEN probe |
| `SESSION_KEY_TTL_SECONDS` | — | `3600` | Session key time-to-live |
| `AUDIT_DB_URL` | — | `sqlite+aiosqlite:///./audit_trail.db` | Async SQLAlchemy DB URL |
| `SLACK_BOT_TOKEN` | — | — | Bot OAuth token for Slack notifications |
| `TEAMS_WEBHOOK_URL` | — | — | Incoming webhook URL for Teams |
| `POLICY_RULES_PATH` | — | `config/policy_rules.json` | Path to policy rules file |
| `LOG_LEVEL` | — | `INFO` | `DEBUG` / `INFO` / `WARNING` / `ERROR` |
| `APP_ENV` | — | `development` | Environment label for health endpoint |
| `APP_VERSION` | — | `1.0.0` | Version label |
| `HOST` | — | `0.0.0.0` | API bind host |
| `PORT` | — | `8000` | API bind port |

---

## API Reference

### `POST /submit-expense`
Submit an expense for compliance evaluation.

**Request:**
```json
{
  "department": "Engineering",
  "amount": 42.50,
  "category": "meals",
  "employee_id": "ENG-001",
  "employee_email": "alice@nexusscale.io",
  "session_key": "<HMAC-signed key>",
  "currency": "USD",
  "description": "Team lunch"
}
```

**Responses:**

| Status | Meaning | When |
|--------|---------|------|
| `200` | `APPROVED` or `FLAGGED` | Successful evaluation |
| `401` | Session key invalid | HMAC mismatch / expired / absent |
| `422` | Payload validation failed | Empty dept / bad schema |
| `503` | MCP bridge unavailable | Circuit breaker OPEN or timeout |
| `500` | Unexpected server error | Unhandled exception |

**Approved Response:**
```json
{
  "trace_id": "a3f2c1d4-...",
  "status": "APPROVED",
  "department": "Engineering",
  "amount_usd": 42.50,
  "limit_usd": 50.00,
  "variance_usd": 0.00,
  "message": "Expense APPROVED within policy limit.",
  "requires_escalation": false,
  "notification_dispatched": false,
  "processing_time_ms": 12.5
}
```

**Flagged Response:**
```json
{
  "trace_id": "b7e9d2f1-...",
  "status": "FLAGGED",
  "department": "Marketing",
  "amount_usd": 120.00,
  "limit_usd": 50.00,
  "variance_usd": 70.00,
  "message": "Expense FLAGGED — $70.00 over limit.",
  "requires_escalation": false,
  "notification_dispatched": true,
  "processing_time_ms": 48.3
}
```

**Error Response (422):**
```json
{
  "error": "DEPARTMENT_EMPTY",
  "message": "The 'department' field is required and cannot be blank.",
  "http_status": 422,
  "trace_id": "...",
  "timestamp": "2026-07-06T...",
  "field_errors": [{"field": "department", "issue": "blank_or_empty"}]
}
```

---

### Other Endpoints

| Method | Path | Description |
|--------|------|-------------|
| `GET` | `/` | Serves the GUI Control Panel |
| `GET` | `/health` | System health: pipeline, agents, MCP state |
| `GET` | `/stats` | Live session statistics (total, approved, flagged, latency) |
| `GET` | `/metrics` | Per-agent run/error/latency metrics |
| `GET` | `/circuit-state` | MCP circuit breaker state snapshot |
| `GET` | `/audit/{trace_id}` | Audit events for a specific trace UUID |
| `GET` | `/audit/recent` | Last 50 processed requests |
| `GET` | `/policy-rules` | Full policy ruleset from config |
| `GET` | `/generate-session-key` | Generate a fresh HMAC session key |
| `POST` | `/admin/circuit-reset` | Force circuit breaker back to CLOSED |
| `WS` | `/ws/logs` | Live log stream (WebSocket) |
| `GET` | `/docs` | Swagger UI |
| `GET` | `/redoc` | ReDoc |

---

## Test Vectors

### Run Tests

```bash
# All tests
pytest tests/ -v

# Individual test cases
pytest tests/test_case_a_approval.py -v    # APPROVED path
pytest tests/test_case_b_flagging.py  -v   # FLAGGED path
pytest tests/test_case_c_security.py  -v   # Security/validation failures

# With coverage
pytest tests/ --cov=. --cov-report=html
```

### The Three Canonical Vectors

| Test | Dept | Amount | Limit | Expected | Key Assertions |
|------|------|--------|-------|----------|----------------|
| **A** | Engineering | $42.50 | $50.00 | ✅ APPROVED | `4250 ≤ 5000 cents`, variance=0, no notification |
| **B** | Marketing | $120.00 | $50.00 | 🚨 FLAGGED | `12000 > 5000 cents`, variance=$70, notification dispatched |
| **C** | *(empty)* | $75.00 | — | ❌ HTTP 422 | `DEPARTMENT_EMPTY`, MCP never reached |

---

## GUI Control Panel

Access at **http://localhost:8000/** after starting the server.

| Section | Purpose |
|---------|---------|
| 📊 **Dashboard** | Live stats cards, request volume chart, pipeline health, recent transactions |
| 📤 **Submit Expense** | Full form with Quick Presets (A/B/C), HMAC key generator, color-coded response |
| 🧪 **Test Runner** | One-click Test A/B/C with live assertion checking (PASS/FAIL + latency) |
| 📋 **Audit Trail** | Query by trace ID or browse last 50 events |
| 🤖 **Agent Monitor** | Per-agent run/error/latency + approval rate + doughnut chart |
| 📜 **Policy Rules** | All rules loaded from config with relative-limit bars |
| ⚡ **Circuit Breaker** | Animated 3-node state diagram + force reset button |
| 🖥️ **System Logs** | Live WebSocket log stream with level filter and auto-scroll |

---

## Security Model

### Phase 1 — Process Preflight
The very first line of `main.py` after imports calls `enforce_enterprise_secret()`.  
If it fails (missing/blank/short), Python calls `sys.exit(1)` **before** any agent is created.

```
Process starts
  └─ enforce_enterprise_secret()
       ├─ ENTERPRISE_AGENT_SECRET absent    → sys.exit(1) ❌
       ├─ blank / whitespace-only           → sys.exit(1) ❌
       ├─ length < 16                       → sys.exit(1) ❌
       └─ valid                             → continue ✅
```

### Session Key HMAC

Every `POST /submit-expense` requires a valid session key:

```
Format:  <unix_timestamp>.<hmac_sha256_hex>

Verification:
  1. Split on '.'  → timestamp + provided_hmac
  2. Check: now - timestamp ≤ SESSION_KEY_TTL_SECONDS
  3. Recompute: hmac.new(secret, f"{employee_id}:{timestamp}", sha256)
  4. Constant-time compare: hmac.compare_digest(provided, expected)
```

Generate a key:
```bash
python start.py --genkey ENG-001
# or via API:
curl "http://localhost:8000/generate-session-key?employee_id=ENG-001"
```

---

## Policy Rules

Stored in `config/policy_rules.json`. Resolution priority (highest to lowest):

1. **Exact match** — `department` + `category`
2. **Department wildcard** — `department` + `category = "*"`
3. **Global default** — `$50.00`

Example rules:

| Department | Category | Limit | Escalation |
|-----------|---------|-------|-----------|
| Engineering | `*` | $50.00 | $200.00 |
| Engineering | `software` | $500.00 | $2,000.00 |
| Marketing | `*` | $50.00 | $300.00 |
| Marketing | `entertainment` | $150.00 | $500.00 |
| Sales | `meals` | $100.00 | $250.00 |
| Executive | `*` | $500.00 | $5,000.00 |

---

## Detailed Documentation

Each module has its own reference doc in the `docs/` directory:

| File | Contents |
|------|---------|
| [docs/AGENTS.md](docs/AGENTS.md) | Every agent class and method with signatures, purpose, and behaviour |
| [docs/CORE.md](docs/CORE.md) | All `core/` functions: models, security, circuit breaker, audit trail, exceptions |
| [docs/MCP.md](docs/MCP.md) | MCP client, stub server, and tool definitions |
| [docs/API_REFERENCE.md](docs/API_REFERENCE.md) | Full HTTP endpoint and WebSocket documentation |
| [docs/SECURITY.md](docs/SECURITY.md) | Security architecture, HMAC protocol, threat model |
| [docs/TESTING.md](docs/TESTING.md) | Test suite structure, fixture guide, running tests |
| [docs/GUI.md](docs/GUI.md) | Control Panel feature guide and keyboard shortcuts |
| [docs/CONFIGURATION.md](docs/CONFIGURATION.md) | All environment variables and config file formats |
| [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) | System design, data flow, and decision rationale |

---

## License

MIT © NexusScale Engineering 2026
