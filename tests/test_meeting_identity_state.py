from __future__ import annotations

import json
import os
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from pathlib import Path
from threading import Barrier, Event
from unittest.mock import patch

import pytest

import obsidian_intake_agent.meetings.identity_state as identity_state_module
from obsidian_intake_agent.meetings.identity_state import (
    compare_and_write_identity_marker,
    identity_marker_transaction,
    load_identity_state,
    read_identity_state,
    write_identity_marker,
)

SCHEDULED_END = datetime(2026, 7, 30, 14, 0, tzinfo=UTC)


def test_v2_fallback_is_upgradeable_through_retry_deadline() -> None:
    retry_until = SCHEDULED_END + timedelta(hours=24)
    state = load_identity_state(
        {
            "schema_version": 2,
            "source_type": "meeting_bundle_processed",
            "processing_source_kind": "fallback",
            "upgrade_state": "awaiting_transcript",
            "retry_until": retry_until.isoformat(),
        },
        SCHEDULED_END,
    )

    assert state.marker_kind == "processed"
    assert state.processing_source_kind == "fallback"
    assert state.upgrade_state == "awaiting_transcript"
    assert state.retry_until == retry_until
    assert state.is_terminal(now=retry_until - timedelta(microseconds=1)) is False
    assert state.is_terminal(now=retry_until) is False


def test_v2_fallback_is_terminal_strictly_after_retry_deadline() -> None:
    retry_until = SCHEDULED_END + timedelta(hours=24)
    state = load_identity_state(
        {
            "schema_version": 2,
            "source_type": "meeting_bundle_processed",
            "processing_source_kind": "fallback",
            "upgrade_state": "awaiting_transcript",
            "retry_until": retry_until.isoformat(),
        },
        SCHEDULED_END,
    )

    assert state.is_terminal(now=retry_until + timedelta(microseconds=1)) is True


@pytest.mark.parametrize(
    "upgrade_fields",
    [{}, {"upgrade_state": "invalid"}],
    ids=["missing-upgrade-state", "invalid-upgrade-state"],
)
def test_v2_fallback_without_valid_awaiting_upgrade_state_is_terminal(
    upgrade_fields: dict[str, object],
) -> None:
    state = load_identity_state(
        {
            "schema_version": 2,
            "source_type": "meeting_bundle_processed",
            "processing_source_kind": "fallback",
            "retry_until": (SCHEDULED_END + timedelta(hours=24)).isoformat(),
            **upgrade_fields,
        },
        SCHEDULED_END,
    )

    assert state.upgrade_state == "manual_review_required"
    assert state.is_terminal(now=SCHEDULED_END) is True


@pytest.mark.parametrize(
    "retry_fields",
    [{}, {"retry_until": "not-a-datetime"}],
    ids=["missing-retry-until", "invalid-retry-until"],
)
def test_v2_fallback_without_valid_retry_deadline_requires_manual_review(
    retry_fields: dict[str, object],
) -> None:
    state = load_identity_state(
        {
            "schema_version": 2,
            "source_type": "meeting_bundle_processed",
            "processing_source_kind": "fallback",
            "upgrade_state": "awaiting_transcript",
            **retry_fields,
        },
        SCHEDULED_END,
    )

    assert state.upgrade_state == "manual_review_required"
    assert state.is_terminal(now=SCHEDULED_END) is True


@pytest.mark.parametrize(
    "retry_until",
    [
        "2026-07-31T14:00:00",
        "2026-07-31",
        "9999-12-31T23:59:59-23:59",
    ],
    ids=["naive-datetime", "date-only", "utc-normalization-overflow"],
)
def test_v2_fallback_retry_deadline_requires_explicit_safe_timezone(retry_until: str) -> None:
    state = load_identity_state(
        {
            "schema_version": 2,
            "source_type": "meeting_bundle_processed",
            "processing_source_kind": "fallback",
            "upgrade_state": "awaiting_transcript",
            "retry_until": retry_until,
        },
        SCHEDULED_END,
    )

    assert state.upgrade_state == "manual_review_required"
    assert state.is_terminal(now=SCHEDULED_END) is True


