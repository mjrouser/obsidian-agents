# Meeting Ingestion Transcript-First Fallback Design

## Summary

This design extends the existing `obsidian-agent meetings sync-transcripts` and
`obsidian-agent meetings process-bundles` workflow so the system can ingest
Teams meetings more reliably without abandoning the current bundle-based
architecture.

The primary rule is:

1. Prefer a real `.vtt` transcript when one is available.
2. Fall back to a full Copilot AI summary when no transcript is available.
3. Enrich either path with Outlook metadata and Teams chat context when
   available.
4. Skip quietly and retry later when neither a transcript nor a full Copilot
   summary is available.

This keeps transcript-backed processing canonical while allowing a summary-based
path to unblock meeting capture when Teams does not publish a transcript.

## Goals

- Automate ingestion of Teams meeting content into the existing meeting
  processor workflow.
- Preserve transcript-first behavior as the preferred and most trustworthy
  source.
- Support a full Copilot AI summary as a processor-acceptable fallback source.
- Enrich all processor-ready meeting inputs with Outlook calendar context and
  Teams meeting chat context when available.
- Keep dry-run visibility, bundle planning, idempotency, and downstream
  processing behavior explicit.
- Keep top-level `00_Intake` clean by isolating machine-managed staging
  artifacts.

## Non-Goals

- Building fuzzy ranking or heuristic confidence scoring for which meetings to
  process.
- Preferring Copilot summaries over transcripts when both are available.
- Pulling every possible Teams artifact such as attachments or shared documents
  in v1.
- Designing a custom Copilot summary template in this phase.
- Replacing the existing meeting processor or canonical note renderer.

## Current Baseline

The repository already has a two-step bundle workflow:

- `meetings sync-transcripts` discovers recent Outlook meetings, evaluates
  source artifacts, and can write bundle metadata plus downloaded transcript
  artifacts.
- `meetings process-bundles` reads bundle metadata and hands a preferred input
  file into the existing meeting processor.

Current implementation already supports:

- Outlook meeting discovery from Microsoft Graph.
- Local transcript discovery for `.vtt`, `.md`, and `.docx`.
- Transcript download planning and preferred processor handoff for transcript
  artifacts.
- Identity markers for rerun dedupe.
- Explicit source and limitation reporting in dry-run output and bundle notes.

Current gaps relevant to this design:

- No chat retrieval yet.
- No Copilot recap / AI summary retrieval yet.
- Bundle staging currently lives directly under `00_Intake` instead of a
  dedicated machine-managed subtree.
- Eligibility filtering is not yet strict enough for low-value or unresponded
  meetings.

## Recommended Approach

Extend the existing bundle planner into a source-resolution pipeline instead of
adding a parallel command or bypassing bundles entirely.

Why this approach:

- It preserves the current dry-run -> write-bundles -> process-bundles
  separation.
- It keeps one meeting mapped to one bundle contract and one preferred processor
  handoff.
- It avoids duplicating meeting discovery, dedupe, and retry logic across
  separate transcript and summary import commands.
- It keeps summary fallback an explicit extension of the existing pipeline
  rather than an unrelated special case.

## End-to-End Flow

For each discovered Outlook event in the requested time window:

1. Apply eligibility filtering.
2. Skip immediately if the meeting is already imported, ineligible, or not yet
   ended.
3. Resolve the primary source in this order:
   - local or downloaded `.vtt` transcript
   - transcript-text artifact if the system already has processor-ready
     transcript text
   - full Copilot AI summary
4. Attach enrichment context:
   - Outlook metadata is always included when available from discovery.
   - Teams chat context is attached when available.
5. Build exactly one bundle contract for the meeting.
6. If a processor-ready primary source exists, create a preferred processor
   handoff file path.
7. If no transcript and no full Copilot summary exists, skip quietly and allow
   future reruns to retry.
8. After successful downstream processing, delete bulky working artifacts but
   keep a compact durable processed marker for idempotency.

## Eligibility And Filtering

Filtering happens before source resolution so the system does not waste work on
meetings that should never become intake bundles.

### Existing skip rules that remain as baseline behavior

- canceled events
- non-Teams events
- events that have not ended yet
- already-imported meetings
- all-day and focus events when no real meeting content exists

### New v1 filter rules

- Skip meetings with `response_status=declined`, even if source artifacts
  exist.
- Skip meetings with `response_status=none` or equivalent no-RSVP state by
  default.
- Skip low-signal subject patterns such as `office hours`.
- Continue to process accepted meetings when they pass the existing content and
  timing checks.

### Design constraints

- Filtering should be explicit and auditable, not fuzzy.
- Dry-run output should report exact reasons such as
  `filtered_subject=office_hours` or `filtered_response_status=not_responded`.
- The initial rules can live in code first, with a config path later if tuning
  becomes necessary.

## Source Resolution Rules

### Primary source priority

The winning primary source is selected in strict order:

1. `Teams .vtt transcript`
2. `Teams transcript text`
3. `Copilot recap / AI summary`

### Rules

- If a `.vtt` transcript is available, it is always primary.
- Copilot summary is fallback only. It must not replace a transcript when a
  transcript exists.
- Teams chat is enrichment only. Chat alone is never a processor-ready primary
  input.
