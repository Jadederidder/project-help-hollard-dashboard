#!/usr/bin/env python3
"""
scripts/core_sink.py

Second sink for the Hollard payload: the same data export_summary.py already
writes to docs/data/summary.json, also written into the Project Help CRM's core
database so the CRM can query it instead of parsing a published JSON file.

DELIBERATELY OPTIONAL AND NON-BREAKING. If CORE_DB_URL is unset, push() logs one
line and returns. The public dashboard's refresh must never fail because the CRM
side is unavailable — summary.json is written first and this runs after it.

Env:
  CORE_DB_URL   postgres connection string for the core database. Unset = skip.
  CORE_DRY_RUN  'true' to log the row counts it would write and change nothing.

Schema: design/migrations/0008_hollard.sql in the CRM repo.
"""
from __future__ import annotations

import logging
import contextlib
import os
import threading
import time
from datetime import datetime, timezone

logger = logging.getLogger(__name__)

PROGRAMMES = ("AIP", "SBMAC")

# libpq waits forever by default. The workflow step that calls this has a
# five-minute cap and commits summary.json only after push() returns, so an
# unreachable CRM must not be able to hold the public refresh open.
CONNECT_TIMEOUT_SECONDS = 20

# connect_timeout only bounds getting the connection. A statement that stalls
# after that is just as able to eat the step's five minutes and stop
# summary.json being committed, so the statements are bounded too. 90s leaves
# the exporter the rest of its budget.
STATEMENT_TIMEOUT_MS = 90_000
# The whole sink, not one statement. STATEMENT_TIMEOUT_MS bounds each
# statement; this bounds their sum, which is the number that actually matters:
# a healthy refresh sends ~50 statements (19 batches of panics, 18 of daily,
# plus the reconcile counts), and the step that calls this has
# timeout-minutes: 5 with the summary.json commit and the Pages deploy AFTER
# it. So a slow-but-not-stalled database could use the step's whole budget
# here and get it killed, leaving summary.json written in the workspace and
# never published -- the CRM sink breaking the public dashboard's refresh,
# which is the one thing this design says it must never do.
#
# Overrunning raises, and export_summary already calls push() inside a
# try/except, so the sink gives up and the commit and deploy steps still run.
# 120s is deliberately well under the 5 minutes: the sheet reads happen first
# and take their share.
SINK_BUDGET_SECONDS = 120
# Reserved out of the budget so the COMMIT at the end has room inside it.
# Without this the last statement could be allowed to run to the deadline and
# the commit would then land outside the budget entirely.
COMMIT_RESERVE_MS = 5_000
# Below this there is not enough time left to do anything useful, and a
# statement_timeout of 0 means NO timeout in Postgres -- the opposite of what
# this code is for -- so the sink gives up rather than ever computing it.
MIN_STATEMENT_MS = 1_000

# Writes go out in batches rather than one executemany over a whole table:
# the real payload is ~1,700 daily rows and ~2,600 panic pins per programme.
WRITE_BATCH = 200


def _rows_monthly(payload):
    for programme in PROGRAMMES:
        for s in payload["views"][programme]["stats"]:
            yield (
                programme,
                s.get("monthSort"),
                s.get("month"),
                s.get("monthlyDownloads"),
                s.get("activeCustomers"),
                s.get("totalDownloads"),
                s.get("adoptionPct"),
                s.get("baseSize"),
                s.get("downloadsToBasePct"),
                s.get("usage"),
            )


def _rows_daily(payload):
    """One row per (programme, day), summing duplicate dates.

    The source has them: SBMAC carries two rows for 2021-10-01, with 2 and 3,
    and the dashboard's all-time call-out total sums both. Letting the last row
    win would put the CRM 2 call-outs behind the dashboard for ever. Summing
    here, then replacing the row on write, keeps repeat runs idempotent and the
    total exact.
    """
    for programme in PROGRAMMES:
        agg: dict[str, list[int | None]] = {}
        for d in payload["views"][programme]["daily"]:
            day = d.get("date")
            if not day:
                continue
            row = agg.setdefault(day, [None, None, None])
            # index 1 is the source's "Cumulative" column, which is not
            # cumulative — it holds the per-day call-out count. See 0008.
            for i, key in enumerate(("callouts", "cumulative", "completed")):
                v = d.get(key)
                if v is not None:
                    row[i] = (row[i] or 0) + v
        for day, (callouts, cumulative, completed) in sorted(agg.items()):
            yield (programme, day, callouts, cumulative, completed)


