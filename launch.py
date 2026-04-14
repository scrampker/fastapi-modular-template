#!/usr/bin/env python3
"""
Generic FastAPI Bootstrap Launcher

Handles everything automatically:
  1. Checks Python version (configurable minimum)
  2. Creates venv if missing
  3. Installs/updates dependencies (hash-gated — skips if pyproject.toml unchanged)
  4. Activates venv via os.execv and re-launches inside it
  5. Copies .env.example → .env on first run
  6. Cleans up stale processes holding the bind port
  7. Manages a PID file so a second invocation kills the first
  8. Probes optional tools (Node, npm, Claude Code) and writes data/env_probe.json
  9. Supervises the uvicorn subprocess — logs crashes, auto-restarts, stops after
     MAX_RAPID_CRASHES crashes within RAPID_CRASH_WINDOW seconds
  10. Forwards arbitrary CLI args to the app entry point

Usage:
    python launch.py                  # Start web server on DEFAULT_PORT
    python launch.py serve            # Same, explicit
    python launch.py serve --port 9090
    python launch.py <any other args> # Forwarded directly to the app CLI
"""

# ---------------------------------------------------------------------------
# Configurable constants — edit these for each project
# ---------------------------------------------------------------------------

APP_NAME = "MyApp"
ENTRY_POINT = "scottycore.main:app"    # uvicorn module:variable  (e.g. "scottycore.main:app")
MIN_PYTHON = (3, 10)            # (major, minor) minimum Python version
DEFAULT_PORT = 8000             # Port used when --port is not supplied
RESTART_EXIT_CODE = 75          # Server signals "please restart" with this code

# Crash-loop protection
MAX_RAPID_CRASHES = 3
RAPID_CRASH_WINDOW = 60         # seconds

# ---------------------------------------------------------------------------
# stdlib imports — no third-party dependencies here
# ---------------------------------------------------------------------------

import os
import sys
import subprocess
import platform
import time
from datetime import datetime
from pathlib import Path

# ---------------------------------------------------------------------------
# Paths derived from project root (directory containing this file)
# ---------------------------------------------------------------------------

PROJECT_ROOT = Path(__file__).parent.resolve()
VENV_DIR = PROJECT_ROOT / ".venv"
REQUIREMENTS_HASH_FILE = VENV_DIR / ".deps_hash"
DATA_DIR = PROJECT_ROOT / "data"
CRASH_LOG_DIR = DATA_DIR / "crash_logs"
PID_FILE = DATA_DIR / "launcher.pid"


# ===========================================================================
# Python version guard
# ===========================================================================

def check_python_version() -> None:
    """Exit early if the system Python is below MIN_PYTHON."""
    major, minor = sys.version_info[:2]
    req_major, req_minor = MIN_PYTHON
    if (major, minor) < (req_major, req_minor):
        print(f"  ERROR: Python {req_major}.{req_minor}+ required, found {major}.{minor}")
        print("  Install from https://www.python.org/downloads/")
        sys.exit(1)


# ===========================================================================
# Virtual environment helpers
# ===========================================================================

def get_venv_python() -> Path:
    """Return the path to the Python executable inside the venv."""
    if platform.system() == "Windows":
        return VENV_DIR / "Scripts" / "python.exe"
    return VENV_DIR / "bin" / "python"


def compute_deps_hash() -> str:
    """Hash pyproject.toml so we can skip reinstall when nothing changed."""
    import hashlib
    pyproject = PROJECT_ROOT / "pyproject.toml"
    if pyproject.exists():
        return hashlib.sha256(pyproject.read_bytes()).hexdigest()[:16]
    return "none"


def create_venv() -> bool:
    """Create the virtual environment if it does not yet exist.

    Returns True if a new venv was created, False if it already existed.
    """
    if get_venv_python().exists():
        return False

    print("  Creating virtual environment...")
    subprocess.run(
        [sys.executable, "-m", "venv", str(VENV_DIR)],
        check=True,
        cwd=str(PROJECT_ROOT),
    )
    print("  Virtual environment created.")
    return True


