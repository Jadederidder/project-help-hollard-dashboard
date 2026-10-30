# ============================================================
# tests/test_core_sink.py
# Run with: pytest tests/test_core_sink.py
#
# The CRM sink replaces what it writes, so every guard in push() exists to
# stop a bad read taking away data the previous run had put there. These
# tests exercise those guards against a fake driver -- there is no database
# in CI, and the guards are the part that must not be taken on trust.
# ============================================================

import os
import sys
import unittest

sys.path.insert(0, os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "scripts"))
import core_sink as cs


def payload(aip_stats=2, aip_daily=3, aip_panics=4,
            sbmac_stats=2, sbmac_daily=3, sbmac_panics=4, raf=2,
            aip_dropped=0, sbmac_dropped=0):
    """A structurally complete payload, sized small enough to reason about."""
    def view(n_stats, n_daily, n_panics, tag, dropped):
        return {
            # What the exporter had to throw away. 0 is the healthy value and
            # the key being ABSENT is not the same thing -- see
            # test_a_payload_that_does_not_say_what_it_dropped_is_refused.
            "sourceDropped": dropped,
            "stats": [{"monthSort": f"2025-{i:02d}", "month": f"M{i}",
                       "monthlyDownloads": i, "activeCustomers": i,
                       "totalDownloads": i, "adoptionPct": 1.0,
                       "baseSize": 10, "downloadsToBasePct": 1.0,
                       "usage": 1} for i in range(1, n_stats + 1)],
            "daily": [{"date": f"2025-01-{i:02d}", "callouts": 1,
                       "cumulative": 1, "completed": 1}
                      for i in range(1, n_daily + 1)],
            "panics": [[-26.0 - i / 1000, 28.0 + i / 1000]
                       for i in range(n_panics)],
        }
    return {
        "views": {"AIP": view(aip_stats, aip_daily, aip_panics, "AIP",
                              aip_dropped),
                  "SBMAC": view(sbmac_stats, sbmac_daily, sbmac_panics, "S",
                                sbmac_dropped)},
        "raf": [{"monthSort": f"2025-{i:02d}", "month": f"M{i}", "calls": i,
                 "valid": i, "nonValid": 0} for i in range(1, raf + 1)],
    }


class TestIncompleteness(unittest.TestCase):
    def test_a_whole_payload_has_no_problems(self):
        self.assertEqual(cs._incompleteness(payload()), [])

    def test_an_empty_panics_list_is_a_problem(self):
        # This is the shape a failed panic read actually arrives in: an empty
        # list, not an exception. Before the check it wiped the table and
        # reported success.
        self.assertIn("AIP: panics is empty",
                      cs._incompleteness(payload(aip_panics=0)))

    def test_empty_stats_and_daily_are_problems(self):
        probs = cs._incompleteness(payload(sbmac_stats=0, sbmac_daily=0))
        self.assertIn("SBMAC: stats is empty", probs)
        self.assertIn("SBMAC: daily is empty", probs)

    def test_a_missing_programme_is_a_problem(self):
        p = payload()
        del p["views"]["SBMAC"]
        self.assertIn("SBMAC: missing from the payload", cs._incompleteness(p))

    def test_empty_raf_is_a_problem(self):
        self.assertIn("raf is empty", cs._incompleteness(payload(raf=0)))

    def test_no_views_at_all(self):
        self.assertEqual(cs._incompleteness({}), ["payload has no views"])

    def test_a_daily_row_with_an_unparseable_date_is_a_problem(self):
        # The partial case: the list is NOT empty, so the emptiness checks
        # pass, but _rows_daily silently drops this row and the reconcile
        # would then take that day out of the CRM.
        p = payload()
        p["views"]["AIP"]["daily"].append(
            {"date": "", "callouts": 4, "cumulative": 4, "completed": 1})
        self.assertIn("AIP: 1 daily row(s) carry no usable date",
                      cs._incompleteness(p))

    def test_a_panic_pin_with_no_coordinates_is_a_problem(self):
        p = payload()
        p["views"]["SBMAC"]["panics"].append([None, None])
        self.assertIn("SBMAC: 1 panic pin(s) carry no coordinates",
                      cs._incompleteness(p))

    def test_source_rows_dropped_by_the_exporter_are_a_problem(self):
        # The case neither emptiness nor the undated check can see. The
        # exporter's _daily_rows drops a row whose date will not parse, so it
        # never reaches the payload: `daily` is still full, every row in it
        # has a usable date, and the only evidence the snapshot is short is
        # the count the exporter now sends along.
        self.assertIn(
            "AIP: the exporter dropped 2 source row(s) it could not read, so "
            "this snapshot is partial",
            cs._incompleteness(payload(aip_dropped=2)))

    def test_a_payload_that_does_not_say_what_it_dropped_is_refused(self):
        # Silence is not reassurance. An older exporter, or one whose count
        # went missing in a refactor, must not be read as "nothing was
        # dropped" -- that is the exact failure this check exists to stop,
        # and it would come back the moment the key quietly disappeared.
        p = payload()
        del p["views"]["SBMAC"]["sourceDropped"]
        self.assertIn(
            "SBMAC: the payload does not say how many source rows were "
            "dropped, so a partial load cannot be ruled out",
            cs._incompleteness(p))

    def test_zero_dropped_is_not_a_problem(self):
        # The healthy value, held explicitly so the check above cannot be
        # "fixed" by making 0 and absent behave the same.
        self.assertEqual(cs._incompleteness(payload(aip_dropped=0)), [])


