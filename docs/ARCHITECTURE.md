# ARCHITECTURE.md — System Design and Architecture

> **Focus:** The overall system design, data flow, error handling strategy, and rationale behind architectural decisions in the NexusScale Compliance Engine.

---

## 1. High-Level Design

The NexusScale Compliance Engine is structured around three primary pillars:
1. **API & Orchestration Layer:** FastAPI, middleware, exception trapping, and pipeline lifecycle.
2. **Agent Layer:** Supervisor-Worker architecture orchestrating rule enforcement, policy evaluation, and notifications.
3. **Infrastructure Layer:** Security preflight, MCP JSON-RPC bridge, circuit breaker, and append-only database audit trail.

---

## 2. Agent Architecture: Supervisor-Worker

NexusScale implements a deterministic, multi-agent pattern specifically suited for non-generative, high-assurance business logic.

```mermaid
graph TD
    API[FastAPI POST /submit-expense] --> Pipe[CompliancePipeline]
    Pipe --> Sup[ExpenseAuditorAgent (Supervisor)]
    
    Sup --> Gate1[Gate 1: Security Validation]
    Gate1 --> Gate2[Gate 2: Policy Evaluation]
    Gate2 --> PolWorker[PolicyEvaluatorWorker]
    PolWorker --> MCP[MCP Client]
    
    Gate2 --> Gate3{Status == FLAGGED?}
    Gate3 -- Yes --> ResWorker[ResolutionCommunicator]
    Gate3 -- No --> Gate4[Gate 4: Construct Response]
    ResWorker --> Gate4
    Gate4 --> API
```

### Why this pattern?
- **Separation of Concerns:** The Supervisor (`ExpenseAuditorAgent`) controls the flow (the *gates*), while Workers handle domain-specific execution (fetching policies, sending Slack messages).
- **Testability:** Workers are injected into the Supervisor via Dependency Injection (`__init__`), allowing trivial mock substitution during unit tests.

---

## 3. The Model Context Protocol (MCP) Bridge

The system relies on an enterprise database bridge for live policy data, implemented via the JSON-RPC 2.0 based MCP.

### 3.1. Circuit Breaker Fault Tolerance
To prevent cascading failures when the remote bridge is down, all MCP calls are wrapped in a thread-safe Async Circuit Breaker.

- **CLOSED:** Normal operation. Failures are counted.
- **OPEN:** Fast-fail mode. `MCPCircuitOpenError` is raised immediately. `503 Service Unavailable` is returned to the client.
- **HALF_OPEN:** After `recovery_timeout_seconds`, the circuit probes the bridge. If successful, it closes. If it fails, it re-opens.

### 3.2. Fallback Mechanisms
If the circuit is OPEN or the call times out, `PolicyEvaluatorWorker` degrades gracefully: it falls back to a locally cached `policy_rules.json` file. The evaluation continues, but the audit trail records that the fallback was used.

---

## 4. Deterministic Arithmetic

Financial compliance demands strict determinism. **Floating-point arithmetic is completely banned in the evaluation logic.**

1. `ExpensePayload` ingests amounts as Python `Decimal`.
2. The payload computes `amount_cents = int(amount * 100)`.
3. `PolicyLimit` provides `limit_cents`.
4. The agent compares `amount_cents <= limit_cents`.

This prevents `42.50000000000001` drift errors that could cause incorrect flagging.

---

## 5. Audit Trail Strategy

NexusScale uses an **Append-Only Event Sourcing** model for auditing.
- Implemented via SQLAlchemy Async ORM.
- The `AuditTrailService` only provides `record()` and `query()`. There are no `update()` or `delete()` methods.
- Events are linked by a `trace_id` generated at the API ingress.

*Critical constraint:* Database failures are caught and swallowed by the `AuditTrailService`. A failure to write an audit log must **never** crash the compliance evaluation pipeline.

---

## 6. Error Handling & Rollback

We use a layered, typed exception hierarchy based on `ComplianceEngineError`.

1. **Domain Exceptions:** Raise anywhere (e.g. `DepartmentEmptyError`).
2. **Propagation:** Handled seamlessly by the Agent `execute` wrapper.
3. **Pipeline Trap:** `CompliancePipeline.process()` traps all exceptions.
4. **Rollback:** For MCP errors, a specific `_safe_rollback()` routine emits a failure audit event.
5. **API Handlers:** FastAPI global exception handlers catch the trapped errors and convert them to deterministic, machine-readable JSON envelopes.
