from __future__ import annotations

import subprocess


def run_codex_stdout(
    prompt: str,
    *,
    model: str | None,
    exec_cmd: list[str] | None = None,
    timeout_seconds: int | None = None,
    output_kind: str,
) -> str:
    command = build_codex_command(prompt, model=model, exec_cmd=exec_cmd)
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
        raise TimeoutError(
            f"Codex CLI timed out after {timeout_seconds} seconds while returning {output_kind}."
        ) from exc
    return completed.stdout


def build_codex_command(prompt: str, *, model: str | None, exec_cmd: list[str] | None = None) -> list[str]:
    command = list(exec_cmd or ["codex", "exec"])
    if model:
        command.extend(["--model", model])
    # Prompts embed untrusted transcript and web-clip text, so never let Codex
    # write files or run with network access unless the operator chose a sandbox.
    if not any(arg in ("-s", "--sandbox") or arg.startswith("--sandbox=") for arg in command):
        command.extend(["--sandbox", "read-only"])
    command.append("--skip-git-repo-check")
    command.append(prompt)
    return command
