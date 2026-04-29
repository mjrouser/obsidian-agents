from __future__ import annotations

import json

from .codex_cli import run_codex_stdout


def run_codex_json(
    prompt: str,
    model: str | None,
    exec_cmd: list[str] | None = None,
    timeout_seconds: int | None = None,
) -> dict:
    stdout = run_codex_stdout(
        prompt,
        model=model,
        exec_cmd=exec_cmd,
        timeout_seconds=timeout_seconds,
        output_kind="JSON",
    ).strip()

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
