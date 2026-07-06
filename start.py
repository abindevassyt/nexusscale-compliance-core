"""
start.py
────────
NexusScale Compliance Engine — One-Command Launcher

Usage:
    python start.py              → Start both MCP stub + compliance API + open browser
    python start.py --api-only   → Start only the compliance API
    python start.py --mcp-only   → Start only the MCP stub server
    python start.py --check      → Check environment and exit
    python start.py --genkey     → Generate a session key and print it
"""

from __future__ import annotations

import asyncio
import os
import signal
import sys
import time

if sys.stdout.encoding.lower() != 'utf-8':
    try:
        sys.stdout.reconfigure(encoding='utf-8')
    except Exception:
        pass
import argparse
import subprocess
import webbrowser
from pathlib import Path


# ─────────────────────────────────────────────────────────────────────────────
# Colours (no third-party deps)
# ─────────────────────────────────────────────────────────────────────────────

def _c(code: str, text: str) -> str:
    return f"\033[{code}m{text}\033[0m" if sys.platform != "win32" or os.environ.get("TERM") else text

GREEN  = lambda t: _c("92", t)
YELLOW = lambda t: _c("93", t)
RED    = lambda t: _c("91", t)
CYAN   = lambda t: _c("96", t)
BOLD   = lambda t: _c("1",  t)
DIM    = lambda t: _c("2",  t)


# ─────────────────────────────────────────────────────────────────────────────
# Banner
# ─────────────────────────────────────────────────────────────────────────────

BANNER = """
╔══════════════════════════════════════════════════════════════╗
║          NexusScale Compliance Engine  v1.0.0                ║
║          Multi-Agent Financial Compliance System             ║
╚══════════════════════════════════════════════════════════════╝
"""

def banner() -> None:
    print(CYAN(BANNER))


# ─────────────────────────────────────────────────────────────────────────────
# Environment check
# ─────────────────────────────────────────────────────────────────────────────

def _load_env() -> None:
    """Load .env file if it exists."""
    env_path = Path(".env")
    if env_path.exists():
        with open(env_path) as f:
            for line in f:
                line = line.strip()
                if line and not line.startswith("#") and "=" in line:
                    key, _, val = line.partition("=")
                    os.environ.setdefault(key.strip(), val.strip())


def check_environment() -> bool:
    """Verify all required environment variables and dependencies."""
    print(BOLD("\n📋 Environment Check"))
    print("─" * 50)
    ok = True

    # Required env vars
    checks = [
        ("ENTERPRISE_AGENT_SECRET", 16, "Runtime security secret (≥16 chars)"),
        ("SESSION_HMAC_SECRET",      8, "HMAC signing key for session tokens"),
    ]
    for var, min_len, desc in checks:
        val = os.environ.get(var, "")
        if len(val.strip()) >= min_len:
            print(GREEN(f"  ✓ {var}") + DIM(f" ({desc})"))
        else:
            print(RED(f"  ✗ {var}") + f" — {desc}")
            if not val:
                print(DIM(f"    Set in .env: {var}=your-secret-here"))
            else:
                print(DIM(f"    Value too short ({len(val.strip())} chars, need ≥{min_len})"))
            ok = False

    # Optional env vars
    opt_checks = [
        ("SLACK_BOT_TOKEN",    "Slack notifications"),
        ("TEAMS_WEBHOOK_URL",  "Microsoft Teams notifications"),
    ]
    for var, desc in opt_checks:
        val = os.environ.get(var, "")
        if val:
            print(GREEN(f"  ✓ {var}") + DIM(f" ({desc})"))
        else:
            print(YELLOW(f"  ⚠ {var}") + DIM(f" — optional ({desc})"))

    # Python packages
    print()
    deps = ["fastapi", "uvicorn", "pydantic", "httpx", "sqlalchemy", "yaml", "tenacity"]
    for dep in deps:
        try:
            __import__(dep.replace("-", "_"))
            print(GREEN(f"  ✓ {dep}"))
        except ImportError:
            print(RED(f"  ✗ {dep}") + " — run: pip install -r requirements.txt")
            ok = False

    # Config files
    print()
    configs = ["config/mcp_config.json", "config/policy_rules.json", "config/logging_config.yaml"]
    for cfg in configs:
        if Path(cfg).exists():
            print(GREEN(f"  ✓ {cfg}"))
        else:
            print(RED(f"  ✗ {cfg}") + " — file missing")
            ok = False

    print()
    if ok:
        print(GREEN("✅ Environment check PASSED — ready to launch"))
    else:
        print(RED("❌ Environment check FAILED — fix errors above before starting"))
    return ok


# ─────────────────────────────────────────────────────────────────────────────
# Generate Session Key
# ─────────────────────────────────────────────────────────────────────────────

