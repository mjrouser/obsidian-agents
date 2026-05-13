# Codex Context Management

Use this guide to keep Codex chats focused and reduce unnecessary context use.

## Default Minimal Thread Prompt

Paste this at the start of coding-focused chats:

```text
Start with minimal context. Use repo files, shell, git, and Superpowers skills
by default. Do not use web search, browser automation, app connectors,
document/spreadsheet/presentation tools, image tools, or desktop UI tools
unless I explicitly ask or you first explain why they are necessary. Before
using any non-default tool, ask.
```

## Loaded-Context Check

Use this when starting a thread and you want to see what the environment made
available:

```text
Before doing work, list the non-default tools, plugins, skills, and connectors
you can currently see. Then list which ones you expect to use for this task.
Do not call tools just to answer this check.
```

## Coding-Only Prompt

Use this when the work should stay inside the repo:

```text
This is a coding-only thread. Use repo files, shell, git, and Superpowers
skills only. Ask before using browser automation, Microsoft connectors,
document/spreadsheet/presentation tools, image tools, desktop UI tools, or web
search.
```

## Handoff Prompt

Use this before starting a fresh thread:

```text
Give me a compact handoff summary for a fresh minimal-tools thread. Include
only the goal, current state, changed files, commands run, checks and results,
open questions, and the next concrete step.
```

## Practical Rules

- Start a new chat for each distinct task.
- Disable unused plugins or connectors before opening a new chat when the UI
  allows it.
- Ask for summarized command output unless full logs are needed.
- Keep pasted context small. Prefer paths and focused excerpts over whole
  documents.
- Use browser or app connectors only when their live data or UI behavior is
  part of the task.

## Plugin Routing

When a disabled plugin would materially improve the result, ask before using it.
Name the plugin, why it helps, whether a fresh chat is recommended, and the
lowest-friction fallback. Use the smallest plugin set needed for the task.

Treat connector writes, desktop UI control, and recurring automations as
explicit-confirmation actions even when the relevant plugin is enabled.

Common routing:

- Outlook Calendar: scheduling, availability, meeting prep, calendar briefs,
  and meeting metadata.
- Outlook Email: inbox triage, email search, reply drafting, and task
  extraction.
- Teams: chat/channel summaries, replies, and Teams follow-ups.
- SharePoint: finding, reading, or editing SharePoint/OneDrive files.
- Documents: creating or editing `.docx` files.
- Presentations: creating or editing slide decks or `.pptx` files.
- Spreadsheets: creating, editing, or analyzing `.xlsx` or CSV files.
- Browser Use: local web app testing, screenshots, and UI flow verification.
- Computer Use: controlling a desktop app UI when no file or API route is
  practical.

## Project Connector Work

For this repo, default to local tests and mocks for connector-related code. Use
live plugins only when validating real integration behavior or working with live
data:

- Outlook Calendar: meeting metadata enrichment, organizer/attendee context,
  response status, join links, and calendar-based meeting lookup.
- Teams: meeting chat enrichment, Teams chat lookup, and Teams follow-up
  extraction.
- SharePoint: SharePoint/OneDrive-hosted source files or documentation
  workflows.

If a needed plugin is disabled, pause and name the plugin, the behavior it
validates, whether to switch to a fresh plugin-enabled chat, and the mock-only
fallback.

## Action Plan

1. Keep heavy plugins disabled globally; keep Superpowers enabled for normal
   coding work.
2. Start fresh focused chats when enabling task-specific plugins.
3. Use mock-first validation for connector code.
4. Confirm connector writes, desktop UI control, recurring automations, broad
   web search, and multi-plugin workflows before acting.
5. Keep live email, calendar, chat, and file content out of commits, PRs, docs,
   and summaries unless explicitly requested.
