#!/usr/bin/env python3
"""
Daily to-do plan: read the Notion task board (the accurate, already-reconciled
source of truth) and post the day's plan to Slack so it pushes to the phone.

Division of labor: a SEPARATE 9 PM job reconciles the board each night. This 9 AM
job only READS the board and SENDS the plan. It does not detect completions.

Message is composed in two parts:
  PART 1  TODAY AT A GLANCE  -> first lines, fills the lock-screen preview
  PART 2  DETAILS            -> focus deep-dive + notes (AI if ANTHROPIC_API_KEY set,
                                otherwise built directly from task data)

Secrets come from environment variables (GitHub Actions secrets / local .env):
  NOTION_TOKEN          (required)
  SLACK_WEBHOOK_URL     (required)
  ANTHROPIC_API_KEY     (optional -> richer PART 2)
  ANTHROPIC_MODEL       (optional -> defaults to claude-sonnet-4-6)
  FORCE_SEND=true       (optional -> skip the 9 AM Eastern time gate; set on manual runs)
"""

import json
import os
import sys
import urllib.request
import urllib.error
from datetime import date, datetime
from zoneinfo import ZoneInfo

# ----------------------------------------------------------------------------
# Config
# ----------------------------------------------------------------------------

DATABASE_ID = "943797e7643f4403abd80536176f220f"   # the "Tasks" board
NOTION_VERSION = "2022-06-28"                       # stable; supports /databases/{id}/query
EASTERN = ZoneInfo("America/New_York")              # handles EDT/EST automatically

PRIORITY_RANK = {"🔴 High": 0, "🟡 Medium": 1, "🟢 Low": 2}
STATUS_DONE = "Done"
STATUS_IN_PROGRESS = "In progress"

# Project context used both for the AI prompt and to keep the fallback on-brand.
PROJECT_CONTEXT = """\
PRODUCT: Branded-dropshipping store around a Bluetooth sleep mask (MUSICOZY-style
multi-colorway fleece headband; ~$13.50 landed; priced $49.99 single / $79.99
couples 2-pack; ~$36 margin). Goal $1,000/day, ~$2k budget, ads-lean.
HOOKS: side-sleeper comfort (earbuds hurt on your side), couples ("she gets a dark
room, he gets his rain sounds"), white-noise/tinnitus wind-down, light/blackout.
COMPLIANCE: comfort/lifestyle claims only — NEVER medical claims.

MBB METHODOLOGY — Formula = Product -> Funnel -> Ads.
FUNNEL: Shopify + Shrine theme; do the 4 Foundational Docs FIRST (customer avatar,
why they buy + triggers, voice-of-customer language, objections — saved as PDFs);
then AI-build the store (Claude copy, NanaBanana Pro images, Kling/Sora/Veo video);
test checkout before ads.
ADS: message > ad, images first, ~80% swipe proven structures / 20% original,
1 CBO -> 3 ad sets -> 2-6 creatives, $50-250/day, broad, Big 4 geo.
DATA: judge on spend ($200-300), cut past breakeven CPA with no sale, scale winners.
MANTRA: money loves speed. Keep the daily ask realistic (~1-2 hrs)."""


# ----------------------------------------------------------------------------
# Notion
# ----------------------------------------------------------------------------

