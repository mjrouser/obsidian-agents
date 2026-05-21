# Meeting Transcript Automation

This note captures the intended path for removing manual Teams summary copy/paste
from the intake workflow.

## Goal

For every meeting Matthew attends:

1. Prefer automatic Teams recording/transcription.
2. Preserve the raw transcript artifact, ideally `.vtt`.
3. Enrich the transcript with Outlook calendar context.
4. Feed the existing intake processor so canonical meeting notes and weekly
   actions continue to use the same rendering, dedupe, dry-run, and archive
   behavior.

## Current Local Foundation

- `.vtt` intake files already run through structured extraction and preserve the
  raw transcript unchanged in the archive.
- Meeting notes now include YAML front matter for Obsidian properties.
- The front matter has explicit slots for:
  - `participants`
  - `organizer`
  - `attendees`
  - `outlook_event_id`
  - `teams_meeting_id`
  - `transcript_id`
  - `intake_file`
- Extracted meeting notes include explicit `Context` and `Source` sections when
  the extractor returns that information. The `Source` section records attendance
  confidence, sources used, and source limitations such as missing chat, missing
  recap, partial transcript visibility, or unconfirmed attendance.

## Connector Findings

The Outlook calendar connector can read calendar event context for meetings
Matthew attends, including subject, organizer, attendees, Teams join body, and
event IDs. That makes Outlook the right source for missing note context.

The available Teams connector is useful for messages and chat/channel lookup,
but it does not currently expose a direct transcript-download action. Transcript
sync will likely need Microsoft Graph access outside the installed Teams
connector.

## Microsoft Graph Auth And Probe Path

Use the local device-code auth flow before probing live meeting sync behavior.
The approved Entra app is a public client: configure its tenant ID and client ID
in local `config.yaml`, then run `obsidian-agent graph login` to create the
MSAL token cache used by unattended sync jobs. Tenant IDs and public-client
client IDs are not credentials, but keep real organization/app values in local
config rather than committed examples so app registrations can be rotated or
replaced without repo changes.

Relevant Microsoft Graph APIs:

- `onlineMeeting: getAllTranscripts`
  - Docs: https://learn.microsoft.com/en-us/graph/api/onlinemeeting-getalltranscripts
  - Useful for delta-style transcript sync.
  - Current limitation: organized meetings only.
  - Permission model: application permission `OnlineMeetingTranscript.Read.All`.
- `callTranscript: get` and `/content`
  - Docs: https://learn.microsoft.com/en-us/graph/api/calltranscript-get
  - Useful for fetching individual transcript metadata and transcript content.
  - Supports delegated work/school permission `OnlineMeetingTranscript.Read.All`
    for online meetings.

Auth and probe sequence:

1. Confirm local `config.yaml` has `outlook_graph_tenant_id` and
   `outlook_graph_client_id`.
2. Run `obsidian-agent graph status`; if no cached account is available, run
   `obsidian-agent graph login` and complete the browser/device-code prompt.
3. Pick one completed meeting Matthew organized and one completed meeting
   Matthew attended but did not organize.
4. Run `obsidian-agent meetings sync-transcripts --since YYYY-MM-DD --dry-run`
   across that small window and confirm Outlook discovery, Teams meeting
   resolution, and transcript source states.
5. Run the same window with `--download-transcripts` only after the dry-run
   source states look correct.
6. Record whether the tenant allows delegated access, requires application
   access, or blocks non-organizer transcript retrieval.

If the cached access token expires, the app attempts a silent MSAL refresh. If
there is no refreshable auth state, sync exits nonzero with
`graph_auth_required` and prints manual recovery commands. In launchd, that
failure also creates the action-needed failure note and best-effort desktop
notification.

## Auto-Recording Strategy

Preferred strategy:

- Tenant/admin policy, meeting template, or sensitivity label enforces automatic
  recording/transcription for all target meetings.

Fallback strategy:

- A pre-meeting automation patches meetings Matthew organizes to enable Teams
  recording/transcription when Graph permissions allow it.

Known limitation:

- Meetings organized by someone else may not be patchable by Matthew. For those,
  the post-meeting sync should still import transcripts that already exist, and
  should report meetings where no transcript was available.

## Future CLI Shape

Proposed command:

```bash
./.venv/bin/obsidian-agent meetings sync-transcripts --since YYYY-MM-DD --dry-run
```

