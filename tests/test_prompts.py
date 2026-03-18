from __future__ import annotations

import unittest

from obsidian_intake_agent.llm.prompts import build_meeting_extraction_prompt


class PromptTests(unittest.TestCase):
    def test_prompt_contains_schema_and_transcript_delimiter(self) -> None:
        prompt = build_meeting_extraction_prompt(
            transcript_text="Speaker: We should ship Friday.",
            meeting_date="2026-03-12",
            source="Teams",
            title="Platform Sync",
        )

        self.assertIn('"action_items": [{"text": str, "owner": str|null, "due": str|null}]', prompt)
        self.assertIn('"participants": [str]', prompt)
        self.assertIn('"verbatim_excerpt": str', prompt)
        self.assertIn("===TRANSCRIPT===", prompt)
        self.assertIn("Speaker: We should ship Friday.", prompt)

    def test_prompt_requires_json_only(self) -> None:
        prompt = build_meeting_extraction_prompt(
            transcript_text="Transcript body",
            meeting_date="2026-03-12",
            source="Unknown",
            title="Weekly Sync",
        )

        self.assertIn("Return STRICT JSON only.", prompt)
        self.assertIn("Output JSON only, no extra text.", prompt)
        self.assertIn("Do NOT invent facts", prompt)


if __name__ == "__main__":
    unittest.main()