- Outlook metadata is supporting context, not a primary input.
- A full Copilot summary is sufficient to continue automatically through normal
  processing.
- If neither transcript nor full Copilot summary is available, no bundle is
  processed on that run.

### Future direction

The system should preserve a clean seam for future Copilot summary template
customization, but template design is out of scope for this phase.

## Bundle Contract

Each eligible meeting produces at most one canonical bundle record.

The bundle contract must capture:

- meeting identity
  - Outlook event ID
  - Teams meeting ID when available
- selected primary source type
- preferred processor input path
- source availability states for transcript, transcript text, chat, and recap
- source limitations and retrieval details
- attendance confidence
- linked enrichment context from Outlook and Teams chat

### Processor handoff

`meetings process-bundles` should remain source-agnostic. It should trust the
bundle metadata and process the preferred input file without needing to know how
Graph, Outlook, or Teams retrieval worked.

That means summary fallback needs a new processor-ready artifact type:

- generated summary-backed Markdown input

This generated Markdown file should be structured as a normal intake artifact
that the current meeting processor can consume, with explicit labeling showing:

- source used
- source limitations
- attendance confidence
- relevant Outlook metadata
- relevant Teams chat context

## Bundle Storage Layout

Bundle artifacts should move under a dedicated machine-managed subtree:

- `00_Intake/bundles/`

Proposed responsibilities:

- `00_Intake/bundles/`:
  - bundle notes
  - Outlook metadata sidecars
  - processor-ready generated fallback Markdown inputs
  - any bundle-local state files
- `00_Intake/bundles/_meeting_sync/identities/`:
  - durable dedupe and processed markers
- `00_Intake/bundles/raw_transcripts/`:
  - downloaded raw `.vtt` transcript artifacts

This keeps top-level `00_Intake` reserved for human-facing or ad hoc intake
files instead of operational staging noise.

## Bundle Lifecycle

Successful processing should clean up bulky working artifacts while retaining a
small durable record.

### After successful canonical note creation

- Delete processor-consumed bundle files.
- Delete temporary generated fallback inputs.
- Delete any no-longer-needed bundle-local metadata that only serves staging.
- Keep a durable processed identity marker.

### Durable record fields

The retained processed marker should be enough to support dedupe and audit:

- Outlook event ID
- Teams meeting ID when available
- selected source type
- processed timestamp
- canonical note path

This supports idempotent reruns without keeping the full staging workspace
forever.

## Chat And Outlook Enrichment

Every processor-ready bundle should include Outlook metadata and Teams chat
context when available.

### Outlook context

Expected fields include:

- meeting subject
- organizer
- attendees
- response status
- start and end times
- join URL or related IDs when available

### Teams chat context

Chat context is supplemental enrichment. It can improve extraction quality for
meetings where side discussion, follow-up actions, or shared context appears in
chat, but it must not become the sole processor input in v1.

If chat retrieval is missing or permission-blocked:

- processing can still continue when the primary source is transcript or full
  Copilot summary
- source limitations must record the missing or blocked chat state

## Retry And Visibility Model

The sync command should stay explicit rather than opaque.

### Retry behavior

- Meetings with no transcript and no full Copilot summary are skipped quietly.
- Later runs retry them naturally through the normal sync command.
- No automation error note is created for this ordinary not-ready state.

### Dry-run output expectations

Dry-run should clearly report why each meeting was:

- skipped
- filtered
- deferred for retry
- processor-ready

Representative reasons include:

- `filtered_subject=office_hours`
- `filtered_response_status=not_responded`
- `missing_transcript`
- `missing_full_copilot_summary`
- `permission_blocked_chat`
- `ready_source=vtt`
- `ready_source=copilot_summary`

## Testing Strategy

This work should be validated in focused slices rather than a single large
integration rewrite.

### 1. Planner tests

Add or update tests for:

- new eligibility filters
- transcript-first source selection
- Copilot summary fallback selection
- quiet skip when neither primary source exists
- clear dry-run reasons for filter and source decisions

### 2. Bundle contract tests

Add or update tests for:

- `00_Intake/bundles` path layout
- generated summary-backed processor handoff artifacts
- bundle metadata for chosen primary source and enrichments
- durable processed marker content
- cleanup behavior after successful processing

### 3. Execution tests

Add or update tests for:

- `process-bundles` consuming summary-backed Markdown inputs
- successful processor handoff for transcript-backed bundles
- post-success cleanup of bundle staging artifacts
- idempotent rerun behavior using retained identity markers

## Open Implementation Questions

These are implementation details to settle in planning, not unresolved product
requirements:

- exact Markdown shape of the generated summary-backed fallback input
- whether filter rules should be hardcoded first or introduced immediately as
  configurable patterns
- whether downloaded raw transcripts should migrate from the current
  `Raw Transcripts` naming to `bundles/raw_transcripts` in one step or through a
  compatibility transition
- exact minimal schema of the durable processed marker

## Implementation Boundaries

This design intentionally keeps the current architecture:

- discovery remains in `meetings sync-transcripts`
- bundle execution remains in `meetings process-bundles`
- canonical note and weekly action generation remain in the existing meeting
  processor

The new work should extend those seams rather than introducing a second meeting
ingestion architecture.