def _rows_panics(payload):
    """Every panic pin, including repeats.

    An earlier version deduplicated on (lat, lon) and made the CRM report 2,596
    AIP panics against the dashboard's 2,598, because one coordinate appears
    three times in the source. Two incidents at one address are two events, so
    collapsing them undercounts exactly what the number measures. Repeats are
    kept; a map can cluster them at render time.
    """
    for programme in PROGRAMMES:
        for point in payload["views"][programme]["panics"]:
            if not point or len(point) != 2:
                continue
            lat, lon = point
            if lat is None or lon is None:
                continue
            yield (programme, round(float(lat), 6), round(float(lon), 6))


def _rows_raf(payload):
    for r in payload.get("raf", []):
        yield (r.get("monthSort"), r.get("month"), r.get("calls"),
               r.get("valid"), r.get("nonValid"))


def _incompleteness(payload) -> list:
    """What is missing from the payload, or [] if it is whole.

    push() replaces what it writes, so a partial payload does not merely write
    less -- it takes away what the previous run had put there. A programme
    whose sheet read failed arrives as an empty list, not as an error, so the
    only place this can be caught is here, before the transaction opens.
    The real payload carries ~69 monthly rows, ~1,710 daily rows and 1,100+
    panic pins per programme, and 32 RAF months; none of those is ever empty
    in a healthy run.
    """
    problems = []
    views = payload.get("views")
    if not isinstance(views, dict):
        return ["payload has no views"]
    for programme in PROGRAMMES:
        view = views.get(programme)
        if not isinstance(view, dict):
            problems.append(f"{programme}: missing from the payload")
            continue
        for key in ("stats", "daily", "panics"):
            if not view.get(key):
                problems.append(f"{programme}: {key} is empty")
        # Empty is not the only way a source goes wrong. A row whose date did
        # not parse is dropped by _rows_daily, and a dropped row leaves the
        # list non-empty -- so the checks above pass, the reconcile then
        # removes that day from the CRM, and the run reports success. A
        # partial snapshot has to be refused for the same reason an empty one
        # is: this sink replaces what it writes.
        undated = sum(1 for d in (view.get("daily") or [])
                      if not (isinstance(d, dict) and d.get("date")))
        if undated:
            problems.append(
                f"{programme}: {undated} daily row(s) carry no usable date")
        unusable = sum(1 for pt in (view.get("panics") or [])
                       if not pt or len(pt) != 2 or pt[0] is None or pt[1] is None)
        if unusable:
            problems.append(
                f"{programme}: {unusable} panic pin(s) carry no coordinates")
        # And the rows that never reached the payload at all. The exporter
        # drops a source row whose date will not parse (_daily_rows), so it
        # leaves no trace here: the list is still non-empty, the checks above
        # still pass, and this sink would then reconcile that day out of the
        # CRM and report success. The two checks above can only see a bad row
        # that is present; this one is the only way to see one that is gone.
        # A payload with no count at all is refused rather than trusted --
        # "the exporter did not say" is not "nothing was dropped".
        dropped = view.get("sourceDropped")
        if dropped is None:
            problems.append(
                f"{programme}: the payload does not say how many source rows "
                "were dropped, so a partial load cannot be ruled out")
        elif dropped:
            problems.append(
                f"{programme}: the exporter dropped {dropped} source row(s) "
                "it could not read, so this snapshot is partial")
    if not payload.get("raf"):
        problems.append("raf is empty")
    # RAF is one shared table rather than per programme, so its dropped count
    # sits at the top level. Same rule as the per-programme one: absent is
    # refused, not assumed to mean nothing was lost.
    raf_dropped = payload.get("rafSourceDropped")
    if raf_dropped is None:
        problems.append(
            "the payload does not say how many RAF source rows were dropped, "
            "so a partial load cannot be ruled out")
    elif raf_dropped:
        problems.append(
            f"the exporter dropped {raf_dropped} RAF source row(s) it could "
            "not read, so this snapshot is partial")
    return problems


