"""
Stage 2: Daily Brief Generator
Reads projects.json + yesterday's processed voice note → calls Claude via OpenRouter → outputs PWA-ready JSON.

Requires:
  pip install openai

Usage:
  python generate_brief.py                    # uses yesterday's date, searches PROCESSED_FOLDER
  python generate_brief.py --date 2026-01-15  # override target recording date
  python generate_brief.py --note path/to/processed-note.md  # supply note directly (for testing)
  python generate_brief.py --dry-run          # print prompt only, skip API call

Environment variables:
  OPENROUTER_API_KEY   your OpenRouter API key (required)
  LIFE_OS_PROJECTS_PATH  override path to projects.json
  LIFE_OS_PROCESSED_PATH override path to processed notes folder
"""

import argparse
import base64
import json
import os
import re
import sys
from datetime import date, datetime, timedelta
from pathlib import Path


# ---------------------------------------------------------------------------
# CONFIG — edit these for your machine, or set as environment variables
# ---------------------------------------------------------------------------

# Path to your local projects.json (or set LIFE_OS_PROJECTS_PATH env var)
DEFAULT_PROJECTS_PATH = Path(r"G:\My Drive\Projects\Note-taking system") / "projects.json"

# Path to processed notes folder (or set LIFE_OS_PROCESSED_PATH env var)
DEFAULT_PROCESSED_FOLDER = Path(r"G:\My Drive\Projects\Note-taking system") / "processed"

# OpenRouter settings
OPENROUTER_BASE_URL = "https://openrouter.ai/api/v1"
CLAUDE_MODEL = "anthropic/claude-sonnet-4-5"  # OpenRouter model ID — adjust if needed

# How many days to walk back looking for a note
MAX_LOOKBACK_DAYS = 3

# ---------------------------------------------------------------------------


def find_note_for_date(processed_folder: Path, target_date: date) -> Path | None:
    """
    Search processed/ (and its _review_needed subfolder) for a note whose filename
    contains the target date in YYYYMMDD format. Walks back up to MAX_LOOKBACK_DAYS.
    """
    search_dirs = [
        processed_folder,
        processed_folder / "_review_needed",
    ]

    for days_back in range(MAX_LOOKBACK_DAYS + 1):
        check_date = target_date - timedelta(days=days_back)
        date_str = check_date.strftime("%Y%m%d")   # e.g. 20260115
        iso_str  = check_date.strftime("%Y-%m-%d")  # e.g. 2026-01-15

        for search_dir in search_dirs:
            if not search_dir.exists():
                continue
            for f in search_dir.glob("processed-*.md"):
                # Match either YYYYMMDD or YYYY-MM-DD anywhere in filename
                if date_str in f.name or iso_str in f.name:
                    if days_back > 0:
                        print(f"  ℹ️  No note for {target_date}, found note from {check_date} (walked back {days_back} day(s))")
                    return f

    return None


def parse_processed_note(note_path: Path) -> dict:
    """
    Extract structured sections from a processed markdown note.
    Returns a dict with keys matching the NoteProcessor insight categories.
    """
    content = note_path.read_text(encoding="utf-8")

    # Extract transcript date from metadata header
    transcript_date = "unknown"
    date_match = re.search(r"\*\*Transcript Date:\*\*\s*(.+)", content)
    if date_match:
        raw = date_match.group(1).strip()
        if raw != "unknown":
            transcript_date = raw

    # Fall back to recording date from filename if LLM returned unknown
    if transcript_date == "unknown":
        # filename pattern: processed-{transcript_date}-{recording_name}.md
        # recording_name often contains YYYYMMDD
        date8_match = re.search(r"(\d{4})(\d{2})(\d{2})", note_path.stem)
        if date8_match:
            y, m, d_ = date8_match.groups()
            transcript_date = f"{y}-{m}-{d_}"

    # Parse ## Extracted Insights section
    insights = {}
    insights_block_match = re.search(
        r"## Extracted Insights\s*(.*?)(?=\n## |\Z)",
        content,
        re.DOTALL,
    )

    if insights_block_match:
        block = insights_block_match.group(1)
        # Split on ### headers
        sections = re.split(r"\n### (.+)\n", block)
        # sections = [pre, header1, body1, header2, body2, ...]
        it = iter(sections[1:])  # skip pre-amble
        for header in it:
            body = next(it, "")
            key = header.strip().lower().replace(" ", "_")
            insights[key] = body.strip()

    return {
        "transcript_date": transcript_date,
        "note_path": str(note_path),
        "insights": insights,
    }


