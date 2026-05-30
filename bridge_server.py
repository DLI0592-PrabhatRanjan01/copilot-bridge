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
        "run_mode": "auto",    # "auto" = detect project type, "manual" = use run_command
        "skip_steps": [],        # Optional list: install,capture,save,pull
        "commit_message": "",   # Custom commit message (auto if empty)
        "save_output_locally": True,
        "output_save_path": "", # Where to save output on COPO side
        "timeout": 120,
        "mode": "single",      # "single" = all on one machine, "split" = NOCOPO on separate machine
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
    """Update a pipeline step status. status: pending|running|done|error|waiting"""
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


def parse_skip_steps(value):
    if not value:
        return set()
    if isinstance(value, str):
        parts = [v.strip().lower() for v in value.replace(";", ",").split(",")]
        return {p for p in parts if p}
    if isinstance(value, list):
        return {str(v).strip().lower() for v in value if str(v).strip()}
    return set()


def is_step_skipped(skip_steps, step_id):
    return step_id.lower() in skip_steps


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


def push_multiple_files_to_github(repo_name, files, commit_message, retries=3):
    """Push multiple files in a single commit using Git Data API (used for output push in single mode).
    files: list of {"path": "filename", "content_bytes": b"..."}
    Returns True on success.
    """
    for attempt in range(retries + 1):
        result = _try_push_multiple_files(repo_name, files, commit_message)
        if result:
            return True
        if attempt < retries:
            wait = 3 * (attempt + 1)
            time.sleep(wait)
    return False


def _try_push_multiple_files(repo_name, files, commit_message):
    """Single attempt to push multiple files in one commit via API."""
    headers = get_headers()
    branch = pipeline_state["config"]["branch"]
    base_url = github_api_base(repo_name)

    ref_url = f"{base_url}/git/refs/heads/{branch}"
    resp = requests.get(ref_url, headers=headers)
    if resp.status_code != 200:
        return False
    latest_commit_sha = resp.json()["object"]["sha"]

    commit_url = f"{base_url}/git/commits/{latest_commit_sha}"
    resp = requests.get(commit_url, headers=headers)
    if resp.status_code != 200:
        return False
    base_tree_sha = resp.json()["tree"]["sha"]

    tree_items = []
    for file_info in files:
        blob_url = f"{base_url}/git/blobs"
        blob_data = {
            "content": base64.b64encode(file_info["content_bytes"]).decode("utf-8"),
            "encoding": "base64"
        }
        resp = requests.post(blob_url, headers=headers, json=blob_data)
        if resp.status_code != 201:
            return False
        blob_sha = resp.json()["sha"]
        tree_items.append({
            "path": file_info["path"],
            "mode": "100644",
            "type": "blob",
            "sha": blob_sha
        })

    tree_url = f"{base_url}/git/trees"
    tree_data = {"base_tree": base_tree_sha, "tree": tree_items}
    resp = requests.post(tree_url, headers=headers, json=tree_data)
    if resp.status_code != 201:
        return False
    new_tree_sha = resp.json()["sha"]

    commit_create_url = f"{base_url}/git/commits"
    commit_data = {"message": commit_message, "tree": new_tree_sha, "parents": [latest_commit_sha]}
    resp = requests.post(commit_create_url, headers=headers, json=commit_data)
    if resp.status_code != 201:
        return False
    new_commit_sha = resp.json()["sha"]

    update_data = {"sha": new_commit_sha, "force": True}
    resp = requests.patch(ref_url, headers=headers, json=update_data)
    return resp.status_code == 200


# ============================================================
# GIT COMMAND HELPERS
# ============================================================
def git_run(args, cwd, timeout=60):
    """Run a git command and return (success, stdout, stderr)."""
    try:
        result = subprocess.run(
            ["git"] + args,
            capture_output=True, text=True, cwd=cwd, timeout=timeout
        )
        return result.returncode == 0, result.stdout.strip(), result.stderr.strip()
    except subprocess.TimeoutExpired:
        return False, "", "Timeout"
    except Exception as e:
        return False, "", str(e)


def git_ensure_remote_url(cwd, repo_name):
    """Ensure the git remote 'origin' uses the PAT token for auth."""
    token = pipeline_state["config"]["pat_token"]
    user = pipeline_state["config"]["github_user"]
    url = f"https://{token}@github.com/{user}/{repo_name}.git"
    git_run(["remote", "set-url", "origin", url], cwd)