def install_dependencies(force: bool = False) -> None:
    """Install/upgrade project dependencies into the venv.

    Skips installation entirely when pyproject.toml hash is unchanged
    and force=False — making repeated launches fast.
    """
    current_hash = compute_deps_hash()

    if not force and REQUIREMENTS_HASH_FILE.exists():
        cached = REQUIREMENTS_HASH_FILE.read_text().strip()
        if cached == current_hash:
            return  # Nothing changed — skip

    venv_py = str(get_venv_python())
    print("  Installing dependencies...")

    # Upgrade pip first (doing it via python -m pip avoids Windows locking issues)
    subprocess.run(
        [venv_py, "-m", "pip", "install", "--upgrade", "pip"],
        check=True,
        capture_output=True,
        cwd=str(PROJECT_ROOT),
    )

    # Install the project in editable mode (adjust extras as needed)
    subprocess.run(
        [venv_py, "-m", "pip", "install", "-e", ".[dev]"],
        check=True,
        cwd=str(PROJECT_ROOT),
    )

    REQUIREMENTS_HASH_FILE.parent.mkdir(parents=True, exist_ok=True)
    REQUIREMENTS_HASH_FILE.write_text(current_hash)
    print("  Dependencies installed.")


def run_in_venv() -> None:
    """If we are not already running inside the venv, exec into it.

    Uses os.execv so the venv Python replaces this process entirely —
    no subprocess overhead and signals propagate correctly.
    """
    venv_python = get_venv_python()

    # Already inside the venv, or venv doesn't exist yet — nothing to do
    if Path(sys.executable).resolve() == venv_python.resolve():
        return
    if not venv_python.exists():
        return

    os.execv(str(venv_python), [str(venv_python), __file__] + sys.argv[1:])


# ===========================================================================
# First-run helpers
# ===========================================================================

def ensure_env_file() -> None:
    """Copy .env.example → .env if .env is missing."""
    env_file = PROJECT_ROOT / ".env"
    env_example = PROJECT_ROOT / ".env.example"
    if not env_file.exists() and env_example.exists():
        import shutil
        shutil.copy2(env_example, env_file)
        print("  Created .env from .env.example — edit it to configure.")


def ensure_data_dirs() -> None:
    """Create standard runtime data directories."""
    for d in [DATA_DIR, CRASH_LOG_DIR, DATA_DIR / "logs"]:
        d.mkdir(parents=True, exist_ok=True)


# ===========================================================================
# PID file management
# ===========================================================================

def kill_existing_launcher() -> None:
    """If a previous instance of this launcher is running, terminate it."""
    if not PID_FILE.exists():
        return

    try:
        old_pid = int(PID_FILE.read_text().strip())
    except (ValueError, OSError):
        PID_FILE.unlink(missing_ok=True)
        return

    if old_pid == os.getpid():
        return  # That's us — nothing to do

    try:
        if platform.system() == "Windows":
            check_result = subprocess.run(
                [
                    "powershell", "-NoProfile", "-Command",
                    f"Get-Process -Id {old_pid} -ErrorAction SilentlyContinue "
                    f"| Select-Object -ExpandProperty Id",
                ],
                capture_output=True, text=True, timeout=5,
            )
            if str(old_pid) not in check_result.stdout:
                PID_FILE.unlink(missing_ok=True)
                return
            print(f"  Stopping previous launcher (PID {old_pid})...")
            subprocess.run(
                [
                    "powershell", "-NoProfile", "-Command",
                    f"Stop-Process -Id {old_pid} -Force -ErrorAction SilentlyContinue",
                ],
                capture_output=True, timeout=5,
            )
        else:
            import signal
            os.kill(old_pid, 0)  # Raises OSError if process is gone
            print(f"  Stopping previous launcher (PID {old_pid})...")
            try:
                os.killpg(os.getpgid(old_pid), signal.SIGTERM)
            except (ProcessLookupError, OSError):
                pass
            time.sleep(1)
            try:
                os.kill(old_pid, signal.SIGKILL)
            except (ProcessLookupError, OSError):
                pass
    except (ProcessLookupError, PermissionError, OSError):
        pass  # Process already gone

    PID_FILE.unlink(missing_ok=True)
    time.sleep(1)  # Give the OS time to release ports


def write_pid_file() -> None:
    """Record this process's PID so a future invocation can kill us."""
    PID_FILE.parent.mkdir(parents=True, exist_ok=True)
    PID_FILE.write_text(str(os.getpid()))


def remove_pid_file() -> None:
    """Remove the PID file on clean shutdown."""
    PID_FILE.unlink(missing_ok=True)


# ===========================================================================
# Port cleanup
# ===========================================================================