def build_prompt(note_data: dict, projects: list, today: date) -> str:
    """
    Build the Claude prompt that maps voice note + projects → PWA JSON.
    """
    # Identify high-priority projects
    shipping_projects = [p for p in projects if p.get("shipping_this_week")]
    stale_projects = [
        p for p in projects
        if p.get("status") == "active" and p.get("last_mentioned")
        and (today - date.fromisoformat(p["last_mentioned"])).days >= 14
    ]

    # Build labelled project list with explicit slot instructions inline
    projects_lines = []
    for p in projects:
        if p.get("status") != "active":
            continue
        flags = []
        if p in shipping_projects:
            flags.append("SHIPPING-THIS-WEEK")
        if p in stale_projects:
            flags.append("STALE-14-DAYS")
        flag_str = f" [{', '.join(flags)}]" if flags else ""
        projects_lines.append(
            f"- {p['name']} ({p['domain']}){flag_str}\n"
            f"  Context: {p['context']}\n"
            f"  Last action: {p['last_action']}\n"
            f"  Last mentioned: {p.get('last_mentioned', 'unknown')}"
        )
    projects_text = "\n".join(projects_lines)

    # Shipping and stale summaries for the task algorithm section
    shipping_names = ", ".join(p["name"] for p in shipping_projects) or "none"
    stale_names    = ", ".join(p["name"] for p in stale_projects)    or "none"

    # Voice note is for energy/mood context only — extract minimal signal
    insights = note_data["insights"]
    energy_text = insights.get("energy_mood", "nothing found")
    # Include tasks from the note only as supplementary signal, clearly labelled
    note_tasks_text = insights.get("tasks", "nothing found")

    today_str = today.isoformat()

    prompt = f"""You produce a daily morning brief as a single JSON object. Follow every instruction exactly.

TODAY: {today_str}
VOICE NOTE DATE: {note_data['transcript_date']}

════════════════════════════════
SECTION A — ACTIVE PROJECTS (source of truth for tasks)
════════════════════════════════
{projects_text}

Projects marked SHIPPING-THIS-WEEK: {shipping_names}
Projects marked STALE-14-DAYS: {stale_names}

════════════════════════════════
SECTION B — VOICE NOTE (use only for energy level and mood context)
════════════════════════════════
Energy/Mood from last night's note:
{energy_text}

Tasks mentioned in last night's note (supplementary only — do NOT copy these directly):
{note_tasks_text}

════════════════════════════════
OUTPUT INSTRUCTIONS
════════════════════════════════

Return ONLY valid JSON. No markdown fences. No explanation. No text before or after the JSON.

Required schema:
{{
  "{today_str}": {{
    "energy": "<low|medium|high>",
    "one": "<single sentence>",
    "tasks": [
      {{"text": "<task>", "done": false}},
      {{"text": "<task>", "done": false}},
      {{"text": "<task>", "done": false}}
    ],
    "os": "<short phrase>",
    "osDetail": "<one sentence>",
    "leisure": "",
    "leisureDetail": ""
  }}
}}

FIELD RULES:

ENERGY:
- Read the energy/mood section from the voice note.
- Default to "medium".
- Use "low" only if the note explicitly describes exhaustion, feeling drained, or depleted.
- Use "high" only if the note explicitly describes feeling energised or excited.

ONE (most important thing today):
- If any project is labelled SHIPPING-THIS-WEEK, the "one" is the next concrete action for the most urgent of those projects.
- Otherwise, pick the single most pressing item from active projects.
- One sentence. No acronyms.

TASKS (exactly 3, generated using this slot-filling algorithm — follow in order):
  Slot 1: The highest-priority SHIPPING-THIS-WEEK project's next action. If no SHIPPING-THIS-WEEK projects exist, use the most urgent active project.
  Slot 2: A second SHIPPING-THIS-WEEK project if one exists. Otherwise, use the next most urgent active project (prefer work domain if slot 1 was personal, or vice versa).
  Slot 3: If any project is labelled STALE-14-DAYS, create a check-in task for it. Otherwise, use the next most urgent active project.
- Each task must start with a verb and be concrete.
- Do NOT copy tasks verbatim from the voice note. The voice note tasks are context only.
- Never use acronyms.

OS (today's work focus area):
- Pick the single active WORK domain project most relevant today. One short phrase. No acronyms.

OSDETAIL:
- One sentence describing what specifically needs to happen in that work area today.

LEISURE + LEISUREDETAIL:
- Always set both to empty string "". Never populate these.
"""

    return prompt


def call_claude(prompt: str) -> str:
    """Call Claude via OpenRouter and return the raw response text."""
    try:
        from openai import OpenAI
    except ImportError:
        print("❌ openai package not installed. Run: pip install openai")
        sys.exit(1)

    api_key = os.environ.get("OPENROUTER_API_KEY")
    if not api_key:
        print("❌ OPENROUTER_API_KEY environment variable not set.")
        sys.exit(1)

    client = OpenAI(
        base_url=OPENROUTER_BASE_URL,
        api_key=api_key,
    )

    response = client.chat.completions.create(
        model=CLAUDE_MODEL,
        max_tokens=1024,
        messages=[{"role": "user", "content": prompt}],
    )
    return response.choices[0].message.content.strip()


