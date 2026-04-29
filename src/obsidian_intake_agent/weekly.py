from __future__ import annotations

import re
import subprocess
from dataclasses import dataclass
from datetime import date, timedelta
from pathlib import Path

from .config import Config
from .utils.dates import monday_of_week
from .utils.fs import safe_write_text

LEADING_DATE = re.compile(r"^(?P<date>\d{4}-\d{2}-\d{2})\b")


@dataclass(slots=True)
class WeeklyRunResult:
    review_path: Path
    changed: bool
    mode: str


def generate_weekly_snapshot(
    config: Config,
    *,
    mode: str,
    target_date: date | None = None,
    dry_run: bool = False,
) -> WeeklyRunResult:
    if mode not in {"briefing", "wrap"}:
        raise ValueError(f"Unsupported weekly mode: {mode}")

    run_date = target_date or date.today()
    monday = monday_of_week(run_date)
    review_dir = config.vault_path / config.weekly_reviews_dir
    review_path = review_dir / _review_filename(monday=monday, mode=mode)
    prompt_path = _prompt_path_for_mode(mode)
    prompt_template = prompt_path.read_text(encoding="utf-8")
    prompt = prompt_template.replace("{{MONDAY_DATE}}", monday.isoformat())
    source_bundle = build_weekly_source_bundle(config, monday=monday, run_date=run_date, mode=mode)
    full_prompt = f"{prompt}\n\nSource material follows. Ground the note in these files only.\n\n{source_bundle}"
    generated = run_codex_markdown(
        full_prompt,
        model=config.codex_model,
        exec_cmd=config.codex_exec_cmd,
        timeout_seconds=config.codex_timeout_seconds,
    ).strip()
    updated = _normalize_review_text(generated)
    existing = review_path.read_text(encoding="utf-8") if review_path.exists() else ""
    changed = updated != existing
    if not dry_run and changed:
        safe_write_text(review_path, updated)
    return WeeklyRunResult(review_path=review_path, changed=changed, mode=mode)


def build_weekly_source_bundle(config: Config, *, monday: date, run_date: date, mode: str) -> str:
    parts: list[str] = []
    actions_path = config.vault_path / config.actions_dir / f"{monday.isoformat()}.md"
    if actions_path.exists():
        parts.append(_render_source(actions_path))

    previous_review = (
        config.vault_path / config.weekly_reviews_dir / _review_filename(monday=monday - timedelta(days=7), mode=mode)
    )
    if previous_review.exists():
        parts.append(_render_source(previous_review))

    meetings_path = config.vault_path / config.meetings_dir
    if meetings_path.exists():
        lookback_start = run_date - timedelta(days=14)
        for path in sorted(meetings_path.glob("*.md")):
            meeting_date = _date_from_filename(path)
            if meeting_date is None:
                continue
            if lookback_start <= meeting_date <= run_date:
                parts.append(_render_source(path))

    if not parts:
        return "No source files were found for this week."
    return "\n\n".join(parts)


def run_codex_markdown(
    prompt: str,
    *,
    model: str | None,
    exec_cmd: list[str] | None,
    timeout_seconds: int | None = None,
) -> str:
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
        raise TimeoutError(f"Codex CLI timed out after {timeout_seconds} seconds while returning markdown.") from exc
    return completed.stdout


def _prompt_path_for_mode(mode: str) -> Path:
    repo_root = Path(__file__).resolve().parents[2]
    if mode == "briefing":
        return repo_root / "prompts" / "monday_weekly_briefing_prompt.md"
    return repo_root / "prompts" / "friday_weekly_wrap_prompt.md"


def _render_source(path: Path) -> str:
    text = path.read_text(encoding="utf-8").strip()
    return f"## Source: {path.name}\n\n{text}"


def _review_filename(*, monday: date, mode: str) -> str:
    suffix = "Weekly Briefing" if mode == "briefing" else "Weekly Wrap"
    return f"{monday.isoformat()} {suffix}.md"


def _date_from_filename(path: Path) -> date | None:
    match = LEADING_DATE.match(path.name)
    if match is None:
        return None
    return date.fromisoformat(match.group("date"))


def _normalize_review_text(text: str) -> str:
    return text.rstrip() + "\n"
