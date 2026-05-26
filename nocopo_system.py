"""
NOCOPO SYSTEM - System without Copilot (sites unblocked, can test code)
This script:
1. Polls every 10 sec to check if COPO pushed new code
2. Pulls the code, runs it, captures output (stdout + stderr)
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
import os
import requests
from datetime import datetime

# Configuration
PAT_TOKEN = os.environ.get("GITHUB_PAT", "")
GITHUB_USER = "DLI0592-PrabhatRanjan01"
REPO_NAME = "copilot-bridge"
BRANCH = "main"
POLL_INTERVAL = 10  # seconds
EXECUTION_TIMEOUT = 60  # max seconds to run code

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
    print("  NOCOPO SYSTEM - Copilot Bridge (Code Runner)")
    print("=" * 60)
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

                # Pull the code
                code, _ = get_file_content("code.py")

                if code:
                    print(f"[NOCOPO] Code received:\n{'─' * 30}")
                    print(code[:300] + ("..." if len(code) > 300 else ""))
                    print(f"{'─' * 30}")

                    # Run the code
                    output = run_code(code)

                    print(f"\n[NOCOPO] Execution output:\n{'═' * 30}")
                    print(output)
                    print(f"{'═' * 30}")

                    # Push the output
                    push_output(output, iteration)
                    last_processed_iteration = iteration

                    print(f"[NOCOPO] Output pushed. Waiting for next iteration...")
                else:
                    print("[NOCOPO] Could not retrieve code.py")

            else:
                print(f"[NOCOPO] Waiting... ({datetime.now().strftime('%H:%M:%S')})")

            time.sleep(POLL_INTERVAL)

    except KeyboardInterrupt:
        print("\n[NOCOPO] Stopped by user.")

    print("\n[NOCOPO] Session ended.")


if __name__ == "__main__":
    main()