class _FakeCursor:
    """A fake that holds rows, so a DELETE actually has to be correct.

    The first version of this fake handed every DELETE the count from the
    SELECT that preceded it. That made the reconciliation tests unable to
    fail: a DELETE that lost its `synced_at <> %s` clause would have wiped
    the rows this run had just written, and the fake would still have
    reported the stale count and the canned final count. CodeRabbit caught
    it. This version keeps a row list per table and applies the predicate,
    so losing that clause takes the fresh rows with it and the end-state
    check in _reconcile fails -- which is the whole point of the guard.
    """

    def __init__(self, existing=None):
        # table -> list of synced_at stamps standing in for stored rows.
        self.tables = {t: list(existing.get(t, []) if existing else [])
                       for t in ("hollard_monthly", "hollard_daily",
                                 "hollard_panics", "hollard_raf_claims")}
        # What the tables held before this transaction touched them. A real
        # connection rolls back on an unclean exit, so the fake has to as
        # well: without this, a test could assert "commit() was not called"
        # and still be sitting on rows the failed push had written, which
        # proves nothing about what the database would be left holding.
        self.initial = {t: list(v) for t, v in self.tables.items()}
        self.rolled_back = False
        self.rowcount = 0
        self.log = []
        self.batches = []
        self._result = 0

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    @staticmethod
    def _table(sql):
        for t in ("hollard_monthly", "hollard_daily", "hollard_panics",
                  "hollard_raf_claims"):
            if t in sql:
                return t
        raise AssertionError(f"statement names no known table: {sql[:80]}")

    def rollback(self):
        self.tables = {t: list(v) for t, v in self.initial.items()}
        self.rolled_back = True

    def execute(self, sql, params=None):
        low = " ".join(sql.split()).lower()
        table = self._table(low)
        stale_scoped = "synced_at <> %s" in low
        fresh_scoped = "and synced_at = %s" in low
        scoped = stale_scoped or fresh_scoped
        now = params[-1] if (scoped and params) else None

        if low.startswith("select count(*)"):
            rows = self.tables[table]
            if stale_scoped:
                self._result = sum(1 for r in rows if r != now)
            elif fresh_scoped:
                self._result = sum(1 for r in rows if r == now)
            else:
                self._result = len(rows)
            self.log.append(("count", table, self._result))
        elif low.startswith("delete"):
            rows = self.tables[table]
            if stale_scoped:
                keep = [r for r in rows if r == now]
            else:
                # A delete with no synced_at clause takes everything the
                # predicate matches, fresh rows included. That is the bug.
                keep = []
            self.rowcount = len(rows) - len(keep)
            self.tables[table] = keep
            self.log.append(("delete", table, self.rowcount))
        else:
            raise AssertionError(f"unexpected statement: {low[:80]}")

    def fetchone(self):
        return (self._result,)

    def executemany(self, sql, seq):
        table = self._table(sql)
        # These inserts all carry synced_at as the last bound value.
        self.tables[table].extend(row[-1] for row in seq)
        self.log.append(("write", table, len(seq)))
        self.batches.append(len(seq))


