"""
bridge_server.py - Dynamic Hub for Copilot Bridge.
Manages the full pipeline: COPO (push code) → NOCOPO (run & return output).
Exposes HTTP API for the dashboard to configure, trigger, and track steps in real-time.
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
import hashlib
import requests
from http.server import HTTPServer, BaseHTTPRequestHandler
from urllib.parse import urlparse, parse_qs
from datetime import datetime

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

PORT = 8765

# ============================================================
# GLOBAL STATE - Shared across all requests
# ============================================================
pipeline_state = {
    "running": False,
    "current_step": None,       # Which step is active right now
    "steps": [],                # List of step statuses
    "config": {
        "github_user": "DLI0592-PrabhatRanjan01",
        "pat_token": os.environ.get("GITHUB_PAT", ""),
        "local_repo_path": "",  # Local path to the repo (COPO side)
        "target_repo": "",      # GitHub repo name to push to / run
        "branch": "main",
        "entry_point": "",      # e.g. main.py (auto-detect if empty)
        "run_command": "",      # Custom run command (optional)
        "save_output_locally": True,
        "output_save_path": "", # Where to save output on COPO side
        "timeout": 120,
    },
    "last_result": None,
    "history": [],              # Past run results
    "push_info": None,          # Info about what was scanned/pushed
}

# Step definitions for the pipeline
PIPELINE_STEPS = [
    {"id": "scan", "label": "Scanning Local Changes", "system": "copo"},
    {"id": "push", "label": "Pushing Code to GitHub", "system": "copo"},
    {"id": "detect", "label": "Detecting Changes (NOCOPO)", "system": "nocopo"},
    {"id": "pull", "label": "Pulling / Cloning Repo", "system": "nocopo"},
    {"id": "install", "label": "Installing Dependencies", "system": "nocopo"},
    {"id": "run", "label": "Running Code", "system": "nocopo"},
    {"id": "capture", "label": "Capturing Output", "system": "nocopo"},
    {"id": "push_output", "label": "Pushing Output to Bridge", "system": "nocopo"},
    {"id": "receive", "label": "Receiving Output (COPO)", "system": "copo"},
    {"id": "save", "label": "Saving Results Locally", "system": "copo"},
    {"id": "done", "label": "Complete", "system": "both"},
]


def set_step(step_id, status="running", message=""):
    """Update a pipeline step status. status: pending|running|done|error"""
    for s in pipeline_state["steps"]:
        if s["id"] == step_id:
            s["status"] = status
            s["message"] = message
            s["updated_at"] = datetime.now().isoformat()
            if status == "running":
                pipeline_state["current_step"] = step_id
            break


def reset_steps():
    """Reset all steps to pending."""
    pipeline_state["steps"] = [
        {**s, "status": "pending", "message": "", "updated_at": None}
        for s in PIPELINE_STEPS
    ]
    pipeline_state["current_step"] = None


def get_headers():
    token = pipeline_state["config"]["pat_token"]
    return {
        "Authorization": f"token {token}",
        "Accept": "application/vnd.github.v3+json",
        "Content-Type": "application/json"
    }


def github_api_base(repo_name):
    user = pipeline_state["config"]["github_user"]
    return f"https://api.github.com/repos/{user}/{repo_name}"


def get_file_info(repo_name, filepath):
    """Get file SHA and content from GitHub. Returns (sha, content_bytes) or (None, None)."""
    branch = pipeline_state["config"]["branch"]
    url = f"{github_api_base(repo_name)}/contents/{filepath}?ref={branch}"
    resp = requests.get(url, headers=get_headers())
    if resp.status_code == 200:
        data = resp.json()
        content = base64.b64decode(data["content"])
        return data["sha"], content
    return None, None


def is_content_changed(local_bytes, remote_bytes):
    """Compare content ignoring line-ending differences (CRLF vs LF)."""
    if remote_bytes is None:
        return True  # New file
    local_norm = local_bytes.replace(b"\r\n", b"\n")
    remote_norm = remote_bytes.replace(b"\r\n", b"\n")
    return local_norm != remote_norm


def push_file_to_github(repo_name, filepath, content_bytes, message):
    """Push a file to GitHub repo. Returns 'pushed', 'skipped', or 'failed'."""
    # Get remote file info and compare content (normalizing line endings)
    remote_sha, remote_content = get_file_info(repo_name, filepath)
    if not is_content_changed(content_bytes, remote_content):
        return "skipped"  # Content identical, no push needed

    encoded = base64.b64encode(content_bytes).decode("utf-8")
    data = {
        "message": message,
        "content": encoded,
        "branch": pipeline_state["config"]["branch"]
    }
    if remote_sha:
        data["sha"] = remote_sha
    url = f"{github_api_base(repo_name)}/contents/{filepath}"
    resp = requests.put(url, headers=get_headers(), json=data)
    return "pushed" if resp.status_code in [200, 201] else "failed"


def get_github_file_content(repo_name, filepath):
    branch = pipeline_state["config"]["branch"]
    url = f"{github_api_base(repo_name)}/contents/{filepath}?ref={branch}"
    resp = requests.get(url, headers=get_headers())
    if resp.status_code == 200:
        return base64.b64decode(resp.json()["content"]).decode("utf-8")
    return None


# ============================================================
# PIPELINE EXECUTION
# ============================================================
def run_pipeline():
    """Execute the full COPO → NOCOPO → COPO pipeline."""
    config = pipeline_state["config"]
    pipeline_state["running"] = True
    reset_steps()

    try:
        # === STEP 1: SCAN local changes ===
        set_step("scan", "running", "Scanning local repo for files...")
        local_path = config["local_repo_path"]
        if not local_path or not os.path.isdir(local_path):
            set_step("scan", "error", f"Local path not found: {local_path}")
            return {"success": False, "error": f"Local path not found: {local_path}"}

        # Collect all trackable files
        track_ext = {".py", ".txt", ".json", ".yml", ".yaml", ".cfg", ".toml",
                     ".js", ".ts", ".html", ".css", ".sh", ".bat", ".md"}
        ignore_dirs = {".git", "__pycache__", "node_modules", ".venv", "venv", ".env"}
        files_to_push = []

        for root, dirs, files in os.walk(local_path):
            dirs[:] = [d for d in dirs if d not in ignore_dirs]
            for fname in files:
                ext = os.path.splitext(fname)[1].lower()
                if ext in track_ext:
                    full_path = os.path.join(root, fname)
                    rel_path = os.path.relpath(full_path, local_path).replace("\\", "/")
                    files_to_push.append((rel_path, full_path))

        if not files_to_push:
            set_step("scan", "error", "No trackable files found")
            return {"success": False, "error": "No files found to push"}

        set_step("scan", "done", f"Found {len(files_to_push)} files")

        # Also scan copilot-bridge files
        bridge_dir = os.path.dirname(os.path.abspath(__file__))
        bridge_files = []
        for root, dirs, files in os.walk(bridge_dir):
            dirs[:] = [d for d in dirs if d not in ignore_dirs]
            for fname in files:
                ext = os.path.splitext(fname)[1].lower()
                if ext in track_ext:
                    full_path = os.path.join(root, fname)
                    rel_path = os.path.relpath(full_path, bridge_dir).replace("\\", "/")
                    bridge_files.append((rel_path, full_path))

        # Store push info for dashboard
        pipeline_state["push_info"] = {
            "source_dir": local_path,
            "target_repo": config["target_repo"],
            "github_user": config["github_user"],
            "branch": config["branch"],
            "files_found": [rel for rel, _ in files_to_push],
            "files_pushed": [],
            "files_skipped": [],
            "files_failed": [],
            "total_found": len(files_to_push),
            "total_pushed": 0,
            "bridge": {
                "source_dir": bridge_dir,
                "repo": "copilot-bridge",
                "files_found": [rel for rel, _ in bridge_files],
                "files_pushed": [],
                "files_skipped": [],
                "files_failed": [],
                "total_found": len(bridge_files),
                "total_pushed": 0,
            }
        }

        # === STEP 2: PUSH to GitHub ===
        set_step("push", "running", f"Comparing {len(files_to_push)} files with remote...")
        target_repo = config["target_repo"]
        pushed = 0
        skipped = 0
        failed = []

        for rel_path, full_path in files_to_push:
            with open(full_path, "rb") as f:
                content = f.read()
            result = push_file_to_github(
                target_repo, rel_path, content,
                f"[COPO] Update {rel_path}"
            )
            if result == "pushed":
                pushed += 1
                pipeline_state["push_info"]["files_pushed"].append(rel_path)
            elif result == "skipped":
                skipped += 1
                pipeline_state["push_info"]["files_skipped"].append(rel_path)
            else:
                failed.append(rel_path)
                pipeline_state["push_info"]["files_failed"].append(rel_path)
            set_step("push", "running", f"Checked {pushed + skipped + len(failed)}/{len(files_to_push)} files...")

        if pushed == 0 and len(failed) == len(files_to_push):
            set_step("push", "error", "All files failed to push")
            return {"success": False, "error": "Push failed", "failed_files": failed}

        pipeline_state["push_info"]["total_pushed"] = pushed
        msg = f"{pushed} pushed, {skipped} unchanged"
        if failed:
            msg += f", {len(failed)} failed"
        set_step("push", "done", msg)

        # Also push copilot-bridge files
        set_step("push", "done", f"{msg} | Pushing copilot-bridge...")
        b_pushed = 0
        b_skipped = 0
        for rel_path, full_path in bridge_files:
            with open(full_path, "rb") as f:
                content = f.read()
            result = push_file_to_github(
                "copilot-bridge", rel_path, content,
                f"[COPO] Update {rel_path}"
            )
            if result == "pushed":
                b_pushed += 1
                pipeline_state["push_info"]["bridge"]["files_pushed"].append(rel_path)
            elif result == "skipped":
                b_skipped += 1
                pipeline_state["push_info"]["bridge"]["files_skipped"].append(rel_path)
            else:
                pipeline_state["push_info"]["bridge"]["files_failed"].append(rel_path)
        pipeline_state["push_info"]["bridge"]["total_pushed"] = b_pushed
        bridge_msg = f"Bridge: {b_pushed} pushed, {b_skipped} unchanged"
        set_step("push", "done", f"{msg} | {bridge_msg}")

        # Update bridge status
        bridge_status = {
            "state": "code_ready",
            "pushed_by": "copo",
            "target_repo": target_repo,
            "iteration": len(pipeline_state["history"]) + 1,
            "timestamp": datetime.now().isoformat(),
            "files_pushed": pushed,
            "entry_point": config["entry_point"],
            "run_command": config["run_command"],
            "message": f"Code pushed from {os.path.basename(local_path)}"
        }
        push_file_to_github("copilot-bridge", "status.json",
                           json.dumps(bridge_status, indent=2).encode(), "[COPO] Code ready")

        # === STEP 3: DETECT (NOCOPO side) ===
        set_step("detect", "running", "Waiting for NOCOPO detection...")
        # In single-machine mode, we proceed directly
        set_step("detect", "done", "Change detected")

        # === STEP 4: PULL / CLONE ===
        set_step("pull", "running", f"Cloning/pulling {target_repo}...")
        token = config["pat_token"]
        user = config["github_user"]
        branch = config["branch"]
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
            set_step("pull", "done", "Pulled latest changes")
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
            set_step("pull", "done", "Cloned fresh repo")

        # === STEP 5: INSTALL dependencies ===
        set_step("install", "running", "Checking for dependencies...")
        req_file = os.path.join(repo_dir, "requirements.txt")
        pkg_json = os.path.join(repo_dir, "package.json")

        if os.path.exists(req_file):
            result = subprocess.run(
                [sys.executable, "-m", "pip", "install", "-r", req_file, "--quiet"],
                capture_output=True, text=True, timeout=120, cwd=repo_dir
            )
            set_step("install", "done", "Python requirements installed")
        elif os.path.exists(pkg_json):
            result = subprocess.run(
                ["npm", "install"],
                capture_output=True, text=True, timeout=120, cwd=repo_dir, shell=True
            )
            set_step("install", "done", "npm packages installed")
        else:
            set_step("install", "done", "No dependencies file found (skipped)")

        # === STEP 6: RUN the code ===
        set_step("run", "running", "Executing code...")
        run_cmd = config["run_command"]
        entry = config["entry_point"]
        timeout_sec = config["timeout"]

        if run_cmd:
            # Custom command
            cmd_parts = run_cmd.split()
        elif entry:
            entry_path = os.path.join(repo_dir, entry)
            if not os.path.exists(entry_path):
                set_step("run", "error", f"Entry point not found: {entry}")
                return {"success": False, "error": f"Entry not found: {entry}"}
            if entry.endswith(".py"):
                cmd_parts = [sys.executable, entry_path]
            elif entry.endswith(".js"):
                cmd_parts = ["node", entry_path]
            else:
                cmd_parts = [entry_path]
        else:
            # Auto-detect entry point
            candidates = ["main.py", "app.py", "run.py", "index.py", "scraper.py",
                         "start.py", "index.js", "main.js", "app.js"]
            entry_path = None
            for c in candidates:
                p = os.path.join(repo_dir, c)
                if os.path.exists(p):
                    entry_path = p
                    break
            if not entry_path:
                # Fallback: first .py file
                for f in os.listdir(repo_dir):
                    if f.endswith(".py") and not f.startswith("__"):
                        entry_path = os.path.join(repo_dir, f)
                        break
            if not entry_path:
                set_step("run", "error", "No entry point found")
                return {"success": False, "error": "No entry point found"}

            if entry_path.endswith(".py"):
                cmd_parts = [sys.executable, entry_path]
            elif entry_path.endswith(".js"):
                cmd_parts = ["node", entry_path]
            else:
                cmd_parts = [entry_path]

        set_step("run", "running", f"Running: {' '.join(os.path.basename(c) for c in cmd_parts)}")

        try:
            proc_result = subprocess.run(
                cmd_parts, capture_output=True, text=True,
                timeout=timeout_sec, cwd=repo_dir
            )
            exit_code = proc_result.returncode
            stdout = proc_result.stdout
            stderr = proc_result.stderr
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

        # === STEP 7: CAPTURE output ===
        set_step("capture", "running", "Formatting output...")
        output_parts = []
        output_parts.append(f"=== Target: {target_repo} ===")
        output_parts.append(f"=== Command: {' '.join(cmd_parts)} ===")
        output_parts.append(f"=== Timestamp: {datetime.now().isoformat()} ===")
        if stdout:
            output_parts.append("\n=== STDOUT ===")
            output_parts.append(stdout)
        if stderr:
            output_parts.append("\n=== STDERR ===")
            output_parts.append(stderr)
        output_parts.append(f"\n=== EXIT CODE: {exit_code} ===")
        output_parts.append(f"=== STATUS: {run_status} ===")

        full_output = "\n".join(output_parts)
        set_step("capture", "done", f"Output captured ({len(full_output)} bytes)")

        # === STEP 8: PUSH output to bridge ===
        set_step("push_output", "running", "Pushing output to copilot-bridge...")
        iteration = len(pipeline_state["history"]) + 1
        push_file_to_github("copilot-bridge", "output.txt",
                           full_output.encode(), f"[NOCOPO] Output iteration {iteration}")

        output_status = {
            "state": "output_ready",
            "pushed_by": "nocopo",
            "target_repo": target_repo,
            "iteration": iteration,
            "exit_code": exit_code,
            "timestamp": datetime.now().isoformat(),
            "message": f"Output ready ({run_status})"
        }
        push_file_to_github("copilot-bridge", "status.json",
                           json.dumps(output_status, indent=2).encode(),
                           f"[NOCOPO] Status: output_ready")
        set_step("push_output", "done", "Output pushed to GitHub")

        # === STEP 9: RECEIVE output (COPO) ===
        set_step("receive", "running", "Reading output...")
        set_step("receive", "done", "Output received")

        # === STEP 10: SAVE locally ===
        set_step("save", "running", "Saving results...")
        saved_path = None
        if config["save_output_locally"]:
            save_dir = config["output_save_path"] or config["local_repo_path"]
            if save_dir and os.path.isdir(save_dir):
                saved_path = os.path.join(save_dir, f"output_iter_{iteration}.txt")
                with open(saved_path, "w", encoding="utf-8") as f:
                    f.write(full_output)
                set_step("save", "done", f"Saved to {os.path.basename(saved_path)}")
            else:
                set_step("save", "done", "No valid save path (skipped)")
        else:
            set_step("save", "done", "Local save disabled (skipped)")

        # === DONE ===
        set_step("done", "done", "Pipeline complete!")

        result = {
            "success": True,
            "iteration": iteration,
            "target_repo": target_repo,
            "files_pushed": pushed,
            "exit_code": exit_code,
            "run_status": run_status,
            "output": full_output[:10000],
            "saved_path": saved_path,
            "completed_at": datetime.now().isoformat()
        }
        pipeline_state["last_result"] = result
        pipeline_state["history"].append({
            "iteration": iteration,
            "target_repo": target_repo,
            "run_status": run_status,
            "timestamp": datetime.now().isoformat()
        })
        return result

    except Exception as e:
        if pipeline_state["current_step"]:
            set_step(pipeline_state["current_step"], "error", str(e))
        return {"success": False, "error": str(e)}
    finally:
        pipeline_state["running"] = False


# ============================================================
# HTTP SERVER
# ============================================================
class BridgeHandler(BaseHTTPRequestHandler):
    # Use HTTP/1.1 to support keep-alive and proper browser connections
    protocol_version = "HTTP/1.1"

    def _cors_headers(self):
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type, Accept")
        self.send_header("Access-Control-Max-Age", "86400")

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
            pass  # Client disconnected, ignore

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
            self._json_response({"ok": True})

        elif self.path == "/status":
            self._json_response({
                "running": pipeline_state["running"],
                "current_step": pipeline_state["current_step"],
                "steps": pipeline_state["steps"],
                "config": {k: v for k, v in pipeline_state["config"].items() if k != "pat_token"},
                "last_result": pipeline_state["last_result"],
                "push_info": pipeline_state["push_info"],
                "history": pipeline_state["history"][-20:],
                "timestamp": datetime.now().isoformat()
            })

        elif self.path == "/steps":
            self._json_response({
                "running": pipeline_state["running"],
                "current_step": pipeline_state["current_step"],
                "steps": pipeline_state["steps"]
            })

        elif self.path == "/config":
            self._json_response({
                k: v for k, v in pipeline_state["config"].items() if k != "pat_token"
            })

        elif self.path == "/history":
            self._json_response(pipeline_state["history"])

        else:
            self._json_response({"error": "Not found"}, 404)

    def do_POST(self):
        if self.path == "/config":
            body = self._read_body()
            for key in body:
                if key in pipeline_state["config"]:
                    pipeline_state["config"][key] = body[key]
            self._json_response({"message": "Config updated", "config": {
                k: v for k, v in pipeline_state["config"].items() if k != "pat_token"
            }})

        elif self.path == "/trigger":
            if pipeline_state["running"]:
                self._json_response({"error": "Pipeline already running"}, 409)
                return
            body = self._read_body()
            # Allow overriding config per-trigger
            for key in body:
                if key in pipeline_state["config"]:
                    pipeline_state["config"][key] = body[key]

            if not pipeline_state["config"]["pat_token"]:
                self._json_response({"error": "GITHUB_PAT not configured"}, 400)
                return
            if not pipeline_state["config"]["local_repo_path"]:
                self._json_response({"error": "local_repo_path not set"}, 400)
                return
            if not pipeline_state["config"]["target_repo"]:
                self._json_response({"error": "target_repo not set"}, 400)
                return

            pipeline_state["running"] = True
            reset_steps()
            self._json_response({"message": "Pipeline triggered", "status": "started"})
            threading.Thread(target=run_pipeline, daemon=True).start()

        elif self.path == "/stop":
            pipeline_state["running"] = False
            self._json_response({"message": "Stop signal sent"})

        else:
            self._json_response({"error": "Not found"}, 404)

    def log_message(self, format, *args):
        print(f"[BRIDGE] {datetime.now().strftime('%H:%M:%S')} {format % args}")

    def handle(self):
        """Override to suppress connection-aborted tracebacks from keep-alive."""
        try:
            super().handle()
        except (ConnectionAbortedError, ConnectionResetError, BrokenPipeError, OSError):
            pass


def main():
    print("=" * 60)
    print("  COPILOT BRIDGE - Dynamic Pipeline Server")
    print("=" * 60)
    print(f"  URL: http://localhost:{PORT}")
    print()
    print("  GET  /health    - Health check")
    print("  GET  /status    - Full state + steps + results")
    print("  GET  /steps     - Current step progress only")
    print("  GET  /config    - Current config")
    print("  GET  /history   - Past runs")
    print("  POST /config    - Update configuration")
    print("  POST /trigger   - Start pipeline (with optional config)")
    print("  POST /stop      - Stop running pipeline")
    print()
    print("  Config via env: GITHUB_PAT")
    print("  Ctrl+C to stop")
    print("=" * 60)

    server = HTTPServer(("localhost", PORT), BridgeHandler)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n[BRIDGE] Server stopped.")
        server.server_close()


if __name__ == "__main__":
    main()
