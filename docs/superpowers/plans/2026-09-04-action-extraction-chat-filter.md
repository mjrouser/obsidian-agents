# Action Extraction Chat Filtering Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Prevent speaker-labeled meeting-chat messages from entering weekly action notes while preserving explicit Markdown assignment formats and existing fallback note content.

**Architecture:** Make ambiguous `Name: text` and `Name - text` parsing context-sensitive inside the existing Markdown extractor. Explicit `Action:` lines, unchecked tasks, `Name to ...`, `Name will ...`, assignment labels, and parenthetical owners remain eligible anywhere; bare owner labels are eligible only inside `Action Items` or `Next Steps`. Keep fallback rendering and action rendering unchanged, and prove the behavior at parser, processor, and bundle-execution boundaries.

**Tech Stack:** Python 3.11+, standard-library `re`, `pathlib`, and `unittest`; existing `MeetingProcessor` and bundle-processing test fixtures; Ruff, mypy, pip-audit, and Make targets.

**Approved design:** `docs/superpowers/specs/2026-09-04-action-extraction-chat-filter-design.md`

---

## Execution Preconditions

The current checkout contains unrelated in-progress edits in `README.md`,
`tests/test_processor.py`, and `tests/test_meeting_process_bundles.py`. Do not
implement this plan in that dirty checkout and do not discard or overwrite those
changes.

At execution time:

1. Invoke `superpowers:using-git-worktrees`.
2. Inspect the current branch and `origin/main` before selecting the base.
3. Create an isolated worktree on branch `codex/fix-action-chat-filter` from the
   current reviewed integration base. If `origin/main` is still the integration
   base, use:

```bash
git fetch origin
git worktree add ../obsidian-agents-action-chat-filter -b codex/fix-action-chat-filter origin/main
```

4. Reopen this plan and the approved design by absolute path from the original
   checkout if they have not yet been committed.
5. Inspect `git status --short --branch` in the isolated worktree before editing.

Project policy prohibits the agent from committing automatically. Commit
commands below are handoff commands for Matthew after review, not commands for
the implementing agent to execute.

## File Map

- Modify `src/obsidian_intake_agent/processors/md_reader.py`
  - Own context-sensitive Markdown action parsing.
- Modify `tests/test_processor.py`
  - Cover syntax policy, section transitions, owner routing, chat exclusion,
    canonical-note preservation, and rerun idempotency.
- Modify `tests/test_meeting_process_bundles.py`
  - Cover the real processor-ready fallback bundle boundary.
- Modify `README.md`
  - Document accepted Markdown action syntax and the owner-label boundary.
- Do not modify `src/obsidian_intake_agent/meetings/sync.py`
  - Meeting chat remains useful canonical-note context.
- Do not modify `src/obsidian_intake_agent/rendering/action_renderer.py`
  - It receives already-classified actions and is not the source of the defect.

### Task 1: Lock the Approved Behavior With Failing Tests

**Files:**
- Modify: `tests/test_processor.py:1181-1264`
- Modify: `tests/test_processor.py:1347-1446`
- Modify: `tests/test_meeting_process_bundles.py:882-914`

- [ ] **Step 1: Update the ambiguous standalone-format expectation**

Replace `test_extracts_standalone_explicit_assignment_formats` with this test.
It preserves the unambiguous standalone formats while documenting that a bare
`Matthew: ...` label is not sufficient outside an action section:

```python
def test_extracts_only_unambiguous_standalone_assignment_formats(self) -> None:
    items = extract_markdown_action_items(
        "Matthew to draft the SOW\nMatthew: draft the SOW\nAssigned to Matthew: draft the SOW\n"
    )

    self.assertEqual(
        [(item.owner, item.text) for item in items],
        [
            ("Matthew", "draft the SOW"),
            ("Matthew", "draft the SOW"),
        ],
    )
```

- [ ] **Step 2: Add a focused parser test for section-aware owner labels**

