# Meeting Fallback Grace and Late Transcript Upgrades Design

## Summary

Meeting sync currently treats a processed Copilot recap fallback as terminal.
Recent production evidence shows that this often happens only minutes after a
meeting ends, before Teams publishes the occurrence's transcript. The durable
processed marker then prevents later discovery and upgrade.

This change introduces a 60-minute transcript grace period, keeps
fallback-processed occurrences eligible for transcript upgrades for the
existing 24-hour artifact-retry window, and makes upgrades safe, occurrence
aware, recoverable, and idempotent.

Recurrence cadence is deliberately irrelevant. A series may occur multiple
times per day, daily, weekly, monthly, irregularly, or over many months. State
and transcript assignment always operate on one Outlook calendar occurrence at
a time.

## Confirmed Root Cause

The current flow has four interacting behaviors:

1. `GraphMeetingFallbackSummaryClient` can create a processor-ready recap as
   soon as Copilot publishes one.
2. Bundle processing immediately processes that recap and writes
   `source_type: meeting_bundle_processed`.
3. `build_transcript_sync_plan()` prefilters every processed marker before
   artifact discovery.
4. The processed bundle's staged artifacts and metadata are cleaned up.

Consequently, a transcript published later cannot replace the recap. Current
fallback Markdown filenames also contain `(fallback)`, which becomes part of
the permanent meeting title and canonical filename. Because generic Markdown
processing does not receive the bundle's source context, fallback canonical
notes have empty `sources_used` and `source_limitations` fields.

Production markers from July 22 through July 29 confirm nine recap fallbacks
were processed approximately 5.6 to 10.8 minutes after their meetings ended,
all while Graph reported no transcript for the occurrence.

## Goals

- Wait 60 minutes after scheduled meeting end before processing a recap when
  no occurrence-specific transcript is available.
- Keep checking a fallback-processed occurrence for a transcript until 24
  hours after scheduled meeting end.
- Treat transcript-processed occurrences as terminal.
- Assign transcripts to individual occurrences without reusing a transcript
  from another occurrence in the same series.
- Retain the existing selection tolerance of 15 minutes before scheduled start
  through 30 minutes after scheduled end.
- Surface ambiguous occurrence assignment clearly instead of guessing.
- Upgrade an unedited fallback automatically and idempotently.
- Never overwrite a manually edited fallback automatically.
- Preserve the prior fallback note and source provenance in recoverable
  storage.
- Avoid duplicate canonical meeting notes and weekly action items.
- Keep new canonical titles and filenames free of `(fallback)`.
- Populate structured fallback status, `sources_used`, and
  `source_limitations`.
- Continue reading existing pending and processed markers safely.

## Non-Goals

- Bulk-renaming or rewriting historical fallback notes.
- Migrating every historical marker to the new schema.
- Weakening occurrence-aware Graph transcript validation.
- Accepting local VTTs by filename alone.
- Assigning timestamp-free or ambiguous Graph transcript records.
- Inferring actual meeting start or end from transcript contents.
- Automatically merging user-edited fallback prose with a transcript-derived
  note.

## Occurrence Identity

### Scheduled occurrence versus actual timing

The Outlook occurrence identifies the unit of work. Scheduled start and end
times are anchors and provenance, not exact transcript-time requirements. A
meeting may start or end early or late without becoming a different
occurrence.

New occurrence state uses:

- Outlook occurrence event ID as the primary identity;
- Teams meeting ID or series identifier as provenance;
- scheduled start;
- scheduled end.

The Teams meeting ID alone must never be a terminal-state or deduplication key
because every occurrence in a recurring series can share it.

When Outlook supplies a stable occurrence event ID, scheduled bounds are
validation and transcript-selection data rather than exact identity
requirements. If an occurrence ID is unavailable, the conservative fallback
identity combines the series identifier with scheduled start. This protects
daily, weekly, irregular, rescheduled, and long-running series without encoding
any cadence assumptions or using actual transcript timing as identity.

### Compatibility lookup

Marker resolution checks both the new occurrence-aware path and the current
legacy path. If a legacy marker exists, it remains authoritative for that
occurrence and is interpreted conservatively. The implementation does not
rename existing marker files.

Malformed or unrecognized processed markers remain terminal rather than risk
duplicate processing.

## Transcript Assignment

### Tolerance window

A timestamped Graph transcript candidate qualifies for an occurrence when its
`createdDateTime` falls from:

- scheduled start minus 15 minutes; through
- scheduled end plus 30 minutes.

The scheduled bounds remain unchanged in the marker even when the meeting
actually starts or ends a few minutes early or late.

Multiple transcript segments uniquely assigned to the same occurrence are
sorted chronologically and merged using the existing multi-segment behavior.

### Cross-occurrence ambiguity

Transcript assignment must consider other discovered occurrences sharing the
same Teams meeting ID. A transcript candidate is not admitted when it falls
inside the tolerance windows of more than one occurrence.

