import os
import re
import json
import subprocess
from datetime import datetime
from urllib.parse import urlparse

PASTEBIN_URL = "https://pastebin.com/raw/AveJ8ejG"
TOKEN = os.getenv("GITHUB_TOKEN")

MAX_SIZE = 45 * 1024 * 1024
TODAY = datetime.utcnow().strftime("%Y-%m-%d")

COMMON_HEADERS = [
    "User-Agent: Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 Chrome/122 Safari/537.36", 
    "Accept: */*", 
    "Accept-Language: en-US,en;q=0.9", 
    "Connection: keep-alive"
]

if TOKEN:
    COMMON_HEADERS.append(f"Authorization: Bearer {TOKEN}")

report = {
    "repo_not_found": [],
    "processed": [],
    "skipped_existing": [],
    "drastically_changed": [],
    "skip": [],
    "null": [],
    "invalid": [],
    "http_errors": []
}

seen_repos = set()
seen_archives = set()


# ---------------- CURL ---------------- #

def curl(url, output=None, capture=False):
    cmd = ["curl", "-L", "--silent", "--show-error"]

    for h in COMMON_HEADERS:
        cmd += ["-H", h]

    if output:
        cmd += ["-o", output]

    cmd.append(url)

    return subprocess.run(cmd, capture_output=capture)


def curl_json(url):
    r = curl(url, capture=True)

    if r.returncode != 0:
        report["http_errors"].append(url)
        return None

    try:
        return json.loads(r.stdout.decode())
    except json.JSONDecodeError:
        report["invalid"].append(url)
        return None


# ---------------- CORE ---------------- #

def fetch_url_list():
    data = curl_json(PASTEBIN_URL)
    if not data:
        raise Exception("Failed to fetch URL list")
    return data


def split_file(path):
    size = os.path.getsize(path)
    if size <= MAX_SIZE:
        return

    with open(path, "rb") as f:
        i = 0
        while True:
            chunk = f.read(MAX_SIZE)
            if not chunk:
                break
            with open(f"{path}.part{i}", "wb") as p:
                p.write(chunk)
            i += 1

    os.remove(path)


def download(url, dest):
    if os.path.exists(dest):
        report["skipped_existing"].append(dest)
        return True

    r = curl(url, output=dest)

    if r.returncode != 0:
        report["http_errors"].append(url)
        return False

    split_file(dest)
    return True


# ---------------- GITHUB ---------------- #

def detect_github_repo(url):
    m = re.match(r"https://github.com/([^/]+)/([^/]+)", url)
    return (m.group(1), m.group(2)) if m else None


def get_repo_data(owner, repo):
    return curl_json(f"https://api.github.com/repos/{owner}/{repo}")


def get_user_data(owner):
    return curl_json(f"https://api.github.com/users/{owner}")


def get_branches(owner, repo):
    data = curl_json(f"https://api.github.com/repos/{owner}/{repo}/branches")
    if not data:
        return []
    return [b["name"] for b in data]


def get_last_commit_sha(owner, repo):
    data = curl_json(f"https://api.github.com/repos/{owner}/{repo}/commits?per_page=1")
    if not data:
        return None
    return data[0]["sha"]


def compare_commits(owner, repo, old_sha, new_sha):
    url = f"https://api.github.com/repos/{owner}/{repo}/compare/{old_sha}...{new_sha}"
    data = curl_json(url)
    if not data:
        return None

    return {
        "total_commits": data.get("total_commits", 0),
        "files_changed": len(data.get("files", []))
    }


def github_info(owner, repo, repo_data, sha):
    return {
        "date-updated": TODAY,
        "owner": repo_data["owner"]["url"],
        "repository": repo_data["url"],
        "last-commit": sha
    }


def save_json(path, filename, data):
    with open(os.path.join(path, filename), "w") as f:
        json.dump(data, f, indent=2)


def process_repo(owner, repo):
    key = f"{owner}/{repo}"

    if key in seen_repos:
        return
    seen_repos.add(key)

    repo_data = get_repo_data(owner, repo)
    if not repo_data:
        report["repo_not_found"].append(key)
        return

    default = repo_data.get("default_branch")
    if not default:
        report["repo_not_found"].append(key)
        return

    # --- SAVE USER JSON ---
    user_base = owner
    os.makedirs(user_base, exist_ok=True)

    user_data = get_user_data(owner)
    if user_data:
        save_json(user_base, "user.json", user_data)

    # --- SAVE REPO JSON ---
    base = key
    os.makedirs(base, exist_ok=True)
    save_json(base, "repo.json", repo_data)

    info_path = os.path.join(base, "info.json")

    old_sha = None
    if os.path.exists(info_path):
        try:
            with open(info_path) as f:
                old_sha = json.load(f).get("last-commit")
        except:
            pass

    new_sha = get_last_commit_sha(owner, repo)
    if not new_sha:
        return

    # ---- SHA SKIP ----
    if old_sha == new_sha:
        report["skipped_existing"].append(key)
        return

    # ---- DRASTIC CHANGE DETECTION ----
    if old_sha:
        cmp = compare_commits(owner, repo, old_sha, new_sha)
        if cmp and (cmp["total_commits"] > 20 or cmp["files_changed"] > 50):
            report["drastically_changed"].append(key)

    branches = set([default])
    all_branches = get_branches(owner, repo)

    for b in ["build", "builds"]:
        if b in all_branches:
            branches.add(b)

    for b in branches:
        url = f"https://github.com/{owner}/{repo}/archive/refs/heads/{b}.tar.gz"
        dest = f"{base}/{b}.tar.gz"

        if download(url, dest):
            report["processed"].append(url)
        else:
            report["invalid"].append(url)

    info = github_info(owner, repo, repo_data, new_sha)
    save_json(base, "info.json", info)


# ---------------- ARCHIVE ---------------- #

def process_archive(url):
    if url in seen_archives:
        return
    seen_archives.add(url)

    parsed = urlparse(url)

    m = re.match(r"https://github.com/([^/]+)/([^/]+)/archive/", url)

    if m:
        owner, repo = m.group(1), m.group(2)

        if f"{owner}/{repo}" in seen_repos:
            return

        base = f"{owner}/{repo}"
        os.makedirs(base, exist_ok=True)

        name = os.path.basename(parsed.path)
        dest = os.path.join(base, name)

        if download(url, dest):
            report["processed"].append(url)

        return

    parts = [p for p in parsed.path.strip("/").split("/") if p]

    if len(parts) >= 2:
        user = parts[0]
        repo = parts[1].replace(".git", "")
        base = os.path.join(user, repo)
    else:
        base = "misc"

    os.makedirs(base, exist_ok=True)

    name = os.path.basename(parsed.path)
    dest = os.path.join(base, name)

    if download(url, dest):
        report["processed"].append(url)
    else:
        report["invalid"].append(url)


# ---------------- MAIN ---------------- #

def main():
    urls = fetch_url_list()

    for url in urls:
        if not url:
            report["null"].append(url)
            continue

        if url.endswith((".tar.gz", ".zip")):
            process_archive(url)
            continue

        repo = detect_github_repo(url)

        if repo:
            process_repo(*repo)
            continue

        report["skip"].append(url)

    with open("report.txt", "w") as f:
        f.write(f"Snapshot Report — {TODAY}\n\n")

        for k, v in report.items():
            f.write(f"{k}:\n")
            f.write("\n".join(map(str, v)))
            f.write("\n\n---\n\n")


if __name__ == "__main__":
    main()
