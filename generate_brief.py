"""
generate_brief.py — Build and deliver the daily brief.

Tasks come directly from backlog.json queued{} — set during Sunday review.
No LLM is used for task selection. Brief is built deterministically.
Voice note is optional and used only for energy level.

Usage:
  python generate_brief.py                          # standard run
  python generate_brief.py --note path/to/note.md   # supply note directly
  python generate_brief.py --projects path/to/p.json
  python generate_brief.py --backlog path/to/b.json
  python generate_brief.py --dry-run                # print brief, skip Telegram

Environment variables:
  TELEGRAM_BOT_TOKEN
  TELEGRAM_CHAT_ID
"""

import argparse
import base64
import json
import os
import re
import ssl
import sys
import urllib.request
import urllib.error
from datetime import date, timedelta
from pathlib import Path


# ---------------------------------------------------------------------------
# CONFIG
# ---------------------------------------------------------------------------

DEFAULT_PROJECTS_PATH = Path(r"G:\My Drive\Projects\Note-taking system") / "projects.json"
DEFAULT_PROCESSED_FOLDER = Path(r"G:\My Drive\Projects\Note-taking system") / "processed"

# Domain key → daily card OS chip value
DOMAIN_TO_OS_CHIP = {
    "creative":   "creative",
    "financial":  "financial",
    "connection": "connection",
    "growth":     "learning",
    "health":     "none",
}

# Ordered preference for OS block (work/personal focus domains)
OS_DOMAIN_PRIORITY = ["creative", "growth", "financial", "connection", "health"]

# ---------------------------------------------------------------------------


def get_ssl_context():
    try:
        import certifi
        return ssl.create_default_context(cafile=certifi.where())
    except ImportError:
        return ssl.create_default_context()


# ---------------------------------------------------------------------------
# Note parsing — energy only
# ---------------------------------------------------------------------------

def parse_energy_from_note(note_path: Path) -> str:
    """
    Extract energy level from a processed note.
    Returns 'low', 'medium', or 'high'. Defaults to 'medium' if absent or unclear.
    """
    if not note_path.exists() or note_path.stat().st_size == 0:
        return "medium"

    content = note_path.read_text(encoding="utf-8")

    # Find the Energy Mood section
    match = re.search(r"### Energy Mood\s*(.*?)(?=\n### |\n## |\Z)", content, re.DOTALL)
    if not match:
        return "medium"

    section = match.group(1).lower()

    # Look for explicit energy level bullet
    level_match = re.search(r"energy level[:\s]+(\w+)", section)
    if level_match:
        raw = level_match.group(1).lower()
        if raw in ("low",):
            return "low"
        if raw in ("high",):
            return "high"
        return "medium"

    # Fallback: keyword scan
    low_signals  = ["exhausted", "drained", "depleted", "cooked", "fried", "low"]
    high_signals = ["energised", "energized", "excited", "great", "high"]

    for word in low_signals:
        if word in section:
            return "low"
    for word in high_signals:
        if word in section:
            return "high"

    return "medium"


# ---------------------------------------------------------------------------
# Brief assembly — deterministic from queued{}
# ---------------------------------------------------------------------------

def sanitise(text: str) -> str:
    """Strip potential prompt injection content and enforce length cap."""
    text = text.strip()[:200]
    text = re.sub(r'(?i)(ignore|system prompt|instruction|<\|)[\s:]*', '', text)
    return text


