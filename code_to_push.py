# Change Detection for web-scraper repo
# This file will be pushed to GitHub by the COPO system

import os
import requests
from datetime import datetime, timezone

PAT_TOKEN = os.environ.get("GITHUB_PAT", "")
GITHUB_USER = "DLI0592-PrabhatRanjan01"
TARGET_REPO = "web-scraper"

API_BASE = f"https://api.github.com/repos/{GITHUB_USER}/{TARGET_REPO}"
HEADERS = {
    "Authorization": f"token {PAT_TOKEN}",
    "Accept": "application/vnd.github.v3+json",
}


def get_recent_commits(limit=10):
    """Get recent commits from the web-scraper repo."""
    resp = requests.get(f"{API_BASE}/commits?per_page={limit}", headers=HEADERS)
    if resp.status_code == 200:
        return resp.json()
    else:
        print(f"Error fetching commits: {resp.status_code} - {resp.text}")
        return []


def get_changed_files(commit_sha):
    """Get list of files changed in a specific commit."""
    resp = requests.get(f"{API_BASE}/commits/{commit_sha}", headers=HEADERS)
    if resp.status_code == 200:
        data = resp.json()
        return data.get("files", [])
    return []


def detect_changes():
    """Detect and report recent changes in the web-scraper repo."""
    print(f"=== Change Detection: {GITHUB_USER}/{TARGET_REPO} ===")
    print(f"Timestamp: {datetime.now(timezone.utc).isoformat()}")
    print()

    commits = get_recent_commits(5)
    if not commits:
        print("No commits found or repo does not exist.")
        return

    print(f"Last {len(commits)} commits:")
    print("-" * 60)

    for commit in commits:
        sha = commit["sha"][:7]
        message = commit["commit"]["message"].split("\n")[0]
        author = commit["commit"]["author"]["name"]
        date = commit["commit"]["author"]["date"]
        print(f"  [{sha}] {message}")
        print(f"         by {author} on {date}")

        files = get_changed_files(commit["sha"])
        if files:
            for f in files:
                status = f["status"]
                filename = f["filename"]
                additions = f.get("additions", 0)
                deletions = f.get("deletions", 0)
                print(f"           {status}: {filename} (+{additions}/-{deletions})")
        print()

    print("-" * 60)
    print(f"Total commits analyzed: {len(commits)}")


if __name__ == "__main__":
    detect_changes()
