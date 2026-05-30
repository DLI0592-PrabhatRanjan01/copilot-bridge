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

PORT = 8766  # Different port from bridge_server (8765)

# ============================================================
# GLOBAL STATE
# ============================================================
nocopo_state = {
    "running": False,         # Is polling active?
    "monitoring": False,      # Is auto-poll loop running?
    "current_step": None,
    "steps": [],
    "transfer_info": None,
    "config": {
        "github_user": "DLI0592-PrabhatRanjan01",
        "pat_token": os.environ.get("GITHUB_PAT", ""),
        "bridge_repo": "copilot-bridge",
        "target_repo": "web-scraper",
        "repo_base_dir": os.path.join(tempfile.gettempdir(), "nocopo_repos"),
        "branch": "main",
        "poll_interval": 10,
        "timeout": 120,
        "skip_steps": [],
        "entry_point": "",
        "run_command": "",
        "run_mode": "auto",       # "auto" = detect project type, "manual" = use run_command
        "commit_message": "",
    },
    "last_result": None,
    "history": [],
    "poll_status": {
        "last_check": None,
        "last_bridge_commit": None,
        "last_target_commit": None,
        "last_status_timestamp": "",
        "checks_count": 0,
        "triggers_count": 0,
    },
    "detected_trigger": None,
    "custom_command_result": None,
}

NOCOPO_STEPS = [
    {"id": "poll", "label": "Polling for Changes", "output": ""},
    {"id": "detect", "label": "Change Detected", "output": ""},
    {"id": "pull", "label": "Pulling / Cloning Repo", "output": ""},
    {"id": "install", "label": "Installing Dependencies", "output": ""},
    {"id": "run", "label": "Running Code", "output": ""},
    {"id": "capture", "label": "Capturing Output", "output": ""},
    {"id": "push_output", "label": "Pushing Output to Bridge", "output": ""},
    {"id": "done", "label": "Complete", "output": ""},
]


def set_step(step_id, status="running", message="", output=""):
    for s in nocopo_state["steps"]:
        if s["id"] == step_id:
            s["status"] = status
            s["message"] = message
            if output:
                if s.get("output"):
                    s["output"] += "\n" + output
                else:
                    s["output"] = output
            s["updated_at"] = datetime.now().isoformat()
            if status == "running":
                nocopo_state["current_step"] = step_id
            break


