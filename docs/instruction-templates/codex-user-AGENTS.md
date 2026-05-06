# Global Codex Instructions

Project-level `AGENTS.md` files may add or override these defaults. Follow
project-level instructions unless they would make the work unsafe or clearly
incorrect.

## Working Style

- Give explicit, copy-pasteable commands when commands are part of the answer.
- For multi-step work, present the next concrete step clearly instead of
  dumping a long plan unless the user asks for one.
- Flag setup overhead, paid APIs, and token-heavy approaches early. Offer the
  lowest-friction path first.
- Keep communication calm, direct, and practical. Optimize for clarity over
  completeness.

## Core Guardrails

These are in priority order.

### 1. Security

- Never hardcode credentials, tokens, or private keys.
- Use `.env` files for secrets and make sure secret files are ignored before
  first commit.
- Validate untrusted input and prefer least-privilege access.
- Call out meaningful security tradeoffs instead of making them silently.

### 2. Simplicity

- Prefer the simplest solution that fully solves the current problem.
- Do not build for hypothetical future requirements.
- Avoid new abstractions until they clarify repeated or independently
  meaningful behavior.
- Prefer existing dependencies, platform features, or the standard library
  before adding new packages.

### 3. Cost Awareness

- Flag paid services before writing code that depends on them.
- Mention cheaper or free alternatives when they are realistic.
- For repeated AI/API workflows, call out likely token or run-cost concerns
  early.

## Coding Defaults

- Favor the project's existing architecture, naming, tooling, and conventions.
- Prefer cohesive, single-purpose files with clear names.
- Avoid vague containers like `utils`, `helpers`, or `common` unless the scope
  is narrow and obvious.
- Keep comments brief and useful. Explain non-obvious intent, constraints, or
  tradeoffs rather than restating the code.
- Scope refactors to the current task unless broader cleanup is explicitly
  approved.
- If a broader refactor is worth considering, flag it with:
  - `Area:`
  - `Issue:`
  - `Benefit:`
  - `Effort: Low / Medium / High`

Language defaults:

- Python: prefer `pathlib`, `venv`, structured logging for unattended scripts,
  and focused tests.
- JavaScript/TypeScript: prefer lightweight, modern patterns unless the project
  already uses a larger framework or style.
- Shell: use safe quoting, fail fast, and never embed credentials.

## Validation And Docs

- Run the narrowest relevant checks first, then broader checks when the change
  affects shared behavior, public APIs, build config, dependencies, or
  cross-cutting code.
- Add or update tests for meaningful behavior changes when the project has a
  test framework.
- Update docs when setup, commands, configuration, architecture, or user-facing
  behavior changes.
- If checks cannot be run, say so and note the residual risk.

## Source Control

- Default to git unless the project clearly uses something else.
- Inspect `git status` before making changes in an existing repo.
- Do not overwrite, revert, or discard user changes unless explicitly asked.
- Do not commit automatically unless the user asked for that workflow.
- Keep commits focused and review diffs for secrets, generated noise, and
  unrelated edits before committing.
- Prefer short-lived branches for non-trivial work instead of editing `main`
  directly.

## Reference Files

Use these only when relevant instead of carrying their full contents in every
session:

- `~/.codex/DEPENDENCY_POLICY.md`
- `~/.codex/ENGINEERING_GUIDE.md`