@contextlib.contextmanager
def _watchdog(conn, deadline):
    """Cancel whatever the server is doing if the budget runs out.

    statement_timeout bounds ONE statement. executemany() sends up to
    WRITE_BATCH executions, so a batch of slow ones can outlive the budget
    even with every statement bounded. That was the reviewer's point and it
    is correct: a deadline checked in Python cannot interrupt work already in
    flight. Only cancelling it can.

    Measured through psycopg 3.2.3 against a throwaway PostgreSQL 16.15
    cluster, because this premise is the only reason the thread below
    exists and it deserved more than an argument:

      statement_timeout 400ms, executemany of 5 x pg_sleep(0.2)
        -> COMPLETED in 1005ms, all 5 rows written
      statement_timeout 300ms, executemany where the 3rd sleeps 2s
        -> 57014 canceling statement due to statement timeout at 402ms

    The first line is the gap: two and a half times its own bound, from
    five executions. WRITE_BATCH is 200, so the real factor is far worse.
    The second line is the bound working exactly as documented -- per
    execution, which is precisely why it cannot save the batch.

    psycopg's cancel() is safe to call from another thread, so a timer does
    it. The cancelled statement raises, the transaction rolls back, and the
    caller's try/except leaves the dashboard's own steps to run.

    The timer is stopped BEFORE commit, deliberately. Cancelling a COMMIT
    leaves it genuinely unknown whether it applied, and a CRM that might or
    might not agree with the dashboard is worse than a refresh that plainly
    did not happen. COMMIT_RESERVE_MS is the headroom for the commit
    instead -- by then every statement has run, so it is the fastest part.

    But Timer.cancel() alone does not deliver that. It only clears the
    wait: a fire() that has already begun runs to completion regardless, so
    the main thread could call cancel(), get nothing, and commit while the
    timer thread cancels underneath it -- exactly the ambiguous outcome the
    paragraph above says is prevented. The window is not theoretical. The
    last _bounded() call sets a statement_timeout that expires at the very
    instant the timer fires, so a statement that uses its whole bound
    returns just as fire() starts.

    So the exposed timer also carries `fired`, and the shutdown joins the
    thread and then commits only if the thread is gone AND it never fired.
    Both halves are needed: `fired` alone misses a thread that has decided
    to run the callback but has not reached its first line, because the
    bounded join can expire with the flag still false; liveness alone
    misses the ordinary case of a callback that fired and finished inside
    the join window. A late cancel then produces a rollback and the
    previous run's rows, which is the outcome this design wanted all along.
    """
    seconds = max(deadline - time.monotonic() - COMMIT_RESERVE_MS / 1000, 0)
    fired = threading.Event()

    def fire():
        # Set first, ahead of anything that can block, log or raise, so the
        # flag is true from the moment the callback starts rather than from
        # the moment it finishes.
        #
        # Being honest about what this ordering is worth: on its own it did
        # not close the gap, and now that the shutdown also refuses a
        # thread still alive after the join, it no longer carries the
        # refusal by itself either. Setting it last would be caught by that
        # liveness check instead. It stays first because it costs nothing
        # and narrows the window a reader has to reason about -- defence in
        # depth, not the load-bearing part.
        fired.set()
        logger.error("CRM sink: %ss budget spent; cancelling the transaction "
                     "so the dashboard's commit and deploy still run",
                     SINK_BUDGET_SECONDS)
        try:
            # cancel_safe(), not the cancel() this used to call, which
            # psycopg deprecated in 3.2. cancel() goes through libpq's
            # legacy cancel request: it opens a fresh connection that
            # PostgreSQL's own docs describe as neither encrypted nor
            # authenticated, and it takes no timeout, so on this daemon
            # thread a stalled cancel would be invisible -- a watchdog that
            # hangs instead of cancelling is no watchdog. cancel_safe uses
            # libpq 17's secure, interruptible cancel and honours a
            # timeout; the commit reserve is the only headroom there is, so
            # that is the timeout. On an older libpq it falls back to the
            # legacy call and the timeout does nothing, which is no worse
            # than what it replaced -- and does not arise here, because
            # psycopg[binary]==3.2.3 ships libpq 17 (checked: pq.version()
            # reports 170000).
            #
            # Measured, not assumed. Against the same throwaway cluster, a
            # cancel_safe(timeout=5.0) called from a threading.Timer while
            # the main thread sat in `select pg_sleep(30)` ended that
            # statement with 57014 "canceling statement due to user
            # request" after 304ms, and the main thread saw the flag set.
            # So this is the mechanism that makes the budget a real bound.
            conn.cancel_safe(timeout=COMMIT_RESERVE_MS / 1000)
        except Exception:               # noqa: BLE001 - nothing left to try
            logger.exception("CRM sink: cancelling the transaction failed")

    timer = threading.Timer(seconds, fire)
    timer.daemon = True                 # must never hold the process open
    timer.fired = fired
    timer.start()
    try:
        yield timer
    finally:
        timer.cancel()


