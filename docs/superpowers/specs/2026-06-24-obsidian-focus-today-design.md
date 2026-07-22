# Obsidian Focus Today Design

## Goal

Create a low-friction Obsidian-native workflow that preserves the current
`07_Actions` system of record while adding a single `Focus Today` dashboard that
surfaces manually selected high-priority tasks by life/work area.

The target behavior is:

- keep existing task capture and weekly action-note structure intact
- categorize tasks with lightweight inline area tags
- manually promote up to two tasks per area into a temporary "selected today" state
- work from one focus note instead of scanning the full backlog
- let selected tasks disappear naturally from the focus view when completed

## Non-Goals

- redesign the current `07_Actions` note structure
- require every task in the vault to be categorized
- automatically infer the user's top priorities
- build a second task system outside Obsidian
- enforce hard limits through custom code in v1

## Design Summary

Use two independent metadata axes on tasks:

1. `area` classification via inline tags
2. `focus selection` via a custom Tasks status

Area tags are durable. The selected-today status is temporary.

The design keeps source tasks in their original notes and uses one dedicated
`Focus Today` note as a dashboard of Tasks queries. The note contains one single
query for all selected tasks, with task selection happening in the weekly
action note rather than in the dashboard.

## Task Model

### Area Tags

Use exactly one of these inline tags on any task that should participate in the
focus workflow:

- `#area/sales`
- `#area/delivery`
- `#area/admin`
- `#area/home`
- `#area/personal`

Rules:

- a task should have at most one `#area/...` tag
- area tags are required before a task can be promoted into the selected-today
  state
- uncategorized tasks are allowed to exist in the normal workflow

### Focus Selection Status

Create one custom Tasks status for "selected today". Example symbol and name:

- symbol: `>`
- name: `Selected Today`
- type: `TODO`

Example tasks:

```md
- [ ] Follow up with CBiz on migration scope #area/sales
- [>] Draft Walgreens workshop readout #area/delivery
- [ ] Clean up project admin follow-ups #area/admin
- [ ] Schedule HVAC check #area/home
```

Behavioral intent:

- `[ ]` means open backlog work
- `[>]` means intentionally selected for near-term focus
- `[x]` means done

### Status Transition

The selected-today status must toggle directly to done when completed.

Desired status flow:

`TODO` -> `Selected Today` -> `DONE`

This prevents stale "today" markers from lingering after completion. The area
tag remains because it is durable classification, but the temporary focus state
disappears as part of normal task completion.

## Focus Note

Create one note, such as `Focus Today.md`, to act purely as a dashboard.

The note should not be a place where tasks are rewritten or duplicated. It
should contain Tasks queries that surface tasks from their original files.

Recommended query shape:

````md
```tasks
not done
status.name includes Selected Today
sort by priority
sort by due
```
````

The focus note should contain only this single list. It is an execution surface,
not a review surface. Area tags matter at selection time in the weekly action
note, but they should not create multiple visible lists in the dashboard.

## Operating Rhythm

### Capture

- capture tasks where they already belong
- do not require immediate categorization unless obvious

### Daily Planning

- review the weekly action note as needed
- add area tags to tasks that are realistic candidates for today
- promote at most two tasks per area into `Selected Today`
- use `Focus Today.md` only as the single working list after selection is done

### Execution

- work from `Focus Today.md`
- mark tasks done directly from the query results
- completed selected tasks disappear from the focus view because they are now
  done

### Reset

- move leftover selected-today tasks back to normal open status if they are no
  longer today's priorities
- do not let the selected-today state accumulate across many days

## Friction Management

### V1 Assumptions

- area tagging is manual
- status selection is manual
- the focus note is query-only and single-list
- the user enforces the two-per-area limit

### Why Manual First

Manual selection is intentional. It preserves the key property of the original
sticky-note workflow: active choice.

Manual area tagging is acceptable in v1 because:

- there are only five area tags
- not every task needs immediate categorization
- only tasks being considered for focus need area tags

### Future Enhancements

Potential later improvements, without changing the core model:

- QuickAdd or similar helper to append one of the five area tags quickly
- a small hotkey or modal workflow to speed up area tagging
- light automation or review queries to flag selected tasks missing area tags
- refinement of the uncategorized query once real vault behavior is observed

These are explicitly optional follow-ons, not prerequisites for the workflow.

## Practical Constraints And Risks

### Tasks Query Semantics

Tasks supports filtering by tags, status, path, heading, and grouping. Because
the focus note is intentionally a single positive-match query on selected status,
the design avoids relying on more fragile negative-tag review logic in the
dashboard itself.

### Incomplete Categorization

Some tasks will remain uncategorized. This is acceptable as long as:

- the focus workflow only depends on categorizing active candidates
- categorization happens where the user is already selecting work, in the weekly
  action note

### Over-Selection

Because v1 does not enforce the two-item limit programmatically, the main risk
is visual sprawl from selecting too many tasks. The design mitigates this by:

- keeping selection in the weekly action note
- keeping the dashboard to one small list
- treating the selected-today state as temporary

## Recommended Initial Setup

1. Configure one custom Tasks status:
   - symbol: `>`
   - name: `Selected Today`
   - type: `TODO`
   - next status on completion: `DONE`
2. Adopt the five area tags:
   - `#area/sales`
   - `#area/delivery`
   - `#area/admin`
   - `#area/home`
   - `#area/personal`
3. Create one `Focus Today.md` dashboard note with:
   - one Tasks query for all selected-today tasks
4. Start with manual tagging and manual focus selection.
5. Observe actual friction for one to two weeks before adding helpers.

## Definition Of Success

The design is successful if:

- the user can keep the current `07_Actions` workflow
- the user can manually select no more than two active tasks per area
- the user can work from a single note instead of scanning a full backlog
- completing a selected task removes it from the focus view naturally
- category tagging feels light enough that the system is used consistently

## References

- Tasks plugin tags: <https://publish.obsidian.md/tasks/Getting+Started/Tags>
- Tasks plugin custom statuses: <https://publish.obsidian.md/tasks/How+To/Set+up+custom+statuses>
- Tasks plugin quick reference: <https://publish.obsidian.md/tasks/Quick+Reference>