Ambiguous candidates are not downloaded as processor-ready inputs and cannot
cause recap processing to masquerade as a transcript upgrade. Before the grace
deadline, the affected occurrence remains pending. After the grace deadline,
an available recap may still be processed as a fallback because the ambiguous
transcript is not a valid transcript input. Transcript assignment remains
unresolved and visibly reported until a later run resolves it or the 24-hour
window expires.

This check supplements, rather than replaces, the existing rules that reject:

- timestamp-free transcript records;
- records outside the occurrence window;
- stale local VTTs without matching occurrence provenance;
- ambiguous filename-only local candidates.

### Structured ambiguity diagnostics

Dry-run and execution output surface a per-occurrence structured error:

```text
meeting_sync_occurrence_error: ambiguous_transcript_assignment
  outlook_event_id: <occurrence event id>
  teams_meeting_id: <shared Teams meeting id>
  scheduled_window: <start> to <end>
  selection_window: <start-minus-15m> to <end-plus-30m>
  conflicting_occurrence: <event id and scheduled window>
  candidate_transcript_id: <Graph transcript id>
  candidate_created_at: <timestamp>
  reason: Transcript matched more than one occurrence tolerance window; no automatic selection was made.
```

The plan also reports an aggregate ambiguity count. An ambiguous occurrence
does not prevent unrelated, unambiguous meetings from processing.

## State Model

### Pending

Pending state retains the current `meeting_sync_pending` source type and adds
explicit occurrence and timing data:

- schema version;
- occurrence key and scheduled bounds;
- selection-window bounds;
- first and last checked timestamps;
- `fallback_not_before`, scheduled end plus the grace period;
- `retry_until`, scheduled end plus 24 hours;
- artifact statuses and transcript diagnostics.

Existing `meeting_sync_identity`, missing-source-type pending markers, and
current `meeting_sync_pending` markers remain readable.

### Processed

The durable processed marker keeps
`source_type: meeting_bundle_processed` for compatibility and adds:

- schema version;
- occurrence data and `retry_until`;
- `processing_source_kind`: `fallback`, `transcript`, or `manual`;
- `upgrade_state`: `awaiting_transcript`, `terminal`, or
  `manual_review_required`;
- preferred input and selected-transcript provenance;
- canonical note path and canonical-note content hash;
- weekly action path;
- archive and upgrade history when applicable.

State interpretation is:

- transcript processed: terminal immediately;
- manual transcript processed: terminal immediately;
- fallback processed before `retry_until`: continue transcript discovery;
- fallback processed after `retry_until`: terminal;
- fallback with a late transcript and edited canonical note:
  `manual_review_required`;
- legacy fallback marker: infer fallback status from
  `preferred_input_source_name` and use the calendar occurrence's scheduled end
  to calculate the missing retry deadline;
- legacy transcript marker: terminal;
- legacy processed marker without enough source information: terminal
  conservatively.

## Processing Flow

### Before the grace deadline

1. Discover the Outlook occurrence and Graph transcript metadata.
2. Apply occurrence assignment and ambiguity checks.
3. Prefer a uniquely assigned transcript immediately when available.
4. A recap may be downloaded and staged, but it is not exposed as the
   processor handoff before `fallback_not_before`.
5. Write or refresh pending bundle metadata and marker state.
6. `process-bundles` reports the recap as deferred by the transcript grace
   period.

### After the grace deadline

1. Prefer a uniquely assigned transcript if one exists.
2. If no valid transcript exists and a recap is available, expose the recap as
   the processor handoff. An ambiguous transcript candidate does not count as a
   valid transcript.
3. Process the recap and write a fallback-processed marker with
   `upgrade_state: awaiting_transcript`.
4. Keep the occurrence eligible for transcript-only discovery until
   `retry_until`.

### Fallback-processed polling

For a fallback-processed occurrence inside the retry window:

1. Run transcript metadata discovery and occurrence validation.
2. Do not redownload or reprocess the Copilot recap.
3. If no valid transcript exists, leave the marker and canonical note
   unchanged.
4. If assignment is ambiguous, emit the structured occurrence error and leave
   the state unchanged.
5. If a uniquely assigned transcript exists, stage it and prepare an upgrade
   bundle.

After the retry deadline, remote artifact discovery stops and the fallback is
terminal.

## Safe Late-Transcript Upgrade

### Edit detection

Fallback processing stores a SHA-256 hash of the generated canonical note.
Before upgrading, the executor compares that hash with the current canonical
note.

- If the hash matches, the note is automation-owned and eligible for automatic
  upgrade.
- If the hash differs, no canonical note or weekly action file is changed. The
  transcript remains staged, the marker records
  `manual_review_required`, and output names the note requiring review.