class _FakeConn:
    def __init__(self, cur):
        self._cur, self.committed, self.clean = cur, False, None

    def __enter__(self):
        return self

    def __exit__(self, exc_type, *a):
        self.clean = exc_type is None
        if exc_type is not None:
            self._cur.rollback()
        return False

    def cursor(self):
        return self._cur

    def commit(self):
        self.committed = True


class _FakePsycopg:
    def __init__(self, cur):
        self._cur, self.conn, self.kwargs = cur, None, None

    def connect(self, dsn, **kw):
        self.kwargs = kw
        self.conn = _FakeConn(self._cur)
        return self.conn


class _PushCase(unittest.TestCase):
    def setUp(self):
        self._env = {k: os.environ.get(k) for k in ("CORE_DB_URL", "CORE_DRY_RUN")}
        os.environ["CORE_DB_URL"] = "postgresql://unused/fake"
        os.environ.pop("CORE_DRY_RUN", None)
        self._mod = sys.modules.get("psycopg")

    def tearDown(self):
        for k, v in self._env.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        if self._mod is None:
            sys.modules.pop("psycopg", None)
        else:
            sys.modules["psycopg"] = self._mod

    def run_push(self, p, existing=None):
        """existing: table -> list of synced_at stamps already stored."""
        cur = _FakeCursor(existing)
        fake = _FakePsycopg(cur)
        sys.modules["psycopg"] = fake
        return fake, cur, cs.push(p)


# A stamp from an earlier run. Anything carrying this is stale by definition.
LAST_RUN = "2026-10-01T00:00:00+00:00"


class TestPushRefusesIncompletePayloads(_PushCase):
    def test_it_never_opens_a_connection(self):
        fake, _cur, ok = self.run_push(payload(aip_panics=0))
        self.assertFalse(ok)
        self.assertIsNone(fake.conn, "refused payload must not connect at all")

    def test_a_missing_programme_returns_false_rather_than_raising(self):
        # _incompleteness has always called this incomplete, but push() used
        # to build its rows first, and _rows_monthly reaches
        # payload["views"][programme]["stats"] with a hard subscript -- so the
        # KeyError landed before the check could speak. The caller caught it
        # and logged a traceback, which looks like a bug in the sink rather
        # than a bad read upstream. The gate runs first now.
        p = payload()
        del p["views"]["SBMAC"]
        fake, _cur, ok = self.run_push(p)
        self.assertFalse(ok)
        self.assertIsNone(fake.conn)

    def test_a_partial_snapshot_returns_false_without_connecting(self):
        fake, _cur, ok = self.run_push(payload(sbmac_dropped=1))
        self.assertFalse(ok)
        self.assertIsNone(fake.conn, "a partial load must not reach the CRM")


