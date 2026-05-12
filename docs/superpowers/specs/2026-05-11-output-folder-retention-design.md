# Output Folder Retention Design

## Context

The vault output folders `07_Actions` and `09_Weekly Reviews` accumulate dated
Markdown records over time. Matthew created archive subfolders named
`Actions Archive` and `Review & Wrap Archive` and wants each root folder to keep
only the four newest dated records for visual manageability.

The existing application already writes to these folders:

- `MeetingProcessor` creates or updates weekly action notes in `07_Actions`
- `generate_weekly_snapshot` creates weekly briefing and wrap notes in
  `09_Weekly Reviews`

The preferred implementation is to run retention after successful writes in
those existing code paths, rather than adding a separate output-folder watcher.

## Requirements

- Keep the four most recent root-level `.md` records in each managed folder.
- Determine recency from the leading `YYYY-MM-DD` date in the filename.
- Move older dated root-level `.md` records into the configured archive
  subfolder.
- Ignore files without a leading date.
- Ignore files already inside archive subfolders.
- Preserve dry-run safety: dry-run processing must not move archive candidates.
- Avoid overwriting an existing archived file with the same name.
- Keep the behavior configurable with conservative defaults.

## Configuration

Add these reusable settings to `Config` and `config.example.yaml`:

- `actions_archive_dir: "Actions Archive"`
- `weekly_reviews_archive_dir: "Review & Wrap Archive"`
- `archive_retention_count: 4`

Archive directory values are interpreted relative to their parent output folder.
For example, `actions_archive_dir` points to
`<vault_path>/<actions_dir>/Actions Archive`.

## Architecture

Add a small retention module dedicated to output folder cleanup. It should expose
a function that receives:

- the root folder path
- the archive folder name or path
- the retention count
- an optional dry-run flag

The function will scan only immediate child files in the root folder, parse the
leading date, sort newest first, keep the configured count, and move the rest
into the archive directory. It will return a summary so callers and tests can
inspect moved and skipped files.

## Data Flow

For action notes:

1. Meeting processing prepares the weekly action note.
2. If not a dry run and the action note changed, the note is written.
3. Retention runs on the resolved actions folder.
4. Meeting processing continues with intake marker/archive behavior.

For weekly reviews:

1. Weekly generation prepares the briefing or wrap note.
2. If not a dry run and the note changed, the note is written.
3. Retention runs on the configured weekly reviews folder.
4. The weekly result is returned.

## Error Handling

- Missing root folders produce an empty summary.
- Archive folders are created only when a move is needed.
- Existing archived files are not overwritten. The source file remains in the
  root folder and is listed in the skipped collision summary.
- Invalid or undated filenames are ignored.

## Testing

Add focused tests for:

- retaining the four newest dated files by filename date
- ignoring modified time when it conflicts with filename date
- ignoring undated files
- leaving source files in place when an archive collision exists
- weekly generation triggering retention after a successful non-dry-run write
- meeting processing triggering action retention after a successful non-dry-run
  action note write
- dry-run paths not moving files

## Out Of Scope

- Watching `07_Actions` or `09_Weekly Reviews` directly for manual edits
- Recursing through archive folders
- Archiving non-Markdown files
- Auto-committing project changes
