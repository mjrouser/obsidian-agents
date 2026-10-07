import hashlib
import json
import tempfile
import unittest
from datetime import datetime
from unittest.mock import patch

from obsidian_intake_agent.meetings import (
    build_bundle_processing_plan,
    execute_bundle_processing_plan,
    render_bundle_processing_plan,
)
from obsidian_intake_agent.meetings.identity_state import (
    IdentityMarkerTransaction,
    load_identity_state,
    write_identity_marker,
)
from obsidian_intake_agent.meetings.transcript_provenance import provenance_path
from tests.recap_fixtures import verify_metadata_fixture
from tests.test_meeting_process_bundles import _processor_for_vault, _ready_marker_bundle


class RecapProcessingTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.vault, self.bundles, self.source, self.metadata, self.marker = _ready_marker_bundle(
            tmp_dir=self.temp.name, preferred_source="Copilot recap / AI summary", extension=".md"
        )
        self.source.write_text("## Summary\nCurrent discussion.\n\n## Action Items\n- Matt to Review the workbook.\n")
        payload = json.loads(self.metadata.read_text())
        payload["fallback_not_before"] = payload["scheduled_end_at"]
        self.metadata.write_text(json.dumps(payload))
        verify_metadata_fixture(self.metadata)
        self.processor = _processor_for_vault(self.vault)
        self.now = datetime.fromisoformat("2026-05-04T14:30:00+00:00")

    def plan(self):
        return build_bundle_processing_plan(intake_root=self.bundles, processor=self.processor, now=self.now)

    def snapshot(self):
        return {str(p.relative_to(self.vault)): p.read_bytes() for p in self.vault.rglob("*") if p.is_file()}

    def test_valid_recap_archives_provenance_and_rerun_does_not_duplicate_actions(self):
        before_hash = hashlib.sha256(self.source.read_bytes()).hexdigest()
        result = execute_bundle_processing_plan(self.plan(), processor=self.processor)
        self.assertEqual(result.processed_count, 1, result.items[0].reasons)
        marker = json.loads(self.marker.read_text())
        self.assertEqual(marker["selected_recap"]["content_sha256"], before_hash)
        self.assertIn('recap_insight_id: "synthetic-insight"', result.items[0].canonical_note_path.read_text())
        self.assertTrue(self.processor.intake_state.archive_destination(provenance_path(self.source)).exists())
        before = self.snapshot()
        execute_bundle_processing_plan(self.plan(), processor=self.processor)
        self.assertEqual(before, self.snapshot())

    def test_missing_provenance_blocks_direct_processing_without_sync(self):
        provenance_path(self.source).unlink()
        before = self.snapshot()
        plan = self.plan()
        self.assertEqual(plan.ready_count, 0)
        self.assertEqual(before, self.snapshot())
        self.assertIn("unverified_cached_recap", str(plan.items[0].reasons))

    def test_source_change_after_plan_blocks_execution(self):
        plan = self.plan()
        self.source.write_text("User edited content")
        result = execute_bundle_processing_plan(plan, processor=self.processor)
        self.assertEqual(result.processed_count, 0)
        self.assertEqual(self.source.read_text(), "User edited content")
        self.assertEqual(json.loads(self.marker.read_text())["source_type"], "meeting_sync_pending")

    def test_manual_label_cannot_admit_managed_recap(self):
        payload = json.loads(self.metadata.read_text())
        payload["processor_handoff"]["preferred_input_source_name"] = "Manual / semi-manual intake"
        self.metadata.write_text(json.dumps(payload))
        self.assertEqual(self.plan().ready_count, 0)

    def test_exclusion_added_after_plan_blocks_execution_and_refresh(self):
        plan = self.plan()
        payload = json.loads(self.marker.read_text())
        payload["processing_exclusion"] = {
            "reason": "invalidated_recap",
            "at": self.now.isoformat(),
            "manifest": "repair.json",
        }
        write_identity_marker(self.marker, payload)
        self.assertTrue(load_identity_state(payload, self.now).is_terminal(now=self.now))
        self.assertEqual(self.plan().ready_count, 0)
        self.assertEqual(execute_bundle_processing_plan(plan, processor=self.processor).processed_count, 0)
        del payload["processing_exclusion"]
        with self.assertRaisesRegex(ValueError, "exclusion"):
            write_identity_marker(self.marker, payload)

    def test_dry_run_is_byte_preserving(self):
        before = self.snapshot()
        output = render_bundle_processing_plan(self.plan())
        self.assertEqual(before, self.snapshot())
        self.assertIn("ready", output)

    def test_marker_failure_restores_source_pair_and_outputs(self):
        source_bytes = self.source.read_bytes()
        sidecar_bytes = provenance_path(self.source).read_bytes()
        with patch.object(IdentityMarkerTransaction, "compare_and_write", side_effect=ValueError("simulated conflict")):
            result = execute_bundle_processing_plan(self.plan(), processor=self.processor)
        self.assertEqual(result.failed_count, 1)
        self.assertEqual(self.source.read_bytes(), source_bytes)
        self.assertEqual(provenance_path(self.source).read_bytes(), sidecar_bytes)
        self.assertFalse(self.processor.intake_state.archive_destination(provenance_path(self.source)).exists())
        self.assertEqual(self.plan().ready_count, 1)
