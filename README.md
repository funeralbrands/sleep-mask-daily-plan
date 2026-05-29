# 🌙 Daily to-do plan → Slack (cloud, always-on)

Every morning at **9:00 AM US Eastern**, GitHub Actions (running on GitHub's
servers — your Mac/PC can be off) reads your Notion **Tasks** board and posts the
day's plan to your `#all-sleep-mask` Slack channel, which pushes to your phone.

It **reads** the board as the source of truth and **sends** the plan. A separate
9 PM job reconciles the board; this 9 AM job never tries to detect completions.

The Slack message has two parts:
- **PART 1 — TODAY AT A GLANCE**: a tight checklist (task names only) as the very
  first lines, so it fills your phone's lock-screen preview.
- **PART 2 — DETAILS**: the focus task's *why*, 3–6 concrete steps, a "where you
  are" in the roadmap, notes on other to-dos, and a nudge. Written by the Claude
  API if `ANTHROPIC_API_KEY` is set, otherwise built directly from the task data.

---

## One-time setup (≈15 min, only needs your Mac once)

### 1. Create a Notion integration & share the page

1. Go to **https://www.notion.so/my-integrations** → **New integration**.
2. Name it e.g. `Daily Plan Bot`, pick your workspace, type **Internal**. Submit.
3. On the integration page, under **Capabilities**, "Read content" is enough.
4. Copy the **Internal Integration Secret** (starts with `ntn_` or `secret_`).
   This is your `NOTION_TOKEN`. Keep it secret.
5. **Share the board with the integration** — this is the step people miss:
   - Open the **"Bluetooth Sleep Mask — Brand Build HQ"** page in Notion.
   - Click the **•••** (top-right) → **Connections** → **Connect to** →
     pick `Daily Plan Bot`.
   - Connecting the parent page also grants the **Tasks** database inside it.

> The database ID (`943797e7643f4403abd80536176f220f`) is already baked into
> `daily_plan.py`, so you don't need to paste it anywhere.

### 2. Create the GitHub repo & push this code

```bash
cd /Users/funeral/sleep-mask-daily-plan

# Create the repo on GitHub and push (GitHub CLI — easiest):
gh repo create sleep-mask-daily-plan --private --source=. --remote=origin
git add .
git commit -m "Daily Notion→Slack plan via GitHub Actions"
git push -u origin main
```

No `gh`? Create an **empty private repo** at https://github.com/new (don't add a
README), then:

```bash
git add . && git commit -m "Daily Notion→Slack plan via GitHub Actions"
git branch -M main
git remote add origin https://github.com/<you>/sleep-mask-daily-plan.git
git push -u origin main
```

### 3. Add the secrets (never in code)

In the repo on GitHub: **Settings → Secrets and variables → Actions →
New repository secret**. Add:

| Name | Value |
|------|-------|
| `NOTION_TOKEN` | the integration secret from step 1 |
| `SLACK_WEBHOOK_URL` | your Slack Incoming Webhook URL |
| `ANTHROPIC_API_KEY` | *(optional)* enables the AI-written DETAILS section |

Or with the CLI:

```bash
gh secret set NOTION_TOKEN
gh secret set SLACK_WEBHOOK_URL
gh secret set ANTHROPIC_API_KEY   # optional
```

### 4. Test it now (manual run)

GitHub → **Actions** tab → **Daily to-do plan to Slack** → **Run workflow**.
Manual runs set `FORCE_SEND=true`, so they bypass the 9 AM gate and post
immediately. Check your phone. 🎉

---

## Changing the time or pausing it

- **Change the time**: edit the two `cron:` lines in
  `.github/workflows/daily-plan.yml`. They're in **UTC**. Keep two entries one
  hour apart that straddle your target (the script's time-gate picks the right
  one across EDT/EST). For 9 AM Eastern: `0 13 * * *` and `0 14 * * *`.
  For a different hour, shift both (e.g. 7 AM Eastern → `0 11` and `0 12`).
  Also update the `now_eastern.hour != 9` check in `daily_plan.py` to match.
- **Pause**: GitHub → **Actions** → select this workflow → **•••** →
  **Disable workflow**. Re-enable the same way.
- **Stop entirely**: delete the repo, or delete the workflow file.

---

## Local testing (optional)

```bash
cp .env.example .env        # fill in NOTION_TOKEN + SLACK_WEBHOOK_URL
set -a; source .env; set +a
python3 daily_plan.py       # FORCE_SEND=true in .env bypasses the time gate
```

`.env` is gitignored — it will not be committed.

## How the 9 AM timing works

GitHub cron is UTC-only and doesn't know about US daylight saving. So the
workflow fires at **both** 13:00 and 14:00 UTC. `daily_plan.py` asks
`zoneinfo("America/New_York")` what time it actually is and exits unless the hour
is 9 — which is true at exactly one of those two UTC times depending on whether
EDT or EST is in effect. Result: exactly one message at 9 AM Eastern, all year.
