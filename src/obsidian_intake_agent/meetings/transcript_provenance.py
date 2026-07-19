from __future__ import annotations

import hashlib
import json
import os
import tempfile
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import TypedDict, cast

SCHEMA_VERSION = 1


class TranscriptProvenanceRecord(TypedDict):
    id: str
    created_at: str | None


class TranscriptProvenancePayload(TypedDict):
    schema_version: int
    event_id: str
    occurrence_start_at: str
    occurrence_end_at: str
    transcripts: list[TranscriptProvenanceRecord]
    content_sha256: str
    downloaded_at: str


def content_sha256(content: bytes) -> str:
    return hashlib.sha256(content).hexdigest()


def provenance_path(transcript_path: Path) -> Path:
    return Path(f"{transcript_path}.provenance.json")


def matching_provenance(
    transcript_path: Path,
    *,
    event_id: str,
    occurrence_start_at: datetime,
    occurrence_end_at: datetime,
) -> TranscriptProvenancePayload | None:
    try:
        content = transcript_path.read_bytes()
        payload = json.loads(provenance_path(transcript_path).read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        return None
    if not _is_valid_payload(payload):
        return None
    if payload["event_id"] != event_id:
        return None
    if payload["occurrence_start_at"] != occurrence_start_at.isoformat():
        return None
    if payload["occurrence_end_at"] != occurrence_end_at.isoformat():
        return None
    if payload["content_sha256"] != content_sha256(content):
        return None
    return cast(TranscriptProvenancePayload, payload)


def write_provenance(
    transcript_path: Path,
    *,
    event_id: str,
    occurrence_start_at: datetime,
    occurrence_end_at: datetime,
    transcripts: Sequence[Mapping[str, object]],
    content: bytes,
) -> None:
    payload = {
        "schema_version": SCHEMA_VERSION,
        "event_id": event_id,
        "occurrence_start_at": occurrence_start_at.isoformat(),
        "occurrence_end_at": occurrence_end_at.isoformat(),
        "transcripts": [dict(transcript) for transcript in transcripts],
        "content_sha256": content_sha256(content),
        "downloaded_at": datetime.now(UTC).isoformat(),
    }
    if not _is_valid_payload(payload):
        raise ValueError("Invalid transcript provenance payload.")
    rendered = (json.dumps(payload, indent=2, sort_keys=True) + "\n").encode("utf-8")
    atomic_write_bytes(provenance_path(transcript_path), rendered)


def archive_stale_transcript(transcript_path: Path, *, archive_dir: Path, content: bytes) -> Path:
    archive_path = archive_dir / f"{transcript_path.stem}-{content_sha256(content)[:8]}.vtt"
    archive_dir.mkdir(parents=True, exist_ok=True)
    if archive_path.exists():
        if archive_path.read_bytes() != content:
            raise ValueError(f"Stale transcript archive collision at {archive_path}")
        return archive_path

    file_descriptor, temporary_name = tempfile.mkstemp(prefix=f".{archive_path.name}.", dir=archive_dir)
    temporary_path = Path(temporary_name)
    try:
        with os.fdopen(file_descriptor, "wb") as temporary_file:
            temporary_file.write(content)
            temporary_file.flush()
            os.fsync(temporary_file.fileno())
        try:
            os.link(temporary_path, archive_path)
            _fsync_directory(archive_dir)
        except FileExistsError:
            if archive_path.read_bytes() != content:
                raise ValueError(f"Stale transcript archive collision at {archive_path}") from None
    finally:
        temporary_path.unlink(missing_ok=True)
    return archive_path


def atomic_write_bytes(path: Path, content: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    file_descriptor, temporary_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    temporary_path = Path(temporary_name)
    try:
        with os.fdopen(file_descriptor, "wb") as temporary_file:
            temporary_file.write(content)
            temporary_file.flush()
            os.fsync(temporary_file.fileno())
        os.replace(temporary_path, path)
        _fsync_directory(path.parent)
    finally:
        temporary_path.unlink(missing_ok=True)


def _is_valid_payload(payload: object) -> bool:
    if not isinstance(payload, dict):
        return False
    expected_fields = {
        "schema_version",
        "event_id",
        "occurrence_start_at",
        "occurrence_end_at",
        "transcripts",
        "content_sha256",
        "downloaded_at",
    }
    if set(payload) != expected_fields:
        return False
    schema_version = payload.get("schema_version")
    if type(schema_version) is not int or schema_version != SCHEMA_VERSION:
        return False
    event_id = payload.get("event_id")
    if not isinstance(event_id, str) or not event_id.strip():
        return False
    timestamp_fields = (
        "occurrence_start_at",
        "occurrence_end_at",
        "downloaded_at",
    )
    if any(not _is_aware_iso_datetime(payload.get(field)) for field in timestamp_fields):
        return False
    content_hash = payload.get("content_sha256")
    if not isinstance(content_hash, str):
        return False
    if len(content_hash) != 64 or any(character not in "0123456789abcdef" for character in content_hash):
        return False
    transcripts = payload.get("transcripts")
    if not isinstance(transcripts, list) or not transcripts:
        return False
    return all(
        isinstance(transcript, dict)
        and set(transcript) == {"id", "created_at"}
        and isinstance(transcript.get("id"), str)
        and bool(transcript["id"].strip())
        and (transcript.get("created_at") is None or _is_aware_iso_datetime(transcript.get("created_at")))
        for transcript in transcripts
    )


def _is_aware_iso_datetime(value: object) -> bool:
    if not isinstance(value, str) or not value.strip():
        return False
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return False
    return parsed.tzinfo is not None and parsed.utcoffset() is not None


def _fsync_directory(path: Path) -> None:
    directory_descriptor = os.open(path, os.O_RDONLY)
    try:
        os.fsync(directory_descriptor)
    finally:
        os.close(directory_descriptor)
