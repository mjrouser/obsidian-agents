# Codex Workflow for Obsidian Agents

(Full version — includes QA, fixes, regression handling, and idempotency validation)

## Where to run Codex

Always start from the repo root so Codex can see:
- source code
- tests
- AGENTS.md
- project docs

Example:
```
cd '/Users/matthew.rouser/repos/obsidian-agents
codex'
```
## QA Workflow

### Step 1 — Run QA (dry-run)

Key requirement:
Always inspect the exact extracted action list.

Core checks:
- meeting note structure
- backlink present
- extracted actions (explicit list)
- Matthew-owned routing
- normalization (owner + source)
- dedupe + rerun safety

Prompt:
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

### Step 2 — If FAIL, implement fix

Rules:
- smallest possible change
- prefer parser fixes over redesign
- do not touch unrelated systems
- add focused tests
- rerun full suite

Prompt:
```
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

### Step 3 — Idempotency

Validate:
- no duplicate actions
- no unnecessary rewrites
- correct dry-run messaging

Prompt:
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

### Step 4 — Add regression test

When a real bug is fixed:
- add minimal test covering exact pattern
- prefer end-to-end if behavior spans multiple layers

## Known Regression Shape

- coordinated owners (X and Y to ...)
- parenthetical owner (Task (Matthew))
- only Matthew routes to 07_Actions
- rerun produces no duplicates

# Fastest practical loop

For most transcripts, use this sequence:

1. Start Codex from the repo root.
2. Run the QA prompt.
3. If needed, run the fix prompt.
4. Run the rerun/idempotency prompt.
5. After Codex reports green, run the real command yourself.

## Practical Rules

- Always inspect extracted actions directly
- Prefer under-matching to over-matching
- Stop parser expansion once explicit formats are covered
- Move to implied-action logic only when needed