@pytest.mark.parametrize(
    "processing_source_fields",
    [{}, {"processing_source_kind": "invalid"}],
    ids=["missing-processing-source-kind", "invalid-processing-source-kind"],
)
def test_v2_marker_without_valid_processing_source_kind_never_uses_legacy_source_inference(
    processing_source_fields: dict[str, object],
) -> None:
    state = load_identity_state(
        {
            "schema_version": 2,
            "source_type": "meeting_bundle_processed",
            "preferred_input_source_name": "Copilot recap / AI summary",
            "upgrade_state": "awaiting_transcript",
            "retry_until": (SCHEDULED_END + timedelta(hours=24)).isoformat(),
            **processing_source_fields,
        },
        SCHEDULED_END,
    )

    assert state.processing_source_kind == "unknown"
    assert state.is_terminal(now=SCHEDULED_END) is True


@pytest.mark.parametrize("processing_source_kind", ["transcript", "manual"])
def test_v2_nonfallback_processed_marker_is_immediately_terminal(processing_source_kind: str) -> None:
    state = load_identity_state(
        {
            "schema_version": 2,
            "source_type": "meeting_bundle_processed",
            "processing_source_kind": processing_source_kind,
            "upgrade_state": "terminal",
            "retry_until": (SCHEDULED_END + timedelta(hours=24)).isoformat(),
        },
        SCHEDULED_END,
    )

    assert state.is_terminal(now=SCHEDULED_END) is True


def test_legacy_processed_fallback_infers_upgrade_window() -> None:
    payload = {
        "source_type": "meeting_bundle_processed",
        "preferred_input_source_name": "Copilot recap / AI summary",
    }

    state = load_identity_state(payload, SCHEDULED_END)

    assert state.marker_kind == "processed"
    assert state.processing_source_kind == "fallback"
    assert state.upgrade_state == "awaiting_transcript"
    assert state.retry_until == SCHEDULED_END + timedelta(hours=24)
    assert state.payload == payload
    assert state.is_terminal(now=SCHEDULED_END + timedelta(hours=23)) is False


@pytest.mark.parametrize(
    "schema_version",
    [3, "2", None, True],
    ids=["unsupported-version", "string-version", "null-version", "boolean-version"],
)
def test_fallback_with_present_invalid_schema_never_uses_legacy_upgrade_inference(
    schema_version: object,
) -> None:
    state = load_identity_state(
        {
            "schema_version": schema_version,
            "source_type": "meeting_bundle_processed",
            "preferred_input_source_name": "Copilot recap / AI summary",
        },
        SCHEDULED_END,
    )

    assert state.processing_source_kind == "unknown"
    assert state.is_terminal(now=SCHEDULED_END) is True


def test_legacy_processed_marker_without_source_information_is_terminal() -> None:
    state = load_identity_state({"source_type": "meeting_bundle_processed"}, SCHEDULED_END)

    assert state.processing_source_kind == "unknown"
    assert state.upgrade_state == "unknown"
    assert state.is_terminal(now=SCHEDULED_END) is True


def test_legacy_meeting_sync_identity_is_processed_and_terminal() -> None:
    state = load_identity_state(
        {
            "source_type": "meeting_sync_identity",
            "preferred_input_source_name": "Copilot recap / AI summary",
        },
        SCHEDULED_END,
    )

    assert state.marker_kind == "processed"
    assert state.processing_source_kind == "unknown"
    assert state.is_terminal(now=SCHEDULED_END) is True


def test_legacy_schema_less_pending_marker_remains_nonterminal() -> None:
    state = load_identity_state({"source_type": "meeting_sync_pending"}, SCHEDULED_END)

    assert state.marker_kind == "pending"
    assert state.retry_until == SCHEDULED_END + timedelta(hours=24)
    assert state.is_terminal(now=SCHEDULED_END + timedelta(days=30)) is False


def test_schema_v2_pending_marker_is_nonterminal() -> None:
    state = load_identity_state(
        {"schema_version": 2, "source_type": "meeting_sync_pending"},
        SCHEDULED_END,
    )

    assert state.marker_kind == "pending"
    assert state.retry_until == SCHEDULED_END + timedelta(hours=24)
    assert state.is_terminal(now=SCHEDULED_END + timedelta(days=30)) is False


