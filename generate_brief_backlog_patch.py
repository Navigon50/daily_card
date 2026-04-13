"""
generate_brief_backlog_patch.py
───────────────────────────────
Add to generate_brief.py to inject the queued OS block task into the
Telegram daily brief.

HOW TO INTEGRATE:
1. Copy this file into the same folder as generate_brief.py
2. Add at the top of generate_brief.py:
       from generate_brief_backlog_patch import format_os_block_for_telegram
3. In your brief-building logic, replace or extend your OS block section:

       os_domain = card_data.get("os", "")   # chip value from daily card

       if os_domain and os_domain != "none":
           os_section = format_os_block_for_telegram(os_domain)
       else:
           os_section = "OS Block — None today"

Env vars required (already set in your pipeline):
    GITHUB_TOKEN
    GITHUB_REPO_DATA    default: Navigon50/life-os-data
"""

import base64
import json
import os

import requests

GITHUB_TOKEN = os.environ["GITHUB_TOKEN"]
REPO_DATA    = os.environ.get("GITHUB_REPO_DATA", "Navigon50/life-os-data")
BACKLOG_FILE = "backlog.json"

DOMAIN_LABELS = {
    "health":     "Health",
    "creative":   "Creative",
    "financial":  "Financial",
    "connection": "Connection",
    "growth":     "Growth",
}

GH_HEADERS = {
    "Authorization": f"Bearer {GITHUB_TOKEN}",
    "Accept": "application/vnd.github.v3+json",
    "X-GitHub-Api-Version": "2022-11-28",
}


def _load_backlog():
    """Fetch backlog.json from the private life-os-data repo."""
    url = f"https://api.github.com/repos/{REPO_DATA}/contents/{BACKLOG_FILE}"
    r = requests.get(url, headers=GH_HEADERS)
    if r.status_code == 404:
        return None
    r.raise_for_status()
    data = r.json()
    return json.loads(base64.b64decode(data["content"]).decode("utf-8"))


def get_queued_os_task(domain: str) -> dict | None:
    """
    Returns the queued task object for the given domain, or None.

    Task object shape:
      { "id": "abc12345", "domain": "health", "title": "...", "firstAction": "..." }
    """
    backlog = _load_backlog()
    if not backlog:
        return None
    return backlog.get("queued", {}).get(domain)


def get_all_queued_tasks() -> dict:
    """
    Returns { domain: task_or_None } for all five domains.
    Useful for a full queue overview in the brief.
    """
    backlog = _load_backlog()
    if not backlog:
        return {}
    return backlog.get("queued", {})


def format_os_block_for_telegram(domain: str) -> str:
    """
    Returns a formatted string for the OS block section of the Telegram brief.

    Example output when a task is queued:

        OS Block — Creative
        Draft the intro section of the client onboarding guide
        First: Open Notion > Clients > Onboarding folder
        ID: abc12345

    Example output when nothing is queued:

        OS Block — Creative
        Nothing queued. Run: python backlog_sync.py --promote creative
    """
    task  = get_queued_os_task(domain)
    label = DOMAIN_LABELS.get(domain, domain.title())
    header = f"OS Block — {label}"

    if not task:
        return f"{header}\nNothing queued. Run: python backlog_sync.py --promote {domain}"

    lines = [header, task["title"]]
    if task.get("firstAction"):
        lines.append(f"First: {task['firstAction']}")
    lines.append(f"ID: {task['id']}")

    return "\n".join(lines)


# ── Optional: Telegram inline keyboard for one-tap Done ───────────────────────
#
# If your bot handles callback_query, attach an inline keyboard to the message:
#
#   task = get_queued_os_task(os_domain)
#   if task:
#       reply_markup = {
#           "inline_keyboard": [[{
#               "text": f"Done — {task['title'][:40]}",
#               "callback_data": f"backlog_done:{task['id']}"
#           }]]
#       }
#   # include reply_markup in your sendMessage call
#
# In your callback_query handler:
#
#   if query.data.startswith("backlog_done:"):
#       task_id = query.data.split(":", 1)[1]
#       import subprocess
#       subprocess.run(["python", "backlog_sync.py", "--done", task_id], check=True)
#       await query.answer("Marked done")