class TestPushHappyPath(_PushCase):
    def test_a_first_run_into_empty_tables_commits(self):
        fake, cur, ok = self.run_push(payload())
        self.assertTrue(ok)
        self.assertTrue(fake.conn.committed)
        self.assertEqual(fake.kwargs.get("connect_timeout"),
                         cs.CONNECT_TIMEOUT_SECONDS)
        self.assertIn(f"statement_timeout={cs.STATEMENT_TIMEOUT_MS}",
                      fake.kwargs.get("options", ""),
                      "a stalled statement must be bounded too, not just the "
                      "connect")
        # 4 monthly keys, 6 daily, 8 panics, 2 RAF -- and nothing else left.
        self.assertEqual([len(cur.tables[t]) for t in
                          ("hollard_monthly", "hollard_daily",
                           "hollard_panics", "hollard_raf_claims")],
                         [4, 6, 8, 2])

    def test_a_second_run_replaces_the_previous_rows_exactly(self):
        existing = {"hollard_monthly": [LAST_RUN] * 4,
                    "hollard_daily": [LAST_RUN] * 6,
                    "hollard_panics": [LAST_RUN] * 8,
                    "hollard_raf_claims": [LAST_RUN] * 2}
        fake, cur, ok = self.run_push(payload(), existing)
        self.assertTrue(ok)
        self.assertTrue(fake.conn.committed)
        self.assertEqual([len(cur.tables[t]) for t in
                          ("hollard_monthly", "hollard_daily",
                           "hollard_panics", "hollard_raf_claims")],
                         [4, 6, 8, 2], "the previous run's rows must be gone")
        self.assertNotIn(LAST_RUN, cur.tables["hollard_panics"])

    def test_panics_are_inserted_before_anything_is_deleted(self):
        # The old code deleted first, leaving the map empty mid-transaction.
        _fake, cur, ok = self.run_push(
            payload(), {"hollard_panics": [LAST_RUN] * 3})
        self.assertTrue(ok)
        kinds = [(k, t) for k, t, _ in cur.log]
        first_panic_write = kinds.index(("write", "hollard_panics"))
        deletes = [i for i, (k, _) in enumerate(kinds) if k == "delete"]
        self.assertTrue(all(i > first_panic_write for i in deletes),
                        f"a delete ran before the panic insert: {kinds}")

    def test_a_withdrawn_source_key_is_reconciled_away(self):
        # One monthly row more than the payload carries: its source line was
        # deleted, so the run must drop it rather than leave it for ever.
        _fake, cur, ok = self.run_push(
            payload(), {"hollard_monthly": [LAST_RUN] * 5})
        self.assertTrue(ok)
        self.assertEqual(("delete", "hollard_monthly", 5),
                         next(e for e in cur.log
                              if e[0] == "delete" and e[1] == "hollard_monthly"))
        self.assertEqual(len(cur.tables["hollard_monthly"]), 4)


class TestDailyAggregation(unittest.TestCase):
    """Duplicate dates must be summed, not collapsed to the last row."""

    def test_two_rows_for_one_day_are_summed(self):
        # The real source has this: SBMAC carries two rows for 2021-10-01,
        # with 2 and 3 call-outs. Keeping the last would put the CRM 2
        # call-outs behind the dashboard for ever.
        p = payload(sbmac_daily=1)
        p["views"]["SBMAC"]["daily"] = [
            {"date": "2021-10-01", "callouts": 2, "cumulative": 2, "completed": 1},
            {"date": "2021-10-01", "callouts": 3, "cumulative": 3, "completed": 2},
        ]
        rows = [r for r in cs._rows_daily(p) if r[0] == "SBMAC"]
        self.assertEqual(len(rows), 1, "one row per (programme, day)")
        _prog, day, callouts, cumulative, completed = rows[0]
        self.assertEqual(day, "2021-10-01")
        self.assertEqual((callouts, cumulative, completed), (5, 5, 3))

    def test_distinct_days_stay_distinct(self):
        p = payload(sbmac_daily=1)
        p["views"]["SBMAC"]["daily"] = [
            {"date": "2021-10-01", "callouts": 2, "cumulative": 2, "completed": 1},
            {"date": "2021-10-02", "callouts": 3, "cumulative": 3, "completed": 2},
        ]
        rows = [r for r in cs._rows_daily(p) if r[0] == "SBMAC"]
        self.assertEqual([r[1] for r in rows], ["2021-10-01", "2021-10-02"])
        self.assertEqual([r[2] for r in rows], [2, 3])


class TestPushBatching(_PushCase):
    def test_no_batch_exceeds_the_limit(self):
        # 450 panic pins per programme = 900 rows through one call site.
        _fake, cur, ok = self.run_push(payload(aip_panics=450,
                                               sbmac_panics=450))
        self.assertTrue(ok)
        self.assertTrue(cur.batches, "nothing was written")
        self.assertTrue(all(b <= cs.WRITE_BATCH for b in cur.batches),
                        f"batch over {cs.WRITE_BATCH}: {cur.batches}")
        panic_rows = sum(n for k, t, n in cur.log
                         if k == "write" and t == "hollard_panics")
        self.assertEqual(panic_rows, 900, "every pin must be written once")
        self.assertEqual(len(cur.tables["hollard_panics"]), 900)


