from __future__ import annotations

import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True, slots=True)
class GitCommitStatus:
    state: str
    repo_path: Path
    detail: str | None = None


def auto_commit_repo(repo_path: Path, message: str) -> GitCommitStatus:
    git_binary = shutil.which("git")
    if git_binary is None:
        raise RuntimeError("git is required for auto-commit but was not found in PATH.")

    if not is_git_repo(repo_path):
        return GitCommitStatus(state="not_git_repo", repo_path=repo_path)

    if not repo_has_changes(repo_path):
        return GitCommitStatus(state="no_changes", repo_path=repo_path)

    try:
        _run_git(repo_path, "add", "-A")
    except subprocess.CalledProcessError as exc:
        return GitCommitStatus(state="failed", repo_path=repo_path, detail=_git_error_detail(exc))

    if not repo_has_changes(repo_path):
        return GitCommitStatus(state="no_changes", repo_path=repo_path)

    try:
        _run_git(repo_path, "commit", "-m", message)
    except subprocess.CalledProcessError as exc:
        return GitCommitStatus(state="failed", repo_path=repo_path, detail=_git_error_detail(exc))
    return GitCommitStatus(state="committed", repo_path=repo_path)


def is_git_repo(repo_path: Path) -> bool:
    result = _run_git(repo_path, "rev-parse", "--is-inside-work-tree", check=False)
    return result.returncode == 0 and result.stdout.strip() == "true"


def repo_has_changes(repo_path: Path) -> bool:
    result = _run_git(repo_path, "status", "--porcelain", check=False)
    return result.returncode == 0 and bool(result.stdout.strip())


def _run_git(repo_path: Path, *args: str, check: bool = True) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", *args],
        check=check,
        cwd=str(repo_path),
        capture_output=True,
        text=True,
    )


def _git_error_detail(exc: subprocess.CalledProcessError) -> str:
    output = (exc.stderr or exc.stdout or "").strip()
    return output or f"git {' '.join(str(arg) for arg in exc.cmd)} failed with exit code {exc.returncode}"
