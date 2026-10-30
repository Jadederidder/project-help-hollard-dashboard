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


STATS_HEADER = ["Month", "Downloads", "Active", "Total", "Adoption",
                "Base", "ToBase", "Usage", "PerDownload"]
RAF_HEADER = ["Month", "Calls", "Valid", "Not valid"]


class TestStatsRows(unittest.TestCase):
    """Same contract as _daily_rows, for the same reason.

    hollard_monthly is reconciled on (programme, month_sort), so a month the
    exporter could not read is deleted from the CRM and the run still
    reports success.
    """

    def test_a_clean_tab_drops_nothing(self):
        rows, dropped = es._stats_rows(_FakeWorksheet([
            STATS_HEADER,
            ["May-2026", "10", "5", "100", "1", "200", "1", "7", "1"],
            ["Jun-2026", "12", "6", "112", "1", "200", "1", "8", "1"],
        ]))
        self.assertEqual(len(rows), 2)
        self.assertEqual(dropped, 0)

    def test_an_unparseable_month_is_counted(self):
        # 'Jly-2026' is the realistic shape: a typo, not obvious junk. The
        # row carries real figures and is silently lost without this count.
        rows, dropped = es._stats_rows(_FakeWorksheet([
            STATS_HEADER,
            ["May-2026", "10", "5", "100", "1", "200", "1", "7", "1"],
            ["Jly-2026", "12", "6", "112", "1", "200", "1", "8", "1"],
        ]))
        self.assertEqual(len(rows), 1)
        self.assertEqual(dropped, 1)

    def test_a_year_outside_the_accepted_range_is_counted(self):
        # _parse_month_dash refuses a year below 2020 or above 2099, which is
        # a drop like any other.
        _rows, dropped = es._stats_rows(_FakeWorksheet([
            STATS_HEADER,
            ["May-1999", "10", "5", "100", "1", "200", "1", "7", "1"],
        ]))
        self.assertEqual(dropped, 1)

    def test_a_blank_row_is_not_a_dropped_reading(self):
        rows, dropped = es._stats_rows(_FakeWorksheet([
            STATS_HEADER,
            ["May-2026", "10", "5", "100", "1", "200", "1", "7", "1"],
            ["", "", "", "", "", "", "", "", ""],
        ]))
        self.assertEqual(len(rows), 1)
        self.assertEqual(dropped, 0)


class TestRafRows(unittest.TestCase):
    def test_a_clean_tab_drops_nothing(self):
        rows, dropped = es._raf_rows(_FakeWorksheet([
            RAF_HEADER,
            ["May-2026", "4", "3", "1"],
            ["Jun-2026", "6", "5", "1"],
        ]))
        self.assertEqual(len(rows), 2)
        self.assertEqual(dropped, 0)

    def test_an_unparseable_month_is_counted(self):
        rows, dropped = es._raf_rows(_FakeWorksheet([
            RAF_HEADER,
            ["May-2026", "4", "3", "1"],
            ["Jly-2026", "6", "5", "1"],
        ]))
        self.assertEqual(len(rows), 1)
        self.assertEqual(dropped, 1)


class _FakeSpreadsheet:
    """Answers worksheet(title) from a dict of title -> rows."""

    def __init__(self, tabs):
        self._tabs = tabs

    def worksheet(self, title):
        return _FakeWorksheet(self._tabs[title])


