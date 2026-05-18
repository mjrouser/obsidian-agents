# Web Clipper Design

## Purpose

Add a local web clipping workflow for Matthew's Obsidian second brain. The
workflow should make it easy to save useful passages from web pages with a short
"why this matters" note, then later surface those saved references in weekly
briefings, wraps, action context, and on-demand search.

The feature has two main goals:

1. Frictionless saving while reading.
2. Useful resurfacing when related work comes up.

## Goals

- Capture one or more passages from a single web page with low friction.
- Preserve captured passages exactly as source evidence.
- Capture Matthew's reason for saving the material.
- Store raw captures in a dedicated intake subfolder.
- Process raw captures into canonical reference notes.
- Surface relevant processed clips in weekly briefings and wraps first.
- Leave room for action-note surfacing and semantic retrieval later.
- Preserve enough structure to support on-demand search later.

## Non-Goals

- Building a browser extension in the first version.
- Adding a paid API, embeddings database, or external service requirement for
  capture.
- Perfect semantic retrieval in the first version.
- Creating concept notes automatically in the first version.
- Implementing an on-demand search or briefing command in the first version.
- Inserting clip links inline beside individual action items before matching
  quality is proven.

## User Workflow

The first version uses a browser bookmarklet plus a local capture endpoint.

1. Matthew clicks a "Clip to Obsidian" bookmarklet while reading a page.
2. The page enters clip mode and shows a small capture panel.
3. Matthew highlights a passage and adds it to the capture.
4. Matthew can repeat the highlight-and-add step for multiple passages on the
   same page.
5. Matthew writes one short "why this matters" note for the capture bundle.
6. The bookmarklet sends the page URL, page title, captured passages, note, and
   timestamp to the local endpoint.
7. The endpoint writes one raw Markdown note to `00_Intake/Web Clips/`.

The capture unit is the source page, not the concept. This keeps saving fast and
avoids requiring Matthew to classify the idea perfectly while reading. Later
processing can infer topics and, in a future slice, suggest concept notes when
patterns emerge across sources.

## Vault Structure

Raw web clip captures live in:

```text
00_Intake/Web Clips/
```

Processed reference notes live in:

```text
10_References/Web Clips/
```

Future concept notes may live in:

```text
10_References/Concepts/
```

The exact concept-note folder can be revisited when that feature is designed.

## Raw Capture Note Format

Raw capture notes are mechanical, source-oriented, and easy to debug.

```markdown
---
type: web_clip_intake
status: unprocessed
captured_at: 2026-05-18T10:30:00-04:00
source_url: "https://example.com/article"
source_title: "Article Title"
---

# Article Title

Source: https://example.com/article

## Why This Matters

Short note from Matthew.

## Captured Passages

> First selected passage.

> Second selected passage.
```

Captured passages must be preserved verbatim. Processing can summarize around
them, but it must not rewrite them as though the rewritten text were source
evidence.

## Processed Reference Note Format

Processed notes are useful reference artifacts while still retaining source
fidelity.

```markdown
---
type: web_clip
source_url: "https://example.com/article"
source_title: "Article Title"
captured_at: 2026-05-18T10:30:00-04:00
topics:
  - stakeholder alignment
  - CRM adoption
related:
  - "[[2026-05-18 - Teams - Example Meeting]]"
---

# Article Title

## Why This Matters

Original note preserved here.

## Summary

Short source-grounded summary.

## Captured Passages

Original excerpts preserved verbatim.

## Application

How this might apply to Matthew's work.

## Related Context

Links to relevant meetings, actions, or weekly reviews when known.
```

The processed note should include both reference and application sections. The
reference sections preserve what was clipped and where it came from. The
application section makes the clip easier to use in briefings, wraps, and future
work.

## Architecture

The feature should be implemented as a sibling pipeline to meeting intake.

Proposed responsibilities:

- `web_clips/capture_server.py`: local-only HTTP endpoint for bookmarklet
  captures.
