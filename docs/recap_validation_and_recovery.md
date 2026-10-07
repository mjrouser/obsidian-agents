# Copilot recap validation and recovery

## Automatic processing

A Copilot recap becomes processor-ready only after its source creation timestamp
matches exactly one calendar occurrence within the existing start-minus-15-minute
to end-plus-30-minute window. More than one qualifying insight is withheld for
review. The existing transcript-first order, fallback grace period, and retry
deadline remain unchanged. No additional service, permission, or dependency is used.

Graph discovery reads all metadata pages before choosing a record. Selected detail
metadata must agree with the listing. Recurring-thread chat is omitted because its
messages are not occurrence-scoped. These checks establish source association,
not actual attendance by the user.

Every new managed fallback has a sibling `<filename>.provenance.json`. It records
the original content hash, source insight metadata, scheduled occurrence, captured
neighboring occurrence windows, and versioned validation policy. Captured context
is limited to the calendar discovery snapshot; it does not prove uniqueness outside
that snapshot. No token or full Graph response belongs in this file.

Both sync and direct `meetings process-bundles` validate the pair. Missing or changed
provenance prevents automatic processing. Old processed notes are not replayed just
because they lack this new sidecar. Transcript upgrades retain their existing rules.

After processing, the durable marker retains `selected_recap`, the original source
hash, archive paths, and the hash of the archived Markdown after its processing
status was added. The archived sidecar describes the original source bytes; its hash
must not be compared directly with the status-prefixed archive without accounting
for that documented mutation. Archive bytes have their own hash in the marker.

## Diagnostics

`recap_diagnostics` in sync output records the selection result and relevant bounds.
Artifact details also report retrieval errors and manual-review requirements.

| Reason | Meaning / next action |
| --- | --- |
| `no_matching_recap` | Complete discovery found no source for this occurrence; ordinary retry rules apply. |
| `invalid_recap_metadata` | Missing or invalid timestamps; do not infer them from the filename. |
| `conflicting_recap_metadata` | Duplicate IDs disagree; investigate the response before reuse. |
| `ambiguous_recap_assignment` | A source matches neighboring occurrence windows; no automatic choice. |
| `multiple_matching_recaps` | Multiple insights match; this version does not merge recap segments. |
| `recap_discovery_incomplete` | Pagination failed or was unsafe; partial results cannot establish a match. |
| `unverified_cached_recap` | Stored content, identity, or policy cannot be verified; preserve it for review. |

Do not delete an old fallback to force processing. Review its original content and
provenance first. A provenance file must never be manufactured using the calendar
date alone. Grace/retry expiry is not permission to accept an unmatched recap.

Discovery-only dry runs defer Graph retrieval. Local processing-plan rendering is
also read-only. Neither is a substitute for validating the live Graph metadata shape.

## Release gate

Run `make check`, `make test`, `make smoke`, `make build`, and `make audit` in the
isolated implementation checkout. Run the synthetic stale-recap regression before
any live probe. Then use the configured Graph endpoint and existing authorization
for a read-only metadata probe. Keep source IDs and private payloads out of committed
fixtures and logs. Use temporary intake storage and do not invoke bundle writes.

Required evidence: metadata has parseable source IDs/timestamps; pagination completes;
the incident's older source is not selected for the later occurrence. Missing
historical data must be recorded as unavailable, not represented as a successful
historical reproduction. Missing required metadata blocks automatic recap deployment.

The initial implementation probe on 2026-10-06 was blocked by
`CERTIFICATE_VERIFY_FAILED` before any recap metadata page was read. It created no
local artifacts and did not save refreshed credentials. **Live validation remains
pending.** Resolve the host's trusted CA configuration and rerun with certificate
verification enabled. Do not deploy this checkout to the scheduled automation until
the gate passes. Do not bypass TLS validation or change Graph API versions to force
the probe through. Local test success does not establish this live contract.

## Targeted repair after deployment

Repair remains a separate authorized operator operation. There is no automatic
historical migration or bulk-deletion command.

1. Inventory the exact canonical note, original fallback, sidecar if present, marker,
   staged bundles, backlinks, and weekly action references. Record a dry-run manifest
   with source/destination paths, hashes, reason, planned changes, and reversal steps.
2. Preserve copies outside canonical meeting and intake scan roots. Coordinate with
   the scheduler and existing bundle/identity locks; recheck the manifest hashes.
3. Before moving any source, preserve the original marker and add a
   `processing_exclusion` record under the same identity lock. Record a reason,
   timezone-aware time, and manifest reference. Any presence of this field blocks
   processing, including malformed content, ordinary retries, and transcript upgrades.
   Normal marker refresh cannot remove or change an existing exclusion.
4. Quarantine the confirmed incorrect canonical note, fallback, and any provenance.
   Leave an explanatory tombstone at the old canonical path with invalidated status
   and a quarantine reference. Do not restate the stale discussion as new meeting facts.
5. Inspect real weekly action files even when the old marker records no action path.
   Change only proven generated references; preserve user edits and unrelated actions.
6. Dry-run sync and direct bundle processing, then verify exclusion enforcement,
   backlinks, original-byte recovery, and rerun idempotence. Record results in the
   repair manifest. Broader findings remain a read-only review list.

Clearing an exclusion requires separately reviewed operator revalidation and a
locked, hash-checked marker edit; it is deliberately not exposed as a force/retry
shortcut. Restore originals using the manifest if repair must be reversed, preserving
the exclusion until the source-to-occurrence association has been revalidated.