def test_missing_source_type_is_unknown_and_terminal() -> None:
    state = load_identity_state({"retry_until": (SCHEDULED_END + timedelta(days=1)).isoformat()}, SCHEDULED_END)

    assert state.marker_kind == "unknown"
    assert state.is_terminal(now=SCHEDULED_END) is True


@pytest.mark.parametrize("raw_content", ["{not-json", "[]", '"string"'])
def test_read_identity_state_treats_malformed_json_and_non_objects_as_terminal(
    tmp_path: Path,
    raw_content: str,
) -> None:
    marker_path = tmp_path / "identity.json"
    marker_path.write_text(raw_content, encoding="utf-8")

    state = read_identity_state(marker_path, SCHEDULED_END)

    assert state.marker_kind == "malformed"
    assert state.is_terminal(now=SCHEDULED_END) is True


def test_read_identity_state_handles_missing_and_unreadable_files_without_leaking_errors(tmp_path: Path) -> None:
    marker_path = tmp_path / "identity.json"

    missing_state = read_identity_state(marker_path, SCHEDULED_END)
    marker_path.write_text("{}\n", encoding="utf-8")
    with patch.object(Path, "read_text", side_effect=PermissionError("private marker contents")):
        unreadable_state = read_identity_state(marker_path, SCHEDULED_END)

    assert missing_state.marker_kind == "unknown"
    assert missing_state.is_terminal(now=SCHEDULED_END) is True
    assert unreadable_state.marker_kind == "malformed"
    assert unreadable_state.is_terminal(now=SCHEDULED_END) is True


def test_write_identity_marker_writes_stable_json_atomically(tmp_path: Path) -> None:
    marker_path = tmp_path / "nested" / "identity.json"
    payload = {"source_type": "meeting_sync_pending", "subject": "Weekly sync"}

    write_identity_marker(marker_path, payload)

    assert json.loads(marker_path.read_text(encoding="utf-8")) == payload
    assert list(marker_path.parent.glob(f".{marker_path.name}.*.tmp")) == []


def test_write_identity_marker_removes_temp_file_when_serialization_fails(tmp_path: Path) -> None:
    marker_path = tmp_path / "identity.json"

    with pytest.raises(TypeError):
        write_identity_marker(marker_path, {"not_serializable": object()})

    assert marker_path.exists() is False
    assert list(marker_path.parent.glob(f".{marker_path.name}.*.tmp")) == []


@pytest.mark.parametrize(
    "replacement_source_type",
    ["meeting_sync_pending", "unexpected_marker_kind"],
)
@pytest.mark.parametrize(
    "existing_source_type",
    ["meeting_bundle_processed", "meeting_sync_identity"],
)
def test_write_identity_marker_refuses_to_downgrade_processed_marker(
    tmp_path: Path,
    replacement_source_type: str,
    existing_source_type: str,
) -> None:
    marker_path = tmp_path / "identity.json"
    processed = {
        "source_type": existing_source_type,
        "schema_version": 2,
        "processing_source_kind": "manual",
        "upgrade_state": "terminal",
    }
    write_identity_marker(marker_path, processed)
    original = marker_path.read_bytes()

    with pytest.raises(ValueError, match="processed identity marker"):
        write_identity_marker(marker_path, {"source_type": replacement_source_type})

    assert marker_path.read_bytes() == original
    assert (marker_path.parent / f".{marker_path.name}.lock").read_bytes() == b""


def test_compare_and_write_identity_marker_allows_only_one_executor_for_same_snapshot(
    tmp_path: Path,
) -> None:
    marker_path = tmp_path / "identity.json"
    write_identity_marker(marker_path, {"source_type": "meeting_sync_pending"})
    expected = marker_path.read_bytes()
    barrier = Barrier(2)
    candidates = (
        {
            "source_type": "meeting_bundle_processed",
            "schema_version": 2,
            "processing_source_kind": "manual",
            "upgrade_state": "terminal",
            "executor": "first",
        },
        {
            "source_type": "meeting_bundle_processed",
            "schema_version": 2,
            "processing_source_kind": "manual",
            "upgrade_state": "terminal",
            "executor": "second",
        },
    )

    def attempt(payload: dict[str, object]) -> str | None:
        barrier.wait()
        try:
            compare_and_write_identity_marker(marker_path, expected, payload)
        except ValueError as exc:
            return str(exc)
        return None

    with ThreadPoolExecutor(max_workers=2) as executor:
        outcomes = list(executor.map(attempt, candidates))

    assert sum(outcome is None for outcome in outcomes) == 1
    assert sum("changed before compare-and-write" in (outcome or "") for outcome in outcomes) == 1
    assert json.loads(marker_path.read_text(encoding="utf-8")) in candidates