def git_get_status(cwd):
    """Run git status and return detailed breakdown of changes.
    Returns dict with: staged, modified, untracked, deleted, all_changes, summary
    """
    ok, stdout, stderr = git_run(["status", "--porcelain"], cwd)
    if not ok:
        return None

    staged = []       # Files added to index (ready to commit)
    modified = []     # Modified but not staged
    untracked = []    # New files not tracked
    deleted = []      # Deleted files
    all_changes = []  # All files with their status

    for line in stdout.splitlines():
        if not line.strip():
            continue
        # Git porcelain format: XY filename
        # X = index status, Y = working tree status
        index_status = line[0] if len(line) > 0 else ' '
        work_status = line[1] if len(line) > 1 else ' '
        filepath = line[3:].strip()
        # Handle quoted paths
        if filepath.startswith('"') and filepath.endswith('"'):
            filepath = filepath[1:-1]

        file_info = {"path": filepath, "index": index_status, "working": work_status}
        all_changes.append(file_info)

        # Categorize
        if index_status == '?' and work_status == '?':
            untracked.append(filepath)
        else:
            if index_status in ('A', 'M', 'D', 'R', 'C'):
                staged.append({"path": filepath, "action": index_status})
            if index_status == 'D' or work_status == 'D':
                deleted.append(filepath)
            if work_status == 'M':
                modified.append(filepath)

    return {
        "staged": staged,
        "modified": modified,
        "untracked": untracked,
        "deleted": deleted,
        "all_changes": all_changes,
        "total_changed": len(all_changes),
        "summary": {
            "staged_count": len(staged),
            "modified_count": len(modified),
            "untracked_count": len(untracked),
            "deleted_count": len(deleted),
        }
    }


def git_count_total_files(cwd):
    """Count total tracked files in the repo using git ls-files."""
    ok, stdout, stderr = git_run(["ls-files"], cwd)
    if not ok:
        return 0
    return len([f for f in stdout.splitlines() if f.strip()])


def git_pull_latest(cwd, branch="main"):
    """Pull latest changes from remote. Stashes local changes if needed."""
    # Clean up any stale rebase state first
    rebase_merge_dir = os.path.join(cwd, ".git", "rebase-merge")
    if os.path.isdir(rebase_merge_dir):
        git_run(["rebase", "--abort"], cwd)
        import shutil
        if os.path.isdir(rebase_merge_dir):
            shutil.rmtree(rebase_merge_dir)

    # Check if there are uncommitted changes
    ok, stdout, _ = git_run(["status", "--porcelain"], cwd)
    has_changes = bool(stdout.strip())

    if has_changes:
        git_run(["stash"], cwd)

    ok, _, err = git_run(["pull", "origin", branch, "--rebase"], cwd)
    if not ok:
        # Abort failed rebase and just reset to origin
        git_run(["rebase", "--abort"], cwd)
        git_run(["reset", "--hard", f"origin/{branch}"], cwd)

    if has_changes:
        git_run(["stash", "pop"], cwd)


