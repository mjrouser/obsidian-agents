# AGENTS.md - Obsidian Agents

## Purpose

This project builds a local filesystem agent for an Obsidian vault.

Primary outputs:
- canonical meeting notes in `01_Meetings`
- weekly action note updates in `07_Actions`
- intake notes in `00_Intake`

Supporting vault rules may exist adjacent to the vault and can be provided later:
- `intake_rules.md`
- `agents.md`
- `meeting_parse_template.md`
- `note_types.md`
- `vault_index.md`

## Core invariants

- Never fabricate attendees, decisions, action items, or facts.
- Preserve raw intake content verbatim; only prepend or append clearly-labeled processing metadata when required.
- Canonical meeting notes must follow the required sections in `note_types.md` and the structure in `meeting_parse_template.md` when those files are available.
- Meeting notes should include a link back to the originating intake/source note when available.
- After processing a meeting note, copy qualifying action items into `/07_Actions/<monday>.md`.
- For now, route only Matthew-owned actions into `07_Actions`; owner matching should normalize common variants such as `Matthew`, `Matt`, and `Matthew Rouser` where appropriate.
- Action insertion into `07_Actions` must be idempotent: rerunning the same transcript must not create duplicate actions.
- Preserve unrelated existing content in action files and meeting notes.

## Transcript-processing QA priorities

When validating transcript-processing changes, always check:

- meeting note generation
- action extraction quality
- owner normalization
- source normalization
- routing of Matthew-owned actions into `07_Actions`
- backlink from meeting note to intake/source note
- dedupe behavior on rerun
- dry-run behavior and messaging
- preservation of unrelated existing actions

## Transcript-processing QA expectations

When asked to QA transcript processing:

1. Use dry-run first.
2. Inspect relevant code paths as needed.
3. Run focused tests first, then broader tests if needed.
4. Report PASS/FAIL clearly.
5. Recommend the smallest viable fix.
6. Avoid broad rewrites unless explicitly requested.
7. Verify rerun behavior when action insertion logic changes.

## `07_Actions` file expectations

For updates to `07_Actions` weekly notes:

- Match the existing file format already in the vault.
- Insert only qualifying actions for Matthew.
- Include a backlink to the source meeting note.
- Include normalized source metadata consistently.
- Avoid duplicate insertions across reruns.
- Preserve unrelated actions already present in the file.
- Avoid reordering existing actions unless explicitly requested.

## Priority commands for validation

Use these commands first when validating changes:

- `make check`
- `make test`
- `make build`
- `make audit`

If one of these commands is unavailable in the repo, fall back to the nearest project-specific equivalent and say so explicitly.

## Development rules

- Prefer small, focused changes.
- Keep changes backward-compatible unless the task explicitly allows breaking changes.
- Add or update tests for behavior changes.
- Update docs (`README.md`) when commands or setup change.
- Prefer small, reviewable commits.
- Keep CLI behavior explicit and discoverable.
- Preserve dry-run safety for file-writing operations.
- Use Ruff through `make lint`, `make format-check`, or `make check` for Python
  linting and formatting validation. Use `make format` when a formatting pass is
  intentionally in scope.
- Do not auto-commit project repo changes from the app. Project code changes
  must be reviewed, tested, committed, and merged manually.
- When Codex changes this repository, finish by prompting Matthew with the
  exact commit and merge commands after checks pass.
- Keep `config.yaml` local and untracked. Update `config.example.yaml` when
  reusable configuration keys or defaults change.
- Keep dependency changes reflected in both `pyproject.toml` and
  `requirements.lock`.
- Run `make audit` after dependency changes.

## Safety rules

- Never commit credentials, tokens, private keys, or production data.
- Use `.env.example` for required environment variables.
- Do not commit machine-specific vault paths, local Codex executable paths, or
  live automation settings from `config.yaml`.
- Call out migrations or destructive operations clearly before running them.
- Prefer non-destructive validation before modifying vault files.

## Code layout notes

Document project-specific structure here once established, for example:

- `src/`: application code
- `tests/`: automated tests
- `scripts/`: project automation scripts

## Definition of done

A task is done when:

1. `make check` passes, or an explicitly documented equivalent passes.
2. `make test` passes, or an explicitly documented equivalent passes.
3. `make build` passes, or an explicitly documented equivalent passes.
4. `make audit` passes when dependencies or packaging change.
5. Relevant docs are updated.
6. Behavior changes include tests where practical.
7. Transcript-processing changes are verified with dry-run output when applicable.
