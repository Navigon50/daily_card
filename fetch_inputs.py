"""
fetch_inputs.py — Fetch inputs from life-os-data for daily brief generation.

Writes to working directory:
  - fetched_projects.json   (always — required)
  - fetched_backlog.json    (always — required, source of truth for tasks)
  - fetched_note.md         (optional — used for energy level only)

Never exits with skip. If no note is found, brief generation continues
with energy defaulting to "medium".

Environment variables:
  LIFE_OS_DATA_TOKEN   GitHub PAT with read access to life-os-data
"""

import base64
import json
import os
import sys
import ssl
import urllib.request
import urllib.error
import urllib.parse
from datetime import date, timedelta

GITHUB_OWNER  = "Navigon50"
GITHUB_REPO   = "life-os-data"
NOTES_FOLDER  = "notes"
MAX_LOOKBACK  = 7   # wider window — processing happens end of week


def get_ssl_context():
    try:
        import certifi
        return ssl.create_default_context(cafile=certifi.where())
    except ImportError:
        return ssl.create_default_context()


def github_get(api_path: str, token: str) -> dict:
    """GET a GitHub Contents API path. Returns {} on 404."""
    req = urllib.request.Request(
        f"https://api.github.com{api_path}",
        headers={
            "Authorization":        f"Bearer {token}",
            "Accept":               "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
        },
        method="GET",
    )
    try:
        with urllib.request.urlopen(req, timeout=15, context=get_ssl_context()) as resp:
            return json.loads(resp.read())
    except urllib.error.HTTPError as e:
        if e.code == 404:
            return {}
        raise RuntimeError(f"GitHub GET {api_path} failed ({e.code}): {e.read().decode()}") from e


def decode_content(item: dict) -> str:
    """Decode base64 file content returned by GitHub Contents API."""
    return base64.b64decode(item["content"].replace("\n", "")).decode("utf-8")


def fetch_projects(token: str) -> dict:
    """Fetch and return parsed projects.json from life-os-data."""
    print("📦 Fetching projects.json...")
    item = github_get(f"/repos/{GITHUB_OWNER}/{GITHUB_REPO}/contents/projects.json", token)
    if not item:
        raise RuntimeError("projects.json not found in life-os-data repo.")
    data = json.loads(decode_content(item))
    print(f"   ✅ {len(data.get('projects', []))} projects loaded.")
    return data


def fetch_backlog(token: str) -> dict:
    """Fetch and return parsed backlog.json from life-os-data."""
    print("📋 Fetching backlog.json...")
    item = github_get(f"/repos/{GITHUB_OWNER}/{GITHUB_REPO}/contents/backlog.json", token)
    if not item:
        print("   ⚠️  backlog.json not found — returning empty backlog.")
        return {"queued": {}, "pending": [], "done": [], "lastUpdated": None}
    data = json.loads(decode_content(item))
    queued = data.get("queued", {})
    filled = sum(1 for v in queued.values() if v)
    print(f"   ✅ {filled}/5 domains queued.")
    return data


def list_notes(token: str) -> list[str]:
    """Return list of filenames in life-os-data/notes/."""
    result = github_get(
        f"/repos/{GITHUB_OWNER}/{GITHUB_REPO}/contents/{NOTES_FOLDER}",
        token
    )
    if not result:
        return []
    return [item["name"] for item in result if item["name"].endswith(".md")]


def find_note_for_date(filenames: list[str], target_date: date) -> str | None:
    """
    Find a note filename matching the target date, walking back MAX_LOOKBACK days.
    Matches YYYYMMDD or YYYY-MM-DD anywhere in the filename.
    """
    for days_back in range(MAX_LOOKBACK + 1):
        check_date = target_date - timedelta(days=days_back)
        date8   = check_date.strftime("%Y%m%d")
        dateiso = check_date.strftime("%Y-%m-%d")
        for name in filenames:
            if date8 in name or dateiso in name:
                if days_back > 0:
                    print(f"   ℹ️  No note for {target_date} — using note from {check_date} ({days_back}d back).")
                return name
    return None


def fetch_note_content(token: str, filename: str) -> str:
    """Fetch and decode a note file from life-os-data/notes/."""
    encoded_name = urllib.parse.quote(filename, safe="")
    item = github_get(
        f"/repos/{GITHUB_OWNER}/{GITHUB_REPO}/contents/{NOTES_FOLDER}/{encoded_name}",
        token
    )
    if not item:
        raise RuntimeError(f"Could not fetch note: {filename}")
    return decode_content(item)


def main():
    token = os.environ.get("LIFE_OS_DATA_TOKEN")
    if not token:
        print("❌ LIFE_OS_DATA_TOKEN not set.")
        sys.exit(1)

    today       = date.today()
    target_date = today - timedelta(days=1)

    # 1. Fetch projects.json — required
    projects = fetch_projects(token)
    with open("fetched_projects.json", "w", encoding="utf-8") as f:
        json.dump(projects, f, indent=2)
    print("   💾 Saved fetched_projects.json")

    # 2. Fetch backlog.json — required (source of truth for tasks)
    backlog = fetch_backlog(token)
    with open("fetched_backlog.json", "w", encoding="utf-8") as f:
        json.dump(backlog, f, indent=2)
    print("   💾 Saved fetched_backlog.json")

    # 3. Fetch yesterday's note — optional (energy context only)
    print(f"\n🔍 Looking for note from {target_date} (optional — for energy level)...")
    filenames = list_notes(token)
    print(f"   {len(filenames)} note(s) in repo.")

    match = find_note_for_date(filenames, target_date)
    if match:
        print(f"   ✅ Found: {match}")
        content = fetch_note_content(token, match)
        with open("fetched_note.md", "w", encoding="utf-8") as f:
            f.write(content)
        print("   💾 Saved fetched_note.md")
    else:
        print(f"   ℹ️  No note found within {MAX_LOOKBACK} days — energy will default to medium.")
        # Write empty sentinel so generate_brief.py knows note is absent
        with open("fetched_note.md", "w", encoding="utf-8") as f:
            f.write("")

    print("\n✅ Inputs ready.")


if __name__ == "__main__":
    main()