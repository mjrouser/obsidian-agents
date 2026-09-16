from __future__ import annotations

from datetime import date
from pathlib import Path

from obsidian_intake_agent.meetings.organization import (
    MeetingOrganizationSummary,
    meeting_output_path,
    organize_meetings,
)


def test_meeting_output_path_uses_year_and_english_month_name(tmp_path: Path) -> None:
    meetings_root = tmp_path / "01_Meetings"

    result = meeting_output_path(
        meetings_root,
        meeting_date="2026-08-12",
        basename="2026-08-12 - Teams - Planning.md",
    )

    assert result == meetings_root / "2026" / "08_August" / "2026-08-12 - Teams - Planning.md"


def test_organize_meetings_dry_run_reports_only_notes_through_cutoff(tmp_path: Path) -> None:
    vault = tmp_path / "vault"
    meetings_root = vault / "01_Meetings"
    meetings_root.mkdir(parents=True)
    july_note = meetings_root / "2026-07-15 - Teams - July Sync.md"
    august_note = meetings_root / "2026-08-12 - Teams - August Sync.md"
    undated_note = meetings_root / "Meeting Notes.md"
    for path in (july_note, august_note, undated_note):
        path.write_text(f"{path.name}\n", encoding="utf-8")

    summary = organize_meetings(
        vault_path=vault,
        meetings_root=meetings_root,
        through=date(2026, 7, 31),
        dry_run=True,
    )

    assert isinstance(summary, MeetingOrganizationSummary)
    assert summary.would_move == [
        (july_note, meetings_root / "2026" / "07_July" / july_note.name),
    ]
    assert summary.skipped_undated == [undated_note]
    assert summary.skipped_after_cutoff == [august_note]
    assert july_note.exists()
    assert not (meetings_root / "2026").exists()


def test_organize_meetings_moves_notes_and_updates_exact_markdown_links(tmp_path: Path) -> None:
    vault = tmp_path / "vault"
    meetings_root = vault / "01_Meetings"
    meetings_root.mkdir(parents=True)
    note = meetings_root / "2026-07-15 - Teams - July Sync.md"
    note.write_text("meeting\n", encoding="utf-8")
    actions = vault / "07_Actions" / "2026-07-13.md"
    actions.parent.mkdir(parents=True)
    actions.write_text(
        "- [ ] Follow up [[01_Meetings/2026-07-15 - Teams - July Sync.md#Action Items|July]]\n"
        "- [ ] Also follow up [[01_Meetings/2026-07-15 - Teams - July Sync#Action Items|July]]\n"
        "- [ ] Leave [[01_Meetings/other-note.md]] unchanged\n",
        encoding="utf-8",
    )

    summary = organize_meetings(
        vault_path=vault,
        meetings_root=meetings_root,
        through=date(2026, 7, 31),
        dry_run=False,
    )

    destination = meetings_root / "2026" / "07_July" / note.name
    assert summary.moved == [(note, destination)]
    assert not note.exists()
    assert destination.exists()
    assert actions.read_text(encoding="utf-8") == (
        "- [ ] Follow up [[01_Meetings/2026/07_July/2026-07-15 - Teams - July Sync.md#Action Items|July]]\n"
        "- [ ] Also follow up [[01_Meetings/2026/07_July/2026-07-15 - Teams - July Sync#Action Items|July]]\n"
        "- [ ] Leave [[01_Meetings/other-note.md]] unchanged\n"
    )


def test_organize_meetings_skips_destination_collision_without_overwriting(tmp_path: Path) -> None:
    vault = tmp_path / "vault"
    meetings_root = vault / "01_Meetings"
    meetings_root.mkdir(parents=True)
    source = meetings_root / "2026-07-15 - Teams - July Sync.md"
    source.write_text("source\n", encoding="utf-8")
    destination = meetings_root / "2026" / "07_July" / source.name
    destination.parent.mkdir(parents=True)
    destination.write_text("existing\n", encoding="utf-8")

    summary = organize_meetings(
        vault_path=vault,
        meetings_root=meetings_root,
        through=date(2026, 7, 31),
        dry_run=False,
    )

    assert summary.moved == []
    assert summary.conflicts == [(source, destination)]
    assert source.read_text(encoding="utf-8") == "source\n"
    assert destination.read_text(encoding="utf-8") == "existing\n"


def test_organize_meetings_repairs_extensionless_links_to_existing_nested_notes(tmp_path: Path) -> None:
    vault = tmp_path / "vault"
    meetings_root = vault / "01_Meetings"
    destination = meetings_root / "2026" / "07_July" / "2026-07-15 - Teams - July Sync.md"
    destination.parent.mkdir(parents=True)
    destination.write_text("meeting\n", encoding="utf-8")
    actions = vault / "07_Actions" / "2026-07-13.md"
    actions.parent.mkdir(parents=True)
    actions.write_text(
        "- [ ] Follow up [[01_Meetings/2026-07-15 - Teams - July Sync#Action Items|July]]\n",
        encoding="utf-8",
    )

    summary = organize_meetings(
        vault_path=vault,
        meetings_root=meetings_root,
        through=date(2026, 7, 31),
        dry_run=False,
    )

    assert summary.moved == []
    assert summary.updated_reference_files == [actions]
    assert actions.read_text(encoding="utf-8") == (
        "- [ ] Follow up [[01_Meetings/2026/07_July/2026-07-15 - Teams - July Sync#Action Items|July]]\n"
    )


def test_organize_meetings_renames_legacy_month_folder_and_updates_links(tmp_path: Path) -> None:
    vault = tmp_path / "vault"
    meetings_root = vault / "01_Meetings"
    legacy_note = meetings_root / "2026" / "July" / "2026-07-15 - Teams - July Sync.md"
    legacy_note.parent.mkdir(parents=True)
    legacy_note.write_text("meeting\n", encoding="utf-8")
    actions = vault / "07_Actions" / "2026-07-13.md"
    actions.parent.mkdir(parents=True)
    actions.write_text(
        "- [ ] Follow up [[01_Meetings/2026/July/2026-07-15 - Teams - July Sync#Action Items|July]]\n",
        encoding="utf-8",
    )

    summary = organize_meetings(
        vault_path=vault,
        meetings_root=meetings_root,
        through=date(2026, 7, 31),
        dry_run=False,
    )

    renamed_folder = meetings_root / "2026" / "07_July"
    renamed_note = renamed_folder / legacy_note.name
    assert summary.renamed_month_folders == [(legacy_note.parent, renamed_folder)]
    assert not legacy_note.parent.exists()
    assert renamed_note.exists()
    assert actions.read_text(encoding="utf-8") == (
        "- [ ] Follow up [[01_Meetings/2026/07_July/2026-07-15 - Teams - July Sync#Action Items|July]]\n"
    )