def free_port(host: str, port: int) -> None:
    """Kill whatever process is listening on host:port, if any.

    This prevents "address already in use" errors when the previous
    server process didn't exit cleanly.
    """
    import socket

    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        sock.settimeout(1)
        sock.connect((host, port))
        sock.close()
    except (ConnectionRefusedError, OSError):
        return  # Port is free — nothing to do

    print(f"  Port {port} is in use — attempting to free it...")

    try:
        if platform.system() == "Windows":
            ps_script = (
                f"$c = Get-NetTCPConnection -LocalPort {port} -State Listen "
                f"    -ErrorAction SilentlyContinue; "
                f"if ($c) {{ $c | ForEach-Object {{ "
                f"Write-Host ('Killing PID ' + $_.OwningProcess); "
                f"Stop-Process -Id $_.OwningProcess -Force "
                f"    -ErrorAction SilentlyContinue }} }}"
            )
            subprocess.run(
                ["powershell", "-NoProfile", "-Command", ps_script],
                capture_output=False, timeout=10,
            )
            time.sleep(1)
        else:
            # lsof works on Linux and macOS
            result = subprocess.run(
                ["lsof", "-ti", f":{port}"],
                capture_output=True, text=True, timeout=5,
            )
            for pid_str in result.stdout.strip().split():
                if pid_str.isdigit():
                    print(f"  Killing PID {pid_str} holding port {port}")
                    subprocess.run(["kill", "-9", pid_str], capture_output=True, timeout=5)
                    time.sleep(1)
    except Exception as exc:
        print(f"  Warning: Could not free port {port}: {exc}")
        print(f"  You may need to manually kill the process on port {port}.")


# ===========================================================================
# Crash log
# ===========================================================================

def write_crash_log(exc: Exception, tb_str: str) -> Path:
    """Persist a crash report to data/crash_logs/ and return the path."""
    CRASH_LOG_DIR.mkdir(parents=True, exist_ok=True)
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    crash_file = CRASH_LOG_DIR / f"crash_{timestamp}.log"

    content = (
        f"{APP_NAME} Crash Report\n"
        f"{'=' * (len(APP_NAME) + 14)}\n"
        f"Time:     {datetime.now().isoformat()}\n"
        f"Python:   {sys.version}\n"
        f"Platform: {platform.platform()}\n"
        f"Args:     {sys.argv}\n\n"
        f"Exception: {type(exc).__name__}: {exc}\n\n"
        f"Traceback:\n{tb_str}\n\n"
        f"Paths:\n"
        f"  Project Root: {PROJECT_ROOT}\n"
        f"  Venv:         {VENV_DIR}\n"
    )
    crash_file.write_text(content, encoding="utf-8")
    return crash_file


# ===========================================================================
# Environment probe
# ===========================================================================

def probe_environment() -> None:
    """Detect optional tools and write a machine-readable summary.

    Writes data/env_probe.json so the running application can check
    tool availability without spawning shell subprocesses at request time.
    Detected tools: Node.js, npm, Claude Code CLI (+ auth).
    """
    import json as _json
    import shutil

    probe: dict = {
        "probed_at": datetime.now().isoformat(),
        "node_installed": False,
        "node_version": None,
        "npm_installed": False,
        "npm_version": None,
        "claude_code_installed": False,
        "claude_code_version": None,
        "claude_code_authenticated": False,
        "env_api_key_set": bool(os.environ.get("ANTHROPIC_API_KEY", "")),
    }

    def _version(cmd: str) -> str | None:
        """Run `cmd --version` and return its first output line, or None."""
        try:
            r = subprocess.run(
                [cmd, "--version"], capture_output=True, text=True, timeout=5,
            )
            if r.returncode == 0:
                return r.stdout.strip().split("\n")[0].strip()
        except Exception:
            pass
        return None

    # Node.js
    node_bin = shutil.which("node")
    if node_bin:
        v = _version(node_bin)
        if v:
            probe["node_installed"] = True
            probe["node_version"] = v

    # npm
    npm_bin = shutil.which("npm")
    if npm_bin:
        v = _version(npm_bin)
        if v:
            probe["npm_installed"] = True
            probe["npm_version"] = v

    # Claude Code CLI
    claude_bin = shutil.which("claude") or (
        shutil.which("claude.cmd") if platform.system() == "Windows" else None
    )
    if claude_bin:
        v = _version(claude_bin)
        if v:
            probe["claude_code_installed"] = True
            probe["claude_code_version"] = v

    # Claude Code authentication — check credentials file
    creds_path = Path.home() / ".claude" / ".credentials.json"
    if creds_path.exists():
        try:
            creds = _json.loads(creds_path.read_text(encoding="utf-8"))
            token = creds.get("claudeAiOauth", {}).get("accessToken", "")
            if token.startswith("sk-ant-"):
                probe["claude_code_authenticated"] = True
        except Exception:
            pass

    # Persist probe results
    probe_path = DATA_DIR / "env_probe.json"
    probe_path.parent.mkdir(parents=True, exist_ok=True)
    probe_path.write_text(_json.dumps(probe, indent=2), encoding="utf-8")

    # --- Print human-readable summary ---
    def _fmt(installed: bool, version: str | None, extra: str = "") -> str:
        return f"{version or 'yes'}{extra}" if installed else "not found"

    cc_extra = ""
    if probe["claude_code_installed"]:
        cc_extra = " (authenticated)" if probe["claude_code_authenticated"] else " (needs auth)"

    print("  Environment:")
    print(f"    Node.js ........... {_fmt(probe['node_installed'], probe['node_version'])}")
    print(f"    npm ............... {_fmt(probe['npm_installed'], probe['npm_version'])}")
    print(f"    Claude Code CLI ... {_fmt(probe['claude_code_installed'], probe['claude_code_version'], cc_extra)}")
    print(f"    API Key (env) ..... {'set' if probe['env_api_key_set'] else 'not set'}")
    print()


