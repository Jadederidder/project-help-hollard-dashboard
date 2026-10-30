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
import pathlib
import threading
import time
import unittest

sys.path.insert(0, os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "scripts"))
import core_sink as cs


def payload(aip_stats=2, aip_daily=3, aip_panics=4,
            sbmac_stats=2, sbmac_daily=3, sbmac_panics=4, raf=2,
            aip_dropped=0, sbmac_dropped=0, raf_dropped=0):
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
        "rafSourceDropped": raf_dropped,
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

    def test_dropped_raf_rows_are_a_problem(self):
        # hollard_raf_claims is reconciled the same way hollard_daily is, so
        # a RAF month the exporter could not read is lost the same way. The
        # count is top-level because RAF is one shared table.
        self.assertIn(
            "the exporter dropped 1 RAF source row(s) it could not read, so "
            "this snapshot is partial",
            cs._incompleteness(payload(raf_dropped=1)))

    def test_a_payload_that_does_not_say_what_raf_dropped_is_refused(self):
        p = payload()
        del p["rafSourceDropped"]
        self.assertIn(
            "the payload does not say how many RAF source rows were dropped, "
            "so a partial load cannot be ruled out",
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
        self.cancelled = False
        # The timeout the watchdog handed cancel_safe(), so a call that
        # quietly drops it cannot pass.
        self.cancel_timeout = None
        # Every `set local statement_timeout` value the sink asked for, in
        # order. This is how the budget's hardness is checked: a bound that is
        # only applied once is not a bound.
        self.timeouts = []
        self.rowcount = 0
        self.log = []
        self.statements_run = []
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

    def cancel(self, timeout=None):
        """Stands in for psycopg's cancel_safe()."""
        self.cancelled = True
        self.cancel_timeout = timeout

    def execute(self, sql, params=None):
        low = " ".join(sql.split()).lower()
        self.statements_run.append((low, params))
        # This fake used to accept `set local statement_timeout = %s`, and
        # that is why 57 tests passed over code that could not run: a real
        # server rejects a bind parameter in SET with 42601, at the first
        # statement of the transaction, so the sink never wrote a row. A fake
        # that is more permissive than the server does not prove the code
        # works, it proves the fake agrees with it. It refuses that form now.
        if low.startswith("set ") and params:
            raise AssertionError(
                "PostgreSQL does not accept a bind parameter in SET (42601); "
                "use select set_config(name, value, is_local)")
        if low.startswith("select set_config('statement_timeout'"):
            # The sink passes text, as set_config requires. Recorded as an
            # int because the assertions compare it with numbers -- and
            # because Postgres reads 0 as "no timeout", which is the one
            # value that must never be asked for.
            assert params, "set_config must be called with a value"
            self.timeouts.append(int(params[0]))
            self.log.append(("timeout", None, self.timeouts[-1]))
            return
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

    def cancel_safe(self, *, timeout=None):
        self._cur.cancel(timeout)

    def cancel(self):
        # Not a server behaviour -- our own decision, held here so it cannot
        # be undone silently. psycopg 3.2 deprecated cancel(): it opens an
        # unencrypted, unauthenticated connection and takes no timeout, so
        # on the watchdog's daemon thread a stalled cancel would be
        # invisible. Verified present in the pinned psycopg 3.2.3:
        # Connection.cancel_safe(*, timeout: float = 30.0).
        raise AssertionError(
            "the watchdog must call cancel_safe(timeout=...), not the "
            "cancel() psycopg deprecated in 3.2")

    def commit(self):
        self.committed = True


class _FakePsycopg:
    def __init__(self, cur):
        self._cur, self.conn, self.kwargs = cur, None, None
        # Overridden by the tests that need a connection which behaves
        # badly in one specific way.
        self.conn_class = _FakeConn

    def connect(self, dsn, **kw):
        self.kwargs = kw
        self.conn = self.conn_class(self._cur)
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
            self.assertIn("of its", msg)
            self.assertIn("budget", msg)
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
        # Keyed on the WORK done, not on a count of clock reads. The first
        # version of this test counted monotonic() calls and expired on the
        # fourth; adding the watchdog introduced one more caller and the test
        # then failed on "daily batch 1" -- right outcome, wrong reason, and
        # it would have gone green again for the wrong reason just as easily.
        # The budget now expires once the first full daily batch has been
        # sent, whatever else reads the clock.
        state = {"expired": False}

        def clock():
            return real() + (cs.SINK_BUDGET_SECONDS + 1
                             if state["expired"] else 0)

        real_many = _FakeCursor.executemany

        def expire_after_the_first_daily_batch(self, sql, seq):
            out = real_many(self, sql, seq)
            if "hollard_daily" in sql:
                state["expired"] = True
            return out

        _time.monotonic = clock
        _FakeCursor.executemany = expire_after_the_first_daily_batch
        try:
            cur = _FakeCursor()
            fake = _FakePsycopg(cur)
            sys.modules["psycopg"] = fake
            # 253 daily rows -> batches of [200, 53]; the second must not go.
            with self.assertRaises(RuntimeError) as e:
                cs.push(payload(aip_daily=250))
            self.assertIn("daily batch 2", str(e.exception),
                          "it must give up at the batch it could not bound")
            self.assertEqual(cur.batches[-1], cs.WRITE_BATCH,
                             "it stopped after the full first daily batch")
            self.assertEqual(sum(cur.batches), 4 + 200,
                             "4 monthly rows and one daily batch, nothing more")
            self.assertFalse(fake.conn.committed)
            self.assertTrue(cur.rolled_back)
        finally:
            _time.monotonic = real
            _FakeCursor.executemany = real_many

    def test_every_statement_is_given_only_the_time_that_is_left(self):
        # The gap the reviewer found. The first version checked the clock
        # before an operation and then let it run for the full
        # STATEMENT_TIMEOUT_MS, so four statements inside one reconcile could
        # spend 360s against a 120s budget and the workflow step would be
        # killed before Python raised. A bound on when a statement STARTS is
        # not a bound.
        _fake, cur, ok = self.run_push(payload())
        self.assertTrue(ok)
        self.assertGreater(len(cur.timeouts), 10,
                           "a timeout must be set before every statement")
        for t in cur.timeouts:
            self.assertIsNotNone(t)
            self.assertGreater(t, 0, "0 means NO timeout in Postgres")
            self.assertLessEqual(t, cs.STATEMENT_TIMEOUT_MS)
        # Monotonically non-increasing: each statement gets what is left, so
        # the total cannot run past the deadline.
        self.assertEqual(cur.timeouts, sorted(cur.timeouts, reverse=True))

    def test_no_statement_runs_without_a_fresh_bound(self):
        # Order, not just presence: every real statement must be immediately
        # preceded by a timeout. A single bound set once at the top would
        # satisfy a count but not this.
        _fake, cur, ok = self.run_push(payload())
        self.assertTrue(ok)
        kinds = [kind for kind, *_ in cur.log]
        for i, kind in enumerate(kinds):
            if kind in ("count", "delete", "write"):
                self.assertEqual(
                    kinds[i - 1], "timeout",
                    f"{kind} at position {i} ran without a fresh bound")

    def test_an_overrun_inside_reconciliation_raises(self):
        # The specific regression asked for: expire the budget AFTER the
        # writes, once reconciliation has started, rather than between
        # batches.
        import time as _time
        real = _time.monotonic
        state = {"expired": False}

        def clock():
            return real() + (cs.SINK_BUDGET_SECONDS + 1
                             if state["expired"] else 0)

        _time.monotonic = clock
        try:
            cur = _FakeCursor({"hollard_monthly": [LAST_RUN]})
            real_execute = _FakeCursor.execute

            def trip_on_first_reconcile_count(self, sql, params=None):
                low = " ".join(sql.split()).lower()
                if low.startswith("select count(*)"):
                    state["expired"] = True
                return real_execute(self, sql, params)

            _FakeCursor.execute = trip_on_first_reconcile_count
            try:
                fake = _FakePsycopg(cur)
                sys.modules["psycopg"] = fake
                with self.assertRaises(RuntimeError) as e:
                    cs.push(payload())
                self.assertIn("reconcile", str(e.exception),
                              "it must name the reconcile step it gave up in")
                self.assertFalse(fake.conn.committed)
                self.assertTrue(cur.rolled_back)
            finally:
                _FakeCursor.execute = real_execute
        finally:
            _time.monotonic = real

    def _with_remaining(self, seconds_left):
        """Run push() with the budget already mostly spent."""
        import time as _time
        real = _time.monotonic
        # push() sets deadline = monotonic() + SINK_BUDGET_SECONDS on its
        # first read, so shifting every LATER read forward leaves exactly
        # `seconds_left` on the clock.
        spent = cs.SINK_BUDGET_SECONDS - seconds_left
        calls = {"n": 0}

        def clock():
            calls["n"] += 1
            return real() + (0 if calls["n"] == 1 else spent)

        _time.monotonic = clock
        try:
            cur = _FakeCursor()
            fake = _FakePsycopg(cur)
            sys.modules["psycopg"] = fake
            try:
                return cur, None, cs.push(payload())
            except RuntimeError as e:
                return cur, e, None
        finally:
            _time.monotonic = real

    def test_the_reserve_is_held_back_from_the_statements(self):
        # Written against BEHAVIOUR, not against the constant. My first
        # version asserted max(timeouts) <= budget - COMMIT_RESERVE_MS, which
        # zeroing the constant satisfied trivially -- and the 90s statement
        # cap hid it as well, because the reserve only bites near the end of
        # the budget. So this drives the clock to the end instead.
        #
        # 10s left: the statements must be handed 10s MINUS the reserve, not
        # the whole 10s, or the commit would land outside the budget.
        cur, err, ok = self._with_remaining(10)
        self.assertIsNone(err, f"10s should still be workable, got {err}")
        self.assertTrue(ok)
        self.assertLessEqual(max(cur.timeouts), 10_000 - cs.COMMIT_RESERVE_MS)
        # Measured as time WITHHELD, with a floor that does not come from the
        # constant under test. My first attempt asserted
        # `max(timeouts) < 10_000`, which passed with the reserve zeroed
        # because sub-millisecond drift truncates 10000 to 9999 -- the test
        # was off by one millisecond and proved nothing.
        self.assertLessEqual(
            max(cur.timeouts), 9_000,
            "at least a second of the budget must be withheld for the commit")

    def test_it_gives_up_while_there_is_still_time_to_commit(self):
        # With only the reserve plus a sliver left there is no honest amount
        # to hand a statement, so the sink stops rather than starting one it
        # cannot bound.
        cur, err, _ok = self._with_remaining(
            (cs.COMMIT_RESERVE_MS + cs.MIN_STATEMENT_MS) / 1000 - 0.5)
        self.assertIsNotNone(err, "it must refuse, not hand out a sliver")
        self.assertIn("left of its", str(err))
        self.assertEqual(cur.timeouts, [],
                         "no statement may be bounded once it has given up")

    def test_the_timeout_is_set_in_a_form_postgres_accepts(self):
        # The defect that 57 passing tests did not catch, because the fake
        # was more permissive than the server. Proven on the live PostgreSQL
        # 17 server: `prepare as set local statement_timeout = $1` is
        # rejected with 42601, while the set_config form is accepted. psycopg
        # 3 binds server-side, so the old statement raised at the FIRST call
        # in the transaction and the sink never wrote a row.
        _fake, cur, ok = self.run_push(payload())
        self.assertTrue(ok)
        setters = [sql for sql, _p in cur.statements_run
                   if "statement_timeout" in sql]
        self.assertTrue(setters, "the sink must bound its statements")
        for sql in setters:
            self.assertIn("set_config", sql)
            self.assertNotIn("set local", sql)

    def test_the_fake_refuses_a_parameterised_set(self):
        # Guards the guard. If the fake ever accepts that form again, the
        # original defect can come back invisibly, so the fake's own refusal
        # is held by a test rather than left to a reader to notice.
        cur = _FakeCursor()
        with self.assertRaises(AssertionError) as e:
            cur.execute("set local statement_timeout = %s", (5000,))
        self.assertIn("42601", str(e.exception))
        # ...and the valid form is still accepted.
        cur.execute("select set_config('statement_timeout', %s, true)",
                    ("5000",))
        self.assertEqual(cur.timeouts, [5000])

    def test_a_watchdog_is_armed_and_always_stopped(self):
        # statement_timeout bounds one statement; executemany sends up to 200
        # of them. Only cancelling in-flight work makes the budget hard, so a
        # timer does it -- and it must be stopped on the way out, or a
        # cancel could land on a later transaction.
        # Probed by behaviour, not by is_alive(): Timer.cancel() prevents the
        # action but the thread winds down on its own time, so is_alive() can
        # still be True immediately after a correct cancel. My first version
        # of this test asserted that and failed for a reason that had nothing
        # to do with the code.
        armed, cancelled = [], []
        real_timer = cs.threading.Timer

        class _Spy(real_timer):
            def __init__(self, interval, function, *a, **kw):
                super().__init__(interval, function, *a, **kw)
                armed.append(self)

            def cancel(self):
                cancelled.append(self)
                return super().cancel()

        cs.threading.Timer = _Spy
        try:
            _fake, cur, ok = self.run_push(payload())
        finally:
            cs.threading.Timer = real_timer
        self.assertTrue(ok)
        self.assertEqual(len(armed), 1, "exactly one watchdog per push")
        self.assertTrue(armed[0].daemon, "it must not hold the process open")
        # Twice, by design: once explicitly before the commit, and again in
        # the context manager's finally, which is what makes it unconditional
        # on every exit path including an exception. Timer.cancel() is
        # idempotent, so the second is free.
        self.assertGreaterEqual(len(cancelled), 1,
                                "the watchdog must be stopped on the way out")
        self.assertFalse(cur.cancelled,
                         "a healthy run must never cancel the transaction")

    def test_the_watchdog_stops_before_the_commit(self):
        # Deliberate: cancelling a COMMIT leaves it unknown whether it
        # applied, and a CRM that might or might not agree with the dashboard
        # is worse than a refresh that plainly did not happen. So the timer
        # must already be dead by the time commit() is called.
        state = {"stopped": False}
        real_timer = cs.threading.Timer

        class _Spy(real_timer):
            def cancel(self):
                state["stopped"] = True
                return super().cancel()

        cur = _FakeCursor()
        fake = _FakePsycopg(cur)
        sys.modules["psycopg"] = fake
        real_commit = _FakeConn.commit

        def commit(self):
            state["stopped_at_commit"] = state["stopped"]
            return real_commit(self)

        _FakeConn.commit = commit
        cs.threading.Timer = _Spy
        try:
            self.assertTrue(cs.push(payload()))
        finally:
            cs.threading.Timer = real_timer
            _FakeConn.commit = real_commit
        self.assertIs(state["stopped_at_commit"], True,
                      "the watchdog must be stopped BEFORE commit() is "
                      "called, so a cancel can never land on the commit")

    def test_a_late_fire_refuses_the_commit(self):
        # Timer.cancel() only clears the wait. A fire() that has already
        # begun runs to completion regardless, so "stop it before the
        # commit" did not actually hold: the main thread could call
        # cancel(), get nothing, and commit while the timer thread cancelled
        # underneath it. CodeRabbit caught it, and the window is not
        # theoretical -- the last _bounded() call sets a statement_timeout
        # that expires at the very instant the timer fires, so a statement
        # that uses its whole bound returns just as fire() starts.
        #
        # The spy models that exactly: cancel() returns having already run
        # the callback. The sink must then roll back, not commit.
        real_timer = cs.threading.Timer

        class _Raced(real_timer):
            def cancel(self):
                out = super().cancel()
                self.function()         # the callback was already running
                return out

        cur = _FakeCursor()
        fake = _FakePsycopg(cur)
        sys.modules["psycopg"] = fake
        cs.threading.Timer = _Raced
        try:
            with self.assertRaises(RuntimeError) as e:
                cs.push(payload())
        finally:
            cs.threading.Timer = real_timer
        self.assertIn("refusing to commit", str(e.exception))
        self.assertFalse(fake.conn.committed,
                         "a cancel in flight must never be committed on top "
                         "of -- that is the ambiguous outcome the whole "
                         "arrangement exists to avoid")
        self.assertTrue(cur.rolled_back)
        self.assertEqual(cur.tables, cur.initial,
                         "and the CRM must be left holding the previous "
                         "run's rows, not a half-written snapshot")

    def test_a_fire_that_is_still_starting_up_is_waited_for(self):
        # The narrower half, and the reason the join() is there rather than
        # just the flag. Once the timer thread has left its wait it is a few
        # instructions away from setting `fired`; a main thread that read the
        # flag without joining could see False and commit anyway. So this
        # spy enters the callback but stalls before it, which is precisely
        # that window. Dropping the join leaves the sink committing here.
        real_timer = cs.threading.Timer

        class _Stalled(real_timer):
            def run(self):
                time.sleep(0.05)        # inside fire(), before fired.set()
                self.function()

        cur = _FakeCursor()
        fake = _FakePsycopg(cur)
        sys.modules["psycopg"] = fake
        cs.threading.Timer = _Stalled
        try:
            with self.assertRaises(RuntimeError) as e:
                cs.push(payload())
        finally:
            cs.threading.Timer = real_timer
        self.assertIn("refusing to commit", str(e.exception))
        self.assertFalse(fake.conn.committed)
        self.assertTrue(cur.cancelled,
                        "the stalled callback must have gone on to cancel, "
                        "or this tests nothing about waiting for it")

    def test_a_cancel_slower_than_the_join_still_refuses_the_commit(self):
        # A cancel slower than the join: the join is bounded by the commit
        # reserve and so is the cancel, so the two can expire together and
        # the main thread stops waiting while the timer thread is still
        # inside cancel_safe(). It must still refuse.
        #
        # Note what this does and does not pin. It was written to prove
        # fired.set() belongs first, and it did -- but the shutdown now
        # also refuses a thread still alive after the join, which catches
        # the same case on its own. So this no longer makes the ordering
        # load-bearing; it holds the refusal for a cancel in flight, by
        # whichever of the two conditions sees it first.
        #
        # The reserve is shrunk for the test so the join expires in 10ms
        # against a cancel that takes 500, rather than waiting out the real
        # five seconds.
        real_timer, real_reserve = cs.threading.Timer, cs.COMMIT_RESERVE_MS

        class _Immediate(real_timer):
            def run(self):
                self.function()

        class _SlowCancel(_FakeConn):
            def cancel_safe(self, *, timeout=None):
                time.sleep(0.5)         # still in flight when join gives up
                return super().cancel_safe(timeout=timeout)

        cur = _FakeCursor()
        fake = _FakePsycopg(cur)
        fake.conn_class = _SlowCancel
        sys.modules["psycopg"] = fake
        cs.threading.Timer = _Immediate
        cs.COMMIT_RESERVE_MS = 10
        try:
            with self.assertRaises(RuntimeError) as e:
                cs.push(payload())
        finally:
            cs.threading.Timer = real_timer
            cs.COMMIT_RESERVE_MS = real_reserve
        self.assertIn("refusing to commit", str(e.exception))
        self.assertFalse(fake.conn.committed,
                         "a join that gives up must not be read as 'the "
                         "watchdog never fired'")

    def test_a_callback_that_outlasts_the_join_refuses_the_commit(self):
        # The narrowest window, and the one `fired` alone could not close.
        # Timer.run() checks `finished` and then calls the callback; a
        # thread descheduled between those two has decided to fire but has
        # not reached fired.set(), so a bounded join can expire with the
        # flag still false and the main thread would commit into a cancel
        # that is still coming. Liveness after the join is what catches it.
        #
        # Deterministic rather than hopeful: the reserve is shrunk so the
        # join expires in 10ms, and the thread is held for 300 before it
        # touches the callback. `fired` is provably false at the moment of
        # the decision, which is the whole point of the case.
        real_timer, real_reserve = cs.threading.Timer, cs.COMMIT_RESERVE_MS
        started = threading.Event()
        timers = []

        class _Stalled(real_timer):
            def __init__(self, interval, function, *a, **kw):
                super().__init__(interval, function, *a, **kw)
                timers.append(self)

            def run(self):
                started.set()
                time.sleep(0.3)         # decided to fire, not yet fired
                self.function()

        cur = _FakeCursor()
        fake = _FakePsycopg(cur)
        sys.modules["psycopg"] = fake
        cs.threading.Timer = _Stalled
        cs.COMMIT_RESERVE_MS = 10
        try:
            with self.assertRaises(RuntimeError) as e:
                cs.push(payload())
            self.assertTrue(started.is_set(), "the stall never happened")
            self.assertFalse(
                timers[0].fired.is_set(),
                "if the flag is already set this is the other test, not "
                "this one -- the gap being covered is precisely a thread "
                "that has not reached fired.set() yet")
        finally:
            cs.threading.Timer = real_timer
            cs.COMMIT_RESERVE_MS = real_reserve
            for t in timers:            # let the stalled thread finish
                t.join(2)
        self.assertIn("refusing to commit", str(e.exception))
        self.assertFalse(fake.conn.committed,
                         "a join that expires on a live thread must not be "
                         "read as 'the watchdog never fired'")
        self.assertEqual(cur.tables, cur.initial,
                         "and the CRM keeps the previous run's rows")

    def test_the_sink_cancels_with_the_deprecated_call_nowhere_in_sight(self):
        # psycopg 3.2 deprecated cancel() -- it opens an unencrypted,
        # unauthenticated connection and takes no timeout, so a stalled
        # cancel on the watchdog's daemon thread would be invisible. The
        # fake connection raises on cancel(), so a regression to it fails
        # loudly instead of passing; this holds the source clean too, since
        # a call added somewhere the tests never reach would not trip that.
        self.assertNotIn(
            "conn.cancel()",
            pathlib.Path(cs.__file__).read_text(),
            "the watchdog must call cancel_safe(timeout=...)")

    def test_the_watchdog_actually_fires_and_cancels(self):
        # The gap my first pass left: deleting `timer.start()` kept all 61
        # tests green, so nothing proved the watchdog did anything at all. It
        # is the only thing that bounds a batch of 200 executions, so "it was
        # constructed and cancelled" is not enough -- it has to fire.
        #
        # Tested on _watchdog directly, with a deadline already spent, so the
        # timer interval is 0 and it fires at once. Going through push() for
        # this would mean waiting out a 115-second timer.
        class _Conn:
            def __init__(self):
                self.cancels, self.timeout = 0, None

            def cancel_safe(self, *, timeout=None):
                self.cancels += 1
                self.timeout = timeout

        conn = _Conn()
        with cs._watchdog(conn, cs.time.monotonic() - 1) as timer:
            timer.join(2)
            self.assertEqual(conn.cancels, 1,
                             "a spent budget must cancel the transaction")
            self.assertEqual(conn.timeout, cs.COMMIT_RESERVE_MS / 1000,
                             "the cancel must be bounded by the only "
                             "headroom there is, or it can hang the thread")
            self.assertTrue(timer.fired.is_set(),
                            "a fired watchdog must say so, because that is "
                            "what stops the commit")

    def test_the_watchdog_is_stopped_even_when_the_push_raises(self):
        # Why the context manager's finally exists. On the happy path the
        # explicit cancel() before commit already stops it, so removing the
        # finally left all 64 tests green -- it only matters when the body
        # raises, which is exactly when it matters most: a timer left running
        # after the transaction is gone could fire cancel() on whatever the
        # connection does next.
        cancelled = []
        real_timer = cs.threading.Timer

        class _Spy(real_timer):
            def cancel(self):
                cancelled.append(self)
                return super().cancel()

        real_many = _FakeCursor.executemany

        def blow_up(self, sql, seq):
            raise RuntimeError("the insert exploded")

        cs.threading.Timer = _Spy
        _FakeCursor.executemany = blow_up
        try:
            cur = _FakeCursor()
            fake = _FakePsycopg(cur)
            sys.modules["psycopg"] = fake
            with self.assertRaises(RuntimeError):
                cs.push(payload())
        finally:
            cs.threading.Timer = real_timer
            _FakeCursor.executemany = real_many
        self.assertTrue(cancelled,
                        "the watchdog must be stopped on the exception path "
                        "too, not only before a successful commit")
        self.assertFalse(fake.conn.committed)

    def test_the_watchdog_does_not_fire_when_stopped_in_time(self):
        # The other half: a healthy run must never cancel. Armed with a
        # deadline far enough out that only an unstopped timer could fire.
        class _Conn:
            def __init__(self):
                self.cancels = 0

            def cancel_safe(self, *, timeout=None):
                self.cancels += 1

        conn = _Conn()
        with cs._watchdog(conn, cs.time.monotonic() + 3600) as timer:
            pass
        timer.join(1)
        self.assertEqual(conn.cancels, 0,
                         "a stopped watchdog must never cancel")
        self.assertFalse(timer.fired.is_set(),
                         "and must not claim it fired, or every healthy run "
                         "would refuse its own commit")

    def test_the_watchdog_survives_a_connection_that_cannot_cancel(self):
        # It runs on a timer thread, so an exception in it would be swallowed
        # by threading and lost. It must log and carry on rather than die
        # silently -- and must not take the process with it.
        class _Conn:
            def cancel_safe(self, *, timeout=None):
                raise RuntimeError("connection already gone")

        with self.assertLogs("core_sink", level="ERROR") as logs:
            with cs._watchdog(_Conn(), cs.time.monotonic() - 1) as timer:
                timer.join(2)
        self.assertTrue(
            any("cancelling the transaction failed" in m for m in logs.output),
            f"the failure must be logged, got {logs.output}")

    def test_no_helper_can_be_called_without_a_deadline(self):
        # The budget helpers used to take deadline=None and skip the bound
        # entirely. That is the same shape as every other defect found in
        # this file -- a guard that looks present and does nothing -- so the
        # argument is now required and this holds it that way.
        import inspect
        for fn in (cs._bounded, cs._write_batched, cs._reconcile):
            params = inspect.signature(fn).parameters
            self.assertIn("deadline", params, fn.__name__)
            self.assertIs(params["deadline"].default,
                          inspect.Parameter.empty,
                          f"{fn.__name__} lets deadline default, so a caller "
                          "can silently get no bound")

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
