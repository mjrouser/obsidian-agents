# Occurrence-Safe Copilot Recaps Implementation Plan

Date: 2026-10-06
Status: Implemented locally through Task 7; Task 8 live verification is blocked by certificate trust. Manual publication and Task 9 remain pending.

Design: [Occurrence-Safe Copilot Recaps](../specs/2026-10-06-occurrence-safe-copilot-recaps-design.md)

## Outcome and working rules

Prevent an old recurring-meeting recap from becoming a note or action for a new
calendar occurrence. Validate fresh and cached recaps, preserve source provenance,
and keep rejected occurrences pending under existing retry rules.

Use Python 3.11, unittest, and existing dependencies. No new AI calls, permissions,
services, or dependency changes are planned. Read every target module's current
code before editing; function names below are navigation anchors, not fixed line
numbers. Implement tasks in order, with failing regression tests before behavior
changes. Do not auto-commit, push, merge, or repair production vault data.

The design is merged via PR #64. Its status line still says Proposed; do not infer
that the requested plan authorizes implementation or historical repair.

## Task 0 — Isolate implementation and capture the baseline

Files: existing `AGENTS.md`, `Makefile`, `config.example.yaml`, and test fixtures.

- [x] Inspect `git status`, current branch, and applicable instructions. Preserve
  user changes. Start implementation on `codex/fix-occurrence-safe-copilot-recaps`
  in an isolated checkout/worktree, because scheduled local scripts can execute
  the active checkout. Do not repoint launchd or copy live `config.yaml` there.
- [x] Use a separate virtual environment with the existing lockfile; use temporary
  vaults and fake Graph clients for all development tests. Do not run the live
  meeting-sync wrapper as a test.
- [x] Run the baseline checks below and record commit, interpreter, and results.
  A missing localhost permission is an environment failure, not a product defect.
- [x] Confirm the incident repair remains a separate post-deployment operation.

```bash
make check
make test
make smoke
make build
make audit
```

Exit: isolated development checkout, reproducible baseline, no live vault writes.

## Task 1 — Implement pure recap metadata validation and selection

Create: `src/obsidian_intake_agent/meetings/recap_selection.py` and
`tests/test_recap_selection.py`.
Read/reuse: `meetings/occurrence_assignment.py`.

- [x] Add synthetic tests for the original failure: July source metadata, October
  requested occurrence, same series, no selected insight. Confirm failure before
  implementing the selector.
- [x] Define small immutable candidate/result records: insight ID, UTC source
  timestamps, optional call/correlation IDs, selected candidate, per-candidate
  rejection reasons, and conflicting occurrence IDs.
- [x] Reject missing IDs, missing/naive/malformed creation timestamps, invalid
  supplied end timestamps, and end-before-creation. Normalize valid offsets to UTC.
- [x] Use existing `selection_bounds()` and `OccurrenceWindow` context; do not
  represent insight IDs as transcript IDs or change transcript selection behavior.
- [x] Select exactly one distinct insight uniquely matching the requested
  occurrence. Reject overlaps and multiple qualifying insights. Deduplicate exact
  repeats; block conflicting metadata for the same ID. An overlapping candidate
  blocks the target even when a second candidate matches it uniquely.
- [x] Test inclusive boundaries, midnight/offset conversion, multiple same-day
  occurrences, irregular recurrence, rescheduling, response-order independence,
  and missing optional end/correlation fields.
- [x] Add detail-versus-list validation: require matching ID and creation time;
  reject contradictory optional fields and empty summary/action content.

```bash
PYTHONPATH=src .venv/bin/python -m unittest discover -s tests -p 'test_recap_selection.py' -v
```

Exit: deterministic selection, no I/O, complete rejection reasons.

## Task 2 — Replace arbitrary Graph recap selection

Modify: `meetings/sync.py` (`GraphMeetingFallbackSummaryClient`,
`_latest_ai_insight`, `_meetings_with_occurrence_context`).
Create: `tests/test_recap_discovery.py`; update affected fixtures in
`tests/test_meeting_sync.py`.

- [x] Add fake-response integration tests proving the old recap detail is never
  requested for a later occurrence, and a valid later-page candidate is selected.
- [x] Replace `_latest_ai_insight()` with complete metadata discovery followed by
  Task 1 selection and validated detail retrieval. Pass occurrence context before
  eligibility filtering so skipped neighboring occurrences still detect ambiguity.
- [x] Follow all pages. Validate HTTPS origin and effective port against the
  configured Graph endpoint before sending credentials; reject userinfo, cycles,
  malformed pages, and off-origin links. Do not select from partial results after
  a pagination failure. Reuse existing retry/error handling where applicable.
- [x] Cache complete series listings and fetched details only within one sync run;
  clear them between runs. Never promote partial listings to successful cache entries.
- [x] Remove automatic recurring-thread chat enrichment from this fallback path;
  render its unavailability explicitly. Preserve independent chat-source behavior.
- [x] Update valid recap fixtures to include actual source metadata. Keep deliberate
  legacy/malformed fixtures to test refusal instead of making all fixtures trusted.
- [x] Test network errors, duplicate/conflicting IDs, empty content, mismatched
  detail, missing required fields, one failed occurrence alongside one valid one,
  and transcript-priority behavior with no unnecessary recap calls.

```bash
PYTHONPATH=src .venv/bin/python -m unittest discover -s tests -p 'test_recap_discovery.py' -v
PYTHONPATH=src .venv/bin/python -m unittest discover -s tests -p 'test_meeting_sync.py' -v
```

Exit: Graph recap content enters the plan only after unique occurrence validation.

## Task 3 — Persist and validate recap provenance

Create: `meetings/recap_provenance.py`, `tests/test_recap_provenance.py`.
Modify: `meetings/sync.py` artifact records, `_persist_planned_artifact_content`,
`write_planned_bundle_notes`, and metadata serializers.

- [x] Define a versioned sidecar schema for `<fallback>.provenance.json`: occurrence
  and series identity, resolved online meeting ID, source IDs/timestamps, scheduled
  and selection bounds, discovery bounds, retrieval time, policy version, outcome,
  and original Markdown SHA-256. Validate types and timezone-aware timestamps.
- [x] Persist the occurrence context needed to replay unique assignment offline,
  not only the target's timestamps. Missing/inconsistent context is unverified.
  Record that this is uniqueness within a captured discovery snapshot.
- [x] Plan Markdown and sidecar together; dry-run must not create either. Carry
  selected recap metadata into bundle metadata using additive optional fields.
- [x] Reuse owned-path/symlink checks and available identity locking. Atomically
  write each file; reject incomplete pairs. Preserve competing creates and verify
  the winner rather than overwriting it or combining files from different runs.
- [x] Test missing/corrupt/unknown-version sidecars, mismatched identities and
  hashes, stale bounds, traversal/symlinks, crashes between writes, and races.
- [x] Hash original source bytes before processor status mutation. Define separate
  original-source and archived-file hash fields; never substitute one for the other.

```bash
PYTHONPATH=src .venv/bin/python -m unittest discover -s tests -p 'test_recap_provenance.py' -v
```

Exit: a source file is reusable only with matching, supported provenance.

## Task 4 — Close local-cache and direct-processing bypasses

Modify: `meetings/sync.py` local and chained discovery;
`meetings/process_bundles.py` (`_load_bundle_metadata_record`,
`_processor_ready_metadata_validation_reasons`, `_plan_bundle_processing_item`,
`_execute_bundle_processing_plan_locked`).
Test: `tests/test_meeting_sync.py`, `tests/test_meeting_process_bundles.py`.

- [x] Add a test where an old recap already exists at the expected filename; it
  must not be accepted without validated sidecar metadata.
- [x] Add direct `process-bundles` tests with stale staged metadata and no preceding
  sync. Validate bytes/provenance before invoking the processor or writing actions.
- [x] Revalidate immediately before execution under the existing transaction;
  a source or marker changed after planning must defer safely.
- [x] Prevent chained/local discovery from replacing a rejected managed recap with
  filename-only availability or relabeling it as a manual summary.
- [x] Preserve unverified or edited files. A valid newly fetched source colliding
  with such a file requires manual review and must not overwrite the existing pair.
- [x] Test independent explicit manual intake still works, while managed fallback
  paths cannot exploit that source type to bypass validation.

Exit: every automatic managed-recap handoff enforces the same trust contract.

## Task 5 — Preserve provenance through processing, cleanup, and upgrades

Modify: `meetings/process_bundles.py` (`_write_durable_processed_marker`,
`_bundle_meeting_context`, `_cleanup_bundle_staging`, `_bundle_cleanup_paths`),
`meetings/identity_state.py`, and applicable canonical metadata/rendering code.
Test: bundle processing, identity-state, meeting-rendering, and upgrade tests.

- [x] Persist selected recap identity and original hash in the durable marker
  before staging cleanup. Add insight ID and source timestamp to canonical front
  matter without claiming attendance; keep source limitations visible.
- [x] Archive source and sidecar with recoverable paths and correct archived hash.
  Preserve enough durable data to inspect provenance after transient metadata removal.
- [x] Simulate failure at archive/marker/cleanup boundaries. Reruns must recover
  without duplicate actions, new canonical notes, or lost provenance.
- [x] Verify pre-existing processed markers remain readable and do not replay
  solely because recap fields are absent. Preserve the existing transcript upgrade
  retry window and edited-note protection.
- [x] Verify Matthew/Matt ownership normalization, source normalization, backlinks,
  idempotent insertion, unrelated action preservation, and dry-run messaging.

```bash
PYTHONPATH=src .venv/bin/python -m unittest discover -s tests -p 'test_meeting_process_bundles.py' -v
PYTHONPATH=src .venv/bin/python -m unittest discover -s tests -p 'test_meeting_identity_state.py' -v
PYTHONPATH=src .venv/bin/python -m unittest discover -s tests -p 'test_meeting_upgrade.py' -v
```

Exit: provenance survives the full lifecycle and reruns remain safe.

## Task 6 — Diagnostics, bounded retries, and end-to-end regression

Modify: sync plan rendering, artifact metadata, relevant CLI tests;
`README.md`, `docs/meeting_transcript_automation.md`.

- [x] Report candidate count, source timestamps/ID, selection/discovery bounds,
  conflicts, structured rejection reason, and cache outcome. Avoid credentials,
  raw payloads, and misleading attendance language.
- [x] Implement the design's `available`/`missing`/`not_attempted` mapping; maintain
  grace and retry deadlines without resetting them on every rejection.
- [x] Run a synthetic stale-recap scenario through sync, explicit bundle write,
  processing, and rerun. Assert no canonical note, action, or processed marker.
  A pending marker is allowed during explicit writes, never during dry-run.
- [x] Run the positive equivalent, including rerun and late transcript upgrade.
- [x] Snapshot temporary vault files before/after discovery-only and processing
  dry runs; require byte-identical state and explicitly deferred remote validation.
- [x] Test expiry and one blocked occurrence alongside an independently valid one.
- [x] Document unavailable recap behavior, legacy cache review, omitted chat,
  multiple-insight refusal, and live validation prerequisites.

Exit: operator output explains rejection and the incident regression passes end to end.

## Task 7 — Add durable exclusion support required for incident repair

Modify: `meetings/identity_state.py`, sync eligibility and bundle processing.
Test: identity-state, sync, and bundle-processing tests; document in the runbook.

- [x] Define an additive `processing_exclusion` record with reason, time, and repair
  manifest reference. Keep the original marker payload recoverable. Recognized
  exclusions override both fresh sync and direct staged-bundle processing.
- [x] Check exclusions during planning and again before execution. Rejected or
  malformed exclusions must not silently authorize processing.
- [x] Require explicit operator revalidation to clear an exclusion; ordinary retry
  expiry, force flags, and late transcript discovery must not clear it automatically.
- [x] Test a quarantined occurrence cannot regenerate its note, even with stale
  bundles present, while unrelated occurrences remain processable.

Exit: production repair can suppress recreation without deleting identity history.

## Task 8 — Release validation and manual publication

- [x] Run all five baseline commands after focused tests pass. No new dependency
  is expected; `make audit` remains required before a PR.
- [ ] Perform a read-only Graph probe of the configured endpoint from the isolated
  checkout using existing auth. Inspect only needed metadata and selection results;
  do not persist tokens or raw responses to the repository or vault. Validate field
  shape, pagination, and rejection of available old records for the affected date.
- [ ] If historical data is unavailable, record that limitation; retain the
  synthetic regression as proof of selection behavior, not as proof of a live
  historical response. If required fields are absent, do not enable automatic
  recap writes. No API-version or permission escalation to make the probe pass.
- [ ] Review the final diff for production data, secret leakage, unrelated changes,
  and accidental configuration edits. Present exact manual commit/push/PR commands
  using the actual implementation branch and changed files. Wait for both Python
  CI jobs before giving the final manual merge command.
- [ ] Deploy only reviewed code after the read-only validation gate. Because the
  scheduler uses the operational checkout, updating that checkout is deployment;
  do not treat a local branch switch as a harmless testing step.

Exit: code validated locally and in CI, reviewed and manually merged; no historical
vault repair implied. Preserve unavailable-recap behavior if rollback is needed,
rather than restoring arbitrary recap selection.

## Task 9 — Separately authorized repair of the October 2 occurrence

Deliverable: a targeted repair manifest and operator procedure, outside automatic
intake scanning. Execute only after implementation is deployed and repair is
explicitly authorized. No general bulk-repair framework is needed for this fix.

- [ ] Dry-run inventory the exact canonical note, archived fallback, available
  sidecars, durable marker, staged bundles, backlinks, and weekly action references.
- [ ] Build a reviewable manifest of source/destination paths, hashes, reason,
  expected edits, and rollback steps. Recheck hashes immediately before mutation.
- [ ] Coordinate with the existing scheduler/identity locks to prevent concurrent
  processing. Preserve the original marker, then establish the exclusion before
  moving the invalid source or canonical note.
- [ ] Preserve originals in a quarantine outside meeting/intake scan roots. Leave
  an explanatory tombstone at the canonical path with invalidated status and a
  quarantine reference. Do not retain the old discussion as October meeting facts.
- [ ] Check actual weekly notes despite the marker's null action path. Change only
  proven generated references; do not remove user edits or unrelated actions.
- [ ] Verify rerun idempotence, exclusion enforcement in both paths, preserved
  backlinks, and recoverable original bytes. Record outcomes in the manifest.
- [ ] Any wider historical audit produces a read-only review list. Missing
  provenance alone is not evidence that a historical note is wrong.

Exit: the confirmed incorrect note is invalidated and cannot be regenerated;
original evidence and an explicit recovery path remain available.

## Completion checklist

- [ ] Tasks 0–8 completed with test evidence and manual publication.
- [ ] No automatic recap can bypass occurrence validation via local cache or bundles.
- [ ] Existing transcript processing, retries, upgrades, and action dedupe preserved.
- [ ] No production permissions, dependencies, or live config changed unexpectedly.
- [ ] Task 9 recorded separately as pending authorization or completed with manifest.


## Execution record

- Isolated managed worktree on `codex/fix-occurrence-safe-copilot-recaps`; operational
  checkout remains on main. No live config was copied and no vault repair ran.
- Baseline: 576 tests plus check, smoke, build, and audit passed on Python 3.11.
- Final local validation: `make check`, 599 tests, `make smoke`, `make build`,
  and `make audit` passed on Python 3.11. The audit found no known vulnerabilities.
  Tests cover selection, provenance, Graph discovery, direct processing, archive
  rollback, dry runs, exclusions, and the stale-source end-to-end regression.
  Implementation remains uncommitted.
- Read-only live probe: TLS certificate-chain verification failed before the first
  metadata page. No source files or token-cache updates were written. Deployment is
  blocked until trusted CA configuration is resolved and the probe is repeated.
- Tasks 8 (live gate / manual publication) and 9 (authorized historical repair) are
  intentionally unfinished. No successful live Graph verification is claimed.
