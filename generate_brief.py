"""
generate_brief.py - Build and deliver the daily brief.

Tasks come from backlog.json queued{} only. The script is read-only with
respect to life-os-data: it reads fetched inputs, writes local runtime output,
and sends a Telegram URL hash payload.
"""

import argparse
import base64
import json
import os
import re
import ssl
import sys
import urllib.request
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo


DEFAULT_PROJECTS_PATH = Path(r"G:\My Drive\Projects\Note-taking system") / "projects.json"
DOMAIN_TO_OS_CHIP = {
    "creative": "creative",
    "financial": "financial",
    "connection": "connection",
    "growth": "learning",
    "health": "none",
}
DOMAIN_PRIORITY = ["creative", "growth", "financial", "connection", "health"]
DAY_NAMES = ["monday", "tuesday", "wednesday", "thursday", "friday"]
AEDT_CRON = "0 20 * * 0-4"
AEST_CRON = "0 21 * * 0-4"
DEFAULT_WEEK_CONTEXT = {
    "monday": "home",
    "tuesday": "home",
    "wednesday": "home",
    "thursday": "home",
    "friday": "home",
}
CONTEXT_INCOMPATIBLE = {
    "home": {"office"},
    "office": {"home"},
    "mobile": {"home", "office"},
}
try:
    LOCAL_TZ = ZoneInfo("Australia/Sydney")
except Exception:
    LOCAL_TZ = None


def first_sunday(year: int, month: int) -> date:
    day = date(year, month, 1)
    while day.weekday() != 6:
        day += timedelta(days=1)
    return day


def sydney_offset_for_utc(utc_now: datetime) -> timedelta:
    year = utc_now.year
    dst_start = datetime.combine(first_sunday(year, 10), datetime.min.time()) - timedelta(hours=8)
    dst_end = datetime.combine(first_sunday(year, 4), datetime.min.time()) - timedelta(hours=8)
    if dst_end <= utc_now < dst_start:
        return timedelta(hours=10)
    return timedelta(hours=11)


def parse_energy_from_note(note_path: Path) -> str:
    """Extract energy level from a processed note, defaulting to medium."""
    if not note_path.exists() or note_path.stat().st_size == 0:
        return "medium"

    content = note_path.read_text(encoding="utf-8")
    match = re.search(r"### Energy Mood\s*(.*?)(?=\n### |\n## |\Z)", content, re.DOTALL)
    if not match:
        return "medium"

    section = match.group(1).lower()
    level_match = re.search(r"energy level[:\s]+(\w+)", section)
    if level_match:
        raw = level_match.group(1).lower()
        if raw == "low":
            return "low"
        if raw == "high":
            return "high"
        return "medium"

    low_signals = ["exhausted", "drained", "depleted", "cooked", "fried", "low"]
    high_signals = ["energised", "energized", "excited", "great", "high"]

    if any(word in section for word in low_signals):
        return "low"
    if any(word in section for word in high_signals):
        return "high"
    return "medium"


def sanitise(text: str) -> str:
    """Strip prompt-like fragments and cap text length for the card."""
    text = str(text or "").strip()[:200]
    return re.sub(r"(?i)(ignore|system prompt|instruction|<\|)[\s:]*", "", text)


def display_text(task: dict) -> str:
    first = sanitise(task.get("firstAction", ""))
    title = sanitise(task.get("title", ""))
    return first if first and len(first) > 10 else title


def load_projects(projects_path: Path) -> list[dict]:
    with open(projects_path, encoding="utf-8") as f:
        data = json.load(f)
    projects_raw = data if isinstance(data, list) else data.get("projects", [])
    return [
        p for p in projects_raw
        if p.get("active", True) and p.get("status") != "on_hold"
    ]


def is_compatible(task: dict, day_context: str) -> bool:
    task_contexts = set(task.get("context") or [])
    if not task_contexts:
        return True
    incompatible_with_day = CONTEXT_INCOMPATIBLE.get(day_context, set())
    return not task_contexts.intersection(incompatible_with_day)


def get_day_context(backlog: dict, today: date) -> str:
    if today.weekday() >= 5:
        return "home"
    week_context = {**DEFAULT_WEEK_CONTEXT, **(backlog.get("weekContext") or {})}
    day_name = DAY_NAMES[today.weekday()]
    day_context = week_context.get(day_name, "home")
    return day_context if day_context in {"home", "office", "mobile"} else "home"


def sort_tasks(queued: dict, day_context: str, projects: list[dict]) -> list[tuple[str, dict]]:
    shipping_names = {
        sanitise(p.get("name", "")).lower()
        for p in projects
        if p.get("shipping_this_week")
    }
    shipping_names.discard("")

    def is_shipping(task: dict) -> bool:
        title = sanitise(task.get("title", "")).lower()
        return any(name in title or title in name for name in shipping_names)

    def sort_key(item: tuple[str, dict]) -> tuple[int, int, int]:
        domain, task = item
        compatible = 0 if is_compatible(task, day_context) else 1
        shipping = 0 if is_shipping(task) else 1
        domain_rank = DOMAIN_PRIORITY.index(domain) if domain in DOMAIN_PRIORITY else 99
        return compatible, shipping, domain_rank

    all_queued = [
        (domain, task)
        for domain, task in queued.items()
        if task and isinstance(task, dict)
    ]
    return sorted(all_queued, key=sort_key)


