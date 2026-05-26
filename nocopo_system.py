"""
NOCOPO SYSTEM - System without Copilot (sites unblocked, can test code)
This script:
1. Monitors copilot-bridge repo for ANY changes (commit-based detection)
2. When changes detected: pulls web-scraper repo, runs it, pushes output
3. Also responds to status.json-based triggers from COPO
4. Auto-reruns when new commits appear in copilot-bridge
"""

import os
import sys
import time
import json
import base64
import subprocess
import tempfile
import shutil
import requests
from datetime import datetime

# Configuration
PAT_TOKEN = os.environ.get("GITHUB_PAT", "")
GITHUB_USER = "DLI0592-PrabhatRanjan01"
REPO_NAME = "copilot-bridge"
TARGET_REPO = "web-scraper"
BRANCH = "main"
POLL_INTERVAL = 10  # seconds
EXECUTION_TIMEOUT = 120  # max seconds to run code

API_BASE = f"https://api.github.com/repos/{GITHUB_USER}/{REPO_NAME}"
TARGET_API_BASE = f"https://api.github.com/repos/{GITHUB_USER}/{TARGET_REPO}"
HEADERS = {
    "Authorization": f"token {PAT_TOKEN}",
    "Accept": "application/vnd.github.v3+json",
    "Content-Type": "application/json"
}

# Track last known commit SHAs
last_bridge_commit = None
last_target_commit = None


def get_file_content(filepath):
    """Get file content and SHA from GitHub."""
    resp = requests.get(f"{API_BASE}/contents/{filepath}?ref={BRANCH}", headers=HEADERS)
    if resp.status_code == 200:
        data = resp.json()
        content = base64.b64decode(data["content"]).decode("utf-8")
        return content, data["sha"]
    return None, None


def push_file(filepath, content, message):
    """Push/update a file on GitHub."""
    encoded = base64.b64encode(content.encode("utf-8")).decode("utf-8")

    # Get existing SHA if file exists
    _, sha = get_file_content(filepath)

    data = {
        "message": message,
        "content": encoded,
        "branch": BRANCH
    }
    if sha:
        data["sha"] = sha

    resp = requests.put(f"{API_BASE}/contents/{filepath}", headers=HEADERS, json=data)
    if resp.status_code in [200, 201]:
        print(f"[NOCOPO] Pushed {filepath} successfully!")
        return True
    else:
        print(f"[NOCOPO] Failed to push {filepath}: {resp.status_code} - {resp.text}")
        return False


def get_status():
    """Get the current status from status.json."""
    content, _ = get_file_content("status.json")
    if content:
        return json.loads(content)
    return None


def push_status(status_data):
    """Update status.json on GitHub."""
    content = json.dumps(status_data, indent=2)
    push_file("status.json", content, f"[NOCOPO] Status update: {status_data.get('state', 'unknown')}")


def clone_or_pull_repo():
    """Clone the target repo if not exists, otherwise pull latest changes."""
    repo_dir = os.path.join(tempfile.gettempdir(), f"nocopo_{TARGET_REPO}")
    repo_url = f"https://{PAT_TOKEN}@github.com/{GITHUB_USER}/{TARGET_REPO}.git"

    if os.path.exists(os.path.join(repo_dir, ".git")):
        print(f"[NOCOPO] Pulling latest changes for {TARGET_REPO}...")
        result = subprocess.run(
            ["git", "pull", "origin", BRANCH],
            capture_output=True, text=True, cwd=repo_dir, timeout=60
        )
        if result.returncode != 0:
            print(f"[NOCOPO] Pull failed: {result.stderr[:200]}")
            print(f"[NOCOPO] Removing and re-cloning...")
            shutil.rmtree(repo_dir, ignore_errors=True)
            # Wait a moment for filesystem to release
            time.sleep(2)
            # Fresh clone (no recursion)
            if os.path.exists(repo_dir):
                shutil.rmtree(repo_dir, ignore_errors=True)
            result = subprocess.run(
                ["git", "clone", "--branch", BRANCH, repo_url, repo_dir],
                capture_output=True, text=True, timeout=60
            )
            if result.returncode != 0:
                print(f"[NOCOPO] Clone also failed: {result.stderr[:200]}")
                return None
    else:
        print(f"[NOCOPO] Cloning {GITHUB_USER}/{TARGET_REPO}...")
        if os.path.exists(repo_dir):
            shutil.rmtree(repo_dir, ignore_errors=True)
        result = subprocess.run(
            ["git", "clone", "--branch", BRANCH, repo_url, repo_dir],
            capture_output=True, text=True, timeout=60
        )
        if result.returncode != 0:
            print(f"[NOCOPO] Clone failed: {result.stderr[:200]}")
            return None

    print(f"[NOCOPO] Repo ready at: {repo_dir}")
    return repo_dir