def _http_json(url, *, headers, data=None, method="GET"):
    body = json.dumps(data).encode() if data is not None else None
    req = urllib.request.Request(url, data=body, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            return json.loads(resp.read().decode())
    except urllib.error.HTTPError as e:
        detail = e.read().decode(errors="replace")
        raise SystemExit(f"HTTP {e.code} calling {url}\n{detail}")
    except urllib.error.URLError as e:
        raise SystemExit(f"Network error calling {url}: {e.reason}")


def fetch_tasks(token):
    """Query every row of the board, following pagination."""
    headers = {
        "Authorization": f"Bearer {token}",
        "Notion-Version": NOTION_VERSION,
        "Content-Type": "application/json",
    }
    url = f"https://api.notion.com/v1/databases/{DATABASE_ID}/query"
    results, cursor = [], None
    while True:
        payload = {"page_size": 100}
        if cursor:
            payload["start_cursor"] = cursor
        page = _http_json(url, headers=headers, data=payload, method="POST")
        results.extend(page.get("results", []))
        if page.get("has_more"):
            cursor = page.get("next_cursor")
        else:
            break
    return [parse_task(p) for p in results]


def _prop(props, name):
    return props.get(name) or {}


def parse_task(page):
    """Pull a flat dict out of a Notion page, tolerant of empty fields."""
    props = page.get("properties", {})

    title_parts = _prop(props, "Task").get("title", [])
    task = "".join(part.get("plain_text", "") for part in title_parts).strip()

    status_obj = _prop(props, "Status").get("status") or {}
    status = status_obj.get("name") or ""

    priority_obj = _prop(props, "Priority").get("select") or {}
    priority = priority_obj.get("name") or ""

    category_obj = _prop(props, "Category").get("select") or {}
    category = category_obj.get("name") or ""

    date_obj = _prop(props, "Due Date").get("date") or {}
    due_raw = date_obj.get("start") or ""
    # "2026-05-29" or "2026-05-29T09:00:00.000-04:00" -> a date
    due = None
    if due_raw:
        try:
            due = datetime.fromisoformat(due_raw.replace("Z", "+00:00")).date()
        except ValueError:
            try:
                due = date.fromisoformat(due_raw[:10])
            except ValueError:
                due = None

    return {
        "task": task or "(untitled task)",
        "status": status,
        "priority": priority,
        "category": category,
        "due": due,
    }


# ----------------------------------------------------------------------------
# Selection logic
# ----------------------------------------------------------------------------

def _sort_key(t):
    """Highest priority first, then earliest due date (undated last)."""
    return (PRIORITY_RANK.get(t["priority"], 3), t["due"] or date.max)


def choose_plan(tasks, today):
    """
    focus = the 'In progress' task (or, if none, highest-priority earliest-due
            task that isn't Done).
    todos = focus + any OTHER tasks due today that aren't Done.
    """
    active = [t for t in tasks if t["status"] != STATUS_DONE]

    in_progress = sorted(
        [t for t in active if t["status"] == STATUS_IN_PROGRESS], key=_sort_key
    )
    if in_progress:
        focus = in_progress[0]
        focus_confirmed_in_progress = True
    elif active:
        focus = sorted(active, key=_sort_key)[0]
        focus_confirmed_in_progress = False
    else:
        return None, [], False  # board is all Done / empty

    due_today = sorted(
        [t for t in active if t["due"] == today and t is not focus], key=_sort_key
    )
    todos = [focus] + due_today
    return focus, todos, focus_confirmed_in_progress


# ----------------------------------------------------------------------------
# Message — PART 1 (deterministic, lock-screen preview)
# ----------------------------------------------------------------------------

def build_glance(focus, todos, today):
    lines = [f"🌙 *Today — {today.strftime('%a, %b %-d')}* ({len(todos)} to-dos)"]
    for i, t in enumerate(todos, start=1):
        tag = " ← focus" if t is focus else ""
        lines.append(f"{i}) {t['task']}{tag}")
    return "\n".join(lines)


# ----------------------------------------------------------------------------
# Message — PART 2 (AI if available, else direct)
# ----------------------------------------------------------------------------

DIVIDER = "\n\n───────────────\n\n"


def _fmt_task_line(t):
    bits = [t["task"]]
    if t["priority"]:
        bits.append(t["priority"])
    if t["due"]:
        bits.append(f"due {t['due'].strftime('%b %-d')}")
    if t["status"]:
        bits.append(t["status"])
    return " · ".join(bits)


def build_details_direct(focus, todos, confirmed):
    """On-brand PART 2 built straight from task data (no AI)."""
    cat = focus["category"].lower()
    name = focus["task"].lower()

    # Map the focus to an MBB stage so the "why" and "where you are" stay grounded.
    if any(k in name for k in ("avatar", "objection", "voice", "why they buy", "foundational", "research", "doc")):
        why = "_Why:_ the 4 Foundational Docs are the base of the funnel — every word of copy and every ad angle pulls from them."
        steps = [
            "Open a Google Doc per missing doc (avatar, why-they-buy + triggers, voice-of-customer, objections).",
            "Pull 15–20 real phrases from Amazon/Reddit reviews of sleep masks — paste them verbatim as your voice-of-customer bank.",
            "Draft the avatar: side-sleeper / couples / wind-down — pick the ONE you lead with.",
            "List the top 5 objections (price, comfort, battery, washability, sound leak) and a one-line answer each.",
            "Export each as a PDF and check it into your project folder. Done = 4 PDFs saved.",
        ]
        where = "_Where you are:_ Funnel stage, pre-build. Docs first, then AI-build the store."
    elif any(k in name for k in ("store", "shopify", "shrine", "theme", "product page", "copy", "checkout", "build")):
        why = "_Why:_ no store = no place to send traffic. Build it from the Foundational Docs, then test checkout before a dollar of ads."
        steps = [
            "Install the Shrine theme on Shopify; set the single ($49.99) and couples 2-pack ($79.99) variants.",
            "Use Claude to write the product page from your voice-of-customer doc — lead with the side-sleeper/couples hook.",
            "Generate hero + lifestyle images with NanaBanana Pro (in-bed, side-sleeping, couple, colorways).",
            "Write 3 objection-busting sections (comfort, battery/washable, sound) — comfort/lifestyle claims only, NO medical claims.",
            "Place a real test order end-to-end. Done = checkout completes and the order shows in Shopify.",
        ]
        where = "_Where you are:_ Funnel stage, AI-build. Finish + test checkout, then move to Ads."
    elif any(k in name for k in ("ad", "creative", "video", "kling", "sora", "veo", "campaign", "cbo", "launch")):
        why = "_Why:_ message > ad and images-first. Proven structures (~80%) get you data fast; money loves speed."
        steps = [
            "Pick the lead hook (side-sleeper comfort or couples) and write it as the message BEFORE touching design.",
            "Build 4 image creatives: ~80% modeled on proven sleep/comfort ads, ~20% original angle.",
            "Add 1–2 short videos (Kling/Sora/Veo) — 5–10s, hook in the first second.",
            "Set up 1 CBO → 3 ad sets → 2–6 creatives, broad, Big 4 geo, $50–250/day.",
            "Confirm pixel + checkout fire on a test event. Done = campaign in review with tracking verified.",
        ]
        where = "_Where you are:_ Ads stage. Launch, then judge on $200–300 spend and cut/scale on the data."
    else:
        why = f"_Why:_ this is today's focus on the path Product → Funnel → Ads ({focus['category'] or 'general'})."
        steps = [
            "Break the task into the smallest shippable version you can finish in ~1 hr.",
            "Do that version end-to-end before polishing anything.",
            "Tie it back to the lead hook (side-sleeper / couples / wind-down).",
            "Keep all claims comfort/lifestyle — never medical.",
            "Define 'done' in one sentence and stop when you hit it.",
        ]
        where = "_Where you are:_ keep the chain moving — Product → Funnel → Ads."

    out = ["*DETAILS*", "", f"*🎯 Focus: {focus['task']}*"]
    if confirmed:
        out.append("_Confirmed: In progress._")
    out.append(why)
    out.append("")
    out.extend(f"  {i}. {s}" for i, s in enumerate(steps, 1))
    out.append("")
    out.append(where)

    others = [t for t in todos if t is not focus]
    if others:
        out.append("")
        out.append("*Also due today:*")
        for t in others:
            out.append(f"  • {_fmt_task_line(t)}")

    out.append("")
    out.append("⚡ _One focused hour beats a perfect plan. Money loves speed — go ship the first step._")
    return "\n".join(out)


def build_details_ai(focus, todos, confirmed, api_key):
    """Richer PART 2 via the Claude API. Falls back to direct on any failure."""
    model = os.environ.get("ANTHROPIC_MODEL", "claude-sonnet-4-6")
    others = [t for t in todos if t is not focus]

    task_block = {
        "focus": {k: (v.isoformat() if isinstance(v, date) else v) for k, v in focus.items()},
        "focus_confirmed_in_progress": confirmed,
        "other_todos_due_today": [
            {k: (v.isoformat() if isinstance(v, date) else v) for k, v in t.items()}
            for t in others
        ],
    }

    system = (
        "You are a sharp, encouraging operator-coach for a solo dropshipping founder. "
        "Write the DETAILS section of a daily Slack message in Slack mrkdwn "
        "(*bold*, _italic_, \\n for line breaks — NOT Markdown ** or #). "
        "Be concrete and specific to a Bluetooth sleep mask. Keep the day realistic (~1–2 hrs). "
        "Comfort/lifestyle claims ONLY — never medical claims.\n\n" + PROJECT_CONTEXT
    )
    user = (
        "Here is today's board data (the board is the accurate source of truth; do NOT "
        "infer completions):\n\n"
        f"{json.dumps(task_block, indent=2)}\n\n"
        "Write ONLY the DETAILS section (it will be appended below a glance checklist). "
        "Structure it exactly as:\n"
        "*DETAILS*\n\n"
        "*🎯 Focus: <focus task name>*  (if focus_confirmed_in_progress, add a short '_Confirmed: In progress._' line)\n"
        "<one-line _why_ tied to the MBB playbook>\n\n"
        "3–6 numbered, concrete step-by-step actions specific to the sleep mask "
        "(which tool, what to build, what 'done' looks like).\n\n"
        "<one-line '_Where you are:_' in the Product → Funnel → Ads roadmap>\n\n"
        "If there are other to-dos due today, a '*Also due today:*' list with a short note each.\n\n"
        "End with a one-line nudge. Return only the section text, no preamble."
    )

    payload = {
        "model": model,
        "max_tokens": 1200,
        "system": system,
        "messages": [{"role": "user", "content": user}],
    }
    headers = {
        "x-api-key": api_key,
        "anthropic-version": "2023-06-01",
        "Content-Type": "application/json",
    }
    try:
        resp = _http_json(
            "https://api.anthropic.com/v1/messages",
            headers=headers, data=payload, method="POST",
        )
        text = "".join(
            blk.get("text", "") for blk in resp.get("content", []) if blk.get("type") == "text"
        ).strip()
        if text:
            return text
        print("Claude returned empty content; using direct fallback.", file=sys.stderr)
    except SystemExit as e:
        print(f"Claude API failed ({e}); using direct fallback.", file=sys.stderr)
    return build_details_direct(focus, todos, confirmed)


# ----------------------------------------------------------------------------
# Slack
# ----------------------------------------------------------------------------

def post_to_slack(webhook_url, text):
    headers = {"Content-Type": "application/json"}
    body = json.dumps({"text": text}).encode()
    req = urllib.request.Request(webhook_url, data=body, headers=headers, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            status = resp.status
            reply = resp.read().decode(errors="replace")
    except urllib.error.HTTPError as e:
        raise SystemExit(f"Slack HTTP {e.code}: {e.read().decode(errors='replace')}")
    except urllib.error.URLError as e:
        raise SystemExit(f"Slack network error: {e.reason}")
    if reply.strip() != "ok":
        raise SystemExit(f"Slack did not return ok (status {status}): {reply}")
    print(f"Posted to Slack OK ({len(text)} chars).")


# ----------------------------------------------------------------------------
# Main
# ----------------------------------------------------------------------------

def main():
    notion_token = os.environ.get("NOTION_TOKEN")
    slack_url = os.environ.get("SLACK_WEBHOOK_URL")
    anthropic_key = os.environ.get("ANTHROPIC_API_KEY")
    force = os.environ.get("FORCE_SEND", "").lower() == "true"

    if not notion_token or not slack_url:
        raise SystemExit("Missing NOTION_TOKEN and/or SLACK_WEBHOOK_URL.")

    now_eastern = datetime.now(EASTERN)
    # Time gate: the workflow fires at 13:00 AND 14:00 UTC so one of them lands on
    # 9 AM Eastern year-round. Exit unless it's really the 9 o'clock hour.
    if not force and now_eastern.hour != 9:
        print(f"Not 9 AM Eastern (now {now_eastern:%H:%M %Z}); skipping. "
              f"Set FORCE_SEND=true to override.")
        return

    today = now_eastern.date()
    tasks = fetch_tasks(notion_token)
    print(f"Fetched {len(tasks)} tasks from Notion.")

    focus, todos, confirmed = choose_plan(tasks, today)
    if focus is None:
        text = (f"🌙 *Today — {today.strftime('%a, %b %-d')}*\n"
                "No active tasks on the board — everything's marked Done. "
                "Set the next focus when you get a sec. ⚡")
        post_to_slack(slack_url, text)
        return

    glance = build_glance(focus, todos, today)
    if anthropic_key:
        print("ANTHROPIC_API_KEY found — composing PART 2 with Claude.")
        details = build_details_ai(focus, todos, confirmed, anthropic_key)
    else:
        print("No ANTHROPIC_API_KEY — building PART 2 directly.")
        details = build_details_direct(focus, todos, confirmed)

    message = glance + DIVIDER + details
    post_to_slack(slack_url, message)


if __name__ == "__main__":
    main()
