# Entra Device-Code Auth Design

## Summary

This design adds first-class Microsoft Entra delegated authentication to the
local Obsidian meeting-ingestion CLI.

The current Graph integration already has Outlook calendar discovery, Teams
transcript discovery/download, Copilot recap fallback, meeting-chat enrichment,
bundle staging, validation-lane processing, and explicit permission/error
reporting. Its main operational gap is authentication: the CLI currently needs a
pre-existing bearer token in `OBSIDIAN_AGENT_GRAPH_ACCESS_TOKEN`.

The recommended change is to add a small local Graph auth layer that uses the
approved Entra public-client app with MSAL device-code login, persists a local
token cache, and lets existing Graph clients keep accepting bearer tokens.

## Goals

- Let an operator authenticate with the approved Entra app from the CLI without
  manually obtaining or pasting bearer tokens.
- Keep Graph API clients focused on API behavior, not token acquisition.
- Preserve `OBSIDIAN_AGENT_GRAPH_ACCESS_TOKEN` as an emergency override and
  backwards-compatible debugging path.
- Provide clear operator commands for login, status inspection, logout, and
  meeting-sync validation.
- Document setup, config keys, required delegated permissions, cache handling,
  and common Graph failure modes.
- Keep secrets and machine-specific values out of committed files.

## Non-Goals

- Switching to application permissions or client-secret/certificate auth.
- Building a web callback login flow.
- Reworking the existing meeting discovery, transcript, recap, chat, bundle, or
  validation-lane behavior.
- Expanding Graph scope beyond meeting-ingestion needs.
- Automatically committing project changes from the app.

## Current Baseline

The repository currently supports Graph-backed meeting ingestion when an access
token is available:

- `main.py` reads the environment variable named by
  `Config.outlook_graph_access_token_env`.
- `_build_meeting_discovery_client()` returns
  `GraphOutlookMeetingDiscoveryClient` only when a token is present.
- `_build_meeting_artifact_discovery_client()` chains Graph transcript,
  fallback-summary, and local transcript clients only when a token is present.
- Without a token, calendar discovery returns a warning-only plan and artifact
  discovery falls back to local intake transcript matching.
- Graph clients already surface many useful permission and retrieval states,
  including `permission_blocked`, missing calendar scopes, missing transcript
  records, and HTTP 401/403 details.

This is a good boundary to preserve: token acquisition should become a new
small dependency of CLI wiring, not a responsibility of each Graph client.

## Recommended Approach

Add a new auth module that wraps MSAL Python public-client authentication.

The CLI should expose:

```bash
.venv/bin/obsidian-agent graph login
.venv/bin/obsidian-agent graph status
.venv/bin/obsidian-agent graph logout
```

Meeting sync should then work without manually exporting a bearer token:

```bash
.venv/bin/obsidian-agent meetings sync-transcripts --since 2026-05-01 --dry-run
```

The auth provider should resolve tokens in this order:

1. Use `OBSIDIAN_AGENT_GRAPH_ACCESS_TOKEN` when it is set.
2. Use a cached account/token silently when possible.
3. Report that Graph auth is unconfigured or expired when no token can be
   acquired silently.

Only `graph login` should initiate the interactive device-code flow. Normal
automation commands should not block waiting for a user to complete login.

## Architecture

### Configuration

Add non-secret Graph auth settings to `Config` and `config.example.yaml`:

- `outlook_graph_tenant_id`
- `outlook_graph_client_id`
- `outlook_graph_scopes`
- `outlook_graph_token_cache_path`
- existing `outlook_graph_access_token_env`
- existing `outlook_graph_api_base_url`

The tenant ID and client ID identify the approved Entra app but are not secrets.
The token cache path is local machine state and should default outside committed
repo content, such as a user cache directory or a `.cache` path that is ignored.

The app must not support or require client secrets for this local delegated
workflow.

### Auth Module

Create `src/obsidian_intake_agent/graph_auth.py` with focused responsibilities:

- Build the MSAL authority URL from tenant ID.
- Build a `PublicClientApplication` from client ID, authority, and token cache.
- Load and persist a serializable token cache.
- Acquire a token silently from cached accounts.
- Start device-code login for `graph login`.
- Remove the cache for `graph logout`.
- Return status information without printing access tokens or refresh tokens.

The module should expose small result types, for example:

- configured or missing config
- env-token override in use
- cache present or absent
- account count
- silent token success or failure
- device-code message for user login

### CLI Wiring

Add a new top-level `graph` command group:

```bash
obsidian-agent graph login
obsidian-agent graph status
obsidian-agent graph logout
```

Expected behavior:

- `graph login` validates that tenant ID, client ID, and scopes are configured,
  prints the device-code instructions returned by MSAL, waits for completion,
  stores the cache, and reports success without printing tokens.
- `graph status` reports whether config is present, whether env override is in
  use, whether cache exists, and whether silent token acquisition succeeds.
- `graph logout` deletes the local token cache file if present and reports the
  path removed.

Meeting sync client builders should use a shared token resolver instead of
directly reading `os.environ`.

### Existing Graph Clients

Keep these clients token-oriented:

- `GraphOutlookMeetingDiscoveryClient`
- `GraphTranscriptDiscoveryClient`
- `GraphTranscriptDownloadClient`
- `GraphMeetingFallbackSummaryClient`

They should continue to accept `access_token` and perform Graph-specific
request/error handling. This keeps unit tests simple and avoids coupling API
parsing to auth mechanics.

## Permissions And Scopes

Document scopes as a practical operator contract, not hidden constants.

Initial recommended delegated scopes:

- `https://graph.microsoft.com/Calendars.Read`
- `https://graph.microsoft.com/OnlineMeetings.Read`
- `https://graph.microsoft.com/OnlineMeetingTranscript.Read.All`
- any additional delegated scope proven necessary for the existing Copilot recap
  or Teams chat fallback endpoints during live validation

Calendar discovery uses Microsoft Graph `calendarView`. Microsoft documents
`Calendars.ReadBasic` as least privileged for calendar view, but this project
needs full meeting context such as body/join URL, organizer, attendees, response
status, and Teams meeting metadata, so `Calendars.Read` remains the intended
calendar scope.

Transcript listing/downloading requires
`OnlineMeetingTranscript.Read.All` for delegated work or school accounts.

Some fallback paths may still return `permission_blocked` even after the initial
approval. That should be documented as a live-validation finding rather than
hidden or treated as a generic failure.

## Documentation Plan

Update documentation in four places:

- `README.md`: setup overview, config keys, and quick-start auth flow.
- `USAGE.md`: copy-pasteable operator commands for login, status, dry run,
  bundle writing, validation execution, logout, and troubleshooting.
- `config.example.yaml`: non-secret Entra settings with safe placeholder values
  and comments warning against committed secrets.
- `docs/meeting_transcript_automation.md`: end-to-end live validation runbook
  using cached auth.

Documentation must cover:

- what the approved Entra app is used for
- what values the operator needs from the Entra app registration
- which values are safe to store in `config.yaml`
- where token cache state is stored
- how to reset auth with `graph logout`
- how env-token override interacts with cached login
- expected `permission_blocked` and missing-scope diagnostics
- the exact validation workflow that writes to `99_Test Notes`

## Testing Strategy

### Unit Tests

Add tests for configuration:

- defaults preserve current no-token behavior when Entra settings are absent
- explicit tenant/client/scopes/cache path load from config
- config example remains parseable

Add tests for auth behavior with fakes:

- env-token override wins and does not inspect the token cache
- silent cached token is returned when MSAL provides one
- missing config returns a safe unavailable result
- failed silent acquisition does not start device-code login during meeting sync
- device-code login prints only MSAL instructions and a success/failure summary
- logout removes only the configured token cache
- status never prints token material

Add tests for CLI builders:

- `_build_meeting_discovery_client()` uses cached auth when available
- `_build_meeting_artifact_discovery_client()` chains Graph clients when cached
  auth is available
- no configured auth still returns `UnconfiguredOutlookMeetingDiscoveryClient`
  and local-only artifact discovery
- env-token override behavior remains backward compatible

### Existing Regression Tests

Run focused tests first:

```bash
PYTHONPATH=src ./.venv/bin/python -m unittest tests.test_config tests.test_main -v
PYTHONPATH=src ./.venv/bin/python -m unittest tests.test_meeting_sync -v
```

Then run the standard project checks:

```bash
make check
make test
make smoke
make build
```

Run `make audit` only if dependencies change. Adding MSAL is a dependency
change, so the implementation must update both `pyproject.toml` and
`requirements.lock`, then run:

```bash
make audit
```

### Live Validation

After implementation, validate against the approved Entra app:

```bash
.venv/bin/obsidian-agent graph status
.venv/bin/obsidian-agent graph login
.venv/bin/obsidian-agent graph status
.venv/bin/obsidian-agent meetings sync-transcripts --since 2026-05-01 --dry-run
.venv/bin/obsidian-agent meetings sync-transcripts --since 2026-05-01 --write-bundles
.venv/bin/obsidian-agent meetings process-bundles --dry-run
.venv/bin/obsidian-agent meetings process-bundles --execute --validation
```

Review:

- calendar candidate discovery
- transcript discovery/download coverage
- Copilot recap fallback behavior
- Teams chat fallback behavior
- permission-blocked sources
- validation-lane meeting notes under `99_Test Notes/Meetings`
- validation-lane Matthew-owned actions under `99_Test Notes/Actions`

## Acceptance Criteria

- An operator can run `graph login` and complete device-code auth with the
  approved Entra app.
- `graph status` reports useful auth state without exposing tokens.
- `graph logout` removes local cached auth state.
- `meetings sync-transcripts` can use cached delegated auth without
  `OBSIDIAN_AGENT_GRAPH_ACCESS_TOKEN`.
- Existing env-token override still works.
- Existing no-token warning behavior still works.
- Documentation includes copy-pasteable setup, validation, and troubleshooting
  commands.
- Tests cover config, auth resolution, CLI wiring, and current Graph behavior.
- Standard checks pass, including `make audit` if MSAL is added.

## References

- MSAL Python token acquisition:
  https://learn.microsoft.com/en-us/entra/msal/python/getting-started/acquiring-tokens
- Microsoft Graph calendar view:
  https://learn.microsoft.com/en-us/graph/api/user-list-calendarview?view=graph-rest-1.0
- Microsoft Graph online meeting lookup:
  https://learn.microsoft.com/en-us/graph/api/onlinemeeting-get?view=graph-rest-1.0
- Microsoft Graph online meeting transcripts:
  https://learn.microsoft.com/en-us/graph/api/onlinemeeting-list-transcripts?view=graph-rest-1.0
- Microsoft Graph permissions reference:
  https://learn.microsoft.com/en-us/graph/permissions-reference?view=graph-rest-1.0

## Open Questions

- Confirm the approved Entra app supports public-client/device-code flow.
- Confirm the tenant ID and client ID values to use in local `config.yaml`.
- Confirm whether live validation shows extra delegated permissions are needed
  for Copilot recap or Teams chat fallback beyond the initial transcript and
  calendar scopes.