def build_brief(queued: dict, projects: list, energy: str, today: date) -> dict:
    """
    Build the daily card JSON deterministically from queued tasks.

    queued: dict of domain → task object (or None), from backlog.json
    projects: list of project objects from projects.json
    energy: 'low'|'medium'|'high'
    today: date object
    """
    today_str = today.isoformat()

    # Index projects by name for shipping_this_week lookup
    shipping_project_names = {
        p["name"].lower() for p in projects
        if p.get("status") == "active" and p.get("shipping_this_week")
    }

    # Collect all queued tasks (non-null), preserving domain
    queued_tasks = [
        (domain, task)
        for domain, task in queued.items()
        if task and isinstance(task, dict)
    ]

    if not queued_tasks:
        # No tasks queued — send a nudge brief
        return {
            today_str: {
                "energy": energy,
                "one": "No tasks queued — open domain backlog and queue this week's tasks.",
                "tasks": [
                    {"text": "Open domain backlog and queue one task per domain", "done": False},
                    {"text": "Review projects and update shipping_this_week flags", "done": False},
                    {"text": "Push updated backlog to GitHub", "done": False},
                ],
                "os": "none",
                "osDetail": "Sunday review needed — backlog is empty.",
                "leisure": "",
                "leisureDetail": "",
            }
        }

    # ── Sort tasks by priority ──────────────────────────────────────────────
    # Priority order:
    # 1. Linked to a shipping_this_week project (by matching task title keywords)
    # 2. Domain order: creative > growth > financial > connection > health
    def task_priority(item):
        domain, task = item
        title_lower = sanitise(task.get("title", "")).lower()
        is_shipping = any(
            proj_name in title_lower or title_lower in proj_name
            for proj_name in shipping_project_names
        )
        domain_rank = OS_DOMAIN_PRIORITY.index(domain) if domain in OS_DOMAIN_PRIORITY else 99
        return (0 if is_shipping else 1, domain_rank)

    queued_tasks.sort(key=task_priority)

    # ── Build tasks list (up to 3) ──────────────────────────────────────────
    tasks = []
    for domain, task in queued_tasks[:3]:
        title = sanitise(task.get("title", ""))
        first = sanitise(task.get("firstAction", ""))
        # Use firstAction as the card task text if it's more specific
        task_text = first if first and len(first) > 10 else title
        tasks.append({"text": task_text, "done": False})

    # Pad to 3 if fewer than 3 domains are queued
    while len(tasks) < 3:
        tasks.append({"text": "Review and queue a task for this domain", "done": False})

    # ── One thing — highest priority task ──────────────────────────────────
    top_domain, top_task = queued_tasks[0]
    top_title  = sanitise(top_task.get("title", ""))
    top_first  = sanitise(top_task.get("firstAction", ""))
    one = top_first if top_first and len(top_first) > 10 else top_title

    # ── OS block — first queued task in a work-relevant domain ─────────────
    os_chip    = "none"
    os_detail  = ""

    for domain in OS_DOMAIN_PRIORITY:
        task_obj = queued.get(domain)
        if task_obj and isinstance(task_obj, dict):
            os_chip   = DOMAIN_TO_OS_CHIP.get(domain, "none")
            os_detail = sanitise(task_obj.get("firstAction", "") or task_obj.get("title", ""))
            break

    return {
        today_str: {
            "energy":        energy,
            "one":           one,
            "tasks":         tasks,
            "os":            os_chip,
            "osDetail":      os_detail,
            "leisure":       "",
            "leisureDetail": "",
        }
    }


# ---------------------------------------------------------------------------
# Telegram delivery
# ---------------------------------------------------------------------------

