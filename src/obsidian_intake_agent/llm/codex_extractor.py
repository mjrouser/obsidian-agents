from __future__ import annotations

import json
import subprocess


def run_codex_json(
    prompt: str,
    model: str | None,
    exec_cmd: list[str] | None = None,
    timeout_seconds: int | None = None,
) -> dict:
    command = list(exec_cmd or ["codex", "exec"])
    if model:
        command.extend(["--model", model])
    command.append("--skip-git-repo-check")
    command.append(prompt)

    try:
        completed = subprocess.run(
            command,
            check=True,
            stdout=subprocess.PIPE,
            stderr=None,
            text=True,
            timeout=timeout_seconds,
        )
    except subprocess.TimeoutExpired as exc:
        raise TimeoutError(f"Codex CLI timed out after {timeout_seconds} seconds while returning JSON.") from exc
    stdout = completed.stdout.strip()

    try:
        parsed = json.loads(stdout)
    except json.JSONDecodeError as exc:
        preview = stdout[:500]
        raise ValueError(
            f"Codex CLI did not return valid JSON on stdout. First 500 chars of stdout: {preview!r}"
        ) from exc

    if not isinstance(parsed, dict):
        raise ValueError(f"Codex CLI returned JSON that was not an object. First 500 chars of stdout: {stdout[:500]!r}")

    return parsed
