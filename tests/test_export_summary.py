# ============================================================
# tests/test_export_summary.py
# Run with: pytest tests/test_export_summary.py
#
# Only the pure row readers are covered here -- the Google Sheets client and
# the file write need credentials and a network. The one thing that must not
# be taken on trust is the dropped-row count: it is the only evidence a
# snapshot is short, and the CRM sink refuses a partial load on the strength
# of it. If this file ever goes back to returning rows alone, the sink has
# nothing to go on.
# ============================================================

import os
import sys
import unittest

sys.path.insert(0, os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "scripts"))
import export_summary as es


class _FakeWorksheet:
    """Stands in for a gspread worksheet: all it needs is get_all_values."""

    def __init__(self, rows):
        self._rows = rows

    def get_all_values(self):
        return self._rows


HEADER = ["Date", "Cumulative", "Call outs", "Completed"]

# The source sheet writes '1 Aug 2021' and _parse_daily_date accepts nothing
# else, so the fixtures below use that form. The misspelling in the bad row is
# the realistic shape of this failure: a typo in a date cell, not a cell full
# of obvious junk.


class TestDailyRows(unittest.TestCase):
    def test_a_clean_tab_drops_nothing(self):
        rows, dropped = es._daily_rows(_FakeWorksheet([
            HEADER,
            ["1 Oct 2021", "10", "2", "1"],
            ["2 Oct 2021", "13", "3", "2"],
        ]))
        self.assertEqual(len(rows), 2)
        self.assertEqual(dropped, 0)

    def test_an_unreadable_date_is_counted_not_just_skipped(self):
        # The row still has real figures on it. Skipping it quietly is what
        # put the CRM a day behind the dashboard while both reported success.
        rows, dropped = es._daily_rows(_FakeWorksheet([
            HEADER,
            ["1 Oct 2021", "10", "2", "1"],
            ["31 Febuary 2021", "13", "3", "2"],
            ["3 Oct 2021", "16", "3", "1"],
        ]))
        self.assertEqual(len(rows), 2)
        self.assertEqual(dropped, 1, "the dropped row must be reported")

    def test_a_blank_row_is_not_a_dropped_reading(self):
        # Sheets pads the bottom of a tab with empty rows. Counting those as
        # losses would refuse every healthy snapshot, which is the fastest
        # way to get the check switched off again.
        rows, dropped = es._daily_rows(_FakeWorksheet([
            HEADER,
            ["1 Oct 2021", "10", "2", "1"],
            ["", "", "", ""],
            ["   ", "", "", ""],
        ]))
        self.assertEqual(len(rows), 1)
        self.assertEqual(dropped, 0)

    def test_an_entirely_empty_tab(self):
        rows, dropped = es._daily_rows(_FakeWorksheet([]))
        self.assertEqual(rows, [])
        self.assertEqual(dropped, 0)

    def test_rows_come_back_in_date_order(self):
        rows, _dropped = es._daily_rows(_FakeWorksheet([
            HEADER,
            ["3 Oct 2021", "16", "3", "1"],
            ["1 Oct 2021", "10", "2", "1"],
        ]))
        self.assertEqual([r["date"] for r in rows],
                         ["2021-10-01", "2021-10-03"])


if __name__ == "__main__":
    unittest.main()
