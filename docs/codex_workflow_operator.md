# Codex Operator Workflow (Fast Loop)

1. Start Codex in repo root

2. Run QA (dry-run)
- check extracted actions
- check Matthew routing
- check dedupe + rerun

3. If FAIL
- apply smallest fix
- add test
- rerun full suite

4. Run idempotency check

5. If bug was real
- add regression test

Key rule:
Always look at extracted actions, not just final output.
