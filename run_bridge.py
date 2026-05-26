"""
run_bridge.py - Push scraper code via copilot-bridge and poll for output.
NOCOPO is already running on another system.
1. Push the scraper code as code.py
2. Update status.json to trigger NOCOPO
3. Poll for output
"""
import os
import sys
import json
import base64
import time
import requests
from datetime import datetime

# Config
PAT_TOKEN = os.environ.get("GITHUB_PAT", "")
if not PAT_TOKEN:
    print("[ERROR] Set GITHUB_PAT environment variable first!")
    sys.exit(1)

GITHUB_USER = "DLI0592-PrabhatRanjan01"
REPO_NAME = "copilot-bridge"
BRANCH = "main"

API_BASE = f"https://api.github.com/repos/{GITHUB_USER}/{REPO_NAME}"
HEADERS = {
    "Authorization": f"token {PAT_TOKEN}",
    "Accept": "application/vnd.github.v3+json",
    "Content-Type": "application/json"
}


def get_file_sha(filepath):
    """Get SHA of existing file (needed for updates)."""
    resp = requests.get(f"{API_BASE}/contents/{filepath}?ref={BRANCH}", headers=HEADERS)
    if resp.status_code == 200:
        return resp.json()["sha"]
    return None


def get_file_content(filepath):
    """Get file content from GitHub."""
    resp = requests.get(f"{API_BASE}/contents/{filepath}?ref={BRANCH}", headers=HEADERS)
    if resp.status_code == 200:
        data = resp.json()
        return base64.b64decode(data["content"]).decode("utf-8"), data["sha"]
    return None, None


def push_file(filepath, content, message):
    """Push/update a file on GitHub."""
    encoded = base64.b64encode(content.encode("utf-8")).decode("utf-8")
    sha = get_file_sha(filepath)

    data = {
        "message": message,
        "content": encoded,
        "branch": BRANCH
    }
    if sha:
        data["sha"] = sha

    resp = requests.put(f"{API_BASE}/contents/{filepath}", headers=HEADERS, json=data)
    if resp.status_code in [200, 201]:
        print(f"  [OK] Pushed {filepath}")
        return True
    else:
        print(f"  [FAIL] {filepath}: {resp.status_code} - {resp.text[:200]}")
        return False


def step1_push_workflow():
    """Push GitHub Actions workflow that runs code.py and pushes output."""
    print("\n[Step 1] Pushing GitHub Actions workflow...")

    workflow_content = """name: Run Code (NOCOPO)

on:
  push:
    paths:
      - 'code.py'
      - 'status.json'
  workflow_dispatch:

permissions:
  contents: write

jobs:
  run-code:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4

      - name: Set up Python
        uses: actions/setup-python@v5
        with:
          python-version: '3.11'

      - name: Install dependencies
        run: |
          pip install requests beautifulsoup4 lxml

      - name: Check if code needs running
        id: check
        run: |
          if [ -f status.json ]; then
            STATE=$(python -c "import json; d=json.load(open('status.json')); print(d.get('state',''))")
            PUSHED_BY=$(python -c "import json; d=json.load(open('status.json')); print(d.get('pushed_by',''))")
            if [ "$STATE" = "code_ready" ] && [ "$PUSHED_BY" = "copo" ]; then
              echo "should_run=true" >> $GITHUB_OUTPUT
            else
              echo "should_run=false" >> $GITHUB_OUTPUT
            fi
          else
            echo "should_run=true" >> $GITHUB_OUTPUT
          fi

      - name: Run code.py
        if: steps.check.outputs.should_run == 'true'
        run: |
          echo "Running code.py..."
          python code.py > output_raw.txt 2>&1 || true
          echo "Exit code: $?" >> output_raw.txt

      - name: Push output
        if: steps.check.outputs.should_run == 'true'
        run: |
          git config user.name "NOCOPO-Bot"
          git config user.email "nocopo@github-actions"

          # Copy output
          cp output_raw.txt output.txt

          # Update status
          python -c "
import json
from datetime import datetime
status = {
    'state': 'output_ready',
    'pushed_by': 'nocopo',
    'iteration': 1,
    'timestamp': datetime.now().isoformat(),
    'message': 'Output ready from GitHub Actions'
}
# Try to read current iteration
try:
    with open('status.json') as f:
        old = json.load(f)
    status['iteration'] = old.get('iteration', 1)
except: pass
with open('status.json', 'w') as f:
    json.dump(status, f, indent=2)
"

          git add output.txt status.json
          git commit -m '[NOCOPO] Output ready' || echo 'No changes to commit'
          git push
"""

    return push_file(".github/workflows/run_code.yml", workflow_content,
                     "[COPO] Add NOCOPO GitHub Actions workflow")


