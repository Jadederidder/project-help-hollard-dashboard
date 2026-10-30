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
import os
from datetime import datetime, timezone

logger = logging.getLogger(__name__)

PROGRAMMES = ("AIP", "SBMAC")

# libpq waits forever by default. The workflow step that calls this has a
# five-minute cap and commits summary.json only after push() returns, so an
# unreachable CRM must not be able to hold the public refresh open.
CONNECT_TIMEOUT_SECONDS = 20

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
    if not payload.get("raf"):
        problems.append("raf is empty")
    return problems


def _write_batched(cur, sql, rows) -> int:
    """executemany in batches of WRITE_BATCH. Returns the rows submitted."""
    for i in range(0, len(rows), WRITE_BATCH):
        cur.executemany(sql, rows[i:i + WRITE_BATCH])
    return len(rows)


def _reconcile(cur, table, now, where, params, expected, label):
    """Remove rows this run did not write, then prove the end state.

    Every row written above carries synced_at = now, so a row still holding an
    older stamp is one whose source key has been withdrawn. Upserting alone
    would leave it behind for ever and still report success.

    The matched rows are counted before the delete rather than after, and the
    table is counted again afterwards and required to equal exactly what this
    run put in. That second count is the real guard: it needs no threshold and
    it fails on any predicate that took rows it should not have.
    """
    cur.execute(f"select count(*) from public.{table} where {where} "
                "and synced_at <> %s", params + (now,))
    stale = cur.fetchone()[0]
    if stale:
        logger.info("CRM sink: %s has %d stale row(s) to drop", label, stale)
        cur.execute(f"delete from public.{table} where {where} "
                    "and synced_at <> %s", params + (now,))
        if cur.rowcount != stale:
            raise RuntimeError(
                f"{label}: delete removed {cur.rowcount} rows but {stale} were "
                "counted a moment earlier; rolling back")
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

    monthly = list(_rows_monthly(payload))
    daily = list(_rows_daily(payload))
    panics = list(_rows_panics(payload))
    raf = list(_rows_raf(payload))
    logger.info("CRM sink: %d monthly, %d daily, %d panics, %d RAF",
                len(monthly), len(daily), len(panics), len(raf))

    problems = _incompleteness(payload)
    if problems:
        logger.error("CRM sink: refusing an incomplete payload (%s). The CRM "
                     "keeps what it already had; summary.json is unaffected.",
                     "; ".join(problems))
        return False

    if os.environ.get("CORE_DRY_RUN", "").lower() == "true":
        logger.info("CORE_DRY_RUN=true — nothing written")
        return False

    try:
        import psycopg
    except ImportError:
        logger.warning("psycopg not installed — skipping the CRM sink")
        return False

    now = datetime.now(timezone.utc)
    # One transaction. A half-written refresh would make the CRM disagree with
    # the dashboard, which is worse than a refresh that did not happen.
    progs = (list(PROGRAMMES),)
    with psycopg.connect(dsn, connect_timeout=CONNECT_TIMEOUT_SECONDS) as conn, \
            conn.cursor() as cur:
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
        """, [r + (now,) for r in monthly])

        _write_batched(cur, """
            insert into public.hollard_daily (programme, day, callouts, cumulative_col, completed, synced_at)
            values (%s,%s,%s,%s,%s,%s)
            on conflict (programme, day) do update set
              callouts = excluded.callouts,
              cumulative_col = excluded.cumulative_col,
              completed = excluded.completed,
              synced_at = excluded.synced_at
        """, [r + (now,) for r in daily])

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
        """, [r + (now,) for r in panics])

        _write_batched(cur, """
            insert into public.hollard_raf_claims (month_sort, month_label, calls, valid, non_valid, synced_at)
            values (%s,%s,%s,%s,%s,%s)
            on conflict (month_sort) do update set
              month_label = excluded.month_label,
              calls = excluded.calls,
              valid = excluded.valid,
              non_valid = excluded.non_valid,
              synced_at = excluded.synced_at
        """, [r + (now,) for r in raf])

        # Withdraw what the source no longer carries. Without this, a monthly,
        # daily or RAF row whose source line was deleted stays in the CRM for
        # ever while every run reports success.
        _reconcile(cur, "hollard_monthly", now, "programme = any(%s)", progs,
                   len({(r[0], r[1]) for r in monthly}), "hollard_monthly")
        _reconcile(cur, "hollard_daily", now, "programme = any(%s)", progs,
                   len({(r[0], r[1]) for r in daily}), "hollard_daily")
        _reconcile(cur, "hollard_panics", now, "programme = any(%s)", progs,
                   len(panics), "hollard_panics")
        _reconcile(cur, "hollard_raf_claims", now, "true", (),
                   len({r[0] for r in raf}), "hollard_raf_claims")

        conn.commit()

    logger.info("CRM sink: written")
    return True