Expected behavior:

- Read completed Outlook meetings in the target window.
- Skip non-meeting blocks unless explicitly configured otherwise.
- Resolve Teams meeting identifiers from Outlook metadata.
- Download available transcript content into
  `00_Intake/bundles/raw_transcripts`.
- Preserve raw transcript artifacts and avoid duplicate downloads using stored
  transcript IDs.
- Write bundle notes plus Outlook metadata sidecars into `00_Intake/bundles`.
- Let the existing intake processor create canonical notes and action updates.
- In dry-run mode, print which meetings would import, which are missing
  transcripts, and which would be skipped as already imported.

Current implementation status in this repo:

- `obsidian-agent meetings sync-transcripts --since YYYY-MM-DD --dry-run` now
  exists as discovery/planning-only CLI plumbing.
- When Entra device-code auth is configured, the command uses the local MSAL
  cache to query Microsoft Graph `me/calendarView` and turns returned events
  into internal Outlook meeting candidates. A temporary token from
  `OBSIDIAN_AGENT_GRAPH_ACCESS_TOKEN` can override the cache for debugging, but
  should not be treated as the normal unattended path.
- The command builds a source bundle for each candidate, marks Outlook calendar
  metadata as the currently available source, and preserves the intended source
  priority for transcript, chat, recap, and manual fallback artifacts.
- For meetings that would be processed, the dry run also plans a concrete
  intake bundle note path and renders the future bundle note content shape with
  attendance confidence, sources used, source limitations, Outlook event ID,
  Teams meeting ID, organizer, attendee list, response status, and join-link
  context when available from Outlook metadata.
- `obsidian-agent meetings sync-transcripts --write-bundles` now materializes
  only those planned bundle notes into `00_Intake/bundles`, writes a sibling
  raw Outlook metadata JSON sidecar for each processable meeting, preserves
  existing bundle files, and backfills the metadata sidecar when a prior bundle
  note exists without it.
- `obsidian-agent meetings sync-transcripts --download-transcripts` now
  downloads available Teams `.vtt` transcript content from Microsoft Graph into
  `00_Intake/bundles/raw_transcripts`, preserves existing local transcript
  files, writes matching bundle notes, and records the downloaded transcript
  path as the preferred processor handoff.
- When transcript `metadataContent` exists without downloadable `.vtt` content,
  the same sync path writes a Markdown transcript artifact into
  `00_Intake/bundles/raw_transcripts` and uses that local file as the preferred
  processor handoff.
- When transcript-quality sources are missing, recap-based fallback artifacts
  are staged under `00_Intake/bundles/fallbacks` and can become the preferred
  processor handoff, with chat preserved only as supplemental context rather
  than as a standalone processor source.
- The sync path also writes a hidden pending meeting identity marker keyed from
  Outlook event ID plus Teams meeting ID under
  `00_Intake/bundles/_meeting_sync/identities`.
- Calendar-only bundles remain pending, not final, for 24 hours after the
  meeting ends. During that window each polling cycle refreshes artifact
  discovery and rewrites bundle metadata so a late Teams `.vtt`, transcript
  text, or recap fallback can become the preferred processor input.
- After 24 hours, sync stops Graph network retries for that meeting but keeps
  local exact-match artifact discovery available for manual transcript
  attachment.
- Dry-run output now rolls up processable meetings by remaining source gaps so
  the polling summary can show how many meetings are still calendar-only versus
  missing transcript, chat, or recap artifacts, and it now breaks those source
  states down explicitly as `available`, `missing`, `permission_blocked`, or
  `not_attempted` for each processable meeting source.
- Bundle source limitations now preserve the underlying retrieval detail from
  Graph or local discovery, making permission failures and unpublished
  transcripts visible without opening the raw metadata sidecar.
- The planner now performs a real local discovery pass for transcript artifacts
  already sitting in `00_Intake`, marking canonical date/title-matched `.vtt`,
  `.md`, and `.docx` files as available transcript sources before any network
  retrieval is added.
- When local transcript discovery does not find an exact date/title match, the
  missing-source detail now reports the expected local transcript filename stem
  and any same-date transcript candidates as suggestions only.
- When local transcript discovery finds a match, the planner now carries the
  exact local artifact path forward into the bundle note artifact plan and the
  raw Outlook metadata sidecar so later ingestion steps can reuse the same
  source file without rediscovering it.
