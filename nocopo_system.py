"""
NOCOPO SYSTEM - System without Copilot (sites unblocked, can test code)
This script:
1. Polls every 10 sec to check if COPO pushed new code
2. Pulls the detected repo (web-scraper), runs it, captures output
3. Pushes the output back to GitHub
4. Repeats until COPO marks as satisfied
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
HEADERS = {
    "Authorization": f"token {PAT_TOKEN}",
    "Accept": "application/vnd.github.v3+json",
    "Content-Type": "application/json"
}


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


def run_code(code_content):
    """Run Python code safely and capture output."""
    # Write code to a temporary file
    temp_dir = tempfile.mkdtemp()
    code_file = os.path.join(temp_dir, "test_code.py")

    with open(code_file, "w", encoding="utf-8") as f:
        f.write(code_content)

    print(f"[NOCOPO] Running code...")

    try:
        result = subprocess.run(
            [sys.executable, code_file],
            capture_output=True,
            text=True,
            timeout=EXECUTION_TIMEOUT,
            cwd=temp_dir
        )

        output_parts = []

        if result.stdout:
            output_parts.append("=== STDOUT ===")
            output_parts.append(result.stdout)

        if result.stderr:
            output_parts.append("=== STDERR ===")
            output_parts.append(result.stderr)

        output_parts.append(f"\n=== EXIT CODE: {result.returncode} ===")

        if result.returncode == 0:
            output_parts.append("=== STATUS: SUCCESS ===")
        else:
            output_parts.append("=== STATUS: FAILED ===")

        output = "\n".join(output_parts)

    except subprocess.TimeoutExpired:
        output = f"=== ERROR: Code execution timed out after {EXECUTION_TIMEOUT} seconds ===\n=== STATUS: TIMEOUT ==="
    except Exception as e:
        output = f"=== ERROR: Failed to execute code ===\n{str(e)}\n=== STATUS: ERROR ==="

    # Cleanup
    try:
        os.remove(code_file)
        os.rmdir(temp_dir)
    except:
        pass

    return output


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
            print(f"[NOCOPO] Pull failed, re-cloning...")
            shutil.rmtree(repo_dir, ignore_errors=True)
            return clone_or_pull_repo()
    else:
        print(f"[NOCOPO] Cloning {GITHUB_USER}/{TARGET_REPO}...")
        if os.path.exists(repo_dir):
            shutil.rmtree(repo_dir, ignore_errors=True)
        result = subprocess.run(
            ["git", "clone", "--branch", BRANCH, repo_url, repo_dir],
            capture_output=True, text=True, timeout=60
        )
        if result.returncode != 0:
            print(f"[NOCOPO] Clone failed: {result.stderr}")
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


def main():
    print("=" * 60)
    print("  NOCOPO SYSTEM - Copilot Bridge (Repo Runner)")
    print("=" * 60)
    print(f"[NOCOPO] Target repo: {GITHUB_USER}/{TARGET_REPO}")
    print(f"[NOCOPO] Polling every {POLL_INTERVAL}s for code from COPO...")
    print(f"[NOCOPO] Execution timeout: {EXECUTION_TIMEOUT}s")
    print("[NOCOPO] Press Ctrl+C to stop\n")

    last_processed_iteration = 0

    try:
        while True:
            # Check status
            status = get_status()

            if status is None:
                print(f"[NOCOPO] Waiting for repo/status... ({datetime.now().strftime('%H:%M:%S')})")
                time.sleep(POLL_INTERVAL)
                continue

            # Check if COPO is satisfied - stop
            if status.get("state") == "satisfied":
                print("\n[NOCOPO] COPO is satisfied! Process complete.")
                break

            # Check if there's new code to run
            if (status.get("state") == "code_ready" and
                status.get("pushed_by") == "copo" and
                status.get("iteration", 0) > last_processed_iteration):

                iteration = status["iteration"]
                print(f"\n{'─' * 40}")
                print(f"  Processing Iteration {iteration}")
                print(f"{'─' * 40}")

                # Pull and run the target repo
                print(f"[NOCOPO] Pulling and running {TARGET_REPO}...")
                output = run_target_repo()

                print(f"\n[NOCOPO] Execution output:\n{'═' * 30}")
                print(output)
                print(f"{'═' * 30}")

                # Push the output
                push_output(output, iteration)
                last_processed_iteration = iteration

                print(f"[NOCOPO] Output pushed. Waiting for next iteration...")

            else:
                print(f"[NOCOPO] Waiting... ({datetime.now().strftime('%H:%M:%S')})")

            time.sleep(POLL_INTERVAL)

    except KeyboardInterrupt:
        print("\n[NOCOPO] Stopped by user.")

    print("\n[NOCOPO] Session ended.")


if __name__ == "__main__":
    main()
