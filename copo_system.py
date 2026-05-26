"""
COPO SYSTEM - System with Copilot (sites blocked, can't test code)
This script:
1. Watches local web-scraper directory for changes
2. Pushes changed files to web-scraper GitHub repo
3. NOCOPO detects changes, runs, pushes output to copilot-bridge
4. COPO reads output from copilot-bridge and displays results
5. Loop: make more changes, push, get output
"""

import os
import sys
import time
import json
import base64
import hashlib
import requests
from datetime import datetime

# Configuration
PAT_TOKEN = os.environ.get("GITHUB_PAT", "")
GITHUB_USER = "DLI0592-PrabhatRanjan01"
REPO_NAME = "copilot-bridge"
TARGET_REPO = "web-scraper"
BRANCH = "main"
POLL_INTERVAL = 10  # seconds

# Local web-scraper directory
LOCAL_REPO_DIR = os.environ.get("WEB_SCRAPER_DIR",
    os.path.join(os.path.expanduser("~"), "Desktop", "web-scraper"))

API_BASE = f"https://api.github.com/repos/{GITHUB_USER}/{REPO_NAME}"
TARGET_API_BASE = f"https://api.github.com/repos/{GITHUB_USER}/{TARGET_REPO}"
HEADERS = {
    "Authorization": f"token {PAT_TOKEN}",
    "Accept": "application/vnd.github.v3+json",
    "Content-Type": "application/json"
}

# Files to track for changes
TRACK_EXTENSIONS = {".py", ".txt", ".json", ".yml", ".yaml", ".cfg", ".toml"}
IGNORE_FILES = {"check_bridge.py", "check_actions.py", "check_logs.py",
                "check_workflow.py", "deploy.py", "quick_push.py",
                "push_and_deploy.py", "check_content.py", "check_run.py",
                "check_v4.py", "check_v6.py", "download_artifact.py",
                "monitor_run.py", "analyze_results.py", "analyze_v3.py"}

# State
file_hashes = {}  # Track file content hashes


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
    """Poll for output - detects changes via commit SHA monitoring."""
    global last_known_commit
    print(f"[COPO] Watching for changes (commit-based detection)...")

    while True:
        # Check for new commits in bridge repo
        current_commit = get_latest_commit()
        if current_commit and current_commit != last_known_commit:
            last_known_commit = current_commit
            info = get_commit_info(current_commit)
            if info and "[NOCOPO]" in info["message"]:
                print(f"[COPO] Change detected! {info['message']}")
                # NOCOPO pushed something - check if output is ready
                status = get_status()
                if status and status.get("state") == "output_ready":
                    output, _ = get_file_content("output.txt")
                    return output, status

        # Fallback: also check status directly
        status = get_status()
        if status and status.get("state") == "output_ready" and status.get("pushed_by") == "nocopo":
            output, _ = get_file_content("output.txt")
            return output, status

        time.sleep(POLL_INTERVAL)
        print(f"[COPO] Watching... ({datetime.now().strftime('%H:%M:%S')})")


def get_latest_commit():
    """Get the latest commit SHA for the bridge repo."""
    url = f"{API_BASE}/commits/{BRANCH}"
    resp = requests.get(url, headers=HEADERS)
    if resp.status_code == 200:
        return resp.json()["sha"]
    return None


