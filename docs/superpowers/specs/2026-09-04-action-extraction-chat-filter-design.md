# Action Extraction Chat Filtering Design

## Summary

Prevent Teams meeting-chat messages from becoming weekly actions while preserving
the existing Markdown formats that provide explicit evidence of an assignment.
The change will make ambiguous owner-label syntax context-sensitive: a line such
as `Matthew Rouser: Thanks!` is a speaker label outside an action section, but an
owner assignment inside `Action Items` or after an explicit `Action:` prefix.

This is a focused parser correction. It does not change meeting discovery,
fallback source selection, owner aliases, action rendering, or transcript-backed
LLM extraction.

## Confirmed Root Cause

Generated Copilot fallback inputs contain distinct Markdown sections for
`Summary`, `Action Items`, `Mentions`, and `Meeting Chat`. Meeting-chat messages
are rendered as ordinary bullets using this shape:

```markdown
- Matthew Rouser: Thanks!
```

`extract_markdown_action_items()` currently attempts to parse every ordinary
Markdown bullet before considering the active section. `OWNER_LABEL_PATTERN`
accepts `Name: text` and `Name - text` as owner assignments, so a speaker-labeled
chat message becomes an `ActionItem`. `MeetingProcessor` then normalizes Matthew's
name and correctly routes the incorrectly classified item into `07_Actions`.

The failure is deterministic and reproducible without calling an LLM. In the
two reported fallback inputs, the parser extracted six Matthew-authored chat
messages from the LP meeting and one from the M&A GTM meeting. The explicit
Copilot `Action Items` sections did not contain those items.

## Goals

- Stop speaker-labeled chat bullets from entering weekly action notes.
- Preserve explicit Markdown action formats currently supported by the project.
- Preserve Matthew owner-alias normalization and Matthew-only routing.
- Keep fallback meeting notes and Teams chat context intact.
- Preserve dry-run behavior, unrelated action-note content, and rerun dedupe.
- Cover the production fallback shape with an end-to-end regression test.
- Provide a separate, review-first path for removing historical false positives.

## Non-Goals

- Changing transcript-versus-fallback source priority.
- Changing the Copilot or Codex extraction prompts as the primary fix.
- Adding a subjective actionability classifier or keyword blacklist.
- Automatically deleting existing weekly actions during normal processing.
- Refactoring meeting-sync or the action renderer beyond what the regression
  requires.
- Treating every commitment written in meeting chat as non-actionable; explicit
  chat commitments may still be captured when Copilot lists them in
  `Action Items`.

## Approved Parsing Policy

The extractor will use the current Markdown heading to decide whether ambiguous
owner-label syntax is actionable.

### Accepted anywhere

- Explicit `Action:` lines, including `Action: Matthew: Send the deck`.
- Unchecked Markdown tasks such as `- [ ] Send the deck (Matthew)`.
- Unambiguous assignment language such as `Matthew to send the deck`,
  `Matthew will send the deck`, `Assigned to Matthew: send the deck`, and
  `Owner: Matthew - send the deck`.
- Existing parenthetical owner syntax such as `Send the deck (Matthew)`.

### Accepted inside `Action Items` or `Next Steps`

- All currently supported formats.
- Ambiguous owner-label bullets such as `- Matthew: Send the deck`.

### Rejected outside an action section unless explicitly prefixed with `Action:`

- `- Matthew Rouser: Thanks!`
- `- Matthew Rouser: Did we cancel today?`
- `- Matthew Rouser - I see it now`
- Equivalent standalone `Name: text` and `Name - text` lines.

The parser will not judge actionability from punctuation, message length, or
specific words. Section context and explicit assignment syntax are the only new
signals.

## Implementation Shape

### Markdown parsing

Keep the change in
`src/obsidian_intake_agent/processors/md_reader.py`. Preserve
`parse_action_text()` as the central syntax parser, but give extraction a way to
disable the ambiguous `OWNER_LABEL_PATTERN` outside an action context. The
default used by direct action parsing should remain explicit and testable rather
than relying on post-processing the returned owner.

`extract_markdown_action_items()` will continue tracking whether the current
heading matches `ACTION_SECTION_PATTERN`. Its control flow will apply these
contexts:

1. `Action:` prefix: parse with owner labels enabled.
2. Unchecked checkbox: parse with owner labels enabled.
3. Ordinary bullet or standalone line inside an action section: parse with
   owner labels enabled.
