"""
nocopo_server.py - NOCOPO Dashboard Server (runs on the NOCOPO machine).
Monitors copilot-bridge for COPO triggers, pulls & runs code, pushes output.
Exposes HTTP API for the NOCOPO dashboard UI.

Architecture (split-system mode):
  [COPO Machine]                    [NOCOPO Machine]
  bridge_server.py ──push──> GitHub <──poll── nocopo_server.py
  dashboard.html (COPO UI)                    nocopo_dashboard.html (NOCOPO UI)
"""
import os
import sys
import json
import time
import base64
import threading
import subprocess
import tempfile
import shutil
import requests
from http.server import HTTPServer, BaseHTTPRequestHandler
from datetime import datetime

PORT = 8765  # Different port from bridge_server (8765)

# ============================================================
# GLOBAL STATE
# ============================================================
nocopo_state = {
    "running": False,         # Is polling active?
    "monitoring": False,      # Is auto-poll loop running?
    "current_step": None,
    "steps": [],
    "config": {
        "github_user": "DLI0592-PrabhatRanjan01",
        "pat_token": os.environ.get("GITHUB_PAT", ""),
        "bridge_repo": "copilot-bridge",
        "target_repo": "web-scraper",
        "branch": "main",
        "poll_interval": 10,
        "timeout": 120,
        "entry_point": "",
        "run_command": "",
    },
    "last_result": None,
    "history": [],
    "poll_status": {
        "last_check": None,
        "last_bridge_commit": None,
        "last_target_commit": None,
        "checks_count": 0,
        "triggers_count": 0,
    },
    "detected_trigger": None,
}

NOCOPO_STEPS = [
    {"id": "poll", "label": "Polling for Changes"},
    {"id": "detect", "label": "Change Detected"},
    {"id": "pull", "label": "Pulling / Cloning Repo"},
    {"id": "install", "label": "Installing Dependencies"},
    {"id": "run", "label": "Running Code"},
    {"id": "capture", "label": "Capturing Output"},
    {"id": "push_output", "label": "Pushing Output to Bridge"},
    {"id": "done", "label": "Complete"},
]


def set_step(step_id, status="running", message=""):
    for s in nocopo_state["steps"]:
        if s["id"] == step_id:
            s["status"] = status
            s["message"] = message
            s["updated_at"] = datetime.now().isoformat()
            if status == "running":
                nocopo_state["current_step"] = step_id
            break


def reset_steps():
    nocopo_state["steps"] = [
        {**s, "status": "pending", "message": "", "updated_at": None}
        for s in NOCOPO_STEPS
    ]
    nocopo_state["current_step"] = None


# ============================================================
# GITHUB HELPERS
# ============================================================
def get_headers():
    token = nocopo_state["config"]["pat_token"]
    return {
        "Authorization": f"token {token}",
        "Accept": "application/vnd.github.v3+json",
        "Content-Type": "application/json"
    }


def api_base(repo_name):
    user = nocopo_state["config"]["github_user"]
    return f"https://api.github.com/repos/{user}/{repo_name}"


def get_file_content(repo_name, filepath):
    branch = nocopo_state["config"]["branch"]
    url = f"{api_base(repo_name)}/contents/{filepath}?ref={branch}"
    resp = requests.get(url, headers=get_headers())
    if resp.status_code == 200:
        data = resp.json()
        content = base64.b64decode(data["content"]).decode("utf-8")
        return content, data["sha"]
    return None, None


def push_file(repo_name, filepath, content, message):
    encoded = base64.b64encode(content.encode("utf-8")).decode("utf-8")
    _, sha = get_file_content(repo_name, filepath)
    data = {
        "message": message,
        "content": encoded,
        "branch": nocopo_state["config"]["branch"]
    }
    if sha:
        data["sha"] = sha
    url = f"{api_base(repo_name)}/contents/{filepath}"
    resp = requests.put(url, headers=get_headers(), json=data)
    return resp.status_code in [200, 201]