def weekday_index(today: date) -> int:
    epoch = date(2026, 1, 5)
    total = 0
    current = epoch
    while current < today:
        if current.weekday() < 5:
            total += 1
        current += timedelta(days=1)
    return total


def rotate_items(items: list[tuple[str, dict]], offset: int) -> list[tuple[str, dict]]:
    if not items:
        return []
    offset %= len(items)
    return items[offset:] + items[:offset]


def rotate_tasks_for_day(
    sorted_tasks: list[tuple[str, dict]],
    day_context: str,
    today: date,
) -> list[tuple[str, dict]]:
    rotation = weekday_index(today)
    compatible = [
        (domain, task)
        for domain, task in sorted_tasks
        if is_compatible(task, day_context)
    ]
    incompatible = [
        (domain, task)
        for domain, task in sorted_tasks
        if not is_compatible(task, day_context)
    ]
    return rotate_items(compatible, rotation) + rotate_items(incompatible, rotation)


def get_one_thing(rotated_tasks: list[tuple[str, dict]]) -> tuple[str | None, dict | None]:
    if not rotated_tasks:
        return None, None
    return rotated_tasks[0]


def build_task_slots(rotated_tasks: list[tuple[str, dict]]) -> list[tuple[str, dict] | None]:
    slots: list[tuple[str, dict] | None] = list(rotated_tasks[:3])
    while len(slots) < 3:
        slots.append(None)
    return slots


def build_tasks(task_slots: list[tuple[str, dict] | None]) -> list[dict]:
    tasks = []
    for slot in task_slots:
        if not slot:
            tasks.append({"text": "Review and queue a task for this domain", "done": False})
            continue
        _, task = slot
        tasks.append({"text": display_text(task), "done": False})
    return tasks


def choose_os_task(sorted_tasks: list[tuple[str, dict]], day_context: str) -> tuple[str, str, dict | None]:
    for preferred_domain in DOMAIN_PRIORITY:
        for domain, task in sorted_tasks:
            if domain == preferred_domain and is_compatible(task, day_context):
                os_task = {
                    "id": task.get("id", ""),
                    "title": sanitise(task.get("title", "")),
                    "firstAction": sanitise(task.get("firstAction", "")),
                }
                return (
                    DOMAIN_TO_OS_CHIP.get(domain, "none"),
                    display_text(task),
                    os_task,
                )
    return "none", "", None


def build_brief(backlog: dict, projects: list[dict], energy: str, today: date) -> dict:
    today_str = today.isoformat()
    queued = backlog.get("queued", {})
    day_context = get_day_context(backlog, today)
    sorted_tasks = sort_tasks(queued, day_context, projects)
    rotated_tasks = rotate_tasks_for_day(sorted_tasks, day_context, today)
    task_slots = build_task_slots(rotated_tasks)

    if not sorted_tasks:
        day = {
            "energy": energy,
            "one": "No tasks queued - open domain backlog and queue this week's tasks.",
            "tasks": build_tasks(task_slots),
            "os": "none",
            "osDetail": "Sunday review needed - backlog is empty.",
            "osTask": None,
            "leisure": "",
            "leisureDetail": "",
        }
    else:
        _, one_task = get_one_thing(rotated_tasks)
        one_text = display_text(one_task) if one_task else ""
        os_chip, os_detail, os_task = choose_os_task(sorted_tasks, day_context)
        day = {
            "energy": energy,
            "one": one_text,
            "tasks": build_tasks(task_slots),
            "os": os_chip,
            "osDetail": os_detail,
            "osTask": os_task,
            "leisure": "",
            "leisureDetail": "",
        }

    # The static client receives display data only, never credentials or write metadata.
    return {today_str: day}


def encode_payload(payload: dict) -> str:
    return base64.b64encode(json.dumps(payload).encode("utf-8")).decode("ascii")