def send_telegram(url: str, brief: dict) -> None:
    """Send the daily brief as a loud Telegram notification."""
    import urllib.request

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
        "chat_id":    chat_id,
        "text":       message,
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

    # Build SSL context — use certifi if available, otherwise fall back to
    # unverified (acceptable here since we're calling a known Telegram endpoint)
    try:
        import ssl, certifi
        ssl_ctx = ssl.create_default_context(cafile=certifi.where())
    except ImportError:
        import ssl
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


def parse_response(raw: str) -> dict:
    """Parse Claude's response, stripping any accidental markdown fences."""
    cleaned = re.sub(r"^```(?:json)?\s*", "", raw.strip())
    cleaned = re.sub(r"\s*```$", "", cleaned.strip())
    return json.loads(cleaned)


def main():
    parser = argparse.ArgumentParser(description="Generate daily brief JSON")
    parser.add_argument("--date", help="Target recording date (YYYY-MM-DD). Defaults to yesterday.")
    parser.add_argument("--note", help="Path to processed note file (skips folder search).")
    parser.add_argument("--projects", help="Path to projects.json. Overrides default.")
    parser.add_argument("--dry-run", action="store_true", help="Print prompt only, skip API call.")
    args = parser.parse_args()

    today = date.today()
    target_date = date.fromisoformat(args.date) if args.date else today - timedelta(days=1)

    # --- Load projects ---
    projects_path = Path(args.projects) if args.projects else Path(
        os.environ.get("LIFE_OS_PROJECTS_PATH", DEFAULT_PROJECTS_PATH)
    )
    if not projects_path.exists():
        print(f"❌ projects.json not found at: {projects_path}")
        print("   Pass --projects /path/to/projects.json or set LIFE_OS_PROJECTS_PATH")
        sys.exit(1)

    with open(projects_path, encoding="utf-8") as f:
        projects_data = json.load(f)
    projects = [p for p in projects_data["projects"] if p.get("status") == "active"]
    print(f"✅ Loaded {len(projects)} active projects from {projects_path.name}")

    # --- Find/load note ---
    if args.note:
        note_path = Path(args.note)
        if not note_path.exists():
            print(f"❌ Note file not found: {note_path}")
            sys.exit(1)
    else:
        processed_folder = Path(
            os.environ.get("LIFE_OS_PROCESSED_PATH", DEFAULT_PROCESSED_FOLDER)
        )
        print(f"🔍 Searching for note from {target_date} in {processed_folder}...")
        note_path = find_note_for_date(processed_folder, target_date)
        if not note_path:
            print(f"❌ No processed note found for {target_date} (searched {MAX_LOOKBACK_DAYS} days back).")
            print("   Use --note path/to/file.md to supply one directly.")
            sys.exit(1)

    print(f"✅ Using note: {note_path.name}")
    note_data = parse_processed_note(note_path)
    print(f"   Transcript date: {note_data['transcript_date']}")
    print(f"   Sections found: {list(note_data['insights'].keys())}")

    # --- Build prompt ---
    prompt = build_prompt(note_data, projects, today)

    if args.dry_run:
        print("\n" + "="*60)
        print("DRY RUN — PROMPT ONLY")
        print("="*60)
        print(prompt)
        sys.exit(0)

    # --- Call Claude ---
    print(f"\n🤖 Calling Claude via OpenRouter ({CLAUDE_MODEL})...")
    raw_response = call_claude(prompt)

    # --- Parse and validate ---
    try:
        brief = parse_response(raw_response)
    except json.JSONDecodeError as e:
        print(f"❌ Claude returned invalid JSON: {e}")
        print("Raw response:")
        print(raw_response)
        sys.exit(1)

    # Basic schema validation
    today_str = today.isoformat()
    if today_str not in brief:
        print(f"⚠️  Warning: expected key '{today_str}' not found in response. Keys: {list(brief.keys())}")

    day_data = brief.get(today_str, list(brief.values())[0])
    task_count = len(day_data.get("tasks", []))
    if task_count != 3:
        print(f"⚠️  Warning: expected 3 tasks, got {task_count}")

    # --- Output ---
    print("\n✅ Brief generated successfully")
    print("="*60)
    print(json.dumps(brief, indent=2))
    print("="*60)

    # Save to file
    out_path = Path("brief_output.json")
    out_path.write_text(json.dumps(brief, indent=2), encoding="utf-8")
    print(f"\n💾 Saved to {out_path.resolve()}")

    # Build pre-populated URL
    encoded = base64.b64encode(json.dumps(brief).encode()).decode()
    url = f"https://navigon50.github.io/daily_card/#{encoded}"
    print(f"\n🔗 Pre-populated URL:")
    print(url)

    # Send Telegram notification
    send_telegram(url, brief)


if __name__ == "__main__":
    main()
