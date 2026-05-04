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

## Microsoft Graph Path To Probe

Use Graph Explorer or a small local probe before building the full sync command.

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

Probe sequence:

1. Pick one completed meeting Matthew organized and one completed meeting Matthew
   attended but did not organize.
2. From Outlook, capture the event ID, organizer, join URL, and meeting thread or
   meeting ID if present in the body.
3. Test whether Graph can resolve the online meeting from the join URL or meeting
   ID.
4. Test transcript listing and transcript content download for both cases.
5. Record whether the tenant allows delegated access, requires application
   access, or blocks non-organizer transcript retrieval.

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
- Download available transcript content into `00_Intake`.
- Preserve raw transcript artifacts and avoid duplicate downloads using stored
  transcript IDs.
- Write a small sidecar/context file or processor context object with Outlook
  attendee metadata.
- Let the existing intake processor create canonical notes and action updates.
- In dry-run mode, print which meetings would import, which are missing
  transcripts, and which would be skipped as already imported.

Current implementation status in this repo:

- `obsidian-agent meetings sync-transcripts --since YYYY-MM-DD --dry-run` now
  exists as discovery/planning-only CLI plumbing.
- When `OBSIDIAN_AGENT_GRAPH_ACCESS_TOKEN` is present, the command queries
  Microsoft Graph `me/calendarView` and turns returned events into internal
  Outlook meeting candidates.
- The command builds a source bundle for each candidate, marks Outlook calendar
  metadata as the currently available source, and preserves the intended source
  priority for transcript, chat, recap, and manual fallback artifacts.
- For meetings that would be processed, the dry run also plans a concrete
  intake bundle note path and renders the future bundle note content shape with
  attendance confidence, sources used, source limitations, Outlook event ID,
  and Teams meeting ID.
- Planning currently skips canceled, declined-without-content, all-day-without-content,
  focus-without-content, non-Teams, and not-yet-ended events with explicit
  reasons.
- Transcript download, chat export, recap retrieval, and `00_Intake` writes are
  still future work.

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
