# TESTING.md — Test Suite Guide

> **Focus:** How to run the automated tests, what the test vectors cover, and how fixtures are structured.

---

## 1. Running Tests

The test suite uses `pytest`. It relies on `httpx.AsyncClient` to mock requests against the FastAPI app.

```bash
# Run all tests
pytest tests/ -v

# Run with coverage report
pytest tests/ --cov=. --cov-report=html
```

---

## 2. The Canonical Test Vectors

We define three primary test vectors that exercise every path in the pipeline.

### Test Case A: Approval (Happy Path)
- **File:** `tests/test_case_a_approval.py`
- **Scenario:** The Engineering department submits an expense for $42.50. The policy limit is $50.00.
- **Assertions:**
  - HTTP Status `200`
  - JSON payload `status == "APPROVED"`
  - `variance_usd == 0.00`
  - `notification_dispatched == False`

### Test Case B: Flagging & Notification
- **File:** `tests/test_case_b_flagging.py`
- **Scenario:** Marketing submits an expense for $120.00. The policy limit is $50.00.
- **Assertions:**
  - HTTP Status `200` (Business logic executed successfully)
  - JSON payload `status == "FLAGGED"`
  - `variance_usd == 70.00`
  - `notification_dispatched == True`
  - The audit trail records the `FLAGGED` outcome.

### Test Case C: Security & Validation Failures
- **File:** `tests/test_case_c_security.py`
- **Scenarios:**
  - Missing department string.
  - Invalid session key (bad timestamp).
  - Tampered HMAC signature.
- **Assertions:**
  - HTTP Status `422` (Validation) or `401` (Security).
  - Specific `error_code` matches `DEPARTMENT_EMPTY` or `SESSION_KEY_INVALID`.
  - The pipeline aborts *before* the MCP bridge is queried.

---

## 3. Fixtures (`conftest.py`)

Common fixtures are defined in `tests/conftest.py` for reusability.

- `test_client`: An `httpx.AsyncClient` connected to the `FastAPI` instance.
- `valid_session_key`: Generates a fresh, valid HMAC key for the `test_client`.
- `mock_mcp`: An `AsyncMock` of the MCP client to simulate database responses without needing the stub server running.

---

## 4. Writing New Tests

When adding a new policy rule or agent:
1. Identify the inputs (Payload).
2. Define the expected outputs (Response & Audit Log).
3. Use `test_client.post("/submit-expense", json=...)`.
4. Assert both the HTTP response and the state of the system (e.g. check the audit trail endpoint).