def _bounded(cur, deadline, label):
    """Give the NEXT statement only the time the budget has left.

    A check that only gates when a statement STARTS is not a bound. The
    first version of this was exactly that: it refused to begin an operation
    past the deadline, but a statement begun one millisecond before it could
    still run for the full STATEMENT_TIMEOUT_MS -- so four of them inside one
    reconcile could spend 360s against a 120s budget, and the workflow step
    would be killed before Python raised anything. The reviewer was right and
    my "bounded" claim was not true.

    So this is called before EVERY statement, and it sets a transaction-local
    statement_timeout to whatever is actually left. Each statement is then
    bounded by the remaining budget at the moment it starts, and because that
    figure only shrinks, the total cannot run past the deadline.

    The transaction-local scope reverts at the end of the transaction, so
    this never leaks onto a pooled connection. None of that is asserted --
    a fake driver cannot establish server behaviour, so it was measured.

    On the live PostgreSQL 17 server, over psql:

      before anything                     -> 2min  (the session default)
      after set_config(..., true)         -> 5s    (so it takes effect)
      after that transaction rolled back  -> 2min  (so it reverts)

    and then, through psycopg 3.2.3 itself against a throwaway PostgreSQL
    16.15 cluster, which closes the three things that psql run could not
    show -- the driver path, the bound actually biting, and the revert
    after a COMMIT rather than after a rollback:

      set local statement_timeout = %s    -> 42601 syntax error at "$1"
      set_config('statement_timeout', %s, true) -> accepted, shows 5s
      150ms bound, then select pg_sleep(5) -> 57014 cancelled at 151ms
      show, set_config(150ms), COMMIT, show -> 0, 150ms, 0

    The last line is the one that matters for a pooled connection: the
    bound is gone after the commit, not only after a rollback. The 57014
    is the one that matters for the budget: the bound is a real stop, not
    a number the server merely accepts.

    There is deliberately no "no deadline" mode. An earlier version took
    deadline=None and returned silently, which is the same shape as every
    other defect found in this file: a guard that looks present and does
    nothing. The callers all have a deadline, so now they must pass one.
    """
    remaining_ms = int((deadline - time.monotonic()) * 1000) - COMMIT_RESERVE_MS
    if remaining_ms < MIN_STATEMENT_MS:
        raise RuntimeError(
            f"CRM sink has {max(remaining_ms, 0)}ms left of its "
            f"{SINK_BUDGET_SECONDS}s budget at {label}; giving up so the "
            "dashboard's own commit and deploy still run")
    # set_config(), NOT `set local ... = %s`. PostgreSQL does not accept a
    # bind parameter in SET, and psycopg 3 binds server-side, so that form
    # raises 42601 at the FIRST call here -- the first statement of the
    # transaction. The sink would never have written a row, and the caller's
    # try/except would have swallowed it into a log line. Proven on the live
    # PostgreSQL 17 server rather than taken on trust:
    #   prepare as set local statement_timeout = $1
    #     -> REJECTED: 42601 syntax error at or near "set"
    #   prepare as select set_config('statement_timeout', $1, true)
    #     -> ACCEPTED
    #   set local statement_timeout = 5000  (literal, the control)
    #     -> ACCEPTED
    # set_config's third argument is is_local, so the transaction-local scope
    # is exactly what `set local` gave. The value goes as text; a bare number
    # is read as milliseconds.
    cur.execute("select set_config('statement_timeout', %s, true)",
                (str(min(remaining_ms, STATEMENT_TIMEOUT_MS)),))


def _write_batched(cur, sql, rows, deadline, label) -> int:
    """executemany in batches of WRITE_BATCH. Returns the rows submitted.

    The deadline is checked per batch, not just once per table: daily and
    panics are ~19 batches each, so a per-table check would let the budget
    run out inside one of them.
    """
    for i in range(0, len(rows), WRITE_BATCH):
        _bounded(cur, deadline, f"{label} batch {i // WRITE_BATCH + 1}")
        cur.executemany(sql, rows[i:i + WRITE_BATCH])
    return len(rows)