def get_commit_info(sha):
    """Get commit details."""
    url = f"{API_BASE}/commits/{sha}"
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
    global last_known_commit

    print("=" * 60)
    print("  COPO SYSTEM - Auto Push & Read Output")
    print("=" * 60)
    print(f"[COPO] Local dir: {LOCAL_REPO_DIR}")
    print(f"[COPO] Target repo: {GITHUB_USER}/{TARGET_REPO}")
    print(f"[COPO] Bridge repo: {GITHUB_USER}/{REPO_NAME}")
    print(f"[COPO] Polling every {POLL_INTERVAL}s")
    print("[COPO] Press Ctrl+C to stop\n")

    if not PAT_TOKEN:
        print("[ERROR] Set GITHUB_PAT environment variable!")
        sys.exit(1)

    if not os.path.isdir(LOCAL_REPO_DIR):
        print(f"[ERROR] Local repo not found: {LOCAL_REPO_DIR}")
        sys.exit(1)

    # Initialize commit tracking
    last_known_commit = get_latest_commit()
    print(f"[COPO] Bridge commit: {last_known_commit[:7] if last_known_commit else 'N/A'}")

    # Initialize file hashes
    scan_local_files()
    print(f"[COPO] Tracking {len(file_hashes)} files\n")

    iteration = 0

    try:
        while True:
            try:
                # 1. Check for local file changes
                changed_files = detect_local_changes()

                if changed_files:
                    iteration += 1
                    print(f"\n{'─' * 50}")
                    print(f"  LOCAL CHANGES DETECTED - Iteration {iteration}")
                    print(f"{'─' * 50}")
                    for f in changed_files:
                        print(f"  [CHANGED] {f}")

                    # Push changed files to web-scraper repo
                    print(f"\n[COPO] Pushing {len(changed_files)} file(s) to {TARGET_REPO}...")
                    pushed = push_changed_files(changed_files, iteration)

                    if pushed > 0:
                        print(f"[COPO] Pushed {pushed}/{len(changed_files)} files.")
                        print(f"[COPO] Waiting for NOCOPO to run and return output...")

                        # Wait for NOCOPO output
                        output = wait_for_output(timeout=180)
                        if output:
                            print(f"\n{'═' * 50}")
                            print("  OUTPUT FROM NOCOPO")
                            print(f"{'═' * 50}")
                            print(output[:3000])
                            if len(output) > 3000:
                                print(f"\n  ... ({len(output)} total bytes)")
                            print(f"{'═' * 50}")

                            # Save locally
                            out_file = os.path.join(LOCAL_REPO_DIR, f"output_iter_{iteration}.txt")
                            with open(out_file, "w", encoding="utf-8") as f:
                                f.write(output)
                            print(f"\n[COPO] Output saved: {out_file}")
                        else:
                            print("[COPO] Timeout waiting for output. NOCOPO may be down.")
                    else:
                        print("[COPO] No files pushed (all failed).")
                else:
                    print(f"[COPO] No local changes. ({datetime.now().strftime('%H:%M:%S')})")

            except (requests.exceptions.ConnectionError,
                    requests.exceptions.Timeout,
                    requests.exceptions.RequestException) as e:
                print(f"[COPO] Network error (will retry): {type(e).__name__}")

            time.sleep(POLL_INTERVAL)

    except KeyboardInterrupt:
        print("\n[COPO] Stopped by user.")

    print("\n[COPO] Session ended.")


def scan_local_files():
    """Build initial hash map of tracked files."""
    for fname in os.listdir(LOCAL_REPO_DIR):
        fpath = os.path.join(LOCAL_REPO_DIR, fname)
        if not os.path.isfile(fpath):
            continue
        ext = os.path.splitext(fname)[1]
        if ext not in TRACK_EXTENSIONS:
            continue
        if fname in IGNORE_FILES:
            continue
        file_hashes[fname] = hash_file(fpath)


def hash_file(filepath):
    """Get MD5 hash of file content."""
    with open(filepath, "rb") as f:
        return hashlib.md5(f.read()).hexdigest()


def detect_local_changes():
    """Detect which tracked files have changed."""
    changed = []
    for fname in os.listdir(LOCAL_REPO_DIR):
        fpath = os.path.join(LOCAL_REPO_DIR, fname)
        if not os.path.isfile(fpath):
            continue
        ext = os.path.splitext(fname)[1]
        if ext not in TRACK_EXTENSIONS:
            continue
        if fname in IGNORE_FILES:
            continue

        current_hash = hash_file(fpath)
        if fname not in file_hashes or file_hashes[fname] != current_hash:
            changed.append(fname)
            file_hashes[fname] = current_hash

    return changed


def push_changed_files(filenames, iteration):
    """Push changed files to the target (web-scraper) repo."""
    pushed = 0
    for fname in filenames:
        fpath = os.path.join(LOCAL_REPO_DIR, fname)
        with open(fpath, "rb") as f:
            content = base64.b64encode(f.read()).decode("utf-8")

        # Get existing SHA
        url = f"{TARGET_API_BASE}/contents/{fname}?ref={BRANCH}"
        resp = requests.get(url, headers=HEADERS)
        sha = resp.json().get("sha") if resp.status_code == 200 else None

        data = {
            "message": f"[COPO] Update {fname} (iteration {iteration})",
            "content": content,
            "branch": BRANCH
        }
        if sha:
            data["sha"] = sha

        resp = requests.put(f"{TARGET_API_BASE}/contents/{fname}",
                           headers=HEADERS, json=data)
        if resp.status_code in [200, 201]:
            print(f"  [OK] {fname}")
            pushed += 1
        else:
            print(f"  [FAIL] {fname}: {resp.status_code}")
    return pushed


def wait_for_output(timeout=180):
    """Poll copilot-bridge for output from NOCOPO."""
    global last_known_commit
    start = time.time()
    start_iso = datetime.now().isoformat()

    while time.time() - start < timeout:
        time.sleep(POLL_INTERVAL)

        status = get_status()
        if (status and status.get("state") == "output_ready"
                and status.get("pushed_by") == "nocopo"
                and status.get("timestamp", "") > start_iso):
            # New output arrived after we started waiting
            output, _ = get_file_content("output.txt")
            return output

        elapsed = int(time.time() - start)
        print(f"  [{elapsed}s] Waiting for NOCOPO...")

    return None


if __name__ == "__main__":
    main()