# ===========================================================================
# Server subprocess management
# ===========================================================================

def start_server_subprocess(args: list[str], open_browser: bool = True) -> int | None:
    """Launch uvicorn as a subprocess and wait for it to exit.

    Parses --host / --port from args; frees the port before binding.
    Returns the subprocess exit code, or None when interrupted by Ctrl-C.
    """
    host = "0.0.0.0"
    port = DEFAULT_PORT

    # Parse --host and --port / -p from the forwarded args
    i = 0
    while i < len(args):
        if args[i] == "--host" and i + 1 < len(args):
            host = args[i + 1]
            i += 2
        elif args[i] in ("--port", "-p") and i + 1 < len(args):
            try:
                port = int(args[i + 1])
            except ValueError:
                print(f"  Warning: invalid port '{args[i + 1]}', using {DEFAULT_PORT}")
            i += 2
        else:
            i += 1

    free_port(host, port)

    browser_host = "127.0.0.1" if host == "0.0.0.0" else host
    url = f"http://{browser_host}:{port}"
    log_path = DATA_DIR / "logs" / "server.log"

    print(f"  Starting {APP_NAME} on http://{host}:{port}  (browser: {url})")
    print(f"  Log: {log_path}")
    print("  Press Ctrl+C to stop")
    print()

    venv_python = str(get_venv_python())
    cmd = [
        venv_python, "-m", "uvicorn",
        ENTRY_POINT,
        "--host", host,
        "--port", str(port),
        "--log-level", "info",
    ]

    try:
        proc = subprocess.run(cmd, cwd=str(PROJECT_ROOT))
        return proc.returncode
    except KeyboardInterrupt:
        return None


