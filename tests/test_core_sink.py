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
from collections import deque

sys.path.insert(0, os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "scripts"))
import core_sink as cs


def payload(aip_stats=2, aip_daily=3, aip_panics=4,
            sbmac_stats=2, sbmac_daily=3, sbmac_panics=4, raf=2):
    """A structurally complete payload, sized small enough to reason about."""
    def view(n_stats, n_daily, n_panics, tag):
        return {
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
        "views": {"AIP": view(aip_stats, aip_daily, aip_panics, "AIP"),
                  "SBMAC": view(sbmac_stats, sbmac_daily, sbmac_panics, "S")},
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


class _FakeCursor:
    def __init__(self, counts):
        self.counts = deque(counts)
        self.rowcount = 0
        self.log = []            # ("count"|"delete"|"write", table, n)
        self.batches = []

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
        return "?"

    def execute(self, sql, params=None):
        low = " ".join(sql.split()).lower()
        if low.startswith("select count(*)"):
            self.log.append(("count", self._table(low), None))
            self._next = self.counts.popleft()
        elif low.startswith("delete"):
            self.log.append(("delete", self._table(low), None))
            self.rowcount = self._next

    def fetchone(self):
        return (self._next,)

    def executemany(self, sql, seq):
        self.log.append(("write", self._table(sql), len(seq)))
        self.batches.append(len(seq))


class _FakeConn:
    def __init__(self, cur):
        self._cur, self.committed, self.clean = cur, False, None

    def __enter__(self):
        return self

    def __exit__(self, exc_type, *a):
        self.clean = exc_type is None
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

    def run_push(self, p, counts):
        cur = _FakeCursor(counts)
        fake = _FakePsycopg(cur)
        sys.modules["psycopg"] = fake
        return fake, cur, cs.push(p)


class TestPushRefusesIncompletePayloads(_PushCase):
    def test_it_never_opens_a_connection(self):
        fake, _cur, ok = self.run_push(payload(aip_panics=0), [])
        self.assertFalse(ok)
        self.assertIsNone(fake.conn, "refused payload must not connect at all")


class TestPushHappyPath(_PushCase):
    def test_it_writes_then_reconciles_and_commits(self):
        p = payload()
        # Four reconciles, each: stale count, then the post-write count which
        # must equal what was written (4 monthly, 6 daily, 8 panics, 2 RAF).
        fake, _cur, ok = self.run_push(p, [0, 4, 0, 6, 0, 8, 0, 2])
        self.assertTrue(ok)
        self.assertTrue(fake.conn.committed)
        self.assertEqual(fake.kwargs.get("connect_timeout"),
                         cs.CONNECT_TIMEOUT_SECONDS)

    def test_panics_are_inserted_before_anything_is_deleted(self):
        # The old code deleted first, leaving the map empty mid-transaction.
        p = payload()
        _fake, cur, ok = self.run_push(p, [0, 4, 0, 6, 3, 8, 0, 2])
        self.assertTrue(ok)
        kinds = [(k, t) for k, t, _ in cur.log]
        first_panic_write = kinds.index(("write", "hollard_panics"))
        deletes = [i for i, (k, _) in enumerate(kinds) if k == "delete"]
        self.assertTrue(all(i > first_panic_write for i in deletes),
                        f"a delete ran before the panic insert: {kinds}")

    def test_stale_rows_are_dropped(self):
        p = payload()
        _fake, cur, ok = self.run_push(p, [1, 4, 0, 6, 0, 8, 0, 2])
        self.assertTrue(ok)
        self.assertIn(("delete", "hollard_monthly", None), cur.log)


class TestPushBatching(_PushCase):
    def test_no_batch_exceeds_the_limit(self):
        # 450 panic pins per programme = 900 rows through one call site.
        p = payload(aip_panics=450, sbmac_panics=450)
        _fake, cur, ok = self.run_push(p, [0, 4, 0, 6, 0, 900, 0, 2])
        self.assertTrue(ok)
        self.assertTrue(cur.batches, "nothing was written")
        self.assertTrue(all(b <= cs.WRITE_BATCH for b in cur.batches),
                        f"batch over {cs.WRITE_BATCH}: {cur.batches}")
        panic_rows = sum(n for k, t, n in cur.log
                         if k == "write" and t == "hollard_panics")
        self.assertEqual(panic_rows, 900, "every pin must be written once")


class TestPushEndStateGuard(_PushCase):
    def test_a_wrong_row_count_after_the_write_rolls_back(self):
        # 4 monthly rows written but 99 left in the table: the predicate took
        # something it should not have, or left something it should have.
        p = payload()
        with self.assertRaises(RuntimeError) as e:
            self.run_push(p, [0, 99, 0, 6, 0, 8, 0, 2])
        self.assertIn("rolling back", str(e.exception))

    def test_a_delete_removing_a_different_number_rolls_back(self):
        p = payload()
        cur = _FakeCursor([2, 4, 0, 6, 0, 8, 0, 2])
        fake = _FakePsycopg(cur)
        sys.modules["psycopg"] = fake

        real_execute = cur.execute

        def execute(sql, params=None):
            real_execute(sql, params)
            if " ".join(sql.split()).lower().startswith("delete"):
                cur.rowcount = 777      # disagrees with the 2 just counted
        cur.execute = execute

        with self.assertRaises(RuntimeError) as e:
            cs.push(p)
        self.assertIn("counted a moment earlier", str(e.exception))
        self.assertFalse(fake.conn.committed)
        self.assertIs(fake.conn.clean, False)


if __name__ == "__main__":
    unittest.main()