def git_add_commit_push(cwd, commit_message, branch="main"):
    """Stage all changes, commit, and push. Returns (success, files_committed, error)."""
    # Remove stale index.lock if it exists (from crashed git process)
    index_lock = os.path.join(cwd, ".git", "index.lock")
    if os.path.exists(index_lock):
        try:
            os.remove(index_lock)
            print(f"[COPO] Removed stale .git/index.lock in {cwd}")
        except OSError:
            pass

    # Ensure node_modules is in .gitignore
    gitignore_path = os.path.join(cwd, ".gitignore")
    ignore_entries = ["node_modules/", "node_modules"]
    needs_update = True
    if os.path.exists(gitignore_path):
        with open(gitignore_path, "r", encoding="utf-8", errors="ignore") as f:
            content = f.read()
        if any(entry in content for entry in ignore_entries):
            needs_update = False
    else:
        content = ""
    if needs_update:
        with open(gitignore_path, "a", encoding="utf-8") as f:
            f.write("\nnode_modules/\n")
        print(f"[COPO] Added node_modules/ to .gitignore in {cwd}")
        # Remove node_modules from git tracking if already tracked
        git_run(["rm", "-r", "--cached", "node_modules"], cwd, timeout=120)

    # Stage all changes (large repos need more time)
    ok, _, err = git_run(["add", "-A"], cwd, timeout=600)
    if not ok:
        return False, [], f"git add failed: {err}"

    # Check if there's anything to commit
    ok, stdout, _ = git_run(["status", "--porcelain"], cwd, timeout=120)
    if not stdout.strip():
        return True, [], ""  # Nothing to commit

    # Get list of staged files
    ok, diff_output, _ = git_run(["diff", "--cached", "--name-only"], cwd, timeout=120)
    committed_files = [f for f in diff_output.splitlines() if f.strip()]

    # Commit
    ok, _, err = git_run(["commit", "-m", commit_message], cwd, timeout=300)
    if not ok:
        if "nothing to commit" in err:
            return True, [], ""
        return False, [], f"git commit failed: {err}"

    # Clean up any stale rebase state before pulling
    rebase_merge_dir = os.path.join(cwd, ".git", "rebase-merge")
    rebase_apply_dir = os.path.join(cwd, ".git", "rebase-apply")
    if os.path.isdir(rebase_merge_dir):
        git_run(["rebase", "--abort"], cwd)
        import shutil
        if os.path.isdir(rebase_merge_dir):
            shutil.rmtree(rebase_merge_dir)
    if os.path.isdir(rebase_apply_dir):
        git_run(["rebase", "--abort"], cwd)
        import shutil
        if os.path.isdir(rebase_apply_dir):
            shutil.rmtree(rebase_apply_dir)

    # Pull --rebase BEFORE pushing (to get remote changes under our commit)
    ok, _, pull_err = git_run(["pull", "origin", branch, "--rebase"], cwd, timeout=300)
    if not ok:
        # If rebase fails due to conflicts, abort and force push
        git_run(["rebase", "--abort"], cwd)
        if os.path.isdir(rebase_merge_dir):
            import shutil
            shutil.rmtree(rebase_merge_dir)
        print(f"[COPO] Pull rebase failed, force pushing: {pull_err}")
        ok, _, err = git_run(["push", "origin", branch, "--force-with-lease"], cwd, timeout=600)
        if not ok:
            return False, committed_files, f"git push failed: {err}"
        return True, committed_files, ""

    # Push
    ok, _, err = git_run(["push", "origin", branch], cwd, timeout=600)
    if not ok:
        # Last resort: force push
        ok, _, err = git_run(["push", "origin", branch, "--force-with-lease"], cwd, timeout=600)
        if not ok:
            return False, committed_files, f"git push failed: {err}"

    return True, committed_files, ""


