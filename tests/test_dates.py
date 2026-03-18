from __future__ import annotations

from datetime import date
import unittest

from obsidian_intake_agent.utils.dates import monday_of_week


class MondayOfWeekTests(unittest.TestCase):
    def test_returns_same_date_for_monday(self) -> None:
        self.assertEqual(monday_of_week(date(2026, 3, 9)), date(2026, 3, 9))

    def test_rolls_back_to_monday(self) -> None:
        self.assertEqual(monday_of_week(date(2026, 3, 11)), date(2026, 3, 9))


if __name__ == "__main__":
    unittest.main()
