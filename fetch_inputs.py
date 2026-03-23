"""
fetch_inputs.py — Fetch projects.json and yesterday's processed note from life-os-data.

Used by GitHub Actions before calling generate_brief.py.
Writes two files to the working directory:
  - fetched_projects.json
  - fetched_note.md

Exits with code 1 if no note can be found (skips brief generation that day).

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
MAX_LOOKBACK  = 3


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
    print(f"   ✅ Loaded {len(data.get('projects', []))} projects.")
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
        date8  = check_date.strftime("%Y%m%d")
        dateiso = check_date.strftime("%Y-%m-%d")
        for name in filenames:
            if date8 in name or dateiso in name:
                if days_back > 0:
                    print(f"   ℹ️  No note for {target_date}, using note from {check_date} ({days_back}d back)")
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

    # 1. Fetch and save projects.json
    projects = fetch_projects(token)
    with open("fetched_projects.json", "w", encoding="utf-8") as f:
        json.dump(projects, f, indent=2)
    print("   💾 Saved fetched_projects.json")

    # 2. List notes and find yesterday's
    print(f"\n🔍 Looking for note from {target_date} in {GITHUB_OWNER}/{GITHUB_REPO}/{NOTES_FOLDER}/...")
    filenames = list_notes(token)
    print(f"   Found {len(filenames)} note(s) in repo.")

    match = find_note_for_date(filenames, target_date)
    if not match:
        print(f"⚠️  No note found for {target_date} (searched {MAX_LOOKBACK} days back).")
        print("   Skipping brief generation today.")
        # Write a sentinel file so the workflow knows to skip
        with open("no_note_found", "w") as f:
            f.write(str(target_date))
        sys.exit(0)

    print(f"   ✅ Found: {match}")
    content = fetch_note_content(token, match)
    with open("fetched_note.md", "w", encoding="utf-8") as f:
        f.write(content)
    print("   💾 Saved fetched_note.md")

    print("\n✅ Inputs ready.")


if __name__ == "__main__":
    main()
