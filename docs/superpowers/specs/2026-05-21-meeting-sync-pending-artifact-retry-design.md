# Meeting Sync Pending Artifact Retry Design

## Goal

Fix the meeting-sync behavior where an early calendar-only bundle can prevent
later runs from checking for transcripts or recap artifacts that publish after
the first sync cycle.

The agreed default is to retry artifact discovery for 24 hours after a meeting
ends. After successful bundle processing, the existing durable processed marker
continues to block future reprocessing.

## Current Problem

`meetings sync-transcripts --download-transcripts` writes a hidden identity
marker when it creates a bundle. The planner treats any existing identity marker
as a reason to skip artifact discovery.

That is correct for already-processed meetings, but too strong for meetings
whose transcript or AI summary has not been generated yet. If Graph returns no
transcript records shortly after the meeting ends, the bundle sidecar records no
preferred processor input. Future sync cycles skip the meeting before artifact
discovery, so the stale missing-artifact state can remain forever.

## Desired Behavior

Calendar-only or otherwise unready bundles stay retryable until one of these
terminal conditions occurs:

- A processor-ready artifact appears and `process-bundles --execute` processes
  it successfully.
- The meeting is more than 24 hours past `end_at` without any transcript-quality
  or recap fallback artifact, at which point network artifact retries stop.
- All usable remote artifact sources are blocked by durable permission or
  meeting-access policy failures.

Within the 24-hour retry window, each scheduled sync cycle should re-run artifact
discovery for the meeting. If a `.vtt`, transcript text, or recap fallback
appears, sync should refresh the bundle note and Outlook metadata sidecar so
`process-bundles` sees a non-null preferred processor input.

## State Model

Use the existing identity marker location, but distinguish marker states by
`source_type`:

- `meeting_sync_pending`: bundle has been staged but is not processed yet.
- `meeting_bundle_processed`: bundle has been successfully processed and should
  not be retried.

Existing `meeting_sync_identity` markers should be treated as pending for
backward compatibility unless they already contain processed-output fields. This
prevents current calendar-only bundles from remaining permanently stuck.

Pending markers should include:

- identity key
- subject
- meeting date
- event ID
- Teams meeting ID
- bundle note path
- metadata sidecar path
- first seen timestamp
- last checked timestamp
- retry-until timestamp, computed as `end_at + 24 hours`
- latest artifact statuses

Processed markers should keep the current behavior and metadata used for
idempotency, cleanup, and auditability.

## Sync Flow

For each eligible ended Teams meeting:

1. Compute the identity marker path.
2. If the marker is `meeting_bundle_processed`, skip as already processed.
3. If the marker is pending and `now <= retry_until`, run artifact discovery.
4. If the marker is pending and `now > retry_until`, run only local artifact
   discovery and skip Graph transcript, recap, and recording calls with an
   explicit retry-expired reason.
5. If there is no marker, run artifact discovery.
6. Render bundle and metadata from the newest artifact discovery result.
7. Write or refresh bundle note and Outlook metadata sidecar when the meeting is
   unprocessed.
8. Write or refresh the pending marker unless the bundle is immediately ready
   and later processed by `process-bundles`.

Bundle and metadata sidecars should no longer be write-once while a meeting is
pending. Refreshing them is required so a newly available artifact can update
`processor_handoff.preferred_input_path`.

## Process Flow

`meetings process-bundles --dry-run` should continue to read sidecar metadata and
report ready versus blocked bundles.

`meetings process-bundles --execute` should continue to process only ready
bundles. On successful processing, it should replace the pending marker with a
`meeting_bundle_processed` marker and clean up machine-managed staging files.

## Expiration Behavior

When a pending meeting passes its 24-hour retry window without a processor-ready
artifact:

- Keep the bundle and metadata sidecar in place.
- Refresh the metadata to show `retry_expired`.
- Stop network retries against Graph for transcript, recap, and recording
  sources.
- Continue cheap local artifact discovery so a manually attached exact-match
  `.vtt`, `.md`, or `.docx` can still unblock the bundle later.
- Report the bundle as blocked with a clear reason:
  `Artifact retry window expired after 24 hours; network artifact discovery was skipped.`

This makes the state visible without deleting evidence. Manual transcript
attachment should still be possible: if a matching local `.vtt`, `.md`, or
`.docx` appears later, `process-bundles --dry-run` can become ready through local
artifact discovery after a sync refresh.

## Permission Blocks

Permission-blocked artifacts should still be visible in bundle source
limitations. Remote sources should not silently retry forever if Graph reports
durable authorization failures such as HTTP 403 for all usable transcript-quality
and fallback sources.

If only some sources are permission-blocked, but another usable source appears
within the retry window, the bundle can proceed with the usable preferred input.

## Tests

Add focused tests for:

- A calendar-only first run writes a pending marker, not a processed marker.
- A pending marker does not prefilter artifact discovery inside the 24-hour
  window.
- A second run refreshes existing bundle and metadata sidecars when a `.vtt`
  appears.
- `process-bundles --dry-run` sees the refreshed preferred input and reports the
  bundle ready.
- Successful execution replaces pending state with `meeting_bundle_processed`.
- A pending marker older than 24 hours skips artifact discovery with a clear
  retry-expired reason.
- Permission-blocked retrieval remains visible and does not create noisy
  infinite retries.

## Out Of Scope

- Webhook or listener-based Graph notifications.
- New recurring schedules.
- New manual dismiss UI.
- Broader changes to meeting eligibility, owner filtering, action rendering, or
  canonical meeting-note formatting.