- The bundle contract now also records a preferred processor handoff input when
  one of those local transcript artifacts is available, including the selected
  source type and exact file path the next ingestion step should process.
- `obsidian-agent meetings process-bundles --dry-run` now reads those written
  Outlook metadata sidecars and reports which bundles are ready to hand to the
  existing intake processor versus blocked by calendar-only state, missing local
  files, or intake artifacts the current processor would skip as already
  processed.
- `obsidian-agent meetings process-bundles --execute` now uses that same
  readiness contract to process only ready bundles through the existing intake
  processor and reports per-bundle `processed`, `skipped`, or `failed` results
  without attempting blocked bundles.
- After a successful `process-bundles --execute` run, the bundle executor
  writes a durable processed marker in
  `00_Intake/bundles/_meeting_sync/identities` and removes machine-managed
  staging files for that meeting: the bundle note, the Outlook metadata
  sidecar, and any bundle-managed transcript or fallback artifact under
  `00_Intake/bundles/raw_transcripts` or `00_Intake/bundles/fallbacks`. It
  does not delete unrelated intake files or manually managed transcript files.
- `obsidian-agent meetings process-bundles --execute --validation` uses the
  same readiness contract and source-selection behavior, but redirects generated
  canonical meeting notes and Matthew-owned action updates into
  `99_Test Notes/Meetings` and `99_Test Notes/Actions`.
- Planning currently skips canceled, declined-without-content, all-day-without-content,
  focus-without-content, non-Teams, meetings whose processed identity marker
  already exists, and not-yet-ended events with explicit reasons.
- The v1 eligibility filter also treats `response_status` values of `declined`,
  `none`, and `notResponded` as ineligible, and skips low-signal office-hours
  subjects before artifact retrieval starts.
- Direct chat retrieval and richer recap expansion are still future work beyond
  the current fallback staging flow.

## Live Validation Lane

Use validation mode after inspecting a dry-run window when you want to process a
small representative sample without updating production meeting notes or weekly
actions.

```bash
obsidian-agent graph status
obsidian-agent graph login
obsidian-agent meetings sync-transcripts --since YYYY-MM-DD --dry-run
obsidian-agent meetings process-bundles --execute --validation
```

This mode preserves transcript, fallback, and filtering semantics. Only the
canonical outputs are redirected into `99_Test Notes/`.

For auth-specific validation, force the cache-cleared path in a non-production
window:

```bash
obsidian-agent graph logout
obsidian-agent meetings sync-transcripts --since YYYY-MM-DD --dry-run
obsidian-agent graph login
```

The cache-cleared sync should report `graph_auth_required` with recovery steps.
When run through launchd, the same failure should create or update
`_System/Agent Errors/ACTION NEEDED - Latest Automation Failure.md` and send the
desktop notification.

## Manual Local Transcript Lane

When Graph transcript permissions are unavailable, place a local `.vtt`, `.md`,
or `.docx` transcript in `00_Intake` using the expected meeting filename, or
attach it to an existing bundle with `meetings attach-transcript`. Near-match
candidates are diagnostic only; the system processes exact matches or explicit
attachments.

## Extraction Standard

The extraction prompt intentionally borrows the strongest quality rules from
Josh's batch workflow:

- Process the full available transcript before writing.
- Preserve named attribution when a person's viewpoint, concern, decision, or
  action materially matters.
- Distinguish explicit decisions from directional alignment, preferences,
  unresolved discussion, or inferred signals.
- Include only transcript-evidenced participants as confirmed participants.
- When attendance is known only from the calendar invite, include:
  `Known from calendar invite; attendance not guaranteed.`
- State source limitations directly instead of hiding missing transcript, chat,
  recap, or attendance visibility.

## Extraction Fallback Monitoring

When Codex CLI times out during `.vtt` extraction, the processor falls back to
heuristic extraction and keeps the automation running. The generated meeting note
records the fallback as a source limitation, and the intake watcher records a
`vtt_extraction_fallback` warning in `logs/intake-watcher.log`.

Use this query to inspect fallback volume and patterns:

```bash
rg -n "vtt_extraction_fallback" logs/intake-watcher.log
```

Chunked transcript extraction remains a potential feature under evaluation. Use
the fallback warning volume, affected meeting lengths, and note-quality impact to
decide whether the added complexity is justified.