- A legacy fallback marker without a trustworthy canonical hash is handled
  conservatively: stage the transcript and require manual review.

### Automatic upgrade sequence

For an unedited fallback:

1. Validate that the staged transcript has occurrence-matching Graph
   provenance.
2. Preserve an exact copy of the prior fallback canonical note under the
   existing archive root in a `Meeting Upgrades` directory.
3. Process the transcript into the clean canonical meeting path.
4. If an old historical canonical filename contains `(fallback)`, remove it
   from the active meetings directory only after the archived copy and clean
   replacement both exist.
5. Repoint existing weekly-action backlinks from the fallback canonical name to
   the clean canonical name while preserving action text, checkbox state,
   ordering, and unrelated content.
6. Let existing action-key deduplication prevent exact transcript actions from
   being appended again.
7. Write the terminal transcript-processed marker with selected transcript
   IDs, provenance, the archived fallback path, and the prior marker state.
8. Clean up only machine-managed bundle staging.

The marker becomes terminal only after the transcript note, action update,
archive, and provenance record succeed. A rerun after success is a no-op.

## Canonical Note Presentation

The machine-managed recap input may retain `(fallback)` in its staging filename
for operational transparency. Bundle processing overrides permanent meeting
metadata from the Outlook occurrence so `(fallback)` does not enter the
canonical title, heading, filename, or action backlink.

Canonical meeting front matter gains structured source state, including:

- `artifact_state: fallback` or `artifact_state: transcript`;
- concrete `sources_used`;
- concrete `source_limitations`;
- upgrade/archive provenance when a transcript supersedes a fallback.

A recap-derived note explicitly records that it is summary-derived and not
verbatim. A transcript upgrade removes that fallback limitation while
preserving other applicable limitations.

## Configuration

Add:

```yaml
meeting_transcript_grace_minutes: 60
```

The value must be a positive integer. It controls only when a recap becomes
processor-ready. It does not change:

- the 15-minute early transcript-selection tolerance;
- the 30-minute late transcript-selection tolerance;
- the existing 24-hour artifact-retry window.

The reusable default is documented in `config.example.yaml`, `README.md`, and
the meeting transcript automation runbook. Local `config.yaml` remains
untracked.

## Error and Operator Visibility

Dry-run and execution output distinguish:

- recap available but deferred by grace period;
- fallback processed and awaiting transcript;
- late transcript uniquely assigned and ready to upgrade;
- overlapping occurrence windows causing ambiguous assignment;
- timestamp-free or wrong-occurrence transcript rejection;
- edited canonical note requiring manual review;
- fallback retry window expired;
- transcript-processed terminal state;
- legacy marker interpreted conservatively.

Operational ambiguity and manual-review errors are per occurrence and do not
abort unrelated meeting processing.

## Test Strategy

Focused sync tests cover:

- recap available 5 to 10 minutes after end remains pending;
- transcript appears during grace and wins;
- grace expires with only recap and fallback becomes processable;
- fallback-processed occurrence remains discoverable before 24 hours;
- fallback becomes terminal after 24 hours;
- transcript-processed occurrence remains terminal;
- daily, weekly, irregular, and long-running series use independent occurrence
  state without cadence assumptions;
- multiple occurrences sharing one Teams meeting ID receive distinct
  transcript IDs and content;
- actual start/end variance is accepted by the 15-minute/30-minute tolerance;
- overlapping occurrence windows surface structured errors and select no
  transcript;
- multiple segments uniquely assigned to one occurrence merge chronologically;
- timestamp-free, wrong-occurrence, stale, and filename-only candidates remain
  rejected;
- existing pending and processed marker formats remain readable.

Focused bundle-processing and processor tests cover:

- fallback metadata defers processor handoff during grace;
- fallback processing writes clean canonical title and structured source fields;
- late transcript upgrades an unedited fallback;
- prior fallback canonical content is archived recoverably;
- edited fallback blocks automatic upgrade and retains the staged transcript;
- historical `(fallback)` canonical filenames are removed from the active
  meeting directory only after safe upgrade;
- existing action backlinks are repointed without changing checkbox state or
  unrelated content;
- no duplicate meeting note or weekly action is introduced;
- repeated runs after upgrade are idempotent.

Existing recurring-series provenance, pagination, stale-transcript,
chain-precedence, dry-run, and bundle-marker tests remain part of regression
validation.

## Documentation and Validation

Update `README.md`, `config.example.yaml`, and
`docs/meeting_transcript_automation.md` with the grace period, state
transitions, occurrence identity, ambiguity messages, safe upgrade behavior,
and manual-review path.

Use TDD with the narrowest focused tests first. Final validation includes:

```bash
make check
make test
make smoke
make build
make audit
```

Run transcript-processing dry-run validation before reporting completion.
Environment-only failures are reported separately.

No repository commit, push, PR, or historical vault migration is part of this
implementation session without separate review and approval.
