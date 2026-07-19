from __future__ import annotations

import json
import os
import tempfile
import unittest
from copy import deepcopy
from datetime import datetime
from pathlib import Path
from unittest.mock import patch

from obsidian_intake_agent.meetings import transcript_provenance
from obsidian_intake_agent.meetings.transcript_provenance import (
    archive_stale_transcript,
    atomic_write_bytes,
    content_sha256,
    matching_provenance,
    provenance_path,
    write_provenance,
)


class TranscriptProvenanceTests(unittest.TestCase):
    def assert_payload_rejected(self, payload: object) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            transcript_path = Path(tmp_dir) / "meeting.vtt"
            transcript_path.write_bytes(_CONTENT)
            provenance_path(transcript_path).write_text(json.dumps(payload), encoding="utf-8")

            result = matching_provenance(
                transcript_path,
                event_id="evt-1",
                occurrence_start_at=_START_AT,
                occurrence_end_at=_END_AT,
            )

            self.assertIsNone(result)

    def test_matching_provenance_rejects_unexpected_secret_bearing_fields(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            transcript_path = Path(tmp_dir) / "meeting.vtt"
            content = b"WEBVTT\n"
            transcript_path.write_bytes(content)
            provenance_path(transcript_path).write_text(
                json.dumps(
                    {
                        "schema_version": 1,
                        "event_id": "evt-1",
                        "occurrence_start_at": "2026-07-17T17:00:00+00:00",
                        "occurrence_end_at": "2026-07-17T17:30:00+00:00",
                        "transcripts": [{"id": "transcript-1", "created_at": "2026-07-17T17:25:00+00:00"}],
                        "content_sha256": content_sha256(content),
                        "downloaded_at": "2026-07-17T18:00:00+00:00",
                        "access_token": "must-not-be-trusted",
                    }
                ),
                encoding="utf-8",
            )

            result = matching_provenance(
                transcript_path,
                event_id="evt-1",
                occurrence_start_at=datetime.fromisoformat("2026-07-17T17:00:00+00:00"),
                occurrence_end_at=datetime.fromisoformat("2026-07-17T17:30:00+00:00"),
            )

            self.assertIsNone(result)

    def test_matching_provenance_rejects_non_integer_schema_versions(self) -> None:
        for invalid_schema_version in (True, "1", 1.0):
            with self.subTest(schema_version=invalid_schema_version):
                payload = _valid_payload()
                payload["schema_version"] = invalid_schema_version
                self.assert_payload_rejected(payload)

    def test_matching_provenance_rejects_empty_transcript_list(self) -> None:
        payload = _valid_payload()
        payload["transcripts"] = []

        self.assert_payload_rejected(payload)

    def test_matching_provenance_rejects_blank_transcript_id(self) -> None:
        payload = _valid_payload()
        payload["transcripts"] = [{"id": "  ", "created_at": None}]

        self.assert_payload_rejected(payload)

    def test_matching_provenance_rejects_non_string_transcript_id(self) -> None:
        payload = _valid_payload()
        payload["transcripts"] = [{"id": 123, "created_at": None}]

        self.assert_payload_rejected(payload)

    def test_matching_provenance_rejects_invalid_top_level_timestamps(self) -> None:
        for field in ("occurrence_start_at", "occurrence_end_at", "downloaded_at"):
            for invalid_timestamp in ("", "not-a-date", "2026-07-17T17:00:00", 123):
                with self.subTest(field=field, timestamp=invalid_timestamp):
                    payload = _valid_payload()
                    payload[field] = invalid_timestamp
                    self.assert_payload_rejected(payload)

    def test_matching_provenance_rejects_invalid_transcript_timestamps(self) -> None:
        for invalid_timestamp in ("", "not-a-date", "2026-07-17T17:25:00", 123):
            with self.subTest(timestamp=invalid_timestamp):
                payload = _valid_payload()
                payload["transcripts"] = [{"id": "transcript-1", "created_at": invalid_timestamp}]
                self.assert_payload_rejected(payload)

    def test_matching_provenance_rejects_entire_list_when_one_transcript_is_malformed(self) -> None:
        payload = _valid_payload()
        payload["transcripts"] = [
            {"id": "transcript-1", "created_at": "2026-07-17T17:25:00+00:00"},
            {"id": "", "created_at": None},
        ]

        self.assert_payload_rejected(payload)

    def test_matching_provenance_rejects_malformed_json(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            transcript_path = Path(tmp_dir) / "meeting.vtt"
            transcript_path.write_bytes(_CONTENT)
            provenance_path(transcript_path).write_text("{not-json", encoding="utf-8")

            result = matching_provenance(
                transcript_path,
                event_id="evt-1",
                occurrence_start_at=_START_AT,
                occurrence_end_at=_END_AT,
            )

            self.assertIsNone(result)

    def test_matching_provenance_rejects_hash_mismatch(self) -> None:
        payload = _valid_payload()
        payload["content_sha256"] = "0" * 64

        self.assert_payload_rejected(payload)

    def test_matching_provenance_rejects_invalid_hash_encoding(self) -> None:
        for invalid_hash in ("a" * 63, "A" * 64, "g" * 64, 123):
            with self.subTest(content_sha256=invalid_hash):
                payload = _valid_payload()
                payload["content_sha256"] = invalid_hash
                self.assert_payload_rejected(payload)

    def test_write_provenance_rejects_extra_transcript_content_fields(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            transcript_path = Path(tmp_dir) / "meeting.vtt"

            with self.assertRaisesRegex(ValueError, "Invalid transcript provenance"):
                write_provenance(
                    transcript_path,
                    event_id="evt-1",
                    occurrence_start_at=_START_AT,
                    occurrence_end_at=_END_AT,
                    transcripts=[
                        {
                            "id": "transcript-1",
                            "created_at": "2026-07-17T17:25:00+00:00",
                            "content": "must-not-be-written",
                        }
                    ],
                    content=_CONTENT,
                )

            self.assertFalse(provenance_path(transcript_path).exists())

    def test_write_provenance_rejects_invalid_transcript_fields(self) -> None:
        invalid_records: tuple[dict[str, object], ...] = (
            {"id": "", "created_at": None},
            {"id": "transcript-1", "created_at": ""},
            {"id": "transcript-1", "created_at": "2026-07-17T17:25:00"},
            {"id": "transcript-1", "created_at": 123},
        )
        for record in invalid_records:
            with self.subTest(record=record), tempfile.TemporaryDirectory() as tmp_dir:
                transcript_path = Path(tmp_dir) / "meeting.vtt"

                with self.assertRaisesRegex(ValueError, "Invalid transcript provenance"):
                    write_provenance(
                        transcript_path,
                        event_id="evt-1",
                        occurrence_start_at=_START_AT,
                        occurrence_end_at=_END_AT,
                        transcripts=[record],
                        content=_CONTENT,
                    )

                self.assertFalse(provenance_path(transcript_path).exists())

    def test_atomic_write_fsyncs_parent_directory_after_replace(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            target_path = Path(tmp_dir) / "meeting.vtt"
            events: list[str] = []
            real_replace = os.replace

            def replace(source: str | Path, target: str | Path) -> None:
                events.append("replace")
                real_replace(source, target)

            with (
                patch.object(transcript_provenance.os, "replace", side_effect=replace),
                patch.object(
                    transcript_provenance,
                    "_fsync_directory",
                    side_effect=lambda path: events.append("directory_fsync"),
                    create=True,
                ),
            ):
                atomic_write_bytes(target_path, _CONTENT)

            self.assertEqual(events, ["replace", "directory_fsync"])

    def test_archive_fsyncs_directory_after_hard_link(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            transcript_path = root / "meeting.vtt"
            archive_dir = root / "archive"
            events: list[str] = []
            real_link = os.link

            def link(source: str | Path, target: str | Path) -> None:
                events.append("link")
                real_link(source, target)

            with (
                patch.object(transcript_provenance.os, "link", side_effect=link),
                patch.object(
                    transcript_provenance,
                    "_fsync_directory",
                    side_effect=lambda path: events.append("directory_fsync"),
                    create=True,
                ),
            ):
                archive_path = archive_stale_transcript(
                    transcript_path,
                    archive_dir=archive_dir,
                    content=_CONTENT,
                )

            self.assertEqual(archive_path.read_bytes(), _CONTENT)
            self.assertEqual(events, ["link", "directory_fsync"])

    def test_atomic_write_removes_temporary_file_when_replace_fails(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            target_path = Path(tmp_dir) / "meeting.vtt"

            with (
                patch.object(transcript_provenance.os, "replace", side_effect=OSError("replace failed")),
                self.assertRaisesRegex(OSError, "replace failed"),
            ):
                atomic_write_bytes(target_path, _CONTENT)

            self.assertFalse(target_path.exists())
            self.assertEqual(list(Path(tmp_dir).iterdir()), [])

    def test_archive_collision_never_overwrites_different_bytes(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            transcript_path = root / "meeting.vtt"
            archive_dir = root / "archive"
            archive_dir.mkdir()
            archive_path = archive_dir / f"meeting-{content_sha256(_CONTENT)[:8]}.vtt"
            archive_path.write_bytes(b"DIFFERENT")

            with self.assertRaisesRegex(ValueError, "archive collision"):
                archive_stale_transcript(
                    transcript_path,
                    archive_dir=archive_dir,
                    content=_CONTENT,
                )

            self.assertEqual(archive_path.read_bytes(), b"DIFFERENT")

    def test_archive_removes_temporary_file_when_hard_link_fails(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            archive_dir = root / "archive"

            with (
                patch.object(transcript_provenance.os, "link", side_effect=OSError("link failed")),
                self.assertRaisesRegex(OSError, "link failed"),
            ):
                archive_stale_transcript(
                    root / "meeting.vtt",
                    archive_dir=archive_dir,
                    content=_CONTENT,
                )

            self.assertEqual(list(archive_dir.iterdir()), [])


_CONTENT = b"WEBVTT\n"
_START_AT = datetime.fromisoformat("2026-07-17T17:00:00+00:00")
_END_AT = datetime.fromisoformat("2026-07-17T17:30:00+00:00")


def _valid_payload() -> dict[str, object]:
    return deepcopy(
        {
            "schema_version": 1,
            "event_id": "evt-1",
            "occurrence_start_at": _START_AT.isoformat(),
            "occurrence_end_at": _END_AT.isoformat(),
            "transcripts": [{"id": "transcript-1", "created_at": "2026-07-17T17:25:00+00:00"}],
            "content_sha256": content_sha256(_CONTENT),
            "downloaded_at": "2026-07-17T18:00:00+00:00",
        }
    )


if __name__ == "__main__":
    unittest.main()
