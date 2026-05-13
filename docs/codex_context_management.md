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
