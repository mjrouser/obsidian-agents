# Codex Workflow for Obsidian Agents

This document describes the recommended Codex workflow for QA, fixes, and rerun validation for transcript processing.

## Where to run Codex

Always start from the repo root so Codex can see:

- the source code
- the tests
- `AGENTS.md`
- any project docs used for workflow guidance

Example:

```bash
cd /Users/matthew.rouser/repos/obsidian-agents
codex
```

## Do I need a new Codex project?

No. Use the existing repo folder as the working directory.

## Where should `AGENTS.md` live?

Put the main project guidance in the repo root:

```text
/Users/matthew.rouser/repos/obsidian-agents/AGENTS.md
```

Codex should be run from the repo root or a subdirectory beneath it so those instructions are discovered.

## Recommended workflow per transcript

### 1. Start Codex in the repo root

```bash
cd /Users/matthew.rouser/repos/obsidian-agents
codex
```

### 2. Run QA first, using dry-run

Paste this prompt into Codex:

```text
You are helping me QA my Obsidian transcript intake workflow.

Context:
- A transcript is processed into:
  1. canonical meeting note in 01_Meetings
  2. weekly action file updates in 07_Actions
  3. intake note in 00_Intake
- I specifically care about:
  - action extraction
  - Matthew-owned action routing
  - dedupe
  - owner normalization
  - source normalization
  - backlink from meeting note to intake/source
  - safe rerun behavior
- Use dry-run first.
- Be strict and concrete.

Please do the following:
1. Run the processor in dry-run mode for this transcript:
   [PASTE TRANSCRIPT PATH HERE]

2. Validate:
   - generated meeting note structure and quality
   - whether the meeting note includes the needed backlink
   - whether Matthew-owned actions are correctly identified
   - whether those actions would be added to the correct weekly action file
   - whether formatting in 07_Actions matches the existing style
   - whether owner normalization and source normalization are working
   - whether dedupe/rerun behavior is safe

3. Inspect relevant code paths if needed.

4. Return a QA report only, with:
   - overall PASS/FAIL
   - checklist
   - exact issues found
   - likely root cause in code
   - recommended minimal code changes
   - recommended tests to add

Do not make code changes yet.
```

### 3. If QA fails, ask Codex to implement the fix

Paste this prompt into the same session:

```text
Implement the recommended fixes from the QA report.

Constraints:
- Make the smallest possible change set.
- Preserve existing correct behavior.
- Add or update focused tests.
- Run tests.
- Then rerun the transcript processor in dry-run mode and confirm expected behavior.

The fixes should address:
- Matthew-owned action routing into 07_Actions
- dedupe
- owner normalization
- source normalization
- backlink from meeting note to intake/source note

Return:
- files changed
- summary of each change
- tests run and results
- dry-run output summary
- whether the workflow now passes QA
```

### 4. Verify rerun / idempotency behavior

Paste this prompt:

```text
You are testing idempotency for my Obsidian transcript workflow.

Goal:
Verify that processing the same transcript repeatedly does not produce duplicate actions, duplicate meeting-note content, or misleading dry-run output.

Steps:
1. Run the processor in dry-run mode for the transcript.
2. Inspect expected outputs.
3. Simulate or evaluate a second run against already-existing outputs.
4. Determine whether any duplicate actions, duplicate inserts, or unnecessary rewrites would occur.

Focus on:
- 07_Actions dedupe
- owner normalization interactions with dedupe
- source normalization interactions with dedupe
- meeting-note rewrite/skip behavior
- dry-run accuracy

Transcript:
[PASTE PATH]

Command:
obsidian-agent --config config.yaml process --dry-run [PASTE PATH]

Return:
- PASS/FAIL
- first-run expected changes
- second-run expected changes
- duplicate risks
- recommended fix if second-run behavior is wrong
```

## Fastest practical loop

For most transcripts, use this sequence:

1. Start Codex from the repo root.
2. Run the QA prompt.
3. If needed, run the fix prompt.
4. Run the rerun/idempotency prompt.
5. After Codex reports green, run the real command yourself.

## When to ask Codex for broader help

Use Codex for:

- dry-run QA
- bug fixing
- test creation
- rerun validation
- formatting checks
- small documentation updates

Avoid using it first for:

- broad refactors not tied to a concrete transcript-processing problem
- architectural changes unless a narrow bug fix is insufficient

## Suggested repo docs structure

```text
obsidian-agents/
├── AGENTS.md
├── docs/
│   └── codex-workflow.md
├── src/
├── tests/
└── ...
```

## Practical notes

- Stay in the repo root unless you have a specific reason to work deeper in the tree.
- Keep one Codex session open while iterating on a single issue.
- Prefer dry-run validation before any real write.
- Ask for minimal changes and focused tests.
- If a behavior change affects output files, ask Codex to verify idempotency explicitly.