def step2_push_scraper_code():
    """Push the scraper code as code.py."""
    print("\n[Step 2] Pushing scraper code as code.py...")

    # Read the local code_to_push.py
    code_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "code_to_push.py")
    if os.path.exists(code_path):
        with open(code_path, "r", encoding="utf-8") as f:
            code = f.read()
        print(f"  Read {len(code)} bytes from code_to_push.py")
    else:
        print(f"  [ERROR] code_to_push.py not found at {code_path}")
        return False

    return push_file("code.py", code, "[COPO] Push scraper code iteration")


def step3_update_status():
    """Update status.json to signal code is ready."""
    print("\n[Step 3] Updating status.json...")

    status = {
        "state": "code_ready",
        "pushed_by": "copo",
        "iteration": 1,
        "timestamp": datetime.now().isoformat(),
        "message": "Code ready for NOCOPO to run"
    }

    # Check existing iteration
    content, _ = get_file_content("status.json")
    if content:
        try:
            old = json.loads(content)
            status["iteration"] = old.get("iteration", 0) + 1
        except:
            pass

    return push_file("status.json", json.dumps(status, indent=2),
                     f"[COPO] Code iteration {status['iteration']} ready")


def step4_poll_for_output(max_wait=300):
    """Poll for output from GitHub Actions."""
    print(f"\n[Step 4] Polling for output (max {max_wait}s)...")
    print("  GitHub Actions may take 30-90s to start and run...\n")

    start = time.time()
    last_status = None

    while time.time() - start < max_wait:
        elapsed = int(time.time() - start)

        # Check status
        content, _ = get_file_content("status.json")
        if content:
            status = json.loads(content)
            if status != last_status:
                last_status = status
                print(f"  [{elapsed}s] Status: {status.get('state')} by {status.get('pushed_by')}")

            if status.get("state") == "output_ready" and status.get("pushed_by") == "nocopo":
                print(f"\n  Output ready! Fetching...")
                output, _ = get_file_content("output.txt")
                if output:
                    print(f"\n{'='*60}")
                    print("  OUTPUT FROM NOCOPO")
                    print(f"{'='*60}")
                    print(output[:5000])
                    if len(output) > 5000:
                        print(f"\n  ... ({len(output)} total bytes, truncated)")
                    print(f"{'='*60}")

                    # Save locally
                    out_path = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                           f"output_iteration_{status.get('iteration', 1)}.txt")
                    with open(out_path, "w", encoding="utf-8") as f:
                        f.write(output)
                    print(f"\n  Saved to: {out_path}")
                    return True

        time.sleep(15)
        print(f"  [{elapsed}s] Waiting...")

    print(f"\n  [TIMEOUT] No output after {max_wait}s.")
    print("  Check GitHub Actions: https://github.com/{}/{}/actions".format(GITHUB_USER, REPO_NAME))
    return False


def main():
    print("=" * 60)
    print("  COPILOT-BRIDGE: Push & Run Scraper")
    print("=" * 60)
    print(f"  Repo: {GITHUB_USER}/{REPO_NAME}")
    print(f"  Time: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")

    # Step 1: Push code
    if not step2_push_scraper_code():
        print("[ERROR] Failed to push code. Exiting.")
        sys.exit(1)

    # Step 2: Update status to trigger NOCOPO
    if not step3_update_status():
        print("[ERROR] Failed to update status. Exiting.")
        sys.exit(1)

    # Step 3: Poll for output from NOCOPO
    step4_poll_for_output(max_wait=300)

    print("\n[DONE] Bridge session complete.")


if __name__ == "__main__":
    main()
