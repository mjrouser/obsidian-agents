# Live Meeting Validation And Test Output Lane Design

## Summary

This design adds a safe live-validation workflow for real meeting-ingestion
runs without muddying the production vault outputs.

The primary rule is:

1. Use the real automated Outlook/Graph meeting discovery pipeline.
2. Start broad validation with dry-run coverage inspection.
3. Execute only a small representative sample for quality review.
4. Route validation-generated canonical notes and Matthew-owned action outputs
   into a permanent test-only lane under `99_Test Notes/`.

This keeps discovery and source-resolution behavior honest while isolating test
artifacts from `01_Meetings` and `07_Actions`.

## Goals

- Validate that the merged meeting-ingestion pipeline can discover real meetings
  and classify them correctly.
- Inspect transcript-backed, Copilot-summary-backed, and skipped meetings over a
  real date window.
- Review filtering behavior for RSVP-based skips, low-signal subjects such as
  `office hours`, and other current v1 exclusions.
- Provide a permanent home for validation-generated meeting notes and action
  outputs.
- Preserve normal production behavior for non-validation runs.

## Non-Goals

- Automating scheduled polling for meeting sync in this slice.
- Reworking the `00_Intake/bundles` staging architecture again.
- Changing transcript-first versus summary-fallback source priority.
- Introducing new filter heuristics beyond what live validation proves we need.
- Adding automatic cleanup of test outputs under `99_Test Notes/`.

## Current Baseline

The repository already supports:

- Outlook/Graph meeting discovery through `meetings sync-transcripts`
- transcript-first source selection with Copilot recap fallback
- bundle staging under `00_Intake/bundles`
- bundle execution through `meetings process-bundles`
- v1 filtering for declined/no-response meetings, low-signal office-hours
  subjects, not-yet-ended meetings, non-Teams meetings, and already-imported
  identities

Current gap relevant to this design:

- Live validation currently risks writing canonical notes and Matthew-owned
  action updates into the real production lanes.
- There is no permanent, first-class destination for validation-only meeting
  outputs.
- We have not yet validated the merged source-resolution and filtering behavior
  against a real recent meeting window.

## Recommended Approach

Add an explicit validation mode that preserves discovery semantics while
redirecting downstream canonical outputs into a permanent test-only lane.

Why this approach:

- It validates the real automated fetch path rather than a synthetic or
  hand-curated workflow.
- It keeps dry-run classification and execution behavior close to production.
- It isolates test artifacts without duplicating the meeting-ingestion pipeline.
- It creates a durable home for future validation runs rather than a one-off
  operator workaround.

## Operator Workflow

The live-validation workflow should be:

1. Run `meetings sync-transcripts --dry-run --since <date>` against a recent
   real date window.
2. Inspect coverage across:
   - transcript-backed candidates
   - Copilot-summary-backed candidates
   - no-usable-source cases
   - filtered meetings and their skip reasons
3. Choose a small representative sample, ideally:
   - one transcript-backed meeting
   - one summary-backed meeting
4. Execute those samples through validation mode so canonical outputs land only
   in the test lane.
5. Review note quality and Matthew-only action routing in the test lane.
6. Use what we learn to drive a later tuning slice for filters or fallback
   behavior.

Normal production runs remain unchanged.

## Validation Mode

Validation mode should be an explicit, first-class operator choice rather than
an ad hoc path override.

### Core behavior

- Discovery behavior stays identical to normal mode.
- Source priority stays identical to normal mode.
- Filtering behavior stays identical to normal mode.
- Only downstream output destinations change.

### Why this matters

The point of live validation is to answer whether the real pipeline works.
Changing eligibility, artifact selection, or bundle semantics in validation mode
would weaken that signal.

## Output Layout

Validation outputs should have a permanent user-facing home under:

- `99_Test Notes/`

Recommended initial structure:

- `99_Test Notes/Meetings/`
- `99_Test Notes/Actions/`

Future additions such as test-only weekly review outputs can be added later if
needed, but they are out of scope for this slice.

## Output Routing Rules

In normal mode:

- canonical meeting notes continue to write to `01_Meetings`
- Matthew-owned actions continue to write to `07_Actions`

In validation mode:

- canonical meeting notes write to `99_Test Notes/Meetings`
- Matthew-owned actions write only to `99_Test Notes/Actions`

This isolation must apply to any downstream processing triggered from
bundle-backed transcript or summary-fallback inputs.

## Bundle And Intake Behavior

This slice should not split the staging system into parallel production and test
bundle roots.

Rules:

- `00_Intake/bundles` remains the shared machine-managed staging area.
- Validation mode affects processor outputs, not bundle discovery or bundle
  storage.
- `meetings sync-transcripts --dry-run` still reports the real meeting
  landscape, regardless of whether later execution uses validation mode.

This keeps the change smaller and avoids unnecessary duplication of staging
logic.

## Safety And Transparency

Validation outputs should be clearly marked so they are recognizable later.

Expected safety properties:

- validation-generated meeting notes include explicit metadata or frontmatter
  indicating validation origin
- validation-generated action outputs are clearly identifiable as test outputs
- no validation execution writes to `01_Meetings`
- no validation execution writes to `07_Actions`

This slice intentionally does not delete prior validation outputs. Keeping them
is part of the value of a permanent test lane.

## Reporting Expectations

The dry-run validation pass should make it easy to assess:

- transcript retrieval coverage
- Copilot fallback coverage
- filtering behavior
- no-usable-source cases

The reporting should stay explicit about skip reasons such as:

- declined response status
- no response / not responded
- low-signal office-hours subject
- meeting not ended yet
- non-Teams event
- already-imported identity

## Testing Strategy

Verify this slice in three layers:

1. Routing tests
   - validation mode writes canonical notes to `99_Test Notes/Meetings`
   - validation mode writes Matthew-owned actions to `99_Test Notes/Actions`
   - normal mode continues to use `01_Meetings` and `07_Actions`
2. Pipeline-behavior tests
   - validation mode does not change eligibility or source selection
   - transcript-backed and summary-backed bundle execution still works with the
     redirected output destinations
3. Operator acceptance tests
   - run a real dry-run date window
   - inspect filtering and source-resolution outcomes
   - execute a representative transcript-backed and summary-backed sample in the
     validation lane

## Acceptance Criteria

This slice is complete when:

- a real dry-run window shows transparent transcript, summary-fallback, and
  filtering outcomes
- validation execution can process representative meetings without touching
  `01_Meetings` or `07_Actions`
- validation canonical notes land under `99_Test Notes/Meetings`
- validation Matthew-owned actions land only under `99_Test Notes/Actions`
- normal non-validation runs remain unchanged

## Open Questions Resolved

- Validation should use a real recent date-window run, not hand-collected
  meeting selection.
- Validation needs both coverage inspection and small end-to-end quality review.
- Test outputs need a permanent home, not a temporary workaround.
- The permanent test lane should isolate both canonical notes and Matthew-owned
  action outputs.
- Bundle staging should remain in `00_Intake/bundles` for this slice.