class TestPushEndStateGuard(_PushCase):
    def test_a_delete_that_loses_its_stamp_filter_is_caught(self):
        # The regression the fake now models: if the reconcile DELETE drops
        # its `synced_at <> %s` clause it takes the rows this run just wrote,
        # and the end-state count must refuse to commit.
        real_execute = _FakeCursor.execute

        def blunt(self, sql, params=None):
            if " ".join(sql.split()).lower().startswith("delete"):
                sql = sql.replace("and synced_at <> %s", "")
                params = params[:-1]
            return real_execute(self, sql, params)

        _FakeCursor.execute = blunt
        try:
            with self.assertRaises(RuntimeError) as e:
                self.run_push(payload(), {"hollard_monthly": [LAST_RUN]})
            self.assertIn("rolling back", str(e.exception))
        finally:
            _FakeCursor.execute = real_execute

    def test_a_delete_removing_a_different_number_rolls_back(self):
        existing = {"hollard_monthly": [LAST_RUN] * 2}
        cur = _FakeCursor(existing)
        fake = _FakePsycopg(cur)
        sys.modules["psycopg"] = fake

        real_execute = cur.execute

        def execute(sql, params=None):
            real_execute(sql, params)
            if " ".join(sql.split()).lower().startswith("delete"):
                cur.rowcount = 777      # disagrees with the 2 just counted
        cur.execute = execute

        with self.assertRaises(RuntimeError) as e:
            cs.push(payload())
        self.assertIn("counted a moment earlier", str(e.exception))
        self.assertFalse(fake.conn.committed)
        self.assertIs(fake.conn.clean, False)

        # "commit() was not called" is only half the claim. What matters is
        # what the database is left holding, so check every table the batch
        # could have written -- all four -- against what it held going in.
        self.assertTrue(cur.rolled_back)
        for table, before in cur.initial.items():
            self.assertEqual(cur.tables[table], before,
                             f"{table} did not come back to its initial rows")
        self.assertEqual(cur.initial["hollard_monthly"], [LAST_RUN] * 2)


class TestPushRespectsItsBudget(_PushCase):
    """The sink must give up in time for the dashboard's own steps to run.

    The workflow step that calls export_summary.py has timeout-minutes: 5,
    and the summary.json commit and the Pages deploy are steps AFTER it. A
    slow-but-not-stalled database could therefore use the whole step budget
    inside the sink and get the step killed, leaving summary.json written in
    the workspace and never published. The per-statement timeout cannot see
    that: ~50 statements each comfortably under 90s still add up.
    """

    def test_an_overrun_raises_rather_than_writing_on(self):
        import time as _time
        real = _time.monotonic
        # Budget spent the moment the clock is read for the second time: the
        # first read sets the deadline, every read after it is past.
        calls = {"n": 0}

        def creeping():
            calls["n"] += 1
            return real() + (0 if calls["n"] == 1 else cs.SINK_BUDGET_SECONDS + 1)

        _time.monotonic = creeping
        try:
            cur = _FakeCursor()
            fake = _FakePsycopg(cur)
            sys.modules["psycopg"] = fake
            with self.assertRaises(RuntimeError) as e:
                cs.push(payload())
            msg = str(e.exception)
            self.assertIn("exceeded its", msg)
            self.assertIn("so the dashboard", msg)
            self.assertFalse(fake.conn.committed)
            self.assertTrue(cur.rolled_back)
            for table, before in cur.initial.items():
                self.assertEqual(cur.tables[table], before)
        finally:
            _time.monotonic = real

    def test_the_caller_turns_an_overrun_into_a_skipped_sink(self):
        # The point of raising: export_summary calls push() inside a
        # try/except, so an overrun stops the sink WITHOUT failing the step,
        # and the commit and deploy steps still run. If that call ever stops
        # being guarded, this comment is the thing that was relied on.
        src = open(os.path.join(
            os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
            "scripts", "export_summary.py")).read()
        i = src.index("_push_to_core(payload)")
        self.assertIn("try:", src[max(0, i - 200):i],
                      "push() must stay inside a try/except in the caller")
        self.assertIn("except Exception", src[i:i + 200])

    def test_the_budget_is_checked_between_batches_not_just_per_table(self):
        # Daily and panics are ~19 batches each on the real payload, so a
        # check taken once per table would let the budget run out INSIDE one
        # of them and never notice. The first draft of these tests did not
        # prove this: deleting the per-batch check left them all green,
        # because the reconcile check fired first and hid it. This one
        # expires the clock partway through the daily write and requires the
        # remaining batches never to be sent.
        import time as _time
        real = _time.monotonic
        # Call 1 sets the deadline. Call 2 is the monthly batch, call 3 the
        # first daily batch; from call 4 -- the second daily batch -- the
        # budget is gone.
        calls = {"n": 0}

        def expire_on_fourth_look():
            calls["n"] += 1
            over = calls["n"] >= 4
            return real() + (cs.SINK_BUDGET_SECONDS + 1 if over else 0)

        _time.monotonic = expire_on_fourth_look
        try:
            cur = _FakeCursor()
            fake = _FakePsycopg(cur)
            sys.modules["psycopg"] = fake
            # 253 daily rows -> batches of [200, 53]; the second must not go.
            with self.assertRaises(RuntimeError) as e:
                cs.push(payload(aip_daily=250))
            self.assertIn("daily batch 2", str(e.exception))
            self.assertEqual(cur.batches[-1], cs.WRITE_BATCH,
                             "it stopped after the full first daily batch")
            self.assertEqual(sum(cur.batches), 4 + 200,
                             "4 monthly rows and one daily batch, nothing more")
            self.assertFalse(fake.conn.committed)
            self.assertTrue(cur.rolled_back)
        finally:
            _time.monotonic = real

    def test_a_healthy_run_is_nowhere_near_the_budget(self):
        # Guards against a budget so tight that a normal refresh trips it.
        _fake, _cur, ok = self.run_push(payload())
        self.assertTrue(ok)