# ============================================================
# PIPELINE EXECUTION
# ============================================================
def run_pipeline():
    """Execute the full COPO → NOCOPO → COPO pipeline."""
    config = pipeline_state["config"]
    skip_steps = parse_skip_steps(config.get("skip_steps", []))

    try:
        # === STEP 1: SCAN local changes using git status ===
        set_step("scan", "running", "Checking git status for changes...")
        local_path = config["local_repo_path"]
        if not local_path or not os.path.isdir(local_path):
            set_step("scan", "error", f"Local path not found: {local_path}")
            return {"success": False, "error": f"Local path not found: {local_path}"}

        target_repo = config["target_repo"]
        branch = config["branch"]
        bridge_dir = os.path.dirname(os.path.abspath(__file__))

        # Ensure git remotes have PAT token
        git_ensure_remote_url(local_path, target_repo)
        git_ensure_remote_url(bridge_dir, "copilot-bridge")

        # Pull latest to avoid conflicts
        git_pull_latest(local_path, branch)
        git_pull_latest(bridge_dir, branch)

        # Get git status for target repo (detailed breakdown)
        target_status = git_get_status(local_path)
        if target_status is None:
            set_step("scan", "error", f"Not a git repo: {local_path}")
            return {"success": False, "error": f"git status failed in {local_path}"}

        # Get git status for bridge repo (detailed breakdown)
        bridge_status_info = git_get_status(bridge_dir)
        if bridge_status_info is None:
            set_step("scan", "error", f"Not a git repo: {bridge_dir}")
            return {"success": False, "error": f"git status failed in {bridge_dir}"}

        # Count total tracked files in each repo
        target_total_files = git_count_total_files(local_path)
        bridge_total_files = git_count_total_files(bridge_dir)

        # Extract file paths for processing
        target_changed_files = [f["path"] for f in target_status["all_changes"]]
        bridge_changed_files = [f["path"] for f in bridge_status_info["all_changes"]]

        # Store push info with detailed breakdown for dashboard
        pipeline_state["push_info"] = {
            "source_dir": local_path,
            "target_repo": target_repo,
            "github_user": config["github_user"],
            "branch": branch,
            "total_files_in_repo": target_total_files,
            "git_status": {
                "staged": target_status["staged"],
                "modified": target_status["modified"],
                "untracked": target_status["untracked"],
                "deleted": target_status["deleted"],
                "summary": target_status["summary"],
            },
            "files_found": target_changed_files,
            "files_pushed": [],
            "files_skipped": [],
            "files_failed": [],
            "total_found": target_status["total_changed"],
            "total_pushed": 0,
            "bridge": {
                "source_dir": bridge_dir,
                "repo": "copilot-bridge",
                "total_files_in_repo": bridge_total_files,
                "git_status": {
                    "staged": bridge_status_info["staged"],
                    "modified": bridge_status_info["modified"],
                    "untracked": bridge_status_info["untracked"],
                    "deleted": bridge_status_info["deleted"],
                    "summary": bridge_status_info["summary"],
                },
                "files_found": bridge_changed_files,
                "files_pushed": [],
                "files_skipped": [],
                "files_failed": [],
                "total_found": bridge_status_info["total_changed"],
                "total_pushed": 0,
                "total_commits": 0,
            }
        }

        # Build detailed scan message
        ts = target_status["summary"]
        bs = bridge_status_info["summary"]
        scan_msg = (
            f"Target: {target_status['total_changed']} to process "
            f"(staged:{ts['staged_count']} modified:{ts['modified_count']} "
            f"untracked:{ts['untracked_count']} deleted:{ts['deleted_count']}) "
            f"of {target_total_files} total | "
            f"Bridge: {bridge_status_info['total_changed']} to process "
            f"(staged:{bs['staged_count']} modified:{bs['modified_count']} "
            f"untracked:{bs['untracked_count']}) of {bridge_total_files} total"
        )
        set_step("scan", "done", scan_msg)

        # === STEP 2: PUSH using git add → git commit → git push ===
        set_step("push", "running", "Pushing changes to GitHub...")
        pushed = 0

        # --- Push target repo (web-scraper) ---
        if target_changed_files:
            custom_msg = config.get("commit_message", "").strip()
            if custom_msg:
                target_commit_msg = f"[COPO] {custom_msg}"
            else:
                target_commit_msg = f"[COPO] Update {', '.join(target_changed_files[:5])}"
                if len(target_changed_files) > 5:
                    target_commit_msg += f" (+{len(target_changed_files)-5} more)"

            set_step("push", "running", f"Pushing {len(target_changed_files)} files to {target_repo}...")
            ok, committed, err = git_add_commit_push(local_path, target_commit_msg, branch)
            if ok:
                pushed = len(committed)
                pipeline_state["push_info"]["files_pushed"] = committed
                pipeline_state["push_info"]["total_pushed"] = pushed
                set_step("push", "running", f"Target: {pushed} pushed")
            else:
                set_step("push", "error", f"Target push failed: {err}")
                return {"success": False, "error": f"Target push failed: {err}"}
        else:
            set_step("push", "running", "Target: no changes")

        # --- Push bridge repo (copilot-bridge) ---
        # Write status.json locally before committing (so it's included in same commit)
        bridge_status = {
            "state": "code_ready",
            "pushed_by": "copo",
            "target_repo": target_repo,
            "iteration": len(pipeline_state["history"]) + 1,
            "timestamp": datetime.now().isoformat(),
            "files_pushed": pushed,
            "entry_point": config["entry_point"],
            "run_command": config["run_command"],
            "run_mode": config.get("run_mode", "auto"),
            "skip_steps": sorted(skip_steps),
            "request_type": config.get("request_type", "output"),
            "modules_command": config.get("modules_command", ""),
            "message": f"Code pushed from {os.path.basename(local_path)}"
        }
        status_path = os.path.join(bridge_dir, "status.json")
        with open(status_path, "w", encoding="utf-8") as f:
            json.dump(bridge_status, f, indent=2)

        # Now git add/commit/push bridge repo (includes status.json + any other changes)
        custom_msg = config.get("commit_message", "").strip()
        if custom_msg:
            bridge_commit_msg = f"[COPO] {custom_msg}"
        else:
            bridge_commit_msg = f"[COPO] Bridge update (iter {bridge_status['iteration']})"

        set_step("push", "running", f"Target: {pushed} pushed | Pushing bridge...")
        ok, b_committed, err = git_add_commit_push(bridge_dir, bridge_commit_msg, branch)
        if ok:
            b_pushed = len(b_committed)
            pipeline_state["push_info"]["bridge"]["files_pushed"] = b_committed
            pipeline_state["push_info"]["bridge"]["total_pushed"] = b_pushed
            pipeline_state["push_info"]["bridge"]["total_commits"] = 1 if b_committed else 0
        else:
            # Bridge push failed - still continue (target was pushed)
            print(f"[COPO] Bridge push failed: {err}")
            b_pushed = 0

        msg = f"Target: {pushed} pushed | Bridge: {b_pushed} pushed (1 commit each)"
        set_step("push", "done", msg)

        # === STEP 3: DETECT (NOCOPO side) ===
        set_step("detect", "running", "Waiting for NOCOPO detection...")

        if config["mode"] == "split":
            # SPLIT MODE: NOCOPO runs on a separate machine
            # Mark steps as "waiting" - they haven't been processed yet
            set_step("detect", "waiting", "Waiting for NOCOPO to detect changes...")

            for wait_step in ["pull", "install", "run", "capture", "push_output"]:
                set_step(wait_step, "waiting", "Waiting for NOCOPO system...")

            # Poll for NOCOPO progress and output_ready
            set_step("receive", "running", "Polling for NOCOPO output...")
            iteration = len(pipeline_state["history"]) + 1
            max_wait = config["timeout"] + 60  # Extra time for NOCOPO overhead
            start_time = time.time()
            full_output = None
            exit_code = -1
            run_status = "WAITING"

            while time.time() - start_time < max_wait:
                if not pipeline_state["running"]:
                    break  # Stop signal received
                status_sha, status_bytes = get_file_info("copilot-bridge", "status.json")
                if status_bytes:
                    try:
                        status_data = json.loads(status_bytes.decode("utf-8"))

                        # Track NOCOPO intermediate progress
                        nocopo_step = status_data.get("nocopo_step", "")
                        nocopo_msg = status_data.get("nocopo_message", "")
                        if nocopo_step:
                            # Update steps based on NOCOPO's reported progress
                            step_order = ["detect", "pull", "install", "run", "capture", "push_output"]
                            nocopo_idx = step_order.index(nocopo_step) if nocopo_step in step_order else -1
                            for i, sid in enumerate(step_order):
                                if i < nocopo_idx:
                                    set_step(sid, "done", "Completed by NOCOPO")
                                elif i == nocopo_idx:
                                    set_step(sid, "running", nocopo_msg or f"NOCOPO: {sid}...")

                        if (status_data.get("state") == "output_ready" and
                            status_data.get("pushed_by") == "nocopo" and
                            status_data.get("iteration", 0) >= iteration):
                            # NOCOPO has finished! Mark all NOCOPO steps done
                            for sid in ["detect", "pull", "install", "run", "capture", "push_output"]:
                                set_step(sid, "done", "Completed by NOCOPO")
                            exit_code = status_data.get("exit_code", 0)
                            run_status = "SUCCESS" if exit_code == 0 else "FAILED"
                            output_content = get_github_file_content("copilot-bridge", "output.txt")
                            full_output = output_content or f"Output iteration {iteration} complete"
                            break

                        if (status_data.get("state") == "modules_ready" and
                            status_data.get("pushed_by") == "nocopo" and
                            status_data.get("iteration", 0) >= iteration):
                            # NOCOPO installed modules and pushed them to target repo
                            for sid in ["detect", "pull", "install", "run", "capture", "push_output"]:
                                set_step(sid, "done", "Completed by NOCOPO")
                            exit_code = 0
                            run_status = "MODULES_PUSHED"
                            # Pull target repo locally so COPO gets the modules
                            local_path = config["local_repo_path"]
                            if local_path and os.path.exists(os.path.join(local_path, ".git")):
                                set_step("receive", "running", "Pulling modules into local repo...")
                                pull_res = subprocess.run(
                                    ["git", "pull", "origin", branch],
                                    capture_output=True, text=True,
                                    cwd=local_path, timeout=600
                                )
                                if pull_res.returncode == 0:
                                    full_output = f"Modules installed by NOCOPO and pulled to local repo.\n{pull_res.stdout}"
                                else:
                                    full_output = f"Modules pushed by NOCOPO but local pull failed: {pull_res.stderr}\nRun 'git pull' manually in {local_path}"
                            else:
                                full_output = f"Modules installed & pushed to {target_repo} by NOCOPO.\nRun 'git pull' in your local repo to get them."
                            break
                    except (json.JSONDecodeError, UnicodeDecodeError):
                        pass
                elapsed = int(time.time() - start_time)
                set_step("receive", "running", f"Waiting for NOCOPO... ({elapsed}s)")
                time.sleep(5)

            if full_output is None:
                if not pipeline_state["running"]:
                    set_step("receive", "error", "Pipeline stopped by user")
                    return {"success": False, "error": "Pipeline stopped"}
                # Mark unfinished NOCOPO steps as error
                for sid in ["detect", "pull", "install", "run", "capture", "push_output"]:
                    step_data = next((s for s in pipeline_state["steps"] if s["id"] == sid), None)
                    if step_data and step_data["status"] == "waiting":
                        set_step(sid, "error", "NOCOPO did not respond")
                set_step("receive", "error", f"Timeout waiting for NOCOPO ({max_wait}s) - Is NOCOPO system running?")
                return {"success": False, "error": "NOCOPO did not respond in time. Check if nocopo_server.py is running."}

            set_step("receive", "done", f"Output received from NOCOPO ({run_status})")

        else:
            # SINGLE MODE: Run everything locally (original behavior)
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
            if is_step_skipped(skip_steps, "install"):
                set_step("install", "done", "Skipped by config")
            else:
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
                if sys.platform == "win32":
                    cmd_parts = ["powershell", "-NoProfile", "-NonInteractive",
                                 "-ExecutionPolicy", "Bypass", "-Command", run_cmd]
                else:
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
                candidates = ["main.py", "app.py", "run.py", "index.py", "scraper.py",
                             "start.py", "index.js", "main.js", "app.js"]
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
            if is_step_skipped(skip_steps, "capture"):
                set_step("capture", "done", "Skipped by config")
                full_output = stdout if stdout else ""
                if stderr:
                    full_output += ("\n" if full_output else "") + f"[STDERR] {stderr}"
                if not full_output.strip():
                    full_output = f"Capture skipped. Exit code: {exit_code} ({run_status})"
            else:
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

            # Build all output files for single commit
            output_status = {
                "state": "output_ready",
                "pushed_by": "nocopo",
                "target_repo": target_repo,
                "iteration": iteration,
                "exit_code": exit_code,
                "skip_steps": sorted(skip_steps),
                "timestamp": datetime.now().isoformat(),
                "message": f"Output ready ({run_status})"
            }

            iter_filename = f"output_iteration_{iteration}.txt"
            output_files = [
                {"path": iter_filename, "content_bytes": full_output.encode()},
                {"path": "output.txt", "content_bytes": full_output.encode()},
                {"path": "status.json", "content_bytes": json.dumps(output_status, indent=2).encode()},
            ]

            # Build commit message: use config message or auto-generate
            custom_msg = config.get("commit_message", "").strip()
            if custom_msg:
                output_commit_msg = f"[NOCOPO] {custom_msg}"
            else:
                file_names = ", ".join(f["path"] for f in output_files)
                output_commit_msg = f"[NOCOPO] Output iteration {iteration} ({run_status}) - {file_names}"

            # Single commit for all output files
            success = push_multiple_files_to_github("copilot-bridge", output_files, output_commit_msg)
            if success:
                for f in output_files:
                    if f["path"] not in pipeline_state["push_info"]["bridge"]["files_pushed"]:
                        pipeline_state["push_info"]["bridge"]["files_pushed"].append(f["path"])
                        pipeline_state["push_info"]["bridge"]["total_pushed"] += 1
                pipeline_state["push_info"]["bridge"]["total_commits"] += 1
            else:
                # Fallback: individual pushes with same commit message
                for f in output_files:
                    push_file_to_github("copilot-bridge", f["path"], f["content_bytes"], output_commit_msg)

            # Remove from skipped list any files that were pushed later in the pipeline
            pushed_set = set(pipeline_state["push_info"]["bridge"]["files_pushed"])
            pipeline_state["push_info"]["bridge"]["files_skipped"] = [
                f for f in pipeline_state["push_info"]["bridge"]["files_skipped"]
                if f not in pushed_set
            ]
            set_step("push_output", "done", f"Output pushed ({len(output_files)} files, 1 commit)")

        # === STEP 9: RECEIVE output (COPO) ===
        if config["mode"] != "split":
            set_step("receive", "running", "Reading output...")
            set_step("receive", "done", "Output received")

        # === STEP 10: SAVE locally ===
        iteration = iteration if 'iteration' in dir() else len(pipeline_state["history"]) + 1
        saved_path = None
        if is_step_skipped(skip_steps, "save"):
            set_step("save", "done", "Skipped by config")
        elif config["save_output_locally"]:
            set_step("save", "running", "Saving results...")
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