Add this test next to the other `extract_markdown_action_items` tests:

```python
def test_owner_label_requires_explicit_action_context(self) -> None:
    items = extract_markdown_action_items(
        "## Summary\n"
        "- Matthew Rouser: These are both great updates!\n"
        "Matthew Rouser - Internet is out at my place\n"
        "## Action Items\n"
        "- Matthew: draft the SOW\n"
        "## Meeting Chat\n"
        "- Matthew Rouser: Did we cancel today?\n"
        "- Matthew Rouser: Thanks!\n"
        "Action: Matthew: send the final note\n"
        "- Matt to review the staffing model\n"
    )

    self.assertEqual(
        [(item.owner, item.text) for item in items],
        [
            ("Matthew", "draft the SOW"),
            ("Matthew", "send the final note"),
            ("Matt", "review the staffing model"),
        ],
    )
```

This one fixture verifies summary rejection, standalone owner-label rejection,
action-section acceptance, heading reset, explicit `Action:` acceptance, and
explicit `to` syntax outside an action section.

- [ ] **Step 3: Add a processor-level regression using the reported chat shape**

Add this test near `test_markdown_action_section_routes_matthew_owned_action`:

```python
def test_markdown_meeting_chat_is_preserved_but_not_routed_as_actions(self) -> None:
    with tempfile.TemporaryDirectory() as tmp_dir:
        vault = Path(tmp_dir) / "vault"
        intake_dir = vault / "00_Intake"
        intake_dir.mkdir(parents=True)
        source = intake_dir / "2026-09-01 - Teams - M&A GTM Meeting - Weekly.md"
        source.write_text(
            "## Action Items\n"
            "- Matthew: Send the client follow-up.\n"
            "\n"
            "## Meeting Chat\n"
            "- Matthew Rouser: Those are both great updates!\n"
            "- Matthew Rouser: Did we cancel today?\n"
            "- Matthew Rouser: Ah, missed it\n"
            "- Matthew Rouser: Internet is out at my place and it’s been a bit of a juggling act\n"
            "- Matthew Rouser: I see it now\n"
            "- Matthew Rouser: Thanks!\n"
            "- Matthew Rouser: I'll be joining 5 minutes late today\n",
            encoding="utf-8",
        )
        processor = MeetingProcessor(_config(vault, dry_run=False))

        result = processor.process_file(source)

        self.assertTrue(result.processed)
        canonical_path = meeting_output_path(
            vault / "01_Meetings",
            meeting_date="2026-09-01",
            basename=source.name,
        )
        canonical_text = canonical_path.read_text(encoding="utf-8")
        self.assertIn("## Meeting Chat", canonical_text)
        self.assertIn("- Matthew Rouser: Thanks!", canonical_text)

        actions_path = vault / "07_Actions" / "2026-08-31.md"
        actions_text = actions_path.read_text(encoding="utf-8")
        self.assertIn("Send the client follow-up.", actions_text)
        for chat_text in (
            "Those are both great updates!",
            "Did we cancel today?",
            "Ah, missed it",
            "Internet is out at my place",
            "I see it now",
            "Thanks!",
            "I'll be joining 5 minutes late today",
        ):
            self.assertNotIn(chat_text, actions_text)

        archived_source = vault / "_Archive" / "Intake" / source.name
        before_rerun = actions_text
        processor.process_file(archived_source, force=True)
        self.assertEqual(actions_path.read_text(encoding="utf-8"), before_rerun)
```

- [ ] **Step 4: Add the fallback bundle boundary regression**

Add this test after `test_execute_processes_ready_bundle_and_renders_outputs` in
`tests/test_meeting_process_bundles.py`:

```python
def test_execute_fallback_routes_action_items_but_not_meeting_chat(self) -> None:
    with tempfile.TemporaryDirectory() as tmp_dir:
        vault, bundle_root, input_path, metadata_path, _marker_path = _ready_marker_bundle(
            tmp_dir=tmp_dir,
            preferred_source="Copilot recap / AI summary",
            extension=".md",
        )
        input_path.write_text(
            "# 2026-05-04 - Teams - Delivery Review\n"
            "\n"
            "## Action Items\n"
            "- Matthew: Send the delivery update.\n"
            "- Priya: Confirm the release window.\n"
            "\n"
            "## Meeting Chat\n"
            "- Matthew Rouser: Thanks!\n"
            "- Matthew Rouser: I see it now\n",
            encoding="utf-8",
        )
        metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
        metadata["fallback_not_before"] = metadata["scheduled_end_at"]
        metadata["retry_until"] = "2026-05-05T13:30:00+00:00"
        metadata_path.write_text(
            json.dumps(metadata, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        processor = _processor_for_vault(vault)
        plan = build_bundle_processing_plan(
            intake_root=bundle_root,
            processor=processor,
            now=datetime.fromisoformat("2026-05-04T14:00:00+00:00"),
        )

        result = execute_bundle_processing_plan(plan, processor=processor)

        self.assertEqual(result.processed_count, 1)
        canonical_path = result.items[0].canonical_note_path
        assert canonical_path is not None
        canonical_text = canonical_path.read_text(encoding="utf-8")
        self.assertIn("## Meeting Chat", canonical_text)
        self.assertIn("- Matthew Rouser: Thanks!", canonical_text)

        actions_path = vault / "07_Actions" / "2026-05-04.md"
        actions_text = actions_path.read_text(encoding="utf-8")
        self.assertEqual(actions_text.count("Send the delivery update."), 1)
        self.assertEqual(actions_text.count("(Owner: Matthew Rouser)"), 1)
        self.assertNotIn("Thanks!", actions_text)
        self.assertNotIn("I see it now", actions_text)
```

- [ ] **Step 5: Run the new tests and verify the current parser fails them**

Run:

```bash
PYTHONPATH=src ./.venv/bin/python -m unittest \
  tests.test_processor.MeetingProcessorTests.test_extracts_only_unambiguous_standalone_assignment_formats \
  tests.test_processor.MeetingProcessorTests.test_owner_label_requires_explicit_action_context \
  tests.test_processor.MeetingProcessorTests.test_markdown_meeting_chat_is_preserved_but_not_routed_as_actions \
  tests.test_meeting_process_bundles.BundleProcessingPlanTests.test_execute_fallback_routes_action_items_but_not_meeting_chat \
  -v
```

Expected: all four tests fail because `OWNER_LABEL_PATTERN` is currently enabled
for every ordinary bullet and standalone line. The failures should show the
speaker messages in extracted items or `07_Actions`, not setup or bundle errors.

### Task 2: Make Owner-Label Parsing Context-Sensitive

**Files:**
- Modify: `src/obsidian_intake_agent/processors/md_reader.py:31-124`
- Test: `tests/test_processor.py`
- Test: `tests/test_meeting_process_bundles.py`

- [ ] **Step 1: Remove the redundant ordinary-bullet regex**

Delete this constant because the revised loop handles ordinary bullets in one
place and no longer needs to revisit them later:

```python
BULLET_PATTERN = re.compile(r"^-\s+(?!\[[ xX]\]\s*)(?P<text>.+)$")
```

- [ ] **Step 2: Replace `extract_markdown_action_items` with section-aware control flow**

Use this complete implementation:

```python
def extract_markdown_action_items(text: str) -> list[ActionItem]:
    items: list[ActionItem] = []
    in_action_section = False
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line:
            continue
        if line.startswith("#"):
            in_action_section = ACTION_SECTION_PATTERN.match(line) is not None
            continue
        if line.lower().startswith("action:"):
            action_text = line[len("action:") :].strip()
            items.append(parse_action_text(action_text, allow_owner_label=True))
            continue
        if line.startswith("- [ ]"):
            action_text = line[len("- [ ]") :].strip()
            items.append(parse_action_text(action_text, allow_owner_label=True))
            continue
        if line.startswith("- "):
            action_text = line[len("- ") :].strip()
            parsed = parse_action_text(action_text, allow_owner_label=in_action_section)
        else:
            parsed = parse_action_text(line, allow_owner_label=in_action_section)
        if parsed.owner is not None:
            items.append(parsed)
    return items
```