class TestPushProvesTheWriteBeforeDeleting(_PushCase):
    """_reconcile counts this run's own rows before it removes anyone else's.

    The end-state check would catch a bad delete as well -- it is one
    transaction, so nothing wrong commits either way -- but it catches it
    after the fact. These two make the sink refuse on the strength of the
    write itself, and say which part did not add up.
    """

    def test_a_short_insert_is_caught_before_the_delete_runs(self):
        # The insert silently puts in one row fewer than push() believes it
        # wrote. Nothing about the delete is wrong; the arithmetic behind it
        # is. The sink must say so and not delete the previous run's rows on
        # the strength of it.
        real_many = _FakeCursor.executemany

        def short(self, sql, seq):
            if "hollard_monthly" in sql:
                seq = list(seq)[:-1]
            return real_many(self, sql, seq)

        _FakeCursor.executemany = short
        try:
            cur = _FakeCursor({"hollard_monthly": [LAST_RUN] * 3})
            fake = _FakePsycopg(cur)
            sys.modules["psycopg"] = fake
            with self.assertRaises(RuntimeError) as e:
                cs.push(payload())
            msg = str(e.exception)
            self.assertIn("carry this run's stamp", msg)
            self.assertIn("refusing to delete", msg)
            self.assertFalse(fake.conn.committed)
            # The three older rows are still there: it refused rather than
            # half-finishing.
            self.assertEqual(cur.tables["hollard_monthly"], [LAST_RUN] * 3)
            self.assertNotIn("delete",
                             [kind for kind, *_ in cur.log],
                             "no delete may run once the counts disagree")
        finally:
            _FakeCursor.executemany = real_many

    def test_the_happy_path_still_counts_both_ways(self):
        # Guards the guard: the fresh-row count must be taken for every
        # table, or the check above is only pretending to run.
        _fake, cur, ok = self.run_push(payload())
        self.assertTrue(ok)
        counted = [t for kind, t, _n in cur.log if kind == "count"]
        for table in ("hollard_monthly", "hollard_daily", "hollard_panics",
                      "hollard_raf_claims"):
            self.assertGreaterEqual(
                counted.count(table), 3,
                f"{table} must be counted stale, fresh and in total")


if __name__ == "__main__":
    unittest.main()
