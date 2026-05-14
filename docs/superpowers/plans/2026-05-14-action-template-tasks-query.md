# Action Template Tasks Query Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add the approved Tasks query block to weekly action notes.

**Architecture:** Keep weekly action-note structure centralized in the existing action renderer. Add a small renderer-level constant/helper for the query block, include it in newly built notes, and ensure existing notes receive it only when they are already being updated with new actions.

**Tech Stack:** Python 3.11, unittest, existing renderer tests.

---

### Task 1: Renderer Tests

**Files:**
- Modify: `tests/test_action_renderer.py`

- [ ] **Step 1: Add failing tests**

Add tests that assert:

```python
rendered = render_actions_note(...)
self.assertIn(
    "# Actions - Week marker followed by Tasks query block before ## This Week",
    rendered,
)
```

The exact expected block is:

````markdown
```tasks

path includes {{query.file.path}}

not done

sort by due

```
````

Also add a test where an existing note without the block receives a new action and the resulting text contains exactly one ` ```tasks ` fence before `## This Week`.

- [ ] **Step 2: Verify tests fail**

Run:

```bash
.venv/bin/python -m pytest tests/test_action_renderer.py -q
```

Expected: the new tests fail because the renderer does not yet add the Tasks query block.

### Task 2: Renderer Implementation

**Files:**
- Modify: `src/obsidian_intake_agent/rendering/action_renderer.py`

- [ ] **Step 1: Add a canonical query block constant**

Define the exact Tasks query block once near the existing heading constants.

- [ ] **Step 2: Add block to new notes**

Update `_build_actions_note` so the query block is appended after the title and before any preamble or action sections.

- [ ] **Step 3: Migrate existing notes on update**

When `render_actions_note` has pending lines to insert into an existing note, ensure the existing text contains the query block before appending the new action lines. If the block is already present, leave it alone.

- [ ] **Step 4: Verify focused tests pass**

Run:

```bash
.venv/bin/python -m pytest tests/test_action_renderer.py -q
```

Expected: all action renderer tests pass.

### Task 3: Documentation and Checks

**Files:**
- Modify: `README.md`

- [ ] **Step 1: Update README behavior note**

Mention that weekly action notes include a Tasks query block at the top for open tasks in the current file.

- [ ] **Step 2: Run priority checks**

Run:

```bash
make check
make test
make build
```

Run `make smoke` too if the renderer change affects CLI output or processing orchestration during review.
