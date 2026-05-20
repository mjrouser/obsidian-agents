# Obsidian Agent Automation Runbook

## Overview
This system automatically:
- Processes new notes added to `00_Intake`
- Updates weekly actions
- Generates weekly review notes (Monday briefing + Friday wrap)

It runs continuously using `launchd` on macOS.

---

## Normal Behavior
- The intake watcher runs continuously in the background
- Weekly jobs run on schedule (Monday morning, Friday midday)
- As long as your computer is on and you are logged in, everything runs automatically

---

## Start the System

Usually happens automatically at login.

To start manually:

```bash
launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/com.obsidian.agent.intake-watcher.plist
launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/com.obsidian.agent.meeting-sync.plist
launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/com.obsidian.agent.weekly-briefing.plist
launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/com.obsidian.agent.weekly-wrap.plist
```

Verify:

```bash
launchctl list | grep obsidian.agent
```

---

## Stop the System

To stop everything:

```bash
launchctl bootout gui/$(id -u) ~/Library/LaunchAgents/com.obsidian.agent.intake-watcher.plist
launchctl bootout gui/$(id -u) ~/Library/LaunchAgents/com.obsidian.agent.meeting-sync.plist
launchctl bootout gui/$(id -u) ~/Library/LaunchAgents/com.obsidian.agent.weekly-briefing.plist
launchctl bootout gui/$(id -u) ~/Library/LaunchAgents/com.obsidian.agent.weekly-wrap.plist
```

---

## Restart After Changes

Use this whenever you update code, config, or prompts.

```bash
launchctl bootout gui/$(id -u) ~/Library/LaunchAgents/com.obsidian.agent.intake-watcher.plist 2>/dev/null || true
launchctl bootout gui/$(id -u) ~/Library/LaunchAgents/com.obsidian.agent.meeting-sync.plist 2>/dev/null || true
launchctl bootout gui/$(id -u) ~/Library/LaunchAgents/com.obsidian.agent.weekly-briefing.plist 2>/dev/null || true
launchctl bootout gui/$(id -u) ~/Library/LaunchAgents/com.obsidian.agent.weekly-wrap.plist 2>/dev/null || true

launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/com.obsidian.agent.intake-watcher.plist
launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/com.obsidian.agent.meeting-sync.plist
launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/com.obsidian.agent.weekly-briefing.plist
launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/com.obsidian.agent.weekly-wrap.plist
```

---

## When You Need to Intervene

You only need to step in when:

- You changed `config.yaml`
- You updated code or scripts
- You pulled new changes from the repo
- The system did not restart after reboot
- A note did not process
- Weekly review did not generate
- You see errors in logs

---

## Troubleshooting

### Check Obsidian Failure Notes

Automation failures write a short note under `_System/Agent Errors` in the
configured vault. Start with `ACTION NEEDED - Latest Automation Failure.md`; it
points to the newest timestamped failure note. Use that note as the quick
signal, then inspect logs for full details. On macOS, failed launchd wrapper
runs also send a best-effort desktop notification unless
`AUTOMATION_FAILURE_NOTIFICATIONS=0` is set.

### Check Watcher Logs

```bash
tail -n 50 logs/intake-watcher.stderr.log
tail -n 50 logs/intake-watcher.log
```

### Check Weekly Jobs

```bash
tail -n 50 logs/weekly-briefing.stderr.log
tail -n 50 logs/weekly-wrap.stderr.log
```

### Check Meeting Sync

```bash
tail -n 50 logs/meeting-sync.stdout.log
tail -n 50 logs/meeting-sync.stderr.log
```

Meeting sync runs Monday-Friday at `:05` and `:35` from `08:35` through `18:05`.
It uses a rolling seven-day window. If Graph auth expires, run:

```bash
./.venv/bin/obsidian-agent graph status
./.venv/bin/obsidian-agent graph login
```

If `config.yaml` is missing or broken, check `logs/automation-failures/` too.
The wrapper now writes a fallback failure note there when it cannot write the
normal vault-based note.

---

## Quick Health Check

After any update, do these two checks:

### 1. Confirm processes are running

```bash
launchctl list | grep obsidian.agent
```

### 2. Drop a test note into Intake

Verify:
- Canonical note is created
- Weekly actions updated
- File moves to `z_Archive/Intake`
- No errors appear in logs

---

## Manual Weekly Run (Optional)

If you want to generate reviews manually:

```bash
./.venv/bin/python scripts/generate_weekly_summary.py briefing
./.venv/bin/python scripts/generate_weekly_summary.py wrap
```

---

## Notes

- The system uses the local virtual environment (`.venv`)
- Do not rely on system Python
- All automation is routed through the repo's `.venv/bin/python`

---

## Mental Model

- Intake = inbox
- Processor = organizer
- Archive = storage
- Weekly reviews = reflection layer

If Intake is empty and logs are quiet, the system is working.