def _reconcile(cur, table, now, where, params, expected, label, deadline):
    """Remove rows this run did not write, then prove the end state.

    Every row written above carries synced_at = now, so a row still holding an
    older stamp is one whose source key has been withdrawn. Upserting alone
    would leave it behind for ever and still report success.

    Three counts, in this order: the stale rows the delete will match, the
    fresh rows this run just wrote, and -- after the delete -- the table
    itself. The first two are compared against what was written BEFORE
    anything is removed, so a bad write is reported as a bad write rather than
    discovered later as a bad table. The third needs no threshold and fails on
    any predicate that took rows it should not have. All of it is inside one
    transaction, so a failure anywhere rolls the whole refresh back.
    """
    _bounded(cur, deadline, f"reconcile {label}: stale count")
    cur.execute(f"select count(*) from public.{table} where {where} "
                "and synced_at <> %s", params + (now,))
    stale = cur.fetchone()[0]
    _bounded(cur, deadline, f"reconcile {label}: fresh count")
    cur.execute(f"select count(*) from public.{table} where {where} "
                "and synced_at = %s", params + (now,))
    fresh = cur.fetchone()[0]
    # Prove the arithmetic BEFORE taking anything away. The end-state check
    # below would catch a bad delete too -- it is one transaction, so nothing
    # wrong ever commits -- but it catches it afterwards, which means the error
    # says "the table is wrong" instead of "the write was wrong". These two
    # counts separate those: if the insert above did not put in what we think
    # it did, that is what gets reported, and the delete never runs.
    if fresh != expected:
        raise RuntimeError(
            f"{label}: {fresh} rows carry this run's stamp but {expected} were "
            f"written; refusing to delete {stale} older row(s) on the strength "
            "of a count that does not add up")
    if stale:
        logger.info("CRM sink: %s has %d stale row(s) to drop", label, stale)
        _bounded(cur, deadline, f"reconcile {label}: delete")
        cur.execute(f"delete from public.{table} where {where} "
                    "and synced_at <> %s", params + (now,))
        if cur.rowcount != stale:
            raise RuntimeError(
                f"{label}: delete removed {cur.rowcount} rows but {stale} were "
                "counted a moment earlier; rolling back")
    _bounded(cur, deadline, f"reconcile {label}: end-state count")
    cur.execute(f"select count(*) from public.{table} where {where}", params)
    after = cur.fetchone()[0]
    if after != expected:
        raise RuntimeError(
            f"{label}: {after} rows left after the write but {expected} were "
            "written; rolling back rather than leaving the CRM disagreeing "
            "with the dashboard")


