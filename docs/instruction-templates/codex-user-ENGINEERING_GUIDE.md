# Engineering Guide

Use this file as colder reference material. It captures longer-lived default
engineering preferences that do not need to sit in the main context on every
task.

## Code Organization

- Prefer cohesive, purpose-specific files and modules.
- Split files when they have clearly separable responsibilities, not just
  because they are long.
- Keep related code close together when that improves comprehension.
- Avoid unnecessary indirection. A future reader should be able to trace
  behavior without opening many tiny wrapper files.
- Favor domain-specific names over generic containers.

## Refactoring

- Keep refactoring scoped to the current task unless broader work is approved.
- Small local cleanup is good when it makes the requested change safer or
  clearer.
- Large reorganizations, sweeping renames, or dependency swaps should usually
  be proposed before being done.

When flagging a larger refactor, use:

- `Area:`
- `Issue:`
- `Benefit:`
- `Effort: Low / Medium / High`
- `Timing: Now / Soon / Later`

## Testing

- Prefer tests that describe externally visible behavior.
- Place tests near the behavior they verify, or follow the project's existing
  test layout.
- Test proportionally to the stakes:
  - quick experiments: basic error handling and a smoke test
  - unattended automation: logging and dry-run support at minimum
  - real data or external services: cover happy path and key failure modes

## Documentation

- Keep docs aligned with setup, commands, configuration, architecture, and
  user-facing behavior.
- Use project-level READMEs for workflows and boundaries, not obvious
  implementation trivia.
- Prefer concise documentation that explains purpose, decisions, and
  constraints.

## Finish Checklist

Before calling work done, sanity-check:

- each edited file is still about one main thing
- names are clear
- docs still match behavior
- relevant checks were run
- the diff is clean and intentional