def find_entry_point(repo_dir):
    """Find the main entry point script in the repo."""
    candidates = ["main.py", "app.py", "run.py", "scraper.py", "index.py", "start.py"]
    for name in candidates:
        path = os.path.join(repo_dir, name)
        if os.path.exists(path):
            return path
    # Fallback: find any .py file in root
    for f in os.listdir(repo_dir):
        if f.endswith(".py") and not f.startswith("__"):
            return os.path.join(repo_dir, f)
    return None


def install_requirements(repo_dir):
    """Install requirements.txt if present."""
    req_file = os.path.join(repo_dir, "requirements.txt")
    if os.path.exists(req_file):
        print(f"[NOCOPO] Installing requirements...")
        result = subprocess.run(
            [sys.executable, "-m", "pip", "install", "-r", req_file, "--quiet"],
            capture_output=True, text=True, timeout=120, cwd=repo_dir
        )
        if result.returncode == 0:
            print(f"[NOCOPO] Requirements installed.")
        else:
            print(f"[NOCOPO] Some requirements failed: {result.stderr[:200]}")


def run_target_repo():
    """Clone/pull the target repo and run it."""
    repo_dir = clone_or_pull_repo()
    if not repo_dir:
        return "=== ERROR: Failed to clone/pull target repo ===\n=== STATUS: ERROR ==="

    install_requirements(repo_dir)

    entry_point = find_entry_point(repo_dir)
    if not entry_point:
        return f"=== ERROR: No Python entry point found in {TARGET_REPO} ===\n=== STATUS: ERROR ==="

    print(f"[NOCOPO] Running: {os.path.basename(entry_point)}")

    try:
        result = subprocess.run(
            [sys.executable, entry_point],
            capture_output=True, text=True,
            timeout=EXECUTION_TIMEOUT, cwd=repo_dir
        )

        output_parts = []
        output_parts.append(f"=== Running {TARGET_REPO}/{os.path.basename(entry_point)} ===")

        if result.stdout:
            output_parts.append("=== STDOUT ===")
            output_parts.append(result.stdout)
        if result.stderr:
            output_parts.append("=== STDERR ===")
            output_parts.append(result.stderr)

        output_parts.append(f"\n=== EXIT CODE: {result.returncode} ===")
        output_parts.append(f"=== STATUS: {'SUCCESS' if result.returncode == 0 else 'FAILED'} ===")

        return "\n".join(output_parts)

    except subprocess.TimeoutExpired:
        return f"=== ERROR: {TARGET_REPO} timed out after {EXECUTION_TIMEOUT}s ===\n=== STATUS: TIMEOUT ==="
    except Exception as e:
        return f"=== ERROR: Failed to run {TARGET_REPO} ===\n{str(e)}\n=== STATUS: ERROR ==="


def push_output(output, iteration):
    """Push execution output to GitHub."""
    push_file("output.txt", output, f"[NOCOPO] Output for iteration {iteration}")

    status = {
        "state": "output_ready",
        "pushed_by": "nocopo",
        "iteration": iteration,
        "timestamp": datetime.now().isoformat(),
        "message": f"Output for iteration {iteration} ready"
    }
    push_status(status)