def send_telegram(url: str, brief: dict) -> None:
    """Send the daily brief as a Telegram notification with inline button."""
    bot_token = os.environ.get("TELEGRAM_BOT_TOKEN")
    chat_id   = os.environ.get("TELEGRAM_CHAT_ID")

    if not bot_token or not chat_id:
        print("⚠️  TELEGRAM_BOT_TOKEN or TELEGRAM_CHAT_ID not set — skipping delivery.")
        return

    day = list(brief.values())[0]
    energy_emoji = {"high": "⚡", "medium": "🟡", "low": "🔵"}.get(day.get("energy", ""), "•")
    tasks = day.get("tasks", [])
    task_lines = "\n".join(f"  • {t['text']}" for t in tasks)

    message = (
        f"☀️ Good morning. Here's your day.\n\n"
        f"{energy_emoji} Energy: {day.get('energy', '—').capitalize()}\n\n"
        f"🎯 One thing:\n  {day.get('one', '—')}\n\n"
        f"✅ Tasks:\n{task_lines}\n\n"
        f"💻 OS block: {day.get('osDetail') or day.get('os') or '—'}"
    )

    payload = json.dumps({
        "chat_id":                  chat_id,
        "text":                     message,
        "disable_web_page_preview": True,
        "reply_markup": json.dumps({
            "inline_keyboard": [[
                {"text": "📋 Open daily card", "url": url}
            ]]
        })
    }).encode()

    req = urllib.request.Request(
        f"https://api.telegram.org/bot{bot_token}/sendMessage",
        data=payload,
        headers={"Content-Type": "application/json"},
        method="POST",
    )

    try:
        import certifi
        ssl_ctx = ssl.create_default_context(cafile=certifi.where())
    except ImportError:
        ssl_ctx = ssl.create_default_context()
        ssl_ctx.check_hostname = False
        ssl_ctx.verify_mode = ssl.CERT_NONE

    try:
        with urllib.request.urlopen(req, timeout=10, context=ssl_ctx) as resp:
            result = json.loads(resp.read())
            if result.get("ok"):
                print("📨 Telegram notification sent.")
            else:
                print(f"⚠️  Telegram API error: {result}")
    except Exception as e:
        print(f"❌ Telegram delivery failed: {e}")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(description="Generate and deliver daily brief")
    parser.add_argument("--note",     help="Path to processed note .md (optional, for energy)")
    parser.add_argument("--projects", help="Path to projects.json")
    parser.add_argument("--backlog",  help="Path to backlog.json (fetched_backlog.json)")
    parser.add_argument("--dry-run",  action="store_true", help="Print brief, skip Telegram")
    args = parser.parse_args()

    today = date.today()

    # ── Load projects ───────────────────────────────────────────────────────
    projects_path = Path(args.projects) if args.projects else Path(
        os.environ.get("LIFE_OS_PROJECTS_PATH", DEFAULT_PROJECTS_PATH)
    )
    if not projects_path.exists():
        print(f"❌ projects.json not found at: {projects_path}")
        sys.exit(1)

    with open(projects_path, encoding="utf-8") as f:
        projects_data = json.load(f)
    projects = [p for p in projects_data["projects"] if p.get("status") == "active"]
    print(f"✅ {len(projects)} active projects loaded.")

    # ── Load backlog ────────────────────────────────────────────────────────
    backlog_path = Path(args.backlog) if args.backlog else Path("fetched_backlog.json")
    if not backlog_path.exists():
        print(f"❌ backlog not found at: {backlog_path}")
        print("   Run fetch_inputs.py first, or pass --backlog path/to/backlog.json")
        sys.exit(1)

    with open(backlog_path, encoding="utf-8") as f:
        backlog_data = json.load(f)

    queued = backlog_data.get("queued", {})
    filled = sum(1 for v in queued.values() if v)
    print(f"✅ Backlog loaded — {filled}/5 domains queued.")

    if filled == 0:
        print("⚠️  No tasks queued — brief will prompt for Sunday review.")

    # ── Parse energy from note ──────────────────────────────────────────────
    note_path = Path(args.note) if args.note else Path("fetched_note.md")
    energy = parse_energy_from_note(note_path)
    note_source = f"from {note_path.name}" if note_path.exists() and note_path.stat().st_size > 0 else "no note — defaulting"
    print(f"✅ Energy: {energy} ({note_source})")

    # ── Build brief ─────────────────────────────────────────────────────────
    brief = build_brief(queued, projects, energy, today)

    print("\n✅ Brief assembled:")
    print("=" * 60)
    print(json.dumps(brief, indent=2))
    print("=" * 60)

    # ── Save ────────────────────────────────────────────────────────────────
    out_path = Path("brief_output.json")
    out_path.write_text(json.dumps(brief, indent=2), encoding="utf-8")
    print(f"\n💾 Saved to {out_path}")

    # ── Build URL ───────────────────────────────────────────────────────────
    encoded = base64.b64encode(json.dumps(brief).encode()).decode()
    url = f"https://navigon50.github.io/daily_card/#{encoded}"
    print(f"\n🔗 Pre-populated URL:")
    print(url)

    if args.dry_run:
        print("\n[dry-run] Skipping Telegram.")
        return

    # ── Send ────────────────────────────────────────────────────────────────
    send_telegram(url, brief)


if __name__ == "__main__":
    main()