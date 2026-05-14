# Action Template Tasks Query Design

## Goal

Add a Tasks plugin query block near the top of weekly action notes so Obsidian shows all open tasks from the current weekly action file before the manually maintained action sections.

## Approved Behavior

- New weekly action notes include this fenced query block immediately after the title:

````
```tasks

path includes {{query.file.path}}

not done

sort by due

```
````

- Existing weekly action notes are migrated when the agent is already updating them with newly inserted actions.
- Existing weekly action notes are not rewritten just to add the query block when no new actions are being inserted.
- The query block is inserted only once and must not break existing action deduplication.

## Implementation Shape

The renderer owns this behavior because it already owns weekly action-note structure, legacy note migration, section preservation, and idempotent inserts. The change should stay in `src/obsidian_intake_agent/rendering/action_renderer.py`, with focused coverage in `tests/test_action_renderer.py`.

## Testing

Tests should cover new-note rendering and on-touch migration of an existing note. Existing idempotency and section-preservation tests should continue to pass.