def get_latest_commit(repo_name):
    """Get the latest commit SHA for a repo."""
    url = f"https://api.github.com/repos/{GITHUB_USER}/{repo_name}/commits/{BRANCH}"
    resp = requests.get(url, headers=HEADERS)
    if resp.status_code == 200:
        return resp.json()["sha"]
    return None


def get_commit_info(repo_name, sha):
    """Get commit details (message, author, changed files)."""
    url = f"https://api.github.com/repos/{GITHUB_USER}/{repo_name}/commits/{sha}"
    resp = requests.get(url, headers=HEADERS)
    if resp.status_code == 200:
        data = resp.json()
        return {
            "sha": sha[:7],
            "message": data["commit"]["message"].split("\n")[0],
            "author": data["commit"]["author"]["name"],
            "files": [f["filename"] for f in data.get("files", [])]
        }
    return None


def check_bridge_changes():
    """Check if copilot-bridge repo has new commits."""
    global last_bridge_commit
    current = get_latest_commit(REPO_NAME)
    if current and current != last_bridge_commit:
        old = last_bridge_commit
        last_bridge_commit = current
        if old is not None:  # Skip first detection (initialization)
            return True, current
    return False, current


def check_target_changes():
    """Check if web-scraper repo has new commits."""
    global last_target_commit
    current = get_latest_commit(TARGET_REPO)
    if current and current != last_target_commit:
        old = last_target_commit
        last_target_commit = current
        if old is not None:
            return True, current
    return False, current


def poll_once():
    """Run a single poll cycle: detect changes, pull & run, push output. Returns result dict."""
    global last_bridge_commit, last_target_commit

    if not PAT_TOKEN:
        return {"success": False, "error": "GITHUB_PAT not set"}

    # Initialize if needed
    if last_bridge_commit is None:
        last_bridge_commit = get_latest_commit(REPO_NAME)
    if last_target_commit is None:
        last_target_commit = get_latest_commit(TARGET_REPO)

    # Get current iteration
    status = get_status()
    iteration = status.get("iteration", 0) if status else 0

    triggered = False
    trigger_reason = ""

    # 1. Check copilot-bridge for changes
    bridge_changed, bridge_sha = check_bridge_changes()
    if bridge_changed:
        info = get_commit_info(REPO_NAME, bridge_sha)
        if info and "[NOCOPO]" not in info["message"]:
            triggered = True
            trigger_reason = f"copilot-bridge changed: {info['message']}"

    # 2. Check web-scraper for changes
    target_changed, target_sha = check_target_changes()
    if target_changed:
        info = get_commit_info(TARGET_REPO, target_sha)
        if info:
            triggered = True
            trigger_reason = f"web-scraper changed: {info['message']}"

    # 3. Check status.json trigger
    if not triggered and status:
        if status.get("state") == "satisfied":
            return {"success": True, "changes": False, "message": "COPO is satisfied. Done."}
        if (status.get("state") == "code_ready" and
            status.get("pushed_by") == "copo" and
            status.get("iteration", 0) > iteration):
            triggered = True
            trigger_reason = f"COPO pushed code (iteration {status['iteration']})"
            iteration = status["iteration"]

    if not triggered:
        return {"success": True, "changes": False, "message": "No changes detected"}

    iteration += 1 if "COPO pushed" not in trigger_reason else 0
    print(f"\n[NOCOPO] Change detected - Iteration {iteration}")
    print(f"  Reason: {trigger_reason}")

    # Pull and run
    output = run_target_repo()
    print(f"\n[NOCOPO] Output:\n{output[:2000]}")

    # Push output
    push_output(output, iteration)

    return {
        "success": True,
        "changes": True,
        "iteration": iteration,
        "trigger": trigger_reason,
        "output": output[:5000]
    }