def get_latest_commit(repo_name):
    branch = nocopo_state["config"]["branch"]
    url = f"{api_base(repo_name)}/commits/{branch}"
    resp = requests.get(url, headers=get_headers())
    if resp.status_code == 200:
        return resp.json()["sha"]
    return None


def get_commit_info(repo_name, sha):
    url = f"{api_base(repo_name)}/commits/{sha}"
    resp = requests.get(url, headers=get_headers())
    if resp.status_code == 200:
        data = resp.json()
        return {
            "sha": sha[:7],
            "message": data["commit"]["message"].split("\n")[0],
            "author": data["commit"]["author"]["name"],
            "files": [f["filename"] for f in data.get("files", [])]
        }
    return None


def get_status_json():
    content, _ = get_file_content(nocopo_state["config"]["bridge_repo"], "status.json")
    if content:
        return json.loads(content)
    return None


# ============================================================
# DETECTION LOGIC
# ============================================================
def detect_changes():
    """Check if COPO pushed new changes. Returns (triggered, reason)."""
    config = nocopo_state["config"]
    poll = nocopo_state["poll_status"]

    # Check copilot-bridge for new commits
    bridge_commit = get_latest_commit(config["bridge_repo"])
    if bridge_commit and bridge_commit != poll["last_bridge_commit"]:
        old = poll["last_bridge_commit"]
        poll["last_bridge_commit"] = bridge_commit
        if old is not None:
            info = get_commit_info(config["bridge_repo"], bridge_commit)
            if info and "[NOCOPO]" not in info["message"]:
                return True, f"Bridge commit: {info['message']} ({info['sha']})"

    # Check target repo for new commits
    target_commit = get_latest_commit(config["target_repo"])
    if target_commit and target_commit != poll["last_target_commit"]:
        old = poll["last_target_commit"]
        poll["last_target_commit"] = target_commit
        if old is not None:
            info = get_commit_info(config["target_repo"], target_commit)
            if info:
                return True, f"Target commit: {info['message']} ({info['sha']})"

    # Check status.json for COPO trigger
    status = get_status_json()
    if status:
        if status.get("state") == "code_ready" and status.get("pushed_by") == "copo":
            current_iter = status.get("iteration", 0)
            last_iter = nocopo_state["last_result"]["iteration"] if nocopo_state["last_result"] else 0
            if current_iter > last_iter:
                return True, f"COPO trigger: iteration {current_iter}"

    return False, "No changes"


