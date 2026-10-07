# Occurrence-Safe Copilot Recaps

Status: Proposed — design only; implementation and vault repair have not run.
Date: 2026-10-06

## Problem and evidence

The October 2 recurring one-to-one note contains a recap of the July 22
discussion. Local transcript comparison established the content match. Sync
logs recorded no transcript for the October 2 occurrence, but accepted a
Copilot fallback. The durable marker records fallback processing at
2026-10-02T17:36:07Z and no weekly action output.

The current code explains the incorrect association:

- `GraphMeetingFallbackSummaryClient._latest_ai_insight()` reads one response
  page and selects `items[-1]`, without occurrence or timestamp validation.
- `_render_fallback_summary_markdown()` labels that content with the requested
  Outlook occurrence's date and scheduled times.
- `discover_artifacts()` trusts an existing fallback path without checking
  which insight produced it.
- Calendar eligibility does not establish attendance. An out-of-office event
  does not automatically cancel a separate recurring meeting occurrence.

The saved fallback does not retain the original insight ID or timestamps.
Therefore the content match and code path are confirmed locally, but the exact
historical Graph response cannot be reconstructed from that file alone.

## Desired behavior

Only a recap uniquely associated with the requested Outlook occurrence may
become an automatic processor input. Otherwise the occurrence remains pending
under existing retry rules and produces no recap-derived canonical note or
weekly actions. Other valid sources and unrelated meetings continue normally.

Occurrence matching establishes the source date, not the user's attendance.
Calendar acceptance, organizer status, recap availability, and mention of a
person must never be presented as attendance verification.

## Scope and non-goals

Implement recap selection, durable provenance, cache and processor validation,
diagnostics, focused tests, and operator documentation. Preserve transcript
priority, the 60-minute fallback grace period, the existing 24-hour retry
window, late transcript upgrades, and manual-edit protection.

Do not add attendance-report permissions, out-of-office suppression rules,
new dependencies, new AI calls, or a bulk historical rewrite. Keep the current
Graph API version and authentication. This reuses the existing Copilot
integration and its licensing; pagination adds ordinary Graph reads, not a
new paid AI service. Cache discovery within a run to avoid repeating the same
series lookup for each occurrence.

Historical repair is a separate, explicitly targeted operation after the fix
passes validation. Writing this spec does not perform that repair.

## Source contract