@pytest.mark.parametrize("existing_content", [b"{not-json", b'{"source_type": "unknown"}\n'])
def test_pending_identity_write_refuses_malformed_or_unknown_existing_marker(
    tmp_path: Path,
    existing_content: bytes,
) -> None:
    marker_path = tmp_path / "identity.json"
    marker_path.write_bytes(existing_content)

    with pytest.raises(ValueError, match="recognized pending identity marker"):
        write_identity_marker(marker_path, {"source_type": "meeting_sync_pending"})

    assert marker_path.read_bytes() == existing_content


def test_identity_marker_transaction_is_non_reentrant_for_same_thread(tmp_path: Path) -> None:
    marker_path = tmp_path / "identity.json"

    with identity_marker_transaction(marker_path):
        with pytest.raises(RuntimeError, match="not reentrant"):
            with identity_marker_transaction(marker_path):
                pytest.fail("nested transaction unexpectedly acquired")


def test_generic_pending_writer_blocks_behind_active_marker_transaction(tmp_path: Path) -> None:
    marker_path = tmp_path / "identity.json"
    write_identity_marker(marker_path, {"source_type": "meeting_sync_pending", "version": 1})
    writer_started = Event()
    writer_completed = Event()

    def refresh_pending_marker() -> None:
        writer_started.set()
        write_identity_marker(
            marker_path,
            {"source_type": "meeting_sync_pending", "version": 2},
        )
        writer_completed.set()

    with ThreadPoolExecutor(max_workers=1) as executor:
        with identity_marker_transaction(marker_path):
            future = executor.submit(refresh_pending_marker)
            assert writer_started.wait(timeout=1)
            assert writer_completed.wait(timeout=0.1) is False
        future.result(timeout=1)

    assert writer_completed.is_set()
    assert json.loads(marker_path.read_text(encoding="utf-8"))["version"] == 2


def test_identity_marker_lock_rejects_symlink_without_touching_target(tmp_path: Path) -> None:
    marker_path = tmp_path / "identity.json"
    lock_path = tmp_path / f".{marker_path.name}.lock"
    target = tmp_path / "lock-target"
    target.write_bytes(b"private lock target")
    lock_path.symlink_to(target)

    with pytest.raises(ValueError, match="lock file"):
        write_identity_marker(marker_path, {"source_type": "meeting_sync_pending"})

    assert target.read_bytes() == b"private lock target"
    assert marker_path.exists() is False


def test_identity_marker_lock_rejects_hard_link_alias(tmp_path: Path) -> None:
    marker_path = tmp_path / "identity.json"
    lock_path = tmp_path / f".{marker_path.name}.lock"
    alias_path = tmp_path / "lock-alias"
    lock_path.touch()
    os.link(lock_path, alias_path)

    with pytest.raises(ValueError, match="lock file"):
        write_identity_marker(marker_path, {"source_type": "meeting_sync_pending"})

    assert marker_path.exists() is False


def test_identity_marker_write_fsyncs_parent_directory_after_replace(tmp_path: Path) -> None:
    marker_path = tmp_path / "identity.json"
    events: list[str] = []
    real_replace = os.replace

    def replace(source: str | Path, target: str | Path) -> None:
        events.append("replace")
        real_replace(source, target)

    with (
        patch.object(identity_state_module.os, "replace", side_effect=replace),
        patch.object(
            identity_state_module,
            "_fsync_parent_directory",
            side_effect=lambda path: events.append(f"directory_fsync:{path}"),
            create=True,
        ),
    ):
        write_identity_marker(marker_path, {"source_type": "meeting_sync_pending"})

    assert events == ["replace", f"directory_fsync:{marker_path.parent}"]