# ============================================================
# EXECUTION PIPELINE
# ============================================================
def run_nocopo_pipeline(trigger_reason="Manual trigger"):
    """Full NOCOPO pipeline: pull, install, run, capture, push output."""
    config = nocopo_state["config"]
    nocopo_state["running"] = True
    reset_steps()

    try:
        # DETECT
        set_step("detect", "done", trigger_reason)

        # PULL
        set_step("pull", "running", f"Cloning/pulling {config['target_repo']}...")
        token = config["pat_token"]
        user = config["github_user"]
        branch = config["branch"]
        target_repo = config["target_repo"]
        repo_dir = os.path.join(tempfile.gettempdir(), f"nocopo_{target_repo}")
        repo_url = f"https://{token}@github.com/{user}/{target_repo}.git"

        if os.path.exists(os.path.join(repo_dir, ".git")):
            result = subprocess.run(
                ["git", "pull", "origin", branch],
                capture_output=True, text=True, cwd=repo_dir, timeout=60
            )
            if result.returncode != 0:
                shutil.rmtree(repo_dir, ignore_errors=True)
                time.sleep(1)
                result = subprocess.run(
                    ["git", "clone", "--branch", branch, repo_url, repo_dir],
                    capture_output=True, text=True, timeout=60
                )
                if result.returncode != 0:
                    set_step("pull", "error", f"Clone failed: {result.stderr[:200]}")
                    return {"success": False, "error": "Clone failed"}
            set_step("pull", "done", "Pulled latest")
        else:
            if os.path.exists(repo_dir):
                shutil.rmtree(repo_dir, ignore_errors=True)
            result = subprocess.run(
                ["git", "clone", "--branch", branch, repo_url, repo_dir],
                capture_output=True, text=True, timeout=60
            )
            if result.returncode != 0:
                set_step("pull", "error", f"Clone failed: {result.stderr[:200]}")
                return {"success": False, "error": "Clone failed"}
            set_step("pull", "done", "Cloned fresh")

        # INSTALL
        set_step("install", "running", "Checking dependencies...")
        req_file = os.path.join(repo_dir, "requirements.txt")
        if os.path.exists(req_file):
            subprocess.run(
                [sys.executable, "-m", "pip", "install", "-r", req_file, "--quiet"],
                capture_output=True, text=True, timeout=120, cwd=repo_dir
            )
            set_step("install", "done", "Requirements installed")
        else:
            set_step("install", "done", "No requirements.txt (skipped)")

        # RUN
        set_step("run", "running", "Executing code...")
        run_cmd = config["run_command"]
        entry = config["entry_point"]
        timeout_sec = config["timeout"]

        if run_cmd:
            cmd_parts = run_cmd.split()
        elif entry:
            entry_path = os.path.join(repo_dir, entry)
            if not os.path.exists(entry_path):
                set_step("run", "error", f"Entry not found: {entry}")
                return {"success": False, "error": f"Entry not found: {entry}"}
            cmd_parts = [sys.executable, entry_path] if entry.endswith(".py") else [entry_path]
        else:
            candidates = ["main.py", "app.py", "run.py", "scraper.py", "index.py"]
            entry_path = None
            for c in candidates:
                p = os.path.join(repo_dir, c)
                if os.path.exists(p):
                    entry_path = p
                    break
            if not entry_path:
                for f in os.listdir(repo_dir):
                    if f.endswith(".py") and not f.startswith("__"):
                        entry_path = os.path.join(repo_dir, f)
                        break
            if not entry_path:
                set_step("run", "error", "No entry point found")
                return {"success": False, "error": "No entry point"}
            cmd_parts = [sys.executable, entry_path]

        set_step("run", "running", f"Running: {' '.join(os.path.basename(c) for c in cmd_parts)}")

        try:
            proc = subprocess.run(
                cmd_parts, capture_output=True, text=True,
                timeout=timeout_sec, cwd=repo_dir
            )
            exit_code = proc.returncode
            stdout = proc.stdout
            stderr = proc.stderr
            run_status = "SUCCESS" if exit_code == 0 else "FAILED"
        except subprocess.TimeoutExpired:
            exit_code = -1
            stdout = ""
            stderr = f"TIMEOUT after {timeout_sec}s"
            run_status = "TIMEOUT"
        except Exception as e:
            exit_code = -1
            stdout = ""
            stderr = str(e)
            run_status = "ERROR"

        set_step("run", "done" if exit_code == 0 else "error",
                f"Exit code: {exit_code} ({run_status})")

        # CAPTURE
        set_step("capture", "running", "Formatting output...")
        output_parts = [
            f"=== Target: {target_repo} ===",
            f"=== Command: {' '.join(cmd_parts)} ===",
            f"=== Timestamp: {datetime.now().isoformat()} ===",
        ]
        if stdout:
            output_parts.append("\n=== STDOUT ===")
            output_parts.append(stdout)
        if stderr:
            output_parts.append("\n=== STDERR ===")
            output_parts.append(stderr)
        output_parts.append(f"\n=== EXIT CODE: {exit_code} ===")
        output_parts.append(f"=== STATUS: {run_status} ===")
        full_output = "\n".join(output_parts)
        set_step("capture", "done", f"Captured ({len(full_output)} bytes)")

        # PUSH OUTPUT
        set_step("push_output", "running", "Pushing output to copilot-bridge...")
        bridge_repo = config["bridge_repo"]
        iteration = (nocopo_state["last_result"]["iteration"] + 1) if nocopo_state["last_result"] else 1

        # Push output_iteration_X.txt
        push_file(bridge_repo, f"output_iteration_{iteration}.txt",
                  full_output, f"[NOCOPO] Output iteration {iteration}")

        # Push output.txt (latest)
        push_file(bridge_repo, "output.txt",
                  full_output, f"[NOCOPO] Update output.txt")

        # Push status.json
        output_status = {
            "state": "output_ready",
            "pushed_by": "nocopo",
            "target_repo": target_repo,
            "iteration": iteration,
            "exit_code": exit_code,
            "timestamp": datetime.now().isoformat(),
            "message": f"Output ready ({run_status})"
        }
        push_file(bridge_repo, "status.json",
                  json.dumps(output_status, indent=2),
                  f"[NOCOPO] Status: output_ready")

        set_step("push_output", "done", f"Pushed output (iteration {iteration})")

        # DONE
        set_step("done", "done", "Pipeline complete!")

        result = {
            "success": True,
            "iteration": iteration,
            "target_repo": target_repo,
            "exit_code": exit_code,
            "run_status": run_status,
            "output": full_output[:10000],
            "trigger": trigger_reason,
            "completed_at": datetime.now().isoformat(),
        }
        nocopo_state["last_result"] = result
        nocopo_state["history"].append({
            "iteration": iteration,
            "target_repo": target_repo,
            "run_status": run_status,
            "trigger": trigger_reason,
            "timestamp": datetime.now().isoformat(),
        })
        nocopo_state["poll_status"]["triggers_count"] += 1
        return result

    except Exception as e:
        if nocopo_state["current_step"]:
            set_step(nocopo_state["current_step"], "error", str(e))
        return {"success": False, "error": str(e)}
    finally:
        nocopo_state["running"] = False