def main():
    global last_bridge_commit, last_target_commit

    once_mode = "--once" in sys.argv

    print("=" * 60)
    print("  NOCOPO SYSTEM - Copilot Bridge (Auto-Detect & Run)")
    print("=" * 60)
    print(f"[NOCOPO] Monitoring: {GITHUB_USER}/{REPO_NAME}")
    print(f"[NOCOPO] Target repo: {GITHUB_USER}/{TARGET_REPO}")
    if once_mode:
        print("[NOCOPO] Mode: SINGLE POLL (--once)")
    else:
        print(f"[NOCOPO] Polling every {POLL_INTERVAL}s for changes...")
    print(f"[NOCOPO] Execution timeout: {EXECUTION_TIMEOUT}s")
    if not once_mode:
        print("[NOCOPO] Press Ctrl+C to stop")
    print()

    if not PAT_TOKEN:
        print("[ERROR] Set GITHUB_PAT environment variable!")
        sys.exit(1)

    # Initialize: get current commit SHAs
    last_bridge_commit = get_latest_commit(REPO_NAME)
    last_target_commit = get_latest_commit(TARGET_REPO)
    print(f"[NOCOPO] Bridge commit: {last_bridge_commit[:7] if last_bridge_commit else 'N/A'}")
    print(f"[NOCOPO] Target commit: {last_target_commit[:7] if last_target_commit else 'N/A'}")

    # Initialize iteration from current status
    status = get_status()
    iteration = status.get("iteration", 0) if status else 0
    print(f"[NOCOPO] Starting at iteration: {iteration}")
    print()

    if once_mode:
        result = poll_once()
        print(f"\n[NOCOPO] Result: {json.dumps(result, indent=2, default=str)}")
        print("[NOCOPO] Single poll complete. Exiting.")
        return result

    try:
        while True:
            try:
                triggered = False
                trigger_reason = ""

                # 1. Check for changes in copilot-bridge repo
                bridge_changed, bridge_sha = check_bridge_changes()
                if bridge_changed:
                    info = get_commit_info(REPO_NAME, bridge_sha)
                    if info:
                        # Skip if this was our own push (output/status)
                        if "[NOCOPO]" not in info["message"]:
                            triggered = True
                            trigger_reason = f"copilot-bridge changed: {info['message']} (files: {', '.join(info['files'])})"

                # 2. Check for changes in web-scraper repo
                target_changed, target_sha = check_target_changes()
                if target_changed:
                    info = get_commit_info(TARGET_REPO, target_sha)
                    if info:
                        triggered = True
                        trigger_reason = f"web-scraper changed: {info['message']} (files: {', '.join(info['files'])})"

                # 3. Also check status.json-based trigger from COPO
                if not triggered:
                    status = get_status()
                    if status:
                        if status.get("state") == "satisfied":
                            print("\n[NOCOPO] COPO is satisfied! Process complete.")
                            break
                        if (status.get("state") == "code_ready" and
                            status.get("pushed_by") == "copo" and
                            status.get("iteration", 0) > iteration):
                            triggered = True
                            trigger_reason = f"COPO pushed code (iteration {status['iteration']})"
                            iteration = status["iteration"]

                # Execute if triggered
                if triggered:
                    iteration += 1 if "COPO pushed" not in trigger_reason else 0
                    print(f"\n{'─' * 50}")
                    print(f"  CHANGE DETECTED - Iteration {iteration}")
                    print(f"  Reason: {trigger_reason}")
                    print(f"{'─' * 50}")

                    # Pull and run the target repo
                    print(f"[NOCOPO] Pulling and running {TARGET_REPO}...")
                    output = run_target_repo()

                    print(f"\n[NOCOPO] Execution output:\n{'═' * 30}")
                    print(output)
                    print(f"{'═' * 30}")

                    # Push the output
                    push_output(output, iteration)
                    print(f"[NOCOPO] Output pushed. Watching for next change...")
                else:
                    print(f"[NOCOPO] No changes detected. ({datetime.now().strftime('%H:%M:%S')})")

            except (requests.exceptions.ConnectionError,
                    requests.exceptions.Timeout,
                    requests.exceptions.RequestException) as e:
                print(f"[NOCOPO] Network error (will retry): {type(e).__name__}")
                time.sleep(POLL_INTERVAL)
                continue

            time.sleep(POLL_INTERVAL)

    except KeyboardInterrupt:
        print("\n[NOCOPO] Stopped by user.")

    print("\n[NOCOPO] Session ended.")


if __name__ == "__main__":
    main()
