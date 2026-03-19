from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import shutil
import subprocess


@dataclass(frozen=True, slots=True)
class GitCommitStatus:
    state: str
    repo_path: Path


def auto_commit_repo(repo_path: Path, message: str) -> GitCommitStatus:
    git_binary = shutil.which("git")
    if git_binary is None:
        raise RuntimeError("git is required for auto-commit but was not found in PATH.")

    if not is_git_repo(repo_path):
        return GitCommitStatus(state="not_git_repo", repo_path=repo_path)

    if not repo_has_changes(repo_path):
        return GitCommitStatus(state="no_changes", repo_path=repo_path)

    _run_git(repo_path, "add", "-A")

    if not repo_has_changes(repo_path):
        return GitCommitStatus(state="no_changes", repo_path=repo_path)

    _run_git(repo_path, "commit", "-m", message)
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
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
