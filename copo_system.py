"""
COPO SYSTEM - System with Copilot (sites blocked, can't test code)
This script:
1. Pushes Python code to GitHub
2. Polls every 10 sec to check if NOCOPO pushed output
3. Reads the output and lets user/copilot decide if satisfied
4. If not satisfied, rewrites code and pushes again
"""

import os
import sys
import time
import json
import base64
import os
import requests
from datetime import datetime

# Configuration
PAT_TOKEN = os.environ.get("GITHUB_PAT", "")
GITHUB_USER = "DLI0592-PrabhatRanjan01"
REPO_NAME = "copilot-bridge"
BRANCH = "main"
POLL_INTERVAL = 10  # seconds

API_BASE = f"https://api.github.com/repos/{GITHUB_USER}/{REPO_NAME}"
HEADERS = {
    "Authorization": f"token {PAT_TOKEN}",
    "Accept": "application/vnd.github.v3+json",
    "Content-Type": "application/json"
}


def create_repo_if_not_exists():
    """Create the repo if it doesn't exist."""
    resp = requests.get(f"https://api.github.com/repos/{GITHUB_USER}/{REPO_NAME}", headers=HEADERS)
    if resp.status_code == 404:
        print("[COPO] Creating repository...")
        data = {
            "name": REPO_NAME,
            "private": True,
            "auto_init": True,
            "description": "Copilot Bridge - Push/Pull mechanism between COPO and NOCOPO systems"
        }
        resp = requests.post("https://api.github.com/user/repos", headers=HEADERS, json=data)
        if resp.status_code == 201:
            print("[COPO] Repository created successfully!")
            time.sleep(2)  # Wait for GitHub to initialize
        else:
            print(f"[COPO] Failed to create repo: {resp.status_code} - {resp.text}")
            sys.exit(1)
    elif resp.status_code == 200:
        print("[COPO] Repository already exists.")
    else:
        print(f"[COPO] Error checking repo: {resp.status_code} - {resp.text}")
        sys.exit(1)


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
        print(f"[COPO] Pushed {filepath} successfully!")
        return True
    else:
        print(f"[COPO] Failed to push {filepath}: {resp.status_code} - {resp.text}")
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
    push_file("status.json", content, f"[COPO] Status update: {status_data.get('state', 'unknown')}")


def push_code(code_content, iteration=1):
    """Push Python code to GitHub and update status."""
    push_file("code.py", code_content, f"[COPO] Code iteration {iteration}")

    status = {
        "state": "code_ready",
        "pushed_by": "copo",
        "iteration": iteration,
        "timestamp": datetime.now().isoformat(),
        "message": f"Code iteration {iteration} ready for testing"
    }
    push_status(status)
    print(f"[COPO] Code pushed (iteration {iteration}). Waiting for NOCOPO to test...")


def wait_for_output():
    """Poll every 10 seconds until NOCOPO pushes output."""
    print(f"[COPO] Polling every {POLL_INTERVAL}s for output from NOCOPO...")

    while True:
        status = get_status()
        if status and status.get("state") == "output_ready" and status.get("pushed_by") == "nocopo":
            print("[COPO] Output received from NOCOPO!")
            output, _ = get_file_content("output.txt")
            return output, status

        time.sleep(POLL_INTERVAL)
        print(f"[COPO] Still waiting... ({datetime.now().strftime('%H:%M:%S')})")


def read_code_from_file(filepath="code_to_push.py"):
    """Read code from a local file."""
    if os.path.exists(filepath):
        with open(filepath, "r", encoding="utf-8") as f:
            return f.read()
    return None


def save_output_locally(output, iteration):
    """Save received output locally for review."""
    filename = f"output_iteration_{iteration}.txt"
    with open(filename, "w", encoding="utf-8") as f:
        f.write(output)
    print(f"[COPO] Output saved to {filename}")


def mark_satisfied():
    """Mark the process as complete/satisfied."""
    status = {
        "state": "satisfied",
        "pushed_by": "copo",
        "timestamp": datetime.now().isoformat(),
        "message": "Copilot is satisfied with the solution!"
    }
    push_status(status)
    print("[COPO] Marked as SATISFIED. Process complete!")


def main():
    print("=" * 60)
    print("  COPO SYSTEM - Copilot Bridge (Code Writer)")
    print("=" * 60)

    create_repo_if_not_exists()

    iteration = 1

    while True:
        print(f"\n{'─' * 40}")
        print(f"  Iteration {iteration}")
        print(f"{'─' * 40}")

        # Check if there's code to push from local file
        code = read_code_from_file("code_to_push.py")

        if code is None:
            print("\n[COPO] No 'code_to_push.py' found!")
            print("[COPO] Create a file named 'code_to_push.py' in this directory")
            print("[COPO] with the Python code you want to test.")
            print("[COPO] Waiting for file...")

            while not os.path.exists("code_to_push.py"):
                time.sleep(3)

            code = read_code_from_file("code_to_push.py")

        print(f"\n[COPO] Code to push:\n{'─' * 30}")
        print(code[:500] + ("..." if len(code) > 500 else ""))
        print(f"{'─' * 30}")

        # Push the code
        push_code(code, iteration)

        # Wait for output from NOCOPO
        output, status = wait_for_output()

        print(f"\n[COPO] Output from NOCOPO:\n{'═' * 30}")
        print(output)
        print(f"{'═' * 30}")

        # Save output locally
        save_output_locally(output, iteration)

        # Ask if satisfied
        print("\n[COPO] Are you satisfied with this output?")
        print("  [y] Yes - mark as complete")
        print("  [n] No  - rewrite code and push again")
        print("  [q] Quit")

        choice = input("\nChoice: ").strip().lower()

        if choice == 'y':
            mark_satisfied()
            print("\n[COPO] Process complete! Final output saved.")
            break
        elif choice == 'q':
            print("[COPO] Exiting without marking satisfied.")
            break
        else:
            # Not satisfied - user should update code_to_push.py
            print("\n[COPO] Update 'code_to_push.py' with the new code.")
            print("[COPO] Press Enter when ready to push the updated code...")
            input()
            iteration += 1

    print("\n[COPO] Session ended.")


if __name__ == "__main__":
    main()