Microsoft documents `createdDateTime` on an AI insight as the corresponding
transcript's creation time, and `endDateTime` as its transcription end time.
`callId` identifies the call; `contentCorrelationId` links the source transcript.
These fields, when returned, provide provenance beyond the shared meeting ID.
See [Microsoft's Meeting AI Insights guide](https://learn.microsoft.com/en-us/microsoftteams/platform/graph-api/meeting-transcripts/meeting-insights)
and [callAiInsight resource](https://learn.microsoft.com/en-us/microsoft-365/copilot/extensibility/api/ai-services/meeting-insights/resources/callaiinsight).

The resource page carries a beta warning while the guide shows v1.0 examples.
Before enabling writes, verify the required fields against a read-only response
from the already configured endpoint. Do not change API versions or infer
missing timestamps to make validation pass. If fields are unavailable, report
the limitation and withhold automatic recaps.

## Recap selection

1. Resolve the online meeting through the existing join-URL lookup. Obtain the
   same occurrence context used by transcript assignment, including skipped
   occurrences in the discovery snapshot, before eligibility filtering.
2. Read every insight metadata page using `@odata.nextLink`. Validate links
   against the configured Graph origin before forwarding authorization; reject
   cycles and malformed pages. A failed or incomplete listing must not yield a
   selected recap. Deduplicate identical IDs; conflicting metadata for the same
   ID blocks selection rather than depending on response order.
3. Require an insight ID and a timezone-aware, parseable `createdDateTime`.
   Normalize to UTC. Reject a supplied invalid `endDateTime`, or an end before
   creation. A missing end is retained as unavailable; it is not the matching
   anchor. Missing creation time means unverified, not a match.
4. Match creation time against the existing inclusive tolerance window:
   scheduled start minus 15 minutes through scheduled end plus 30 minutes.
   Reuse the tolerance constants and occurrence windows. Never select by title,
   local filename, calendar date alone, response position, or series ID alone.
5. A candidate must match exactly one discovered occurrence in its series and
   that occurrence must be the requested one. Overlapping matches are ambiguous.
   Context is limited to the calendar discovery snapshot; diagnostics record
   its bounds. Do not claim global uniqueness outside those bounds.
6. For this first change, require exactly one distinct qualifying insight.
   Multiple insights for one occurrence are reported as ambiguous, even if
   they share a call ID. Do not choose the newest or merge summaries. This
   deliberately withholds some legitimate multi-segment recaps; existing
   transcript segment handling remains available. Supporting recap segment
   merging is outside this fix.
7. Fetch the selected detail and validate its ID and creation timestamp against
   the selected metadata. Conflicting call/correlation IDs or end timestamps
   also block selection. Optional fields absent on one response may be filled
   from the other; contradictory values may not. A missing required detail
   timestamp blocks selection. Require meaningful summary/action content.
8. Render only after validation. Retain the scheduled date as the note date,
   with the source timestamps separately recorded in provenance.

Old, out-of-window insights are normal exclusions. Timestamp-free insights
cannot prove a match; report them without using them. Any candidate that
matches the target and another occurrence blocks that target's recap, even
if a second candidate matches the target uniquely.

### Meeting chat boundary

The existing chat lookup reads recent messages from the recurring thread.
An unfiltered thread is another path for old content to enter a correct recap.
For this change, omit automatic meeting-chat enrichment from newly generated
Copilot fallbacks and report that occurrence-scoped chat is unavailable.
Do not change standalone chat sources. Adding reliable chat occurrence
association is a separate enhancement; legacy cached chat is not grandfathered
into newly validated fallback content.

## Provenance and storage

Persist a versioned `<fallback>.provenance.json` sidecar with:

- Outlook occurrence ID, scheduled UTC start/end, Teams series ID, and resolved
  Graph online meeting ID;
- insight ID, transcript creation/end timestamps, optional call and correlation
  IDs, retrieval time, and selection policy version;
- selection bounds, discovery context bounds, and validation outcome;
- SHA-256 of the exact fallback Markdown bytes.

Copy the selected source identity into bundle metadata and durable processed
state so provenance survives intake cleanup. Expose the insight ID and source
timestamp in canonical front matter; retain the fallback/source limitations.
Never store credentials, authorization headers, or full Graph payloads in logs.

Use existing owned-path and symlink checks. Plan Markdown and provenance
together; only the explicit write phase persists them. Temporary writes and
atomic replacement prevent partial file contents. Because two files cannot be
committed atomically, readers must reject a missing sidecar or hash mismatch.
Concurrent creation must preserve the winner's files and revalidate them,
never pair one run's Markdown with another run's provenance.

Processing may prepend its existing status metadata. Validate the original
bytes before processing; preserve that source hash in durable state and record
a separate archived-file hash if the bytes change. Do not recompute a source
hash from mutated content and treat it as the original download.

## Close cache and handoff bypasses

Validation applies at both discovery and `process-bundles` handoff, including
direct processing of previously staged bundles without a fresh sync.

- A cached managed fallback is reusable only when its provenance, content hash,
  current occurrence bounds, and unique assignment all validate.
- Missing, malformed, stale, or mismatched provenance makes the automatic recap
  unavailable. Local artifact scanning and chained discovery must not override
  that decision with a filename match or stale bundle metadata.
- Preserve unverified files byte-for-byte. If fresh Graph discovery finds a
  valid recap but the existing target is occupied by unverified or edited
  content, surface `manual_review_required`; do not overwrite it automatically.
- Archive/cleanup handles the source and its sidecar together and retains
  durable selected-recap metadata before deleting transient bundle metadata.
- Existing processed markers remain readable and terminal under their existing
  rules. Missing recap provenance does not authorize replay or invalidate a
  correct transcript note. Active late-transcript upgrade checks continue.
- Explicit manual-summary intake remains a separate source. It must not act as
  an automatic downgrade path for rejected managed Copilot content.

No blanket schema migration is required. Optional recap provenance fields are
additive. Unknown policy versions are unverified for automatic recap reuse.

## State and diagnostics

Use existing artifact statuses: `available` only after successful validation;
`missing` for a complete lookup with no matching recap; `not_attempted` for
failed, incomplete, ambiguous, or unverifiable selection. Persist a structured
reason separately from the user-readable detail.

Expose candidate count, selected insight ID and timestamps, occurrence/window,
rejection reason, conflicting occurrence IDs, and local cache outcome in plan
output. Suggested reasons include `no_matching_recap`, `invalid_recap_metadata`,
`ambiguous_recap_assignment`, `multiple_matching_recaps`,
`recap_discovery_incomplete`, and `unverified_cached_recap`.

Pending occurrences keep existing grace/retry deadlines. Rejection does not
write `meeting_bundle_processed`, extend the deadline indefinitely, generate
actions, or stop unrelated occurrences. At expiry, retain diagnostics without
claiming the meeting happened or that the user attended.

Discovery-only dry runs continue to defer network artifact retrieval. They may
validate existing local provenance but must report deferred remote checks as
unknown. Neither ordinary dry runs nor focused read-only Graph validation may
write sources, sidecars, markers, canonical notes, or actions.

## Implementation boundaries

| Location | Responsibility |
| --- | --- |
| `meetings/recap_selection.py` (new) | Pure metadata validation, selection results, occurrence matching, and rejection reasons. |
| `meetings/recap_provenance.py` (new) | Sidecar schema, hash/identity validation, and cache trust rules. |
| `meetings/sync.py` | Graph pagination, validated detail retrieval, artifact planning, and diagnostics; remove arbitrary-last selection and filename-only trust. |
| `meetings/occurrence_assignment.py` | Reuse existing bounds/context; avoid changing transcript behavior or repurposing transcript IDs as insight IDs. |
| `meetings/process_bundles.py`, `identity_state.py` | Handoff validation, additive durable provenance, archive/cleanup handling. |
| Existing metadata/rendering code | Carry verified recap fields into the canonical note without implying attendance. |
| Focused new recap tests plus existing sync/bundle tests | Selection, persistence, bypass prevention, and integration regressions. |
| `README.md`, `docs/meeting_transcript_automation.md` | Explain unavailable recaps, cache review, provenance, and dry-run limitations. |

Keep Graph HTTP calls in the existing client. Do not refactor the wider meeting
pipeline or introduce a generic artifact framework for this fix.

## Tests and acceptance criteria

Use synthetic records and temporary vaults; do not commit production transcripts,
meeting IDs, user addresses, or vault paths.

1. Regression: a requested October occurrence sharing a series with a July
   insight yields no canonical note, action insertion, or processed marker.
2. A single valid occurrence recap succeeds after grace, carries provenance,
   normalizes Matthew/Matt ownership, and includes the source backlink.
3. Response ordering and pagination do not change selection. Test later-page
   matches, duplicate IDs, conflicting duplicates, failed pages, cycles, and
   off-origin pagination links without credential forwarding.
4. Test missing/invalid/naive timestamps, UTC offsets, inclusive boundaries,
   mismatched detail metadata, and empty content. Late publication uses source
   transcript time rather than retrieval time.
5. Test multiple same-day occurrences, irregular cadence, overlapping windows,
   rescheduling, and multiple matching insights. Never guess an assignment.
6. Test a correct cached source, missing/corrupt sidecar, modified Markdown,
   stale schedule, unknown policy version, partial writes, and competing creates.
7. Test local-scanner/chained-client and direct `process-bundles` bypass attempts;
   an unverified managed recap cannot become processor-ready by another path.
8. Verify no unscoped recurring chat appears in a newly generated fallback.
9. Verify idempotent reruns, unchanged unrelated actions, preserved edited notes,
   no writes in dry-run, legacy processed-marker compatibility, source/sidecar
   archival, and safe late transcript upgrades.
10. Verify one rejected occurrence does not stop a valid one, and expiry does
    not create a note or restart an endless retry cycle.

Run focused tests first, followed by `make check`, `make test`, `make smoke`,
and `make build`. Run `make audit` before any PR. Complete a synthetic
end-to-end dry run before touching live vault files. Read-only live validation
must confirm the configured API's metadata shape and rejection of the known
old-content case before enabling automatic recap writes.

## Targeted incident repair and rollout

After the code passes checks, perform a dry-run inventory limited initially to
the affected occurrence and its source, marker, backlinks, and action references.
Preserve copies and hashes in a repair manifest before changing anything.

For the confirmed incorrect note, the proposed repair is to move the generated
note, fallback, and available provenance into a dedicated quarantine outside
`01_Meetings` and automatic intake scanning. Leave an explanatory tombstone at
the original canonical path marked as an invalidated recap, so existing links
resolve without presenting the old discussion as an October meeting. Keep an
occurrence exclusion in durable state, recognized by both sync and bundle
processing, until explicit operator revalidation. Preserve the original marker
in quarantine; simply deleting it could recreate the bad note.

Do not remove weekly actions based only on the marker's null action path.
Check actual action references. Remove or annotate only proven generated
references to the invalid note; preserve unrelated or manually edited content.
This incident's local marker records no generated weekly action output.

Record paths, before/after hashes, reason, and reverse steps in the manifest.
Make repair idempotent and perform it only as a separately authorized vault
operation. Wider historical findings remain a read-only review list; absence
of provenance is a reason to investigate, not proof that a note is incorrect.

Roll out code independently of historical repair. If recap validation exposes
an unexpected API shape, leave automatic recaps unavailable and preserve
transcript processing. Do not roll back to arbitrary recap selection.
