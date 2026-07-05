# GUI.md — Control Panel User Guide

> **Focus:** Navigating and utilizing the dark-mode SPA Control Panel.

---

## 1. Overview

The NexusScale Control Panel is a Single Page Application (SPA) served directly from the FastAPI root (`GET /`). 
It provides real-time visibility into the multi-agent pipeline.

**Access:** Open `http://localhost:8000/` in your browser.

---

## 2. Features by Section

### 📊 Dashboard
- **Live Metrics:** Four cards displaying Total Requests, Approved, Flagged, and Avg Latency. Updates every 4 seconds.
- **Volume Chart:** A Chart.js bar chart showing the last 20 requests stacked by outcome.
- **Pipeline Status:** Green/Red dots indicating the health of the 3 agents and the MCP circuit breaker.
- **Recent Transactions:** A mini-table of the last 10 requests. Click a Trace ID to jump to the Audit Trail.

### 📤 Submit Expense
- **Form:** A full UI for `ExpensePayload`.
- **Quick Presets:** Buttons that instantly populate the form for Test A (Approval), Test B (Flagged), and an Escalation scenario.
- **Key Generator:** A button to automatically call `/generate-session-key` and fill the hidden field.
- **Response Panel:** Displays the JSON response. The border color changes based on the outcome (Green = Approved, Amber = Flagged, Red = Error).

### 🧪 Test Runner
- Provides cards for Test A, Test B, and Test C.
- Executes the tests against the live API.
- Displays `PASS` or `FAIL` badges based on strict assertions (e.g. variance calculations, HTTP status codes).
- The "Run All Tests" button executes them sequentially with a 400ms delay.

### 📋 Audit Trail
- **Lookup:** Paste a Trace ID to fetch all events for that specific request.
- **Recent:** View the last 50 processed requests.
- **Cross-Navigation:** Clickable Trace IDs link back and forth between the Dashboard and Audit Trail.

### 🤖 Agent Monitor
- Tracks the health of `ExpenseAuditorAgent`, `PolicyEvaluatorWorker`, and `ResolutionCommunicator`.
- Displays Run Count, Error Count, and Average Latency per agent.
- Visualizes the Approval Rate via a doughnut chart and a progress bar.

### 📜 Policy Rules
- Dynamically loads `config/policy_rules.json`.
- Displays Department, Category, Limit, and Escalation Thresholds.
- Features a "Relative Limit" bar, visually comparing each rule's limit against the highest limit in the system.

### ⚡ Circuit Breaker
- Visualizes the `CLOSED -> OPEN -> HALF_OPEN` state machine.
- Highlights the active state in real-time.
- Shows failure counts and thresholds.
- **Force Reset:** A button to manually clear the failure count and force the circuit `CLOSED`.

### 🖥️ System Logs
- Connects to `/ws/logs` via WebSockets.
- Streams live log entries from Python's `logging` module.
- Color-coded by severity (Cyan=INFO, Amber=WARNING, Red=ERROR).
- Features auto-scroll, log clearing, and severity filtering.

---

## 3. Configuration

By default, the GUI communicates with the API at the same origin it was served from.
If you need to point the GUI to a different backend URL, click the "NexusScale" logo in the header and enter a new URL. This is persisted in your browser's `localStorage`.