def run_server_with_crash_watch(args: list[str], first_launch: bool = True) -> None:
    """Supervise the uvicorn subprocess with restart and crash-loop protection.

    Behaviour:
      - Exit code RESTART_EXIT_CODE (75): reinstall deps, wait for port
        release, then restart immediately.
      - Exit code 0 / None (Ctrl-C): clean shutdown, stop.
      - Any other exit code: log crash, restart after 3 s.
      - MAX_RAPID_CRASHES crashes within RAPID_CRASH_WINDOW seconds: give up.
    """
    crash_times: list[float] = []
    open_browser = first_launch

    while True:
        exit_code = start_server_subprocess(args, open_browser=open_browser)
        open_browser = False  # Only open (or announce) browser URL on first run

        # --- Requested restart (e.g. "update & restart" from the web UI) ---
        if exit_code == RESTART_EXIT_CODE:
            print()
            print(f"  {'=' * 60}")
            print(f"  RESTART REQUESTED")
            print(f"  {'=' * 60}")
            install_dependencies()   # Pick up any dep changes from the update
            print("  Waiting for port release...")
            time.sleep(3)
            print("  Restarting server...")
            print()
            continue

        # --- Clean exit ---
        if exit_code is None or exit_code == 0:
            break

        # --- Crash ---
        print()
        print(f"  {'=' * 60}")
        print(f"  CRASH DETECTED (exit code {exit_code})")
        print(f"  {'=' * 60}")
        print(f"  Check crash logs in: {CRASH_LOG_DIR}")
        print(f"  {'=' * 60}")
        print()

        # Track how many crashes happened in the last RAPID_CRASH_WINDOW seconds
        now = time.time()
        crash_times.append(now)
        crash_times = [t for t in crash_times if now - t < RAPID_CRASH_WINDOW]

        if len(crash_times) >= MAX_RAPID_CRASHES:
            print(
                f"  {MAX_RAPID_CRASHES} crashes within {RAPID_CRASH_WINDOW}s — "
                f"giving up to avoid a crash loop."
            )
            print(f"  Review logs in: {CRASH_LOG_DIR}")
            print()
            break

        print("  Auto-restarting in 3 seconds... (Ctrl+C to cancel)")
        try:
            time.sleep(3)
        except KeyboardInterrupt:
            print("\n  Shutdown requested.")
            break

        print("  Restarting server...")
        print()


# ===========================================================================
# CLI forwarding
# ===========================================================================

def start_cli(args: list[str]) -> None:
    """Forward non-serve arguments to the application's own CLI.

    The application is expected to expose a Typer / Click / argparse app
    importable via ENTRY_POINT.  Adjust the import path below if your
    project uses a different CLI entry point.
    """
    # Derive a reasonable module path from ENTRY_POINT.
    # e.g. "scottycore.main:app" → try "app.cli.main:app" first, fall back to ENTRY_POINT.
    module_path, _, attr = ENTRY_POINT.partition(":")
    parts = module_path.split(".")

    # Heuristic: look for a cli sub-module (app.cli.main or similar)
    cli_module = ".".join(parts[:-1] + ["cli", "main"]) if len(parts) > 1 else module_path

    try:
        import importlib
        mod = importlib.import_module(cli_module)
        cli_app = getattr(mod, attr, None)
        if cli_app is None:
            raise ImportError(f"No attribute '{attr}' in {cli_module!r}")
    except (ImportError, ModuleNotFoundError):
        # Fall back to ENTRY_POINT itself
        try:
            import importlib
            mod = importlib.import_module(module_path)
            cli_app = getattr(mod, attr)
        except Exception as exc:
            print(f"  ERROR: Could not load CLI from '{ENTRY_POINT}': {exc}")
            sys.exit(1)

    sys.argv = [APP_NAME.lower()] + args
    cli_app()


# ===========================================================================
# Banner
# ===========================================================================

def print_banner() -> None:
    """Print a minimal startup banner."""
    print()
    print(f"  {APP_NAME}")
    print(f"  {'─' * len(APP_NAME)}")
    print(f"  Python {sys.version_info.major}.{sys.version_info.minor}.{sys.version_info.micro}"
          f"  |  {platform.system()} {platform.machine()}")
    print()


# ===========================================================================
# Entry point
# ===========================================================================

def main() -> None:
    check_python_version()

    # If we are not in the venv (and the venv exists), re-exec into it.
    # This call does NOT return when it re-execs.
    run_in_venv()

    print_banner()

    # --- Bootstrap: create venv and install deps if this is the first run ---
    venv_created = create_venv()
    if venv_created:
        install_dependencies(force=True)
        print()
        print("  Setup complete!  Re-launching inside virtual environment...")
        print()
        venv_python = str(get_venv_python())
        os.execv(venv_python, [venv_python, __file__] + sys.argv[1:])
        # execv replaces the process; the lines below are never reached.

    install_dependencies()
    ensure_env_file()
    ensure_data_dirs()
    probe_environment()

    # --- Route on CLI args ---
    args = sys.argv[1:]

    if not args or args[0] == "serve":
        # Kill any pre-existing launcher, register ours, then serve
        kill_existing_launcher()
        write_pid_file()
        try:
            serve_args = args[1:] if args and args[0] == "serve" else args
            run_server_with_crash_watch(serve_args, first_launch=True)
        finally:
            remove_pid_file()
    else:
        # Anything else is treated as a CLI subcommand
        start_cli(args)


if __name__ == "__main__":
    main()