# ============================================================
# AUTO-POLL LOOP
# ============================================================
def poll_loop():
    """Background thread: poll GitHub for changes and auto-trigger pipeline."""
    config = nocopo_state["config"]
    poll = nocopo_state["poll_status"]

    # Initialize commit SHAs
    poll["last_bridge_commit"] = get_latest_commit(config["bridge_repo"])
    poll["last_target_commit"] = get_latest_commit(config["target_repo"])

    while nocopo_state["monitoring"]:
        try:
            poll["last_check"] = datetime.now().isoformat()
            poll["checks_count"] += 1

            if not nocopo_state["running"]:
                triggered, reason = detect_changes()
                if triggered:
                    nocopo_state["detected_trigger"] = reason
                    run_nocopo_pipeline(reason)

        except Exception as e:
            print(f"[NOCOPO] Poll error: {e}")

        time.sleep(config["poll_interval"])


# ============================================================
# HTTP SERVER
# ============================================================
class NocopoHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def _cors_headers(self):
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type, Accept")

    def _json_response(self, data, status=200):
        body = json.dumps(data, default=str).encode("utf-8")
        try:
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self._cors_headers()
            self.end_headers()
            self.wfile.write(body)
        except (ConnectionAbortedError, ConnectionResetError, BrokenPipeError):
            pass

    def _read_body(self):
        length = int(self.headers.get("Content-Length", 0))
        if length:
            return json.loads(self.rfile.read(length).decode("utf-8"))
        return {}

    def do_OPTIONS(self):
        self.send_response(204)
        self._cors_headers()
        self.send_header("Content-Length", "0")
        self.end_headers()

    def do_GET(self):
        if self.path == "/health":
            self._json_response({"ok": True, "system": "nocopo"})

        elif self.path == "/status":
            self._json_response({
                "running": nocopo_state["running"],
                "monitoring": nocopo_state["monitoring"],
                "current_step": nocopo_state["current_step"],
                "steps": nocopo_state["steps"],
                "config": {k: v for k, v in nocopo_state["config"].items() if k != "pat_token"},
                "last_result": nocopo_state["last_result"],
                "poll_status": nocopo_state["poll_status"],
                "history": nocopo_state["history"][-20:],
                "detected_trigger": nocopo_state["detected_trigger"],
                "timestamp": datetime.now().isoformat(),
            })

        elif self.path == "/steps":
            self._json_response({
                "running": nocopo_state["running"],
                "current_step": nocopo_state["current_step"],
                "steps": nocopo_state["steps"],
            })

        elif self.path == "/history":
            self._json_response(nocopo_state["history"])

        else:
            self._json_response({"error": "Not found"}, 404)

    def do_POST(self):
        if self.path == "/config":
            body = self._read_body()
            for key in body:
                if key in nocopo_state["config"]:
                    nocopo_state["config"][key] = body[key]
            self._json_response({"message": "Config updated", "config": {
                k: v for k, v in nocopo_state["config"].items() if k != "pat_token"
            }})

        elif self.path == "/start-monitoring":
            if nocopo_state["monitoring"]:
                self._json_response({"message": "Already monitoring"})
                return
            nocopo_state["monitoring"] = True
            threading.Thread(target=poll_loop, daemon=True).start()
            self._json_response({"message": "Monitoring started"})

        elif self.path == "/stop-monitoring":
            nocopo_state["monitoring"] = False
            self._json_response({"message": "Monitoring stopped"})

        elif self.path == "/trigger":
            if nocopo_state["running"]:
                self._json_response({"error": "Pipeline already running"}, 409)
                return
            body = self._read_body()
            reason = body.get("reason", "Manual trigger from dashboard")
            threading.Thread(target=run_nocopo_pipeline, args=(reason,), daemon=True).start()
            self._json_response({"message": "Pipeline triggered", "reason": reason})

        elif self.path == "/poll-once":
            if nocopo_state["running"]:
                self._json_response({"error": "Pipeline already running"}, 409)
                return
            # Single poll check
            triggered, reason = detect_changes()
            if triggered:
                threading.Thread(target=run_nocopo_pipeline, args=(reason,), daemon=True).start()
                self._json_response({"message": "Change detected, pipeline started", "reason": reason})
            else:
                nocopo_state["poll_status"]["last_check"] = datetime.now().isoformat()
                nocopo_state["poll_status"]["checks_count"] += 1
                self._json_response({"message": "No changes detected"})

        else:
            self._json_response({"error": "Not found"}, 404)

    def log_message(self, format, *args):
        print(f"[NOCOPO] {datetime.now().strftime('%H:%M:%S')} {format % args}")

    def handle(self):
        try:
            super().handle()
        except (ConnectionAbortedError, ConnectionResetError, BrokenPipeError, OSError):
            pass


def main():
    print("=" * 60)
    print("  NOCOPO SERVER - Copilot Bridge (Remote Execution)")
    print("=" * 60)
    print(f"  URL: http://localhost:{PORT}")
    print()
    print("  GET  /health          - Health check")
    print("  GET  /status          - Full state + steps")
    print("  GET  /steps           - Current pipeline steps")
    print("  POST /config          - Update config")
    print("  POST /start-monitoring - Start auto-polling")
    print("  POST /stop-monitoring  - Stop auto-polling")
    print("  POST /trigger         - Manual trigger pipeline")
    print("  POST /poll-once       - Single poll check")
    print()

    if not nocopo_state["config"]["pat_token"]:
        print("  [WARN] GITHUB_PAT not set! Set it via /config or env var.")
    print()

    server = HTTPServer(("0.0.0.0", PORT), NocopoHandler)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n[NOCOPO] Server stopped.")
        server.server_close()


if __name__ == "__main__":
    main()