4. Ordinary bullet or standalone line outside an action section: parse with
   owner labels disabled while retaining the other explicit assignment forms.

No change is required in the fallback Markdown renderer. Retaining `Meeting
Chat` in canonical notes remains useful context, and the parser should be safe
when other non-action sections adopt the same speaker-label format.

### Processing and rendering

`MeetingProcessor` will continue receiving `ActionItem` objects and filtering
them through the configured owner aliases. `action_renderer.py` remains
unchanged because it receives already-classified actions and is not the source
of this defect.

### Documentation

Update the README's Markdown-action behavior description to document the
accepted explicit forms and state that ambiguous `Name: text` labels require an
action section or `Action:` prefix.

## Data Flow After the Change

For a generated fallback bundle:

1. Meeting sync writes Copilot actions under `## Action Items` and chat under
   `## Meeting Chat`.
2. Bundle processing passes the generated Markdown input to `MeetingProcessor`.
3. The Markdown extractor accepts assigned items from `Action Items`.
4. After the `Meeting Chat` heading, `Name: text` is treated as a speaker label
   and is not returned as an `ActionItem`.
5. The existing owner filter routes only qualifying Matthew-owned actions.
6. The action renderer inserts only new semantic action records and preserves
   existing content.

## Compatibility And Failure Handling

- Existing `Matt to ...` and `Matthew will ...` bullets outside action sections
  remain supported.
- The intentionally ambiguous standalone test for `Matthew: draft the SOW`
  will change: that syntax must appear inside an action section or after
  `Action:`.
- Fallback files with no `Action Items` section will still yield explicit
  `Action:`, checkbox, `to`, `will`, `Assigned to`, `Owner:`, and parenthetical
  formats; they will not recover ambiguous speaker-label lines.
- No new exception or warning path is needed. Rejected ambiguous lines are
  ordinary non-actions, consistent with existing behavior for prose.
- Dry runs will exercise the same extraction logic and continue to avoid file
  writes.

## Testing Strategy

### Focused parser tests

Add tests in `tests/test_processor.py` that demonstrate:

- `Matthew: draft the SOW` remains accepted under `Action Items`.
- `Action: Matthew: draft the SOW` remains accepted outside a section.
- `Matt to review the staffing model` remains accepted outside a section.
- speaker-labeled bullets and standalone lines are rejected under `Meeting
  Chat`, `Summary`, and ordinary prose.
- heading transitions reset action context correctly.

### Processor regression

Process a Markdown fixture containing both a real Matthew-owned action section
and the reported Matthew-authored chat lines. Assert that the weekly action note
contains the real action and excludes the chat messages. Process the fixture a
second time with `force=True` and assert that the action note is unchanged.

### Bundle regression

Add a focused execution test in `tests/test_meeting_process_bundles.py` using a
processor-ready fallback Markdown input. Assert that bundle processing creates
the canonical meeting note, preserves the `Meeting Chat` content there, routes
the explicit Matthew action once, and does not route speaker-labeled chat.

### Validation sequence

Run the narrow parser and processor tests first, then the bundle-focused test.
After those pass, run the repository's required `make check`, `make test`,
`make smoke`, `make build`, and `make audit` gates because bundle processing and
action routing are production automation paths.

## Historical Cleanup

Historical cleanup is a separate operational step after the parser correction
is verified. It will not be embedded in normal meeting processing.

1. Build a read-only candidate report by finding weekly action text that exactly
   matches Matthew speaker messages in source `Meeting Chat` sections.
2. Present candidates with weekly-note path, source note, checkbox state, and
   text for review.
3. Retain any chat message that represents a real commitment despite its source.
4. After explicit approval, remove only confirmed false-positive lines while
   preserving unrelated actions, ordering, headings, and note formatting.
5. Re-run the candidate report and confirm that no approved false positives
   remain.

No reusable cleanup command will be added unless repeated cleanup proves
necessary.

## Acceptance Criteria

- None of the seven reported chat messages are extracted as actions.
- Explicit Matthew-owned actions from the same fallback inputs remain eligible.
- Supported explicit assignments outside action sections continue to work.
- Teams chat remains present in the canonical meeting note.
- Rerunning processing does not duplicate or reorder actions.
- Dry-run processing does not modify the vault.
- Focused and required repository checks pass.
- Historical cleanup, if authorized, changes only reviewed tracker lines.
