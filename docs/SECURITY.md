# SECURITY.md — Security Architecture and Threat Model

> **Focus:** Preflight checks, HMAC session key validation, and runtime payload security.

---

## 1. Phase 1: The Hard Abort (Preflight)

NexusScale operates on a high-assurance model. The system must never start if its cryptographic foundation is weak.

In `main.py`, before any agent, database, or API route is initialized, `core.security.enforce_enterprise_secret()` runs.

### Criteria for Abort:
- `ENTERPRISE_AGENT_SECRET` is unset.
- It is blank or consists only of whitespace.
- It is fewer than 16 characters.

If any check fails, the system logs a `CRITICAL` error, prints to `stderr`, and calls `sys.exit(1)`.

---

## 2. Session Key Protocol (HMAC)

Every incoming API request must provide a cryptographically sound `session_key`.

### 2.1. Key Generation
Keys are generated using HMAC-SHA256. 
Format: `<unix_timestamp>.<hmac_hex>`

```python
message = f"{employee_id}:{timestamp}".encode("utf-8")
signature = hmac.new(SESSION_HMAC_SECRET, message, hashlib.sha256).hexdigest()
```

### 2.2. Key Verification
During Gate 1 of the `ExpenseAuditorAgent`:
1. **Format Check:** Ensures `<timestamp>.<hmac>` exists.
2. **TTL Check:** Evaluates `now() - timestamp`. If it exceeds `SESSION_KEY_TTL_SECONDS` (default 3600), it rejects the payload.
3. **Tamper Check:** The system recomputes the HMAC using its local secret.
4. **Constant-Time Compare:** Uses `hmac.compare_digest(provided, expected)` to prevent timing attacks.

---

## 3. Input Validation & Sanitization

Security isn't just cryptography; it's also memory and logic safety.

- **Pydantic v2:** All payloads are strictly typed. Extraneous fields are ignored (or dropped based on config).
- **String Sanitization:** `department` is automatically `strip()`ped and title-cased. Blank strings raise `422 Unprocessable Entity` before entering the business logic.
- **Amounts:** Floats are parsed strictly into `Decimal` with `max_digits` and `decimal_places` constraints. Amounts less than `0.01` or greater than `1,000,000.00` are rejected immediately.

---

## 4. Threat Model Mitigation

| Threat | Mitigation |
|--------|------------|
| **Replay Attacks** | Partially mitigated by the `SESSION_KEY_TTL_SECONDS` window. Fully mitigated by `payload_fingerprint` deduplication checks (if implemented in downstream MCP bridge). |
| **Timing Attacks** | `hmac.compare_digest()` is used for all cryptographic comparisons. |
| **Data Tampering** | Audit events are append-only. They cannot be modified via the ORM after insertion. |
| **Secret Leakage** | `_redact()` utility runs on all MCP payloads before logging to mask tokens, passwords, and session keys. |
| **Denial of Service** | Upstream API gateway rate-limiting is recommended. The FastAPI app handles concurrent load via `asyncio`. |
