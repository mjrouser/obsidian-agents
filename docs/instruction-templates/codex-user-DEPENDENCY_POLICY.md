# Dependency Policy

Use this file when deciding whether to add, replace, or upgrade a dependency.

## Default Stance

- Do not add dependencies casually.
- Prefer the standard library, platform APIs, or existing project dependencies
  when they solve the problem cleanly enough.
- Treat auth, networking, code execution, and data-parsing dependencies as
  higher-risk than ordinary libraries.

## Approval Heuristics

Before adding a dependency, check that it:

- solves a real problem that is not already handled well in the project
- is actively maintained
- has recent releases or visible maintenance activity
- has a compatible license
- is reasonably trusted for the use case
- does not introduce an unnecessarily heavy dependency tree
- does not require risky install scripts or native binaries unless justified

For packages touching auth, networking, or secrets, also check for known
vulnerabilities when project tooling supports it, such as `npm audit`,
`pip-audit`, `uv`, or the repo's existing audit command.

## Change Hygiene

- Update the manifest and lockfile together when the project uses a lockfile.
- Prefer the project's existing package manager and audit workflow.
- If a vulnerability is accepted temporarily, document the reason instead of
  ignoring it silently.
- If a package choice is non-obvious, include a brief rationale in the change
  summary.