class TestBuildPayloadCarriesTheCounts(unittest.TestCase):
    """The wiring, not the readers.

    Both readers are covered above and the sink's refusal is covered in
    test_core_sink.py, but each in isolation. Nothing proved the count
    actually travels from one to the other: deleting `+ aip_stats_dropped`
    from the payload assembly left all 45 tests green. So a monthly row
    could go missing, the exporter would know, and the sink would still be
    told zero. These tests close that.
    """

    TABS = {
        "AIP Stats": [STATS_HEADER,
                      ["May-2026", "10", "5", "100", "1", "200", "1", "7", "1"]],
        "SBMAC Stats": [STATS_HEADER,
                        ["May-2026", "9", "4", "90", "1", "200", "1", "6", "1"]],
        "AIP Daily call outs": [HEADER, ["1 Oct 2021", "10", "2", "1"]],
        "SBMAC Daily Call outs": [HEADER, ["1 Oct 2021", "8", "1", "1"]],
        "AIP Panics": [["Coords"], ["-26.1,28.1"]],
        "SBMAC Panics": [["Coords"], ["-26.2,28.2"]],
        "RAF Claims": [RAF_HEADER, ["May-2026", "4", "3", "1"]],
    }

    def _build(self, tabs):
        import unittest.mock as mock
        with mock.patch.object(es, "_auth") as auth:
            auth.return_value.open_by_key.return_value = _FakeSpreadsheet(tabs)
            return es.build_payload()

    def _tabs_with(self, title, extra_row):
        tabs = {k: [list(r) for r in v] for k, v in self.TABS.items()}
        tabs[title] = tabs[title] + [extra_row]
        return tabs

    def test_a_healthy_book_reports_zero_everywhere(self):
        p = self._build({k: [list(r) for r in v]
                         for k, v in self.TABS.items()})
        self.assertEqual(p["views"]["AIP"]["sourceDropped"], 0)
        self.assertEqual(p["views"]["SBMAC"]["sourceDropped"], 0)
        self.assertEqual(p["rafSourceDropped"], 0)

    def test_a_dropped_monthly_row_reaches_that_programme_s_count(self):
        p = self._build(self._tabs_with(
            "AIP Stats",
            ["Jly-2026", "12", "6", "112", "1", "200", "1", "8", "1"]))
        self.assertEqual(p["views"]["AIP"]["sourceDropped"], 1)
        self.assertEqual(p["views"]["SBMAC"]["sourceDropped"], 0,
                         "a drop must not be attributed to the other programme")

    def test_a_dropped_daily_row_reaches_that_programme_s_count(self):
        p = self._build(self._tabs_with(
            "SBMAC Daily Call outs", ["31 Febuary 2021", "9", "1", "1"]))
        self.assertEqual(p["views"]["SBMAC"]["sourceDropped"], 1)
        self.assertEqual(p["views"]["AIP"]["sourceDropped"], 0)

    def test_monthly_and_daily_drops_add_up_rather_than_overwrite(self):
        tabs = self._tabs_with(
            "AIP Stats",
            ["Jly-2026", "12", "6", "112", "1", "200", "1", "8", "1"])
        tabs["AIP Daily call outs"] = tabs["AIP Daily call outs"] + [
            ["31 Febuary 2021", "13", "3", "2"]]
        p = self._build(tabs)
        self.assertEqual(p["views"]["AIP"]["sourceDropped"], 2,
                         "both readers' losses must be counted, not one")

    def test_a_dropped_raf_row_reaches_the_top_level_count(self):
        p = self._build(self._tabs_with("RAF Claims",
                                        ["Jly-2026", "6", "5", "1"]))
        self.assertEqual(p["rafSourceDropped"], 1)

    def test_the_sink_refuses_what_this_payload_reports(self):
        # End to end across the two modules: a typo in one monthly cell must
        # stop the CRM write. Imported here so test_export_summary stays
        # runnable on its own if core_sink is ever moved.
        sys.path.insert(0, os.path.join(
            os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
            "scripts"))
        import core_sink
        p = self._build(self._tabs_with(
            "AIP Stats",
            ["Jly-2026", "12", "6", "112", "1", "200", "1", "8", "1"]))
        problems = core_sink._incompleteness(p)
        self.assertTrue(
            any("AIP: the exporter dropped 1 source row(s)" in x
                for x in problems),
            f"the sink must refuse this payload; got {problems}")


class TestPublicView(unittest.TestCase):
    """What gets published is not what the sink sees.

    The counts are a data-quality signal about our own source sheet. The CRM
    sink needs them; whoever opens the dashboard does not.
    """

    def _payload(self, aip_dropped=0, sbmac_dropped=0, raf_dropped=0):
        return {
            "lastUpdated": "2026-10-30T00:00:00Z",
            "views": {
                "AIP": {"kpis": {}, "stats": [], "daily": [], "panics": [],
                        "sourceDropped": aip_dropped},
                "SBMAC": {"kpis": {}, "stats": [], "daily": [], "panics": [],
                          "sourceDropped": sbmac_dropped},
            },
            "raf": [],
            "rafSourceDropped": raf_dropped,
        }

    def test_the_counts_are_stripped_from_what_is_written(self):
        public = es._public_view(self._payload(aip_dropped=3, raf_dropped=2))
        self.assertNotIn("rafSourceDropped", public)
        for view in public["views"].values():
            self.assertNotIn("sourceDropped", view)
        # And nothing else is lost in the process.
        self.assertEqual(set(public["views"]), {"AIP", "SBMAC"})
        self.assertEqual(set(public["views"]["AIP"]),
                         {"kpis", "stats", "daily", "panics"})
        self.assertIn("lastUpdated", public)
        self.assertIn("raf", public)

    def test_the_serialised_json_mentions_neither_count(self):
        # The assertion that matches the actual risk: the published bytes.
        import json
        text = json.dumps(es._public_view(self._payload(sbmac_dropped=9,
                                                        raf_dropped=9)))
        self.assertNotIn("sourceDropped", text)
        self.assertNotIn("rafSourceDropped", text)

    def test_the_original_payload_is_not_mutated(self):
        # The sink is handed the full payload AFTER this runs, so filtering
        # must copy rather than strip in place.
        p = self._payload(aip_dropped=1, raf_dropped=1)
        es._public_view(p)
        self.assertEqual(p["views"]["AIP"]["sourceDropped"], 1)
        self.assertEqual(p["rafSourceDropped"], 1)


if __name__ == "__main__":
    unittest.main()