def reset_steps():
    nocopo_state["steps"] = [
        {**s, "status": "pending", "message": "", "output": "", "updated_at": None}
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


def push_multiple_files(repo_name, files, commit_message):
    """Push multiple files in a single commit using Git Data API.
    files: list of {"path": "filename", "content": "string content"}
    Returns True on success.
    """
    headers = get_headers()
    branch = nocopo_state["config"]["branch"]
    base_url = api_base(repo_name)

    # 1. Get latest commit SHA for the branch
    ref_url = f"{base_url}/git/refs/heads/{branch}"
    resp = requests.get(ref_url, headers=headers)
    if resp.status_code != 200:
        return False
    latest_commit_sha = resp.json()["object"]["sha"]

    # 2. Get the tree SHA from that commit
    commit_url = f"{base_url}/git/commits/{latest_commit_sha}"
    resp = requests.get(commit_url, headers=headers)
    if resp.status_code != 200:
        return False
    base_tree_sha = resp.json()["tree"]["sha"]

    # 3. Create blobs for each file
    tree_items = []
    for file_info in files:
        blob_url = f"{base_url}/git/blobs"
        blob_data = {
            "content": file_info["content"],
            "encoding": "utf-8"
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

    # 4. Create new tree
    tree_url = f"{base_url}/git/trees"
    tree_data = {
        "base_tree": base_tree_sha,
        "tree": tree_items
    }
    resp = requests.post(tree_url, headers=headers, json=tree_data)
    if resp.status_code != 201:
        return False
    new_tree_sha = resp.json()["sha"]

    # 5. Create commit
    commit_create_url = f"{base_url}/git/commits"
    commit_data = {
        "message": commit_message,
        "tree": new_tree_sha,
        "parents": [latest_commit_sha]
    }
    resp = requests.post(commit_create_url, headers=headers, json=commit_data)
    if resp.status_code != 201:
        return False
    new_commit_sha = resp.json()["sha"]

    # 6. Update branch reference
    update_data = {"sha": new_commit_sha, "force": False}
    resp = requests.patch(ref_url, headers=headers, json=update_data)
    return resp.status_code == 200


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


def clone_or_reuse_repo(repo_url, branch, repo_dir):
    """Clone into repo_dir when missing; otherwise reuse the existing directory."""
    if os.path.isdir(repo_dir) and os.path.exists(os.path.join(repo_dir, ".git")):
        return True, "pull", f"Repository exists, will pull in: {repo_dir}"

    if os.path.isdir(repo_dir) and os.listdir(repo_dir):
        return False, None, f"Directory exists but is not a git repo: {repo_dir}"

    parent = os.path.dirname(repo_dir)
    if parent:
        os.makedirs(parent, exist_ok=True)

    result = subprocess.run(
        ["git", "clone", "--branch", branch, repo_url, repo_dir],
        capture_output=True, text=True, timeout=60
    )
    if result.returncode == 0:
        return True, "clone", f"Cloned fresh -> {repo_dir}"

    error_text = (result.stderr or result.stdout or "Unknown clone error")[:200]
    return False, None, f"Clone failed: {error_text}"


def resolve_repo_dir(target_repo):
    base_dir = nocopo_state["config"].get("repo_base_dir", "").strip() or os.path.join(tempfile.gettempdir(), "nocopo_repos")
    safe_repo = target_repo.replace("/", "_").replace("\\", "_").strip() or "target_repo"
    return os.path.abspath(os.path.join(base_dir, safe_repo))


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


def normalize_command_for_windows(cmd, cwd):
    if not isinstance(cmd, list) or not cmd:
        return cmd, False
    normalized = list(cmd)
    first = normalized[0]
    first_lower = first.lower()

    if sys.platform == "win32":
        if first_lower in ("./mvnw", "mvnw"):
            wrapper = os.path.join(cwd, "mvnw.cmd")
            if os.path.exists(wrapper):
                normalized[0] = wrapper
        elif first_lower in ("./gradlew", "gradlew"):
            wrapper = os.path.join(cwd, "gradlew.bat")
            if os.path.exists(wrapper):
                normalized[0] = wrapper

    shell_needed = False
    if sys.platform == "win32" and normalized:
        launcher = os.path.basename(normalized[0]).lower()
        shell_needed = launcher in {
            "npm", "npm.cmd", "npx", "npx.cmd",
            "mvn", "mvn.cmd", "gradle", "gradle.bat", "gradlew.bat", "mvnw.cmd"
        }
    return normalized, shell_needed


def summarize_status(status):
    if not status:
        return None
    return {
        "state": status.get("state"),
        "pushed_by": status.get("pushed_by"),
        "iteration": status.get("iteration"),
        "target_repo": status.get("target_repo"),
        "request_type": status.get("request_type"),
        "timestamp": status.get("timestamp"),
        "message": status.get("message"),
    }


def get_repo_snapshot(repo_name, sha):
    info = get_commit_info(repo_name, sha) if sha else None
    return {
        "repo": repo_name,
        "sha": info["sha"] if info else (sha[:7] if sha else None),
        "message": info["message"] if info else None,
        "author": info["author"] if info else None,
        "files": info["files"] if info else [],
    }


def build_idle_transfer_info(note=None):
    config = nocopo_state["config"]
    status = get_status_json()
    poll = nocopo_state["poll_status"]
    bridge_sha = poll.get("last_bridge_commit") or get_latest_commit(config["bridge_repo"])
    target_sha = poll.get("last_target_commit") or get_latest_commit(config["target_repo"])
    state = status.get("state") if status else None
    summary = note
    if state == "satisfied":
        summary = summary or "COPO marked the workflow satisfied. Monitoring can stop."
    elif state in ("output_ready", "modules_ready"):
        summary = summary or "NOCOPO has already pushed the latest result."
    else:
        summary = summary or "Watching for the next COPO or target-repo change."

    return {
        "status": "satisfied" if state == "satisfied" else "idle",
        "summary": summary,
        "trigger": nocopo_state.get("detected_trigger"),
        "bridge": get_repo_snapshot(config["bridge_repo"], bridge_sha),
        "target": get_repo_snapshot(config["target_repo"], target_sha),
        "status_json": summarize_status(status),
        "pulled": None,
        "pushed": None,
        "updated_at": datetime.now().isoformat(),
    }


# ============================================================
# PROJECT DETECTION & MULTI-SERVICE RUN
# ============================================================
def detect_project_type(repo_dir):
    """Detect project type(s) in the repo. Returns list of detected services.
    Each service: {"type": "python"|"react"|"node"|"springboot"|"maven"|"gradle"|"static",
                   "dir": path, "name": str, "install_cmd": [...], "run_cmd": [...]}
    """
    services = []

    # Check for monorepo structure (frontend/ backend/ dirs)
    subdirs_to_check = [repo_dir]
    for name in os.listdir(repo_dir):
        sub = os.path.join(repo_dir, name)
        if os.path.isdir(sub) and not name.startswith('.') and name != 'node_modules':
            subdirs_to_check.append(sub)

    for check_dir in subdirs_to_check:
        rel_name = os.path.relpath(check_dir, repo_dir) if check_dir != repo_dir else "root"
        detected_list = _detect_all_in_dir(check_dir, rel_name)
        services.extend(detected_list)

    # Deduplicate: if root detected AND a subdir detected same type, prefer subdir
    if len(services) > 1:
        non_root = [s for s in services if s["dir"] != repo_dir]
        root_services = [s for s in services if s["dir"] == repo_dir]
        # Keep root only if it has a unique type not in subdirs
        subdir_types = {s["type"] for s in non_root}
        for rs in root_services:
            if rs["type"] not in subdir_types:
                non_root.append(rs)
        if non_root:
            services = non_root

    # Sort: backends first (springboot, maven, python), then frontends (react, node)
    priority = {"springboot": 0, "maven": 1, "gradle": 2, "python": 3, "node": 4, "react": 5, "static": 6}
    services.sort(key=lambda s: priority.get(s["type"], 99))

    return services


def _detect_all_in_dir(project_dir, name):
    """Detect ALL project types in a directory. Returns a list (can have multiple if e.g. pom.xml + package.json coexist)."""
    results = []
    pom_xml = os.path.join(project_dir, "pom.xml")
    build_gradle = os.path.join(project_dir, "build.gradle")
    package_json = os.path.join(project_dir, "package.json")
    requirements_txt = os.path.join(project_dir, "requirements.txt")
    setup_py = os.path.join(project_dir, "setup.py")
    pyproject = os.path.join(project_dir, "pyproject.toml")

    # Spring Boot / Maven
    if os.path.exists(pom_xml):
        try:
            with open(pom_xml, 'r', encoding='utf-8') as f:
                pom_content = f.read()
            if 'spring-boot' in pom_content:
                results.append({
                    "type": "springboot",
                    "dir": project_dir,
                    "name": f"{name}-backend" if name == "root" else name,
                    "install_cmd": ["mvn", "clean", "install", "-DskipTests", "-q"] if shutil.which("mvn") else ["./mvnw", "clean", "install", "-DskipTests", "-q"],
                    "run_cmd": ["mvn", "spring-boot:run"] if shutil.which("mvn") else ["./mvnw", "spring-boot:run"],
                    "is_server": True,
                    "startup_wait": 15,
                })
            else:
                results.append({
                    "type": "maven",
                    "dir": project_dir,
                    "name": f"{name}-backend" if name == "root" else name,
                    "install_cmd": ["mvn", "clean", "install", "-DskipTests", "-q"] if shutil.which("mvn") else ["./mvnw", "clean", "install", "-DskipTests", "-q"],
                    "run_cmd": ["mvn", "exec:java", "-q"] if shutil.which("mvn") else ["./mvnw", "exec:java", "-q"],
                    "is_server": False,
                    "startup_wait": 5,
                })
        except:
            pass

    # Gradle
    if os.path.exists(build_gradle) and not any(r["type"] in ("springboot", "maven") for r in results):
        try:
            with open(build_gradle, 'r', encoding='utf-8') as f:
                gradle_content = f.read()
            if 'spring-boot' in gradle_content or 'org.springframework.boot' in gradle_content:
                gradle_cmd = "gradle" if shutil.which("gradle") else ("./gradlew" if not sys.platform.startswith("win") else "gradlew.bat")
                results.append({
                    "type": "springboot",
                    "dir": project_dir,
                    "name": f"{name}-backend" if name == "root" else name,
                    "install_cmd": [gradle_cmd, "build", "-x", "test", "-q"],
                    "run_cmd": [gradle_cmd, "bootRun"],
                    "is_server": True,
                    "startup_wait": 15,
                })
        except:
            pass

    # React / Node.js (check package.json)
    if os.path.exists(package_json):
        try:
            with open(package_json, 'r', encoding='utf-8') as f:
                pkg = json.load(f)
            deps = {**pkg.get("dependencies", {}), **pkg.get("devDependencies", {})}
            scripts = pkg.get("scripts", {})

            # React app
            if "react" in deps or "react-scripts" in deps or "next" in deps or "vite" in deps:
                if "build" in scripts:
                    run_cmd = ["npm", "run", "build"]
                    is_server = False
                elif "start" in scripts:
                    run_cmd = ["npm", "start"]
                    is_server = True
                else:
                    run_cmd = ["npm", "run", "dev"]
                    is_server = True
                results.append({
                    "type": "react",
                    "dir": project_dir,
                    "name": f"{name}-frontend" if name == "root" else name,
                    "install_cmd": ["npm", "install"],
                    "run_cmd": run_cmd,
                    "is_server": is_server,
                    "startup_wait": 10,
                })
            elif not results:
                # Regular Node.js — only if no other type detected in this dir
                run_cmd = None
                is_server = False
                if "start" in scripts:
                    run_cmd = ["npm", "start"]
                    is_server = True
                elif "main" in pkg:
                    run_cmd = ["node", pkg["main"]]
                else:
                    for candidate in ["index.js", "main.js", "app.js", "server.js"]:
                        if os.path.exists(os.path.join(project_dir, candidate)):
                            run_cmd = ["node", candidate]
                            is_server = "server" in candidate or "app" in candidate
                            break
                if run_cmd:
                    results.append({
                        "type": "node",
                        "dir": project_dir,
                        "name": name,
                        "install_cmd": ["npm", "install"],
                        "run_cmd": run_cmd,
                        "is_server": is_server,
                        "startup_wait": 5,
                    })
        except:
            pass

    # Python
    if os.path.exists(requirements_txt) or os.path.exists(setup_py) or os.path.exists(pyproject):
        install_cmd = None
        if os.path.exists(requirements_txt):
            install_cmd = [sys.executable, "-m", "pip", "install", "-r", "requirements.txt", "--quiet"]
        elif os.path.exists(pyproject):
            install_cmd = [sys.executable, "-m", "pip", "install", ".", "--quiet"]

        run_cmd = None
        candidates = ["main.py", "app.py", "run.py", "manage.py", "scraper.py", "index.py", "start.py"]
        for c in candidates:
            if os.path.exists(os.path.join(project_dir, c)):
                run_cmd = [sys.executable, c]
                break
        if not run_cmd:
            for f in os.listdir(project_dir):
                if f.endswith(".py") and not f.startswith("__") and f != "setup.py":
                    run_cmd = [sys.executable, f]
                    break
        if run_cmd:
            results.append({
                "type": "python",
                "dir": project_dir,
                "name": name,
                "install_cmd": install_cmd,
                "run_cmd": run_cmd,
                "is_server": False,
                "startup_wait": 0,
            })

    return results


def run_service(service, timeout_sec, capture_server_output=True):
    """Run a single service and capture output.
    For servers (is_server=True): start, wait for startup, capture initial output, then kill.
    For scripts: run to completion.
    Returns (exit_code, stdout, stderr, run_status)
    """
    cmd = service["run_cmd"]
    cwd = service["dir"]
    is_server = service.get("is_server", False)

    if is_server and capture_server_output:
        # For servers: start process, wait for startup, capture output, kill
        startup_wait = min(service.get("startup_wait", 10), timeout_sec)
        try:
            proc = subprocess.Popen(
                cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                cwd=cwd, text=True,
                creationflags=subprocess.CREATE_NEW_PROCESS_GROUP if sys.platform == "win32" else 0
            )
            time.sleep(startup_wait)
            # Check if still running (server started successfully)
            if proc.poll() is None:
                # Server is running - capture what output we have
                # Give it a moment to produce output
                time.sleep(2)
                proc.terminate()
                try:
                    stdout, stderr = proc.communicate(timeout=10)
                except subprocess.TimeoutExpired:
                    proc.kill()
                    stdout, stderr = proc.communicate()
                return 0, stdout, stderr + "\n[Server started successfully, terminated after capture]", "SUCCESS"
            else:
                # Server exited on its own (error or quick task)
                stdout, stderr = proc.communicate()
                exit_code = proc.returncode
                return exit_code, stdout, stderr, "SUCCESS" if exit_code == 0 else "FAILED"
        except Exception as e:
            return -1, "", str(e), "ERROR"
    else:
        # For scripts: run to completion
        try:
            proc = subprocess.run(
                cmd, capture_output=True, text=True,
                timeout=timeout_sec, cwd=cwd
            )
            return proc.returncode, proc.stdout, proc.stderr, "SUCCESS" if proc.returncode == 0 else "FAILED"
        except subprocess.TimeoutExpired:
            return -1, "", f"TIMEOUT after {timeout_sec}s", "TIMEOUT"
        except Exception as e:
            return -1, "", str(e), "ERROR"


def install_service(service):
    """Install dependencies for a service. Returns (success, message)."""
    if not service.get("install_cmd"):
        return True, "No install needed"
    try:
        cmd, shell_needed = normalize_command_for_windows(service["install_cmd"], service["dir"])
        result = subprocess.run(
            cmd,
            capture_output=True, text=True,
            timeout=300, cwd=service["dir"],
            shell=shell_needed
        )
        if result.returncode == 0:
            return True, f"{service['type']} deps installed"
        else:
            err_text = (result.stderr or result.stdout or "Unknown install failure")[:200]
            return False, f"Install failed: {err_text}"
    except subprocess.TimeoutExpired:
        return False, "Install timeout (300s)"
    except FileNotFoundError as e:
        missing = service["install_cmd"][0] if isinstance(service.get("install_cmd"), list) and service["install_cmd"] else "command"
        return False, f"Install error: command not found ({missing}) - {e}"
    except Exception as e:
        return False, f"Install error: {str(e)}"


# ============================================================
# DETECTION LOGIC
# ============================================================
def update_bridge_progress(step_id, message=""):
    """Push intermediate progress to copilot-bridge status.json so COPO can track steps."""
    config = nocopo_state["config"]
    try:
        current_status, sha = get_file_content(config["bridge_repo"], "status.json")
        if current_status:
            status_data = json.loads(current_status)
        else:
            status_data = {}
        status_data["nocopo_step"] = step_id
        status_data["nocopo_message"] = message
        status_data["nocopo_updated_at"] = datetime.now().isoformat()
        push_file(config["bridge_repo"], "status.json",
                  json.dumps(status_data, indent=2),
                  f"[NOCOPO] Progress: {step_id} - {message}")
    except Exception as e:
        print(f"[NOCOPO] Warning: Failed to push progress update: {e}")


def detect_changes():
    """Check if COPO pushed new changes. Returns (triggered, reason)."""
    config = nocopo_state["config"]
    poll = nocopo_state["poll_status"]

    # Check copilot-bridge for new commits
    bridge_commit = get_latest_commit(config["bridge_repo"])
    if bridge_commit and bridge_commit != poll["last_bridge_commit"]:
        old = poll["last_bridge_commit"]
        if old is not None:
            info = get_commit_info(config["bridge_repo"], bridge_commit)
            if info and "[NOCOPO]" not in info["message"]:
                # Only mark as seen AFTER successful detection
                poll["last_bridge_commit"] = bridge_commit
                return True, f"Bridge commit: {info['message']} ({info['sha']})"
            elif info and "[NOCOPO]" in info["message"]:
                # It's our own commit, mark seen and skip
                poll["last_bridge_commit"] = bridge_commit
            elif info is None:
                # API failed - do NOT update SHA so we retry next poll
                print("[NOCOPO] Warning: get_commit_info failed for bridge, will retry")
            else:
                poll["last_bridge_commit"] = bridge_commit
        else:
            # First run initialization
            poll["last_bridge_commit"] = bridge_commit

    # Check target repo for new commits
    target_commit = get_latest_commit(config["target_repo"])
    if target_commit and target_commit != poll["last_target_commit"]:
        old = poll["last_target_commit"]
        if old is not None:
            info = get_commit_info(config["target_repo"], target_commit)
            if info:
                # Only mark as seen AFTER successful detection
                poll["last_target_commit"] = target_commit
                return True, f"Target commit: {info['message']} ({info['sha']})"
            else:
                # API failed - do NOT update SHA so we retry next poll
                print("[NOCOPO] Warning: get_commit_info failed for target, will retry")
        else:
            # First run initialization
            poll["last_target_commit"] = target_commit

    # Check status.json for COPO trigger (iteration OR timestamp based)
    status = get_status_json()
    if status:
        if status.get("state") == "satisfied":
            nocopo_state["transfer_info"] = build_idle_transfer_info()
            return False, "COPO marked workflow satisfied"
        if status.get("state") == "code_ready" and status.get("pushed_by") == "copo":
            current_iter = status.get("iteration", 0)
            last_iter = nocopo_state["last_result"]["iteration"] if nocopo_state["last_result"] else 0
            # Trigger if iteration is higher
            if current_iter > last_iter:
                return True, f"COPO trigger: iteration {current_iter}"
            # Also trigger if timestamp is newer (handles COPO restart with same iteration)
            status_ts = status.get("timestamp", "")
            last_ts = poll.get("last_status_timestamp", "")
            if status_ts and status_ts > last_ts:
                poll["last_status_timestamp"] = status_ts
                return True, f"COPO trigger: new push at {status_ts[:19]} (iter {current_iter})"

    return False, "No changes"


# ============================================================
# EXECUTION PIPELINE
# ============================================================
def run_nocopo_pipeline(trigger_reason="Manual trigger"):
    """Full NOCOPO pipeline: pull, install, run, capture, push output."""
    config = nocopo_state["config"]
    nocopo_state["running"] = True
    reset_steps()
    nocopo_state["transfer_info"] = {
        "status": "running",
        "summary": trigger_reason,
        "trigger": trigger_reason,
        "bridge": None,
        "target": None,
        "status_json": summarize_status(get_status_json()),
        "pulled": None,
        "pushed": None,
        "updated_at": datetime.now().isoformat(),
    }

    try:
        # Check status.json for COPO-provided run config (run_mode, run_command, entry_point)
        status = get_status_json()
        request_type = "output"  # default
        modules_command = ""
        if status and status.get("pushed_by") == "copo":
            if status.get("run_mode"):
                config["run_mode"] = status["run_mode"]
            if status.get("run_command"):
                config["run_command"] = status["run_command"]
            if status.get("entry_point"):
                config["entry_point"] = status["entry_point"]
            if status.get("target_repo"):
                config["target_repo"] = status["target_repo"]
            if status.get("skip_steps") is not None:
                config["skip_steps"] = status.get("skip_steps", [])
            request_type = status.get("request_type", "output")
            modules_command = status.get("modules_command", "")

        skip_steps = parse_skip_steps(config.get("skip_steps", []))

        # DETECT
        set_step("detect", "done", trigger_reason)
        update_bridge_progress("detect", trigger_reason)

        # PULL
        set_step("pull", "running", f"Cloning/pulling {config['target_repo']}...")
        update_bridge_progress("pull", f"Pulling {config['target_repo']}...")
        token = config["pat_token"]
        user = config["github_user"]
        branch = config["branch"]
        target_repo = config["target_repo"]
        repo_dir = resolve_repo_dir(target_repo)
        repo_url = f"https://{token}@github.com/{user}/{target_repo}.git"
        used_existing_repo = os.path.exists(os.path.join(repo_dir, ".git"))
        pull_method = "pull" if used_existing_repo else None
        target_head_before = get_latest_commit(target_repo)
        bridge_head_before = get_latest_commit(config["bridge_repo"])
        nocopo_state["transfer_info"].update({
            "bridge": get_repo_snapshot(config["bridge_repo"], bridge_head_before),
            "target": get_repo_snapshot(target_repo, target_head_before),
            "updated_at": datetime.now().isoformat(),
        })

        if is_step_skipped(skip_steps, "pull"):
            if os.path.isdir(repo_dir) and os.listdir(repo_dir):
                pull_method = "skipped-existing-dir"
                set_step("pull", "done", f"Skipped by config, using existing directory -> {repo_dir}")
            else:
                set_step("pull", "error", f"Pull step skipped but repo directory not available: {repo_dir}")
                return {"success": False, "error": "Pull skipped but repo directory is missing"}
        elif os.path.exists(os.path.join(repo_dir, ".git")):
            set_step("pull", "running", f"Pulling latest into: {repo_dir}")
            result = subprocess.run(
                ["git", "pull", "origin", branch],
                capture_output=True, text=True, cwd=repo_dir, timeout=60
            )
            if result.returncode != 0:
                ok, fallback_method, fallback_message = clone_or_reuse_repo(repo_url, branch, repo_dir)
                if not ok:
                    set_step("pull", "error", f"Pull failed and {fallback_message}")
                    return {"success": False, "error": "Pull failed"}
                pull_method = fallback_method
                if fallback_method == "clone":
                    set_step("pull", "done", fallback_message)
                else:
                    set_step("pull", "done", f"Pull failed, reusing existing directory -> {repo_dir}")
            else:
                set_step("pull", "done", f"Pulled latest -> {repo_dir}")
        else:
            ok, fallback_method, fallback_message = clone_or_reuse_repo(repo_url, branch, repo_dir)
            if not ok:
                set_step("pull", "error", fallback_message)
                return {"success": False, "error": "Clone/pull prep failed"}

            if fallback_method == "pull":
                set_step("pull", "running", f"Pulling latest into: {repo_dir}")
                result = subprocess.run(
                    ["git", "pull", "origin", branch],
                    capture_output=True, text=True, cwd=repo_dir, timeout=60
                )
                if result.returncode != 0:
                    set_step("pull", "error", f"Pull failed: {(result.stderr or result.stdout or '')[:200]}")
                    return {"success": False, "error": "Pull failed"}
                pull_method = "pull"
                set_step("pull", "done", f"Pulled latest -> {repo_dir}")
            else:
                pull_method = fallback_method
                set_step("pull", "done", fallback_message)

        nocopo_state["transfer_info"]["pulled"] = {
            "repo": target_repo,
            "local_path": repo_dir,
            "branch": branch,
            "method": pull_method or ("pull" if used_existing_repo else "clone"),
            "commit": get_repo_snapshot(target_repo, get_latest_commit(target_repo)),
            "updated_at": datetime.now().isoformat(),
        }
        nocopo_state["transfer_info"]["updated_at"] = datetime.now().isoformat()

        # ============================================================
        # MODULES MODE: Install deps → push to target repo → done
        # ============================================================
        if request_type == "modules":
            if is_step_skipped(skip_steps, "install"):
                set_step("install", "done", "Skipped by config")
                install_output = ["[INFO] Install step skipped by configuration"]
            else:
                set_step("install", "running", "Installing modules (MODULES mode)...")
                update_bridge_progress("install", "MODULES mode: installing dependencies...")

                # Determine install command
                install_output = []
                if modules_command:
                    # User-provided install command
                    cmd_str = modules_command
                else:
                    # Auto-detect
                    pkg_json = os.path.join(repo_dir, "package.json")
                    req_file = os.path.join(repo_dir, "requirements.txt")
                    pom_xml = os.path.join(repo_dir, "pom.xml")
                    if os.path.exists(pkg_json):
                        cmd_str = "npm install"
                    elif os.path.exists(req_file):
                        cmd_str = f"{sys.executable} -m pip install -r requirements.txt"
                    elif os.path.exists(pom_xml):
                        if shutil.which("mvn"):
                            cmd_str = "mvn clean install -DskipTests -q"
                        elif sys.platform == "win32" and os.path.exists(os.path.join(repo_dir, "mvnw.cmd")):
                            cmd_str = "mvnw.cmd clean install -DskipTests -q"
                        else:
                            cmd_str = "./mvnw clean install -DskipTests -q"
                    else:
                        set_step("install", "error", "No package.json/requirements.txt/pom.xml found")
                        return {"success": False, "error": "Cannot detect what to install"}

                set_step("install", "running", f"Running: {cmd_str}")
                try:
                    proc = subprocess.run(
                        cmd_str, capture_output=True, text=True,
                        timeout=600, cwd=repo_dir, shell=True
                    )
                    install_output.append(f"$ {cmd_str}")
                    if proc.stdout:
                        install_output.append(proc.stdout)
                    if proc.stderr:
                        install_output.append(f"[STDERR] {proc.stderr}")
                    install_output.append(f"Exit: {proc.returncode}")
                    if proc.returncode != 0:
                        full_out = "\n".join(install_output)
                        set_step("install", "error", f"Install failed (exit {proc.returncode})", full_out)
                        return {"success": False, "error": f"Install failed: {proc.stderr[:300]}"}
                    full_out = "\n".join(install_output)
                    set_step("install", "done", f"Modules installed via: {cmd_str}", full_out)
                except subprocess.TimeoutExpired:
                    full_out = "\n".join(install_output) + "\n[TIMEOUT] Install timed out (600s)"
                    set_step("install", "error", "Install timed out (600s)", full_out)
                    return {"success": False, "error": "Install timeout"}
                except Exception as e:
                    full_out = "\n".join(install_output) + f"\n[ERROR] {str(e)}"
                    set_step("install", "error", str(e), full_out)
                    return {"success": False, "error": str(e)}

            # PUSH modules back to target repo
            set_step("run", "running", "Pushing installed modules to target repo...")
            update_bridge_progress("run", "Pushing modules to target repo...")

            # Remove .gitignore entry for node_modules if it exists (so we CAN push them)
            gitignore_path = os.path.join(repo_dir, ".gitignore")
            if os.path.exists(gitignore_path):
                with open(gitignore_path, "r", encoding="utf-8", errors="ignore") as f:
                    lines = f.readlines()
                filtered = [l for l in lines if l.strip() not in ("node_modules", "node_modules/", "/node_modules")]
                with open(gitignore_path, "w", encoding="utf-8") as f:
                    f.writelines(filtered)

            # Git add, commit, push
            try:
                subprocess.run(["git", "add", "-A"], cwd=repo_dir, capture_output=True, text=True, timeout=600)
                subprocess.run(
                    ["git", "commit", "-m", "[NOCOPO] Installed modules (pushed by NOCOPO)"],
                    cwd=repo_dir, capture_output=True, text=True, timeout=300
                )
                push_result = subprocess.run(
                    ["git", "push", "origin", branch],
                    cwd=repo_dir, capture_output=True, text=True, timeout=600
                )
                if push_result.returncode != 0:
                    set_step("run", "error", f"Push failed: {push_result.stderr[:200]}")
                    return {"success": False, "error": f"Modules push failed: {push_result.stderr[:200]}"}
                set_step("run", "done", "Modules pushed to target repo")
            except Exception as e:
                set_step("run", "error", str(e))
                return {"success": False, "error": str(e)}

            # Update status.json to tell COPO to pull
            set_step("capture", "done", "Modules mode — no output capture needed")
            set_step("push_output", "running", "Updating bridge status...")

            iteration = (nocopo_state["last_result"]["iteration"] + 1) if nocopo_state["last_result"] else 1
            output_status = {
                "state": "modules_ready",
                "pushed_by": "nocopo",
                "request_type": "modules",
                "target_repo": target_repo,
                "iteration": iteration,
                "exit_code": 0,
                "skip_steps": sorted(skip_steps),
                "timestamp": datetime.now().isoformat(),
                "message": f"Modules installed & pushed to {target_repo}. COPO should pull."
            }
            full_output = "\n".join(install_output)
            files_to_push = [
                {"path": "status.json", "content": json.dumps(output_status, indent=2)},
                {"path": "output.txt", "content": full_output},
            ]
            push_multiple_files(config["bridge_repo"], files_to_push,
                                f"[NOCOPO] Modules installed & pushed (iter {iteration})")

            bridge_head_after = get_latest_commit(config["bridge_repo"])
            nocopo_state["transfer_info"].update({
                "status": "complete",
                "summary": f"Modules installed for {target_repo} and bridge files pushed.",
                "status_json": summarize_status(output_status),
                "bridge": get_repo_snapshot(config["bridge_repo"], bridge_head_after),
                "pushed": {
                    "repo": config["bridge_repo"],
                    "branch": config["branch"],
                    "files": [f["path"] for f in files_to_push],
                    "commit": get_repo_snapshot(config["bridge_repo"], bridge_head_after),
                    "updated_at": datetime.now().isoformat(),
                },
                "updated_at": datetime.now().isoformat(),
            })

            set_step("push_output", "done", "Bridge status updated")
            set_step("done", "done", "Modules installed & pushed to target repo!")

            result = {
                "success": True,
                "iteration": iteration,
                "target_repo": target_repo,
                "exit_code": 0,
                "run_status": "MODULES_PUSHED",
                "output": full_output[:5000],
                "trigger": trigger_reason,
                "completed_at": datetime.now().isoformat(),
            }
            nocopo_state["last_result"] = result
            nocopo_state["history"].append({
                "iteration": iteration,
                "target_repo": target_repo,
                "run_status": "MODULES_PUSHED",
                "trigger": trigger_reason,
                "timestamp": datetime.now().isoformat(),
            })
            nocopo_state["poll_status"]["triggers_count"] += 1
            return result

        # ============================================================
        # OUTPUT MODE (default): Install → Run → Capture → Push output
        # ============================================================
        # INSTALL
        if is_step_skipped(skip_steps, "install"):
            set_step("install", "done", "Skipped by config")
            detected_services = []
        else:
            set_step("install", "running", "Checking dependencies...")
            update_bridge_progress("install", "Installing dependencies...")

        run_mode = config.get("run_mode", "auto")
        run_cmd = config["run_command"].strip()
        entry = config["entry_point"].strip()
        timeout_sec = config["timeout"]

        if not is_step_skipped(skip_steps, "install") and run_mode == "manual" and run_cmd:
            # Manual mode: just install based on what's available
            req_file = os.path.join(repo_dir, "requirements.txt")
            pkg_json = os.path.join(repo_dir, "package.json")
            pom_xml = os.path.join(repo_dir, "pom.xml")
            if os.path.exists(req_file):
                subprocess.run(
                    [sys.executable, "-m", "pip", "install", "-r", req_file, "--quiet"],
                    capture_output=True, text=True, timeout=180, cwd=repo_dir
                )
                set_step("install", "done", "Python requirements installed")
            elif os.path.exists(pkg_json):
                subprocess.run(
                    ["npm", "install"], capture_output=True, text=True,
                    timeout=180, cwd=repo_dir, shell=(sys.platform == "win32")
                )
                set_step("install", "done", "npm packages installed")
            elif os.path.exists(pom_xml):
                if shutil.which("mvn"):
                    mvn = "mvn"
                elif sys.platform == "win32" and os.path.exists(os.path.join(repo_dir, "mvnw.cmd")):
                    mvn = "mvnw.cmd"
                else:
                    mvn = "./mvnw"
                subprocess.run(
                    [mvn, "clean", "install", "-DskipTests", "-q"],
                    capture_output=True, text=True, timeout=300, cwd=repo_dir
                )
                set_step("install", "done", "Maven dependencies installed")
            else:
                set_step("install", "done", "No dependencies found (skipped)")
            detected_services = []
        elif not is_step_skipped(skip_steps, "install"):
            # Auto mode: detect project structure
            detected_services = detect_project_type(repo_dir)
            if detected_services:
                install_msgs = []
                for svc in detected_services:
                    set_step("install", "running", f"Installing: {svc['name']} ({svc['type']})...")
                    update_bridge_progress("install", f"Installing {svc['name']} ({svc['type']})...")
                    ok, msg = install_service(svc)
                    install_msgs.append(f"{svc['name']}({svc['type']}): {msg}")
                    if not ok:
                        set_step("install", "error", f"Install failed for {svc['name']}: {msg}")
                        return {"success": False, "error": f"Install failed: {msg}"}
                set_step("install", "done", " | ".join(install_msgs))
            else:
                # Fallback: try basic install
                req_file = os.path.join(repo_dir, "requirements.txt")
                if os.path.exists(req_file):
                    subprocess.run(
                        [sys.executable, "-m", "pip", "install", "-r", req_file, "--quiet"],
                        capture_output=True, text=True, timeout=180, cwd=repo_dir
                    )
                    set_step("install", "done", "Python requirements installed")
                else:
                    set_step("install", "done", "No dependencies detected (skipped)")

        # RUN
        set_step("run", "running", "Executing code...")
        update_bridge_progress("run", "Running code...")

        all_outputs = []
        exit_code = 0
        run_status = "SUCCESS"

        if run_mode == "manual" and run_cmd:
            # Manual mode: run command(s) specified by user
            # Chain multiple commands with && for sequential execution (preserves directory changes)
            all_outputs.append(f"=== Working Dir: {repo_dir} ===")
            all_outputs.append(f"=== Commands ===")
            for line in run_cmd.strip().split("\n"):
                if line.strip():
                    all_outputs.append(f"  {line.strip()}")
            all_outputs.append("")

            # Join commands with && to run sequentially in one shell session
            # This preserves directory changes across commands
            chained_cmd = " && ".join(line.strip() for line in run_cmd.strip().split("\n") if line.strip())

            set_step("run", "running", f"Running {len([l for l in run_cmd.strip().split(chr(10)) if l.strip()])} commands sequentially...")
            update_bridge_progress("run", "Running chained commands...")

            try:
                if sys.platform == "win32":
                    # Use PowerShell for Windows to handle && properly
                    cmd_args = ["powershell", "-NoProfile", "-NonInteractive",
                               "-ExecutionPolicy", "Bypass", "-Command", chained_cmd]
                    proc = subprocess.run(
                        cmd_args, capture_output=True, text=True,
                        timeout=timeout_sec, cwd=repo_dir, shell=False
                    )
                else:
                    # Use shell for Linux/Mac
                    proc = subprocess.run(
                        chained_cmd, capture_output=True, text=True,
                        timeout=timeout_sec, cwd=repo_dir, shell=True
                    )

                exit_code = proc.returncode
                if proc.stdout:
                    all_outputs.append(proc.stdout)
                if proc.stderr:
                    all_outputs.append(f"[STDERR] {proc.stderr}")
                all_outputs.append(f"--- Exit Code: {exit_code} ---")

                if exit_code != 0:
                    run_status = "FAILED"
                else:
                    run_status = "SUCCESS"
            except subprocess.TimeoutExpired:
                all_outputs.append(f"[TIMEOUT] Commands timed out after {timeout_sec}s")
                exit_code = -1
                run_status = "TIMEOUT"
            except Exception as e:
                all_outputs.append(f"[ERROR] {str(e)}")
                exit_code = -1
                run_status = "ERROR"

            full_cmd_output = "\n".join(all_outputs)
            if run_status == "FAILED":
                set_step("run", "error", f"Exit code: {exit_code} (FAILED)", full_cmd_output)
            elif run_status == "TIMEOUT":
                set_step("run", "error", "Commands timed out", full_cmd_output)
            else:
                set_step("run", "done" if exit_code == 0 else "error", f"Exit code: {exit_code}", full_cmd_output)

        elif run_mode == "manual" and entry:
            # Manual mode with entry point only
            entry_path = os.path.join(repo_dir, entry)
            if not os.path.exists(entry_path):
                set_step("run", "error", f"Entry not found: {entry_path}")
                return {"success": False, "error": f"Entry not found: {entry_path}"}
            if entry.endswith(".py"):
                cmd_parts = [sys.executable, entry_path]
            elif entry.endswith(".js"):
                cmd_parts = ["node", entry_path]
            else:
                cmd_parts = [entry_path]

            full_cmd_str = ' '.join(cmd_parts)
            set_step("run", "running", f"Running: {full_cmd_str}")
            all_outputs.append(f"=== Working Dir: {repo_dir} ===")
            all_outputs.append(f"=== Command: {full_cmd_str} ===")
            try:
                proc = subprocess.run(
                    cmd_parts, capture_output=True, text=True,
                    timeout=timeout_sec, cwd=repo_dir
                )
                exit_code = proc.returncode
                if proc.stdout:
                    all_outputs.append(proc.stdout)
                if proc.stderr:
                    all_outputs.append(f"[STDERR] {proc.stderr}")
                run_status = "SUCCESS" if exit_code == 0 else "FAILED"
            except subprocess.TimeoutExpired:
                exit_code = -1
                all_outputs.append(f"TIMEOUT after {timeout_sec}s")
                run_status = "TIMEOUT"
            except Exception as e:
                exit_code = -1
                all_outputs.append(str(e))
                run_status = "ERROR"

        elif detected_services:
            # Auto mode: run detected services
            svc_info = ", ".join(f"{s['name']}({s['type']})" for s in detected_services)
            set_step("run", "running", f"Auto-running: {svc_info}")
            update_bridge_progress("run", f"Auto-running: {svc_info}")

            for i, svc in enumerate(detected_services):
                svc_label = f"{svc['name']} ({svc['type']})"
                svc_dir = svc['dir']
                full_cmd = ' '.join(svc['run_cmd'])
                set_step("run", "running", f"[{i+1}/{len(detected_services)}] Running {svc_label} in {svc_dir}")
                update_bridge_progress("run", f"Running {svc_label} in {svc_dir}")

                svc_exit, svc_stdout, svc_stderr, svc_status = run_service(svc, timeout_sec)
                all_outputs.append(f"=== Service: {svc_label} ===")
                all_outputs.append(f"=== Working Dir: {svc_dir} ===")
                all_outputs.append(f"=== Command: {full_cmd} ===")
                if svc_stdout:
                    all_outputs.append(svc_stdout)
                if svc_stderr:
                    all_outputs.append(f"[STDERR] {svc_stderr}")
                all_outputs.append(f"=== Exit: {svc_exit} ({svc_status}) ===\n")

                if svc_exit != 0:
                    exit_code = svc_exit
                    run_status = svc_status

        else:
            # Fallback: try to find and run something
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
                set_step("run", "error", "No entry point or project detected")
                return {"success": False, "error": "No entry point found. Set run_mode to 'manual' and provide a run_command."}

            cmd_parts = [sys.executable, entry_path]
            full_cmd_str = f"{sys.executable} {entry_path}"
            set_step("run", "running", f"Running: {full_cmd_str}")
            all_outputs.append(f"=== Working Dir: {repo_dir} ===")
            all_outputs.append(f"=== Command: {full_cmd_str} ===")
            try:
                proc = subprocess.run(
                    cmd_parts, capture_output=True, text=True,
                    timeout=timeout_sec, cwd=repo_dir
                )
                exit_code = proc.returncode
                if proc.stdout:
                    all_outputs.append(proc.stdout)
                if proc.stderr:
                    all_outputs.append(f"[STDERR] {proc.stderr}")
                run_status = "SUCCESS" if exit_code == 0 else "FAILED"
            except subprocess.TimeoutExpired:
                exit_code = -1
                all_outputs.append(f"TIMEOUT after {timeout_sec}s")
                run_status = "TIMEOUT"
            except Exception as e:
                exit_code = -1
                all_outputs.append(str(e))
                run_status = "ERROR"

        stdout = "\n".join(all_outputs)
        stderr = ""  # Already merged into stdout above

        set_step("run", "done" if exit_code == 0 else "error",
                f"Exit code: {exit_code} ({run_status})", stdout)

        # CAPTURE
        if is_step_skipped(skip_steps, "capture"):
            full_output = stdout if stdout else ""
            if stderr:
                full_output += ("\n" if full_output else "") + f"[STDERR] {stderr}"
            if not full_output.strip():
                full_output = f"Capture skipped. Exit code: {exit_code} ({run_status})"
            set_step("capture", "done", "Skipped by config", full_output)
        else:
            set_step("capture", "running", "Formatting output...")
            update_bridge_progress("capture", "Capturing output...")
            output_parts = [
                f"=== Target: {target_repo} ===",
                f"=== Run Mode: {run_mode} ===",
                f"=== Repo Cloned At: {repo_dir} ===",
                f"=== Python Interpreter: {sys.executable} ===",
                f"=== Timestamp: {datetime.now().isoformat()} ===",
            ]
            if detected_services:
                output_parts.append(f"=== Detected: {', '.join(s['name']+'('+s['type']+')' for s in detected_services)} ===")
            if stdout:
                output_parts.append("\n=== OUTPUT ===")
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
        update_bridge_progress("push_output", "Pushing output to bridge...")
        bridge_repo = config["bridge_repo"]
        iteration = (nocopo_state["last_result"]["iteration"] + 1) if nocopo_state["last_result"] else 1

        # Build files list for single commit
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

        files_to_push = [
            {"path": f"output_iteration_{iteration}.txt", "content": full_output},
            {"path": "output.txt", "content": full_output},
            {"path": "status.json", "content": json.dumps(output_status, indent=2)},
        ]

        # Build commit message: use UI message if set, otherwise auto-generate
        custom_msg = config.get("commit_message", "").strip()
        if custom_msg:
            commit_msg = f"[NOCOPO] {custom_msg}"
        else:
            file_names = ", ".join(f["path"] for f in files_to_push)
            commit_msg = f"[NOCOPO] Output iteration {iteration} ({run_status}) - {file_names}"

        # Single commit for all files
        success = push_multiple_files(bridge_repo, files_to_push, commit_msg)
        if not success:
            # Fallback: push files individually
            all_ok = True
            for f in files_to_push:
                if not push_file(bridge_repo, f["path"], f["content"], commit_msg):
                    all_ok = False
            if not all_ok:
                set_step("push_output", "error", "Failed to push output")
                return {"success": False, "error": "Push output failed"}

        set_step("push_output", "done", f"Pushed {len(files_to_push)} files in 1 commit (iter {iteration})")

        bridge_head_after = get_latest_commit(bridge_repo)
        nocopo_state["transfer_info"].update({
            "status": "complete",
            "summary": f"Pulled {target_repo}, ran it, and pushed {len(files_to_push)} bridge file(s).",
            "status_json": summarize_status(output_status),
            "bridge": get_repo_snapshot(bridge_repo, bridge_head_after),
            "pushed": {
                "repo": bridge_repo,
                "branch": config["branch"],
                "files": [f["path"] for f in files_to_push],
                "commit": get_repo_snapshot(bridge_repo, bridge_head_after),
                "updated_at": datetime.now().isoformat(),
            },
            "updated_at": datetime.now().isoformat(),
        })

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
        if nocopo_state["transfer_info"] is not None:
            nocopo_state["transfer_info"].update({
                "status": "error",
                "summary": str(e),
                "updated_at": datetime.now().isoformat(),
            })
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

    # Initialize status timestamp to avoid triggering on existing status.json
    status = get_status_json()
    if status:
        poll["last_status_timestamp"] = status.get("timestamp", "")

    while nocopo_state["monitoring"]:
        try:
            poll["last_check"] = datetime.now().isoformat()
            poll["checks_count"] += 1

            if not nocopo_state["running"]:
                status = get_status_json()
                if status and status.get("state") == "satisfied":
                    nocopo_state["transfer_info"] = build_idle_transfer_info()
                    nocopo_state["monitoring"] = False
                    break
                triggered, reason = detect_changes()
                if triggered:
                    nocopo_state["detected_trigger"] = reason
                    run_nocopo_pipeline(reason)
                elif nocopo_state["transfer_info"] is None:
                    nocopo_state["transfer_info"] = build_idle_transfer_info(reason)

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
                "transfer_info": nocopo_state["transfer_info"] or build_idle_transfer_info(),
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

        elif self.path == "/run-command-status":
            result = nocopo_state.get("custom_command_result")
            if result:
                self._json_response(result)
            else:
                self._json_response({"status": "IDLE", "message": "No command has been run"})

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

        elif self.path == "/run-command":
            body = self._read_body()
            command = body.get("command", "").strip()
            cwd = body.get("cwd", "").strip() or None
            timeout_sec = int(body.get("timeout", 60))
            if not command:
                self._json_response({"error": "No command provided"}, 400)
                return

            def _exec_custom_command(cmd_str, working_dir, t_sec):
                try:
                    proc = subprocess.run(
                        cmd_str, capture_output=True, text=True,
                        timeout=t_sec, cwd=working_dir, shell=True
                    )
                    nocopo_state["custom_command_result"] = {
                        "command": cmd_str,
                        "cwd": working_dir or os.getcwd(),
                        "stdout": proc.stdout,
                        "stderr": proc.stderr,
                        "exit_code": proc.returncode,
                        "status": "SUCCESS" if proc.returncode == 0 else "FAILED",
                        "completed_at": datetime.now().isoformat(),
                    }
                except subprocess.TimeoutExpired:
                    nocopo_state["custom_command_result"] = {
                        "command": cmd_str,
                        "cwd": working_dir or os.getcwd(),
                        "stdout": "",
                        "stderr": f"TIMEOUT after {t_sec}s",
                        "exit_code": -1,
                        "status": "TIMEOUT",
                        "completed_at": datetime.now().isoformat(),
                    }
                except Exception as e:
                    nocopo_state["custom_command_result"] = {
                        "command": cmd_str,
                        "cwd": working_dir or os.getcwd(),
                        "stdout": "",
                        "stderr": str(e),
                        "exit_code": -1,
                        "status": "ERROR",
                        "completed_at": datetime.now().isoformat(),
                    }

            nocopo_state["custom_command_result"] = {"status": "RUNNING", "command": command}
            threading.Thread(target=_exec_custom_command, args=(command, cwd, timeout_sec), daemon=True).start()
            self._json_response({"message": "Command started", "command": command})

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
    print("  POST /run-command     - Run a custom command")
    print("  GET  /run-command-status - Get custom command result")
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