This keeps explicit syntax active in every section and changes only the
ambiguous owner-label rule.

- [ ] **Step 3: Add the owner-label policy parameter to `parse_action_text`**

Replace the existing function with this implementation. Keep
`allow_owner_label=True` as the default so direct callers retain the existing
general parsing API; `extract_markdown_action_items` supplies the stricter
context explicitly.

```python
def parse_action_text(action_text: str, *, allow_owner_label: bool = True) -> ActionItem:
    patterns = (
        OWNER_WILL_PATTERN,
        COORDINATED_OWNER_TO_PATTERN,
        OWNER_TO_PATTERN,
        TRAILING_OWNER_WILL_PATTERN,
        ASSIGNED_TO_PATTERN,
        OWNER_ASSIGNMENT_PATTERN,
    )
    if allow_owner_label:
        patterns = (*patterns, OWNER_LABEL_PATTERN)

    for pattern in patterns:
        match = pattern.match(action_text)
        if match and _is_owner_like(match.group("owner")):
            return ActionItem(
                owner=match.group("owner").strip(),
                text=match.group("text").strip(),
            )

    owner_match = TASK_OWNER_PATTERN.match(action_text)
    if owner_match and _is_owner_like(owner_match.group("owner")):
        return ActionItem(
            owner=owner_match.group("owner").strip(),
            text=owner_match.group("text").strip(),
        )
    return ActionItem(owner=None, text=action_text.strip())
```

- [ ] **Step 4: Run the four focused regressions and verify they pass**

Run:

```bash
PYTHONPATH=src ./.venv/bin/python -m unittest \
  tests.test_processor.MeetingProcessorTests.test_extracts_only_unambiguous_standalone_assignment_formats \
  tests.test_processor.MeetingProcessorTests.test_owner_label_requires_explicit_action_context \
  tests.test_processor.MeetingProcessorTests.test_markdown_meeting_chat_is_preserved_but_not_routed_as_actions \
  tests.test_meeting_process_bundles.BundleProcessingPlanTests.test_execute_fallback_routes_action_items_but_not_meeting_chat \
  -v
```

Expected: `Ran 4 tests` and `OK`.

- [ ] **Step 5: Run the complete processor and bundle test modules**

Run:

```bash
PYTHONPATH=src ./.venv/bin/python -m unittest \
  tests.test_processor \
  tests.test_meeting_process_bundles \
  -v
```

Expected: both modules pass. Pay particular attention to existing coverage for
owner aliases, `Matt to ...` outside action sections, parenthetical owners,
rerun dedupe, fallback upgrades, and rollback.

- [ ] **Step 6: Review the focused diff without committing**

Run:

```bash
git diff -- src/obsidian_intake_agent/processors/md_reader.py tests/test_processor.py tests/test_meeting_process_bundles.py
git diff --check -- src/obsidian_intake_agent/processors/md_reader.py tests/test_processor.py tests/test_meeting_process_bundles.py
```

Expected: only the approved parser behavior and regression coverage appear;
`git diff --check` prints nothing and exits zero.

### Task 3: Document the Markdown Action Boundary

**Files:**
- Modify: `README.md:408`
- Test: `README.md`

- [ ] **Step 1: Replace the current Markdown extraction description**

Replace:

```markdown
- Markdown intake files extract action items from `Action:` lines and `- [ ]` checkboxes.
```

with:

```markdown
- Markdown intake files extract actions from explicit `Action:` lines, unchecked
  checkboxes, `Name to ...` / `Name will ...` assignments, `Assigned to` /
  `Owner:` labels, and parenthetical owners. Ambiguous `Name: text` or
  `Name - text` labels require an `Action Items` / `Next Steps` section or an
  explicit `Action:` prefix, so speaker-labeled meeting chat is not routed to
  weekly actions.
```

- [ ] **Step 2: Check documentation formatting and scope**

Run:

```bash
git diff --check -- README.md
git diff -- README.md
```

Expected: the README describes the implemented behavior without adding config,
commands, or claims about LLM classification.

### Task 4: Complete Repository And Dry-Run Verification

**Files:**
- Verify: `src/obsidian_intake_agent/processors/md_reader.py`
- Verify: `tests/test_processor.py`
- Verify: `tests/test_meeting_process_bundles.py`
- Verify: `README.md`

- [ ] **Step 1: Run the repository's required static checks**

Run:

```bash
make check
```

Expected: Ruff lint, mypy, Ruff format-check, and `scripts/check.sh` all pass.
If Ruff requests formatting for the touched Python files, run:

```bash
./.venv/bin/python -m ruff format src/obsidian_intake_agent/processors/md_reader.py tests/test_processor.py tests/test_meeting_process_bundles.py
make check
```

- [ ] **Step 2: Run the full automated test suite**

Run:

```bash
make test
```

Expected: all unit and integration tests pass.

- [ ] **Step 3: Run CLI smoke and bytecode build checks**

Run:

```bash
make smoke
make build
```

Expected: CLI smoke checks pass and Python compilation completes without
errors.

- [ ] **Step 4: Run the required dependency audit**

Run:

```bash
make audit
```

Expected: pip-audit reports no known vulnerabilities. No dependency files
should change. If the sandbox blocks network or cache access, request approval
to rerun this exact Make target with normal network access rather than changing
dependencies.

- [ ] **Step 5: Verify the fix against a real reported fallback using dry-run**

Record hashes of the exact source, canonical note, and weekly action note:

```bash
shasum -a 256 \
  "/Users/matthew.rouser/Library/CloudStorage/OneDrive-Slalom/Documents/Work Second Brain (Obsidian)/z_Archive/Intake/bundles/fallbacks/2026-08-31 - Teams - LP - Brandi - Matthew - Caleb - Sync (fallback).md" \
  "/Users/matthew.rouser/Library/CloudStorage/OneDrive-Slalom/Documents/Work Second Brain (Obsidian)/01_Meetings/2026/08_August/2026-08-31 - Teams - LP - Brandi - Matthew - Caleb - Sync.md" \
  "/Users/matthew.rouser/Library/CloudStorage/OneDrive-Slalom/Documents/Work Second Brain (Obsidian)/07_Actions/2026-08-31.md"
```

Then run:

```bash
./.venv/bin/obsidian-agent process --force --dry-run \
  "/Users/matthew.rouser/Library/CloudStorage/OneDrive-Slalom/Documents/Work Second Brain (Obsidian)/z_Archive/Intake/bundles/fallbacks/2026-08-31 - Teams - LP - Brandi - Matthew - Caleb - Sync (fallback).md"
```

Run the same `shasum -a 256` command again.

Expected:

- dry-run reports the planned canonical processing without writing files;
- it does not propose the six reported chat messages as new actions;
- all three hashes are identical before and after the dry run.

- [ ] **Step 6: Review the final repository diff and status**

Run:

```bash
git diff --check
git status --short --branch
git diff --stat
git diff -- src/obsidian_intake_agent/processors/md_reader.py tests/test_processor.py tests/test_meeting_process_bundles.py README.md
```

Expected: no whitespace errors, no generated noise, no dependency changes, and
no files outside the approved file map.

- [ ] **Step 7: Hand Matthew the exact manual commit commands**

After Matthew reviews the final diff, provide these commands for him to run in
the isolated worktree:

```bash
git add \
  src/obsidian_intake_agent/processors/md_reader.py \
  tests/test_processor.py \
  tests/test_meeting_process_bundles.py \
  README.md \
  docs/superpowers/specs/2026-09-04-action-extraction-chat-filter-design.md \
  docs/superpowers/plans/2026-09-04-action-extraction-chat-filter.md
git commit -m "fix: exclude meeting chat from action extraction"
```

If the approved spec and plan remain only in the original dirty checkout, do
not stage nonexistent paths. Recreate their reviewed contents in the isolated
worktree using `apply_patch`, rerun `git diff --check`, and then use the complete
command above.

- [ ] **Step 8: Hand Matthew the protected-branch publication commands**

After the manual commit:

```bash
git push -u origin codex/fix-action-chat-filter
gh pr create \
  --base main \
  --head codex/fix-action-chat-filter \
  --title "Exclude meeting chat from action extraction" \
  --body "Prevents ambiguous speaker-labeled Teams chat messages from being routed into weekly actions while preserving explicit Markdown assignments. Adds parser, processor, and fallback bundle regressions."
gh pr checks --watch
```

After required checks pass and Matthew approves the PR:

```bash
gh pr merge --squash --delete-branch
```

### Task 5: Produce The Historical Cleanup Review Report

**Files:**
- Read: configured vault `01_Meetings`
- Read: configured vault `z_Archive/Intake/bundles/fallbacks`
- Read: configured vault `07_Actions`
- Modify: none

This task is read-only. Do it after the parser fix is verified. Do not delete or
edit tracker lines during this task.

- [ ] **Step 1: Run the exact-match candidate report**

Run:

```bash
./.venv/bin/python - <<'PY'
from pathlib import Path
import re

vault = Path(
    "/Users/matthew.rouser/Library/CloudStorage/OneDrive-Slalom/Documents/Work Second Brain (Obsidian)"
)
chat_messages: set[str] = set()
for root in (
    vault / "01_Meetings",
    vault / "z_Archive" / "Intake" / "bundles" / "fallbacks",
    vault / "00_Intake" / "bundles" / "fallbacks",
):
    if not root.exists():
        continue
    for path in root.rglob("*.md"):
        for line in path.read_text(encoding="utf-8").splitlines():
            match = re.match(r"^- Matthew(?: Rouser)?:\s*(.+?)\s*$", line)
            if match:
                chat_messages.add(match.group(1))

action_pattern = re.compile(
    r"^- \[(?P<status>[^]])\] (?P<text>.+?) "
    r"\(Owner: Matthew Rouser\) — Source: (?P<source>.+)$"
)
candidate_count = 0
for path in sorted((vault / "07_Actions").glob("2026-*.md")):
    for line_number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        match = action_pattern.match(line)
        if match is None or match.group("text") not in chat_messages:
            continue
        candidate_count += 1
        print(
            f"{path}:{line_number}\n"
            f"  status={match.group('status')}\n"
            f"  text={match.group('text')}\n"
            f"  source={match.group('source')}"
        )
print(f"candidate_count={candidate_count}")
PY
```

Expected from the September 4 investigation snapshot: 24 exact matches across
the retained weekly notes beginning August 10. The count may increase if new
fallbacks are processed before the fix is deployed.

- [ ] **Step 2: Present the candidate report and stop for review**

Group candidates by weekly action note and clearly distinguish open from
completed items. Ask Matthew to identify any chat messages that are genuine
commitments and must remain.

Do not perform cleanup based solely on exact matching. A real commitment can be
made in chat even though the current examples are false positives.

- [ ] **Step 3: Create a separate cleanup patch only after explicit approval**

Once Matthew approves exact candidate lines, create a separate, narrowly scoped
vault patch that removes only those lines. Preserve headings, task ordering,
checkbox state of retained actions, source backlinks, and unrelated content.
Re-run the report and `git diff --check` in the vault, then show the vault diff
before any vault commit.

Historical cleanup is intentionally not part of the code commit or PR. It is an
operational follow-up with its own approval and review boundary.