def gen_session_key(employee_id: str = "ENG-001") -> None:
    _load_env()
    try:
        from core.security import generate_session_key
        key = generate_session_key(employee_id)
        print(BOLD(f"\n🔑 Session Key for {employee_id}:"))
        print(CYAN(f"   {key}"))
        print(DIM(f"\n   Paste this into the 'session_key' field of your API request.\n"))
    except Exception as e:
        print(RED(f"Failed to generate session key: {e}"))
        print(DIM("Ensure ENTERPRISE_AGENT_SECRET and SESSION_HMAC_SECRET are set."))


# ─────────────────────────────────────────────────────────────────────────────
# Process Launchers
# ─────────────────────────────────────────────────────────────────────────────

def launch_mcp_server(port: int = 9000) -> subprocess.Popen:
    """Start the local MCP stub server in a subprocess."""
    print(BOLD(f"\n🗄  Starting MCP Stub Server on port {port}…"))
    proc = subprocess.Popen(
        [sys.executable, "-m", "uvicorn", f"mcp.server:app", "--port", str(port), "--log-level", "warning"],
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
    )
    time.sleep(1.5)
    if proc.poll() is not None:
        print(RED("  ✗ MCP server failed to start"))
    else:
        print(GREEN(f"  ✓ MCP stub server running at http://localhost:{port}"))
    return proc


def launch_api_server(host: str = "0.0.0.0", port: int = 8000) -> subprocess.Popen:
    """Start the FastAPI compliance API server."""
    print(BOLD(f"\n🚀 Starting Compliance API on port {port}…"))
    env = os.environ.copy()
    proc = subprocess.Popen(
        [sys.executable, "-m", "uvicorn", "main:app", "--host", host, "--port", str(port), "--log-level", "info"],
        env=env,
    )
    time.sleep(2.5)
    if proc.poll() is not None:
        print(RED("  ✗ API server failed to start"))
    else:
        print(GREEN(f"  ✓ Compliance API running at http://localhost:{port}"))
        print(GREEN(f"  ✓ Control Panel (GUI) at  http://localhost:{port}/"))
        print(GREEN(f"  ✓ API Docs              at  http://localhost:{port}/docs"))
    return proc


# ─────────────────────────────────────────────────────────────────────────────
# Main
# ─────────────────────────────────────────────────────────────────────────────

def main() -> None:
    parser = argparse.ArgumentParser(description="NexusScale Compliance Engine Launcher")
    parser.add_argument("--api-only",  action="store_true", help="Start only the compliance API")
    parser.add_argument("--mcp-only",  action="store_true", help="Start only the MCP stub server")
    parser.add_argument("--check",     action="store_true", help="Check environment and exit")
    parser.add_argument("--genkey",    metavar="EMPLOYEE_ID", nargs="?", const="ENG-001", help="Generate a session key")
    parser.add_argument("--host",      default="0.0.0.0",   help="API host (default: 0.0.0.0)")
    parser.add_argument("--port",      type=int, default=8000, help="API port (default: 8000)")
    parser.add_argument("--mcp-port",  type=int, default=9000, help="MCP stub port (default: 9000)")
    parser.add_argument("--no-browser",action="store_true", help="Don't open browser automatically")
    args = parser.parse_args()

    banner()
    _load_env()

    if args.genkey:
        gen_session_key(args.genkey)
        return

    if args.check:
        check_environment()
        return

    if not check_environment():
        print(RED("\n⛔  Fix environment errors before launching.\n"))
        sys.exit(1)

    procs = []
    try:
        if args.mcp_only:
            procs.append(launch_mcp_server(args.mcp_port))
        elif args.api_only:
            procs.append(launch_api_server(args.host, args.port))
        else:
            # Full stack
            procs.append(launch_mcp_server(args.mcp_port))
            procs.append(launch_api_server(args.host, args.port))

        print()
        print(BOLD("─" * 60))
        if not args.mcp_only:
            print(CYAN(f"  🌐  Control Panel  : http://localhost:{args.port}/"))
            print(CYAN(f"  📖  API Docs       : http://localhost:{args.port}/docs"))
            print(CYAN(f"  📊  Dashboard      : http://localhost:{args.port}/"))
        if not args.api_only:
            print(CYAN(f"  🗄   MCP Stub       : http://localhost:{args.mcp_port}/health"))
        print(BOLD("─" * 60))
        print(DIM("  Press Ctrl+C to stop all services\n"))

        if not args.no_browser and not args.mcp_only:
            time.sleep(1)
            webbrowser.open(f"http://localhost:{args.port}/")

        # Wait for all processes
        for p in procs:
            p.wait()

    except KeyboardInterrupt:
        print(YELLOW("\n\n🛑  Shutting down services…"))
        for p in procs:
            try: p.terminate()
            except: pass
        time.sleep(0.5)
        for p in procs:
            try: p.kill()
            except: pass
        print(GREEN("  All services stopped. Goodbye!\n"))


if __name__ == "__main__":
    main()
