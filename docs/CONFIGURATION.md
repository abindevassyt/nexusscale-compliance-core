# CONFIGURATION.md — Configuration Guide

> **Focus:** Environment variables, JSON configs, and Logging setup.

---

## 1. Environment Variables (`.env`)

The system uses `os.environ` to read configuration. A `.env` file in the root directory is automatically loaded by the `start.py` launcher.

### Security (Required)
| Variable | Description | Example |
|----------|-------------|---------|
| `ENTERPRISE_AGENT_SECRET` | Master security secret. Process aborts if missing or < 16 chars. | `super-secret-key-12345` |
| `SESSION_HMAC_SECRET` | Key used to sign HMAC session tokens. | `hmac-signing-key-98765` |
| `SESSION_KEY_TTL_SECONDS` | Time-to-live for session keys in seconds. | `3600` |

### MCP & Circuit Breaker
| Variable | Description | Default |
|----------|-------------|---------|
| `MCP_SERVER_URL` | URL of the MCP JSON-RPC bridge. | `http://localhost:9000/mcp` |
| `MCP_TIMEOUT_SECONDS` | Request timeout for MCP calls. | `10.0` |
| `MCP_CIRCUIT_BREAKER_THRESHOLD` | Failures before circuit opens. | `5` |
| `MCP_CIRCUIT_RECOVERY_SECONDS` | Seconds before probing bridge again. | `30.0` |

### Database
| Variable | Description | Default |
|----------|-------------|---------|
| `AUDIT_DB_URL` | Async SQLAlchemy connection string. | `sqlite+aiosqlite:///./audit_trail.db` |

### Notifications
| Variable | Description |
|----------|-------------|
| `SLACK_BOT_TOKEN` | OAuth token for the Slack Web API. |
| `TEAMS_WEBHOOK_URL` | Incoming Webhook URL for Microsoft Teams. |

---

## 2. Policy Rules (`config/policy_rules.json`)

Defines the departmental spending limits. The `PolicyEvaluatorWorker` falls back to this file if the MCP bridge is down.

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
      "description": "Standard engineering limit"
    }
  ]
}
```

- **`department`:** Must exactly match the incoming payload (case-insensitive). Use `*` for all.
- **`category`:** Use `*` to apply to all categories for that department.
- **`limit_usd`:** The maximum allowed spend before being FLAGGED.
- **`escalation_threshold_usd`:** (Optional) Spend above this triggers `requires_escalation=True`.

---

## 3. MCP Config (`config/mcp_config.json`)

Used by `MCPClient.from_config()` to initialize the bridge connection.

```json
{
  "base_url": "http://localhost:9000",
  "timeout_seconds": 10.0,
  "max_retries": 3,
  "circuit_breaker": {
    "name": "mcp-bridge",
    "failure_threshold": 5,
    "recovery_timeout_seconds": 30.0
  }
}
```

---

## 4. Logging Config (`config/logging_config.yaml`)

Python `logging.config.dictConfig` definition.

- Defines standard formatters (e.g., `%(asctime)s - %(name)s - %(levelname)s - %(message)s`).
- Defines handlers (`console` outputting to `ext://sys.stdout`).
- Configures log levels per module (e.g., `agents` -> `INFO`, `core` -> `WARNING`).
- Contains the custom `_LogBroadcaster` handler for the GUI WebSocket stream.