- `processors/web_clip_processor.py`: raw intake parsing, processed-state
  detection, canonical note generation, dry-run output, and processed marking or
  archiving.
- `rendering/web_clip_renderer.py`: raw and processed Markdown rendering.
- Weekly generation code: retrieval of a small set of relevant processed clips
  and inclusion in briefing/wrap prompt context.

The meeting processor should not absorb web clip behavior. Meeting intake and
web clip intake have different source formats, output folders, and surfacing
rules, so separate modules keep each workflow easier to reason about and test.

## Matching And Surfacing

The first retrieval version should be simple, local, and debuggable.

During processing, the agent infers lightweight topics and application notes
from:

- captured passages
- source title
- source URL
- Matthew's "why this matters" note

During weekly briefing and wrap generation, the agent compares processed clip
metadata and note text against the current weekly context. Relevant context may
include current actions, recent meeting summaries or titles, and the existing
briefing/wrap source material.

Briefings and wraps may include up to 3-5 relevant clips in a small "Relevant
Saved Clips" block. Each surfaced clip should include:

- link to the processed reference note
- source title
- one-sentence reason it may matter now

Action-note surfacing should come after weekly surfacing proves useful. The
first action-note version should prefer a weekly "Relevant References" block
over inline insertion beside individual tasks, because inline matches can
clutter the working task list if retrieval is noisy.

On-demand search or briefing over saved clips is part of the target direction,
but should be designed after the canonical note shape and weekly surfacing path
are proven.

## Semantic Retrieval Later

The first version should not require embeddings or a semantic index. It should,
however, store enough structured metadata to add semantic retrieval later:

- stable source URL
- captured timestamp
- canonical processed note path
- inferred topics
- preserved passages
- application notes

Future semantic retrieval can use the same canonical notes as source documents
without changing the capture workflow.

## Safety And Privacy

- The capture server binds to localhost only.
- The server rejects non-local requests where practical.
- The server writes only inside configured vault subfolders.
- Page titles are sanitized before becoming filenames.
- Captured passages are preserved exactly.
- Processing supports dry-run mode before writing canonical notes.
- Capture itself does not require paid APIs or external services.
- LLM-based topic and application inference, if used, follows the project's
  existing provider configuration and must be optional or clearly surfaced.

## Configuration

Add reusable configuration keys to `config.example.yaml` when implementation
begins. Likely keys:

- `web_clips_intake_dir`: defaults to `00_Intake/Web Clips`
- `web_clips_references_dir`: defaults to `10_References/Web Clips`
- `web_clips_capture_host`: defaults to `127.0.0.1`
- `web_clips_capture_port`: default chosen during implementation
- `web_clips_max_weekly_results`: defaults to 3 or 5

`config.yaml` remains local and untracked.

## Testing

Focused tests should cover:

- raw clip Markdown rendering
- processed reference note rendering
- filename sanitization from page titles
- raw intake parsing
- processed-state detection
- idempotent processing
- dry-run behavior
- preservation of captured passages
- weekly briefing/wrap inclusion without breaking existing prompt behavior

A smoke path should process a representative clip in a temporary vault. Manual
validation should test the bookmarklet against a local test page.

## GitHub Issue Timing

Create GitHub issues after the design spec is reviewed and the implementation
plan is written. The design establishes scope, but the implementation plan gives
cleaner issue boundaries.

A reasonable workflow is:

1. Approve this design spec.
2. Write the implementation plan.
3. Create one GitHub epic issue for the web clipper.
4. Create focused implementation issues from the plan, such as capture endpoint,
   bookmarklet UI, web clip processor, note rendering, weekly surfacing, and
   tests.

## Open Decisions For Implementation Planning

- Exact local capture port.
- Whether raw captures are marked processed in place or archived after canonical
  note creation.
- Whether initial topic/application inference uses the existing Codex CLI path
  or starts with deterministic heuristics.
- Exact command names for starting the capture server and processing web clips.