def send_telegram(url: str, brief: dict, today_str: str) -> None:
    bot_token = os.environ.get("TELEGRAM_BOT_TOKEN")
    chat_id = os.environ.get("TELEGRAM_CHAT_ID")
    if not bot_token or not chat_id:
        print("TELEGRAM_BOT_TOKEN or TELEGRAM_CHAT_ID not set - skipping delivery.")
        return

    day = brief[today_str]
    tasks = day.get("tasks", [])
    task_lines = "\n".join(f"  - {t['text']}" for t in tasks)
    message = (
        "Good morning. Here's your day.\n\n"
        f"Energy: {day.get('energy', '-').capitalize()}\n\n"
        f"One thing:\n  {day.get('one', '-')}\n\n"
        f"Tasks:\n{task_lines}\n\n"
        f"OS block: {day.get('osDetail') or day.get('os') or '-'}"
    )
    payload = json.dumps({
        "chat_id": chat_id,
        "text": message,
        "disable_web_page_preview": True,
        "reply_markup": json.dumps({
            "inline_keyboard": [[{"text": "Open daily card", "url": url}]]
        }),
    }).encode("utf-8")

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

    try:
        with urllib.request.urlopen(req, timeout=10, context=ssl_ctx) as resp:
            result = json.loads(resp.read())
            if result.get("ok"):
                print("Telegram notification sent.")
            else:
                print(f"Telegram API error: {result}")
    except Exception as exc:
        print(f"Telegram delivery failed: {exc}")


def local_now() -> datetime:
    if LOCAL_TZ:
        return datetime.now(LOCAL_TZ)
    utc_now = datetime.now(timezone.utc).replace(tzinfo=None)
    return utc_now + sydney_offset_for_utc(utc_now)


def resolve_today(date_arg: str | None) -> date:
    if date_arg:
        return date.fromisoformat(date_arg)
    return local_now().date()


def expected_schedule_for_local_time(now: datetime) -> str:
    offset = now.utcoffset()
    if offset is None:
        utc_now = datetime.now(timezone.utc).replace(tzinfo=None)
        offset = sydney_offset_for_utc(utc_now)
    return AEDT_CRON if offset == timedelta(hours=11) else AEST_CRON


def should_send_now(date_arg: str | None, dry_run: bool) -> bool:
    if date_arg or dry_run:
        return True
    if os.environ.get("GITHUB_EVENT_NAME") == "workflow_dispatch":
        return True
    now = local_now()
    if now.weekday() >= 5:
        return False

    if os.environ.get("GITHUB_EVENT_NAME") == "schedule":
        actual_schedule = os.environ.get("GITHUB_EVENT_SCHEDULE", "")
        expected_schedule = expected_schedule_for_local_time(now)
        if actual_schedule:
            return actual_schedule == expected_schedule
        return 6 <= now.hour <= 9

    return now.hour == 7


def main() -> None:
    parser = argparse.ArgumentParser(description="Generate and deliver daily brief")
    parser.add_argument("--note", help="Path to processed note .md")
    parser.add_argument("--projects", help="Path to projects.json")
    parser.add_argument("--backlog", help="Path to backlog.json")
    parser.add_argument("--date", help="Override local date for deterministic tests (YYYY-MM-DD)")
    parser.add_argument("--dry-run", action="store_true", help="Print redacted brief and skip Telegram")
    args = parser.parse_args()

    today = resolve_today(args.date)
    today_str = today.isoformat()

    projects_path = Path(args.projects) if args.projects else Path(
        os.environ.get("LIFE_OS_PROJECTS_PATH", DEFAULT_PROJECTS_PATH)
    )
    if not projects_path.exists():
        print(f"projects.json not found at: {projects_path}")
        sys.exit(1)
    projects = load_projects(projects_path)
    print(f"{len(projects)} active projects loaded.")

    backlog_path = Path(args.backlog) if args.backlog else Path("fetched_backlog.json")
    if not backlog_path.exists():
        print(f"backlog not found at: {backlog_path}")
        print("Run fetch_inputs.py first, or pass --backlog path/to/backlog.json")
        sys.exit(1)
    with open(backlog_path, encoding="utf-8") as f:
        backlog = json.load(f)

    queued = backlog.get("queued", {})
    filled = sum(1 for value in queued.values() if value)
    print(f"Backlog loaded - {filled}/5 domains queued.")
    print(f"Day context: {get_day_context(backlog, today)} ({today_str})")
    print(f"Weekday rotation index: {weekday_index(today)}")

    note_path = Path(args.note) if args.note else Path("fetched_note.md")
    energy = parse_energy_from_note(note_path)
    print(f"Energy: {energy}")

    brief = build_brief(backlog, projects, energy, today)
    redacted = brief

    print("\nBrief assembled:")
    print("=" * 60)
    print(json.dumps(redacted, indent=2))
    print("=" * 60)

    out_path = Path("brief_output.json")
    out_path.write_text(json.dumps(redacted, indent=2), encoding="utf-8")
    print(f"Saved redacted runtime output to {out_path}")

    encoded = encode_payload(brief)
    url = f"https://navigon50.github.io/daily_card/#{encoded}"
    print("\nPre-populated URL:")
    print(url)
    print("Task completion is stored locally; no GitHub write token is embedded in the URL.")

    if args.dry_run:
        print("\n[dry-run] Skipping Telegram.")
        return

    if not should_send_now(args.date, args.dry_run):
        print("Outside the active Australia/Sydney scheduled send window - skipping delivery.")
        return

    send_telegram(url, brief, today_str)


if __name__ == "__main__":
    main()