def push(payload) -> bool:
    """Write the payload to core. Returns True if it wrote, False if skipped."""
    dsn = os.environ.get("CORE_DB_URL", "").strip()
    if not dsn:
        logger.info("CORE_DB_URL unset — skipping the CRM sink (summary.json is unaffected)")
        return False

    # Validate FIRST. This used to run after the four row builders, which made
    # it unreachable for the case it names most clearly: _rows_monthly reaches
    # payload["views"][programme]["stats"] with a hard subscript, so a payload
    # missing a programme raised KeyError here before the check could say
    # "AIP: missing from the payload" and return False. A gate behind the
    # thing it guards is not a gate.
    problems = _incompleteness(payload)
    if problems:
        logger.error("CRM sink: refusing an incomplete payload (%s). The CRM "
                     "keeps what it already had; summary.json is unaffected.",
                     "; ".join(problems))
        return False

    monthly = list(_rows_monthly(payload))
    daily = list(_rows_daily(payload))
    panics = list(_rows_panics(payload))
    raf = list(_rows_raf(payload))
    logger.info("CRM sink: %d monthly, %d daily, %d panics, %d RAF",
                len(monthly), len(daily), len(panics), len(raf))

    if os.environ.get("CORE_DRY_RUN", "").lower() == "true":
        logger.info("CORE_DRY_RUN=true — nothing written")
        return False

    try:
        import psycopg
    except ImportError:
        logger.warning("psycopg not installed — skipping the CRM sink")
        return False

    now = datetime.now(timezone.utc)
    deadline = time.monotonic() + SINK_BUDGET_SECONDS
    # One transaction. A half-written refresh would make the CRM disagree with
    # the dashboard, which is worse than a refresh that did not happen.
    progs = (list(PROGRAMMES),)
    with psycopg.connect(
            dsn,
            connect_timeout=CONNECT_TIMEOUT_SECONDS,
            options=f"-c statement_timeout={STATEMENT_TIMEOUT_MS}") as conn, \
            conn.cursor() as cur, \
            _watchdog(conn, deadline) as watchdog:
        _write_batched(cur, """
            insert into public.hollard_monthly (programme, month_sort, month_label,
              monthly_downloads, active_customers, total_downloads, adoption_pct,
              base_size, downloads_to_base_pct, usage, synced_at)
            values (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
            on conflict (programme, month_sort) do update set
              month_label = excluded.month_label,
              monthly_downloads = excluded.monthly_downloads,
              active_customers = excluded.active_customers,
              total_downloads = excluded.total_downloads,
              adoption_pct = excluded.adoption_pct,
              base_size = excluded.base_size,
              downloads_to_base_pct = excluded.downloads_to_base_pct,
              usage = excluded.usage,
              synced_at = excluded.synced_at
        """, [r + (now,) for r in monthly], deadline, "monthly")

        _write_batched(cur, """
            insert into public.hollard_daily (programme, day, callouts, cumulative_col, completed, synced_at)
            values (%s,%s,%s,%s,%s,%s)
            on conflict (programme, day) do update set
              callouts = excluded.callouts,
              cumulative_col = excluded.cumulative_col,
              completed = excluded.completed,
              synced_at = excluded.synced_at
        """, [r + (now,) for r in daily], deadline, "daily")

        # Panics cannot be upserted: rows repeat legitimately (one coordinate
        # appears three times in the source) so there is no natural key to
        # conflict on. They are therefore inserted first and the previous
        # run's rows dropped afterwards by their older synced_at -- NOT
        # deleted first. Deleting first meant that between the delete and the
        # insert the map was empty, and that an empty panics list wiped the
        # table and still reported success.
        _write_batched(cur, """
            insert into public.hollard_panics (programme, lat, lon, synced_at)
            values (%s,%s,%s,%s)
        """, [r + (now,) for r in panics], deadline, "panics")

        _write_batched(cur, """
            insert into public.hollard_raf_claims (month_sort, month_label, calls, valid, non_valid, synced_at)
            values (%s,%s,%s,%s,%s,%s)
            on conflict (month_sort) do update set
              month_label = excluded.month_label,
              calls = excluded.calls,
              valid = excluded.valid,
              non_valid = excluded.non_valid,
              synced_at = excluded.synced_at
        """, [r + (now,) for r in raf], deadline, "raf")

        # Withdraw what the source no longer carries. Without this, a monthly,
        # daily or RAF row whose source line was deleted stays in the CRM for
        # ever while every run reports success.
        _reconcile(cur, "hollard_monthly", now, "programme = any(%s)", progs,
                   len({(r[0], r[1]) for r in monthly}), "hollard_monthly",
                   deadline)
        _reconcile(cur, "hollard_daily", now, "programme = any(%s)", progs,
                   len({(r[0], r[1]) for r in daily}), "hollard_daily",
                   deadline)
        _reconcile(cur, "hollard_panics", now, "programme = any(%s)", progs,
                   len(panics), "hollard_panics", deadline)
        _reconcile(cur, "hollard_raf_claims", now, "true", (),
                   len({r[0] for r in raf}), "hollard_raf_claims",
                   deadline)

        # Every statement has run, so the watchdog's job is done. Stopping
        # it is not enough on its own: Timer.cancel() cannot stop a fire()
        # that has already begun. So stop it, wait for the thread, and then
        # commit only if BOTH of these hold -- the thread is gone, and it
        # never fired.
        #
        # Both, because either alone leaves a hole. `fired` alone misses a
        # thread that has decided to run the callback but has not reached
        # its first line yet: the bounded join can expire with the flag
        # still false, and the commit would then race the cancel after all.
        # `is_alive()` alone misses a callback that fired and finished
        # inside the join window, which is the ordinary case.
        #
        # A thread still alive after the whole commit reserve is treated as
        # unsafe rather than probably-fine. That can refuse a run that was
        # in fact healthy, if the interpreter starves the timer thread for
        # five seconds. The cost of being wrong that way is the CRM keeping
        # the previous run and the log saying why; the cost of being wrong
        # the other way is a commit nobody can vouch for. Everything else
        # in this file fails closed, and so does this.
        watchdog.cancel()
        watchdog.join(COMMIT_RESERVE_MS / 1000)
        if watchdog.is_alive() or watchdog.fired.is_set():
            raise RuntimeError(
                "CRM sink: the budget watchdog was still live or had already "
                "fired when the last statement finished, so a cancel may be "
                "on its way to the server; refusing to commit on top of one, "
                "which leaves the CRM on the previous run rather than on a "
                "snapshot nobody can vouch for")
        conn.commit()

    logger.info("CRM sink: written")
    return True
