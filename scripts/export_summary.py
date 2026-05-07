#!/usr/bin/env python3
"""
scripts/export_summary.py

Reads the 6 tabs of the Hollard AIP/SBMAC source sheet and writes
docs/data/summary.json — consumed by docs/index.html.

Source sheet: Hollard AIP SBMAC_Download Figures
  AIP Stats / SBMAC Stats     — monthly KPI rows
  AIP Panics / SBMAC Panics   — single-column "lat,lon" strings
  AIP Daily call outs /
    SBMAC Daily Call outs     — daily call-out rows
  RAF Claims                  — shared monthly RAF table

Env:
  GOOGLE_SHEETS_CREDENTIALS  service-account JSON (required)
  DRY_RUN=true               print payload, write nothing
"""
import json
import logging
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

import gspread
from google.oauth2.service_account import Credentials

logger = logging.getLogger(__name__)

SHEET_ID = "1O8VmhNwnGdvjzweNZQDEls2ZzRXP0CdlPxyLyh6C_TY"
SCOPES   = ["https://www.googleapis.com/auth/spreadsheets.readonly"]
OUT_PATH = Path(__file__).resolve().parent.parent / "docs" / "data" / "summary.json"
DRY_RUN  = os.environ.get("DRY_RUN", "").lower() == "true"

MONTH_SHORT = ["Jan","Feb","Mar","Apr","May","Jun",
               "Jul","Aug","Sep","Oct","Nov","Dec"]
MONTH_IDX   = {m: i for i, m in enumerate(MONTH_SHORT, start=1)}


def _auth():
    creds_json = os.environ.get("GOOGLE_SHEETS_CREDENTIALS")
    if not creds_json:
        raise RuntimeError("GOOGLE_SHEETS_CREDENTIALS env var not set")
    creds = Credentials.from_service_account_info(
        json.loads(creds_json), scopes=SCOPES,
    )
    return gspread.authorize(creds)


def _to_int(v):
    if v is None: return None
    s = str(v).strip()
    if s in ("", "-", "—", "null", "N/A", "n/a"): return None
    s = s.replace(",", "").replace(" ", "")
    try:
        f = float(s)
        return int(f) if f.is_integer() else round(f, 2)
    except ValueError:
        return None


def _to_pct(v):
    """Parse '35.75%' → 35.75 (float). Empty / unparseable → None."""
    if v is None: return None
    s = str(v).strip().rstrip("%").replace(",", "").replace(" ", "")
    if not s or s in ("-", "—", "null"): return None
    try:
        return round(float(s), 2)
    except ValueError:
        return None


def _parse_month_dash(s):
    """'May-2026' → ('2026-05', 'May 2026'). Returns (None, None) if junk."""
    s = (s or "").strip()
    if "-" not in s: return (None, None)
    mon, year = s.split("-", 1)
    mon = mon.strip().capitalize()[:3]
    if mon not in MONTH_IDX or not year.isdigit(): return (None, None)
    yr = int(year)
    if yr < 2020 or yr > 2099: return (None, None)
    return (f"{yr:04d}-{MONTH_IDX[mon]:02d}", f"{mon} {yr}")


def _parse_daily_date(s):
    """'1 Aug 2021' → '2021-08-01'. Returns None if junk."""
    s = (s or "").strip()
    try:
        return datetime.strptime(s, "%d %b %Y").strftime("%Y-%m-%d")
    except ValueError:
        return None


def _stats_rows(ws):
    """Return list of monthly stat dicts, sorted chronologically."""
    rows = ws.get_all_values()
    if not rows: return []
    out = []
    for r in rows[1:]:
        if not r or not r[0].strip(): continue
        sort_key, label = _parse_month_dash(r[0])
        if sort_key is None: continue
        # Pad row to 11 cols defensively
        r = (r + [""] * 11)[:11]
        out.append({
            "month":             label,
            "monthSort":         sort_key,
            "monthlyDownloads":  _to_int(r[1]),
            "activeCustomers":   _to_int(r[2]),
            "totalDownloads":    _to_int(r[3]),
            "adoptionPct":       _to_pct(r[4]),
            "baseSize":          _to_int(r[5]),
            "downloadsToBasePct": _to_pct(r[6]),
            "usage":             _to_int(r[7]),
            "usagePerDownloadsPct": _to_pct(r[8]),
        })
    out.sort(key=lambda x: x["monthSort"])
    return out


def _daily_rows(ws):
    rows = ws.get_all_values()
    if not rows: return []
    out = []
    for r in rows[1:]:
        if not r or not r[0].strip(): continue
        date = _parse_daily_date(r[0])
        if date is None: continue
        # Only first 4 cols matter; everything past col D is the stale
        # AIP "Active Apps by Month" pivot — confirmed dead since Jun 2023.
        r = (r + [""] * 4)[:4]
        out.append({
            "date":       date,
            "cumulative": _to_int(r[1]),
            "callouts":   _to_int(r[2]),
            "completed":  _to_int(r[3]),
        })
    out.sort(key=lambda x: x["date"])
    return out


def _panic_points(ws):
    """Each row is a single 'lat,lon' string. Skip junk like ',' or empty."""
    rows = ws.get_all_values()
    if not rows: return []
    out = []
    for r in rows[1:]:
        if not r: continue
        s = r[0].strip()
        if not s or "," not in s: continue
        a, b = s.split(",", 1)
        try:
            lat = float(a.strip())
            lon = float(b.strip())
        except ValueError:
            continue
        # Sanity bounds — skip clearly bogus coordinates
        if not (-90 <= lat <= 90 and -180 <= lon <= 180): continue
        if lat == 0 and lon == 0: continue
        out.append([round(lat, 6), round(lon, 6)])
    return out


def _raf_rows(ws):
    rows = ws.get_all_values()
    if not rows: return []
    out = []
    for r in rows[1:]:
        if not r or not r[0].strip(): continue
        sort_key, label = _parse_month_dash(r[0])
        if sort_key is None: continue
        r = (r + [""] * 4)[:4]
        out.append({
            "month":     label,
            "monthSort": sort_key,
            "calls":     _to_int(r[1]),
            "valid":     _to_int(r[2]),
            "nonValid":  _to_int(r[3]),
        })
    out.sort(key=lambda x: x["monthSort"])
    return out


def _kpis(stats, daily, panic_count):
    """Latest-month tiles (top row) + all-time tiles (bottom row).

    Source-data note: in this sheet 'Total Downloads' (col D) and 'Monthly
    Downloads' (col B) hold the same per-month value — they're NOT
    cumulative. The cumulative count is computed here as the sum across
    every monthly row. 'Active Customers' (col C) is already a running
    tally in the source, so the latest-month value IS the all-time total.
    """
    if not stats:
        return {
            "latestMonth": None,
            "monthly":  {"usage": None, "monthlyDownloads": None, "activeApps": None},
            "allTime":  {"cumulativeDownloads": None, "cumulativeCallouts": None,
                         "totalPanics": panic_count},
        }
    latest = stats[-1]
    cum_downloads = sum((r.get("monthlyDownloads") or 0) for r in stats)
    # The Daily tab's `Cumulative` column is mislabelled in the source —
    # it holds the per-day call-out count (matches `# Callouts` exactly on
    # the early rows where both are populated, then `# Callouts` stops being
    # backfilled while `Cumulative` keeps going). Summing `Cumulative` gives
    # the true all-time call-out total.
    cum_callouts  = sum((d.get("cumulative") or 0) for d in (daily or []))
    return {
        "latestMonth": latest.get("month"),
        "monthly": {
            "usage":            latest.get("usage"),
            "monthlyDownloads": latest.get("monthlyDownloads"),
            "activeApps":       latest.get("activeCustomers"),
        },
        "allTime": {
            "cumulativeDownloads": cum_downloads,
            "cumulativeCallouts":  cum_callouts,
            "totalPanics":         panic_count,
        },
    }


def build_payload():
    gc = _auth()
    sh = gc.open_by_key(SHEET_ID)

    def ws(title): return sh.worksheet(title)

    aip_stats   = _stats_rows(ws("AIP Stats"))
    sbmac_stats = _stats_rows(ws("SBMAC Stats"))
    aip_daily   = _daily_rows(ws("AIP Daily call outs"))
    sbmac_daily = _daily_rows(ws("SBMAC Daily Call outs"))
    aip_panics  = _panic_points(ws("AIP Panics"))
    sbmac_panics = _panic_points(ws("SBMAC Panics"))
    raf         = _raf_rows(ws("RAF Claims"))

    payload = {
        "lastUpdated": datetime.now(timezone.utc)
                            .isoformat(timespec="seconds")
                            .replace("+00:00", "Z"),
        "views": {
            "AIP": {
                "kpis":   _kpis(aip_stats, aip_daily, len(aip_panics)),
                "stats":  aip_stats,
                "daily":  aip_daily,
                "panics": aip_panics,
            },
            "SBMAC": {
                "kpis":   _kpis(sbmac_stats, sbmac_daily, len(sbmac_panics)),
                "stats":  sbmac_stats,
                "daily":  sbmac_daily,
                "panics": sbmac_panics,
            },
        },
        "raf": raf,
    }
    return payload


def main():
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s  %(levelname)-8s  %(message)s",
        handlers=[logging.StreamHandler(sys.stdout)],
    )
    payload = build_payload()
    for view in ("AIP", "SBMAC"):
        v = payload["views"][view]
        logger.info(
            "%s — %d stats months (latest %s), %d daily rows, %d panic points",
            view, len(v["stats"]),
            v["kpis"].get("latestMonth"),
            len(v["daily"]), len(v["panics"]),
        )
    logger.info("RAF — %d months", len(payload["raf"]))

    json_str = json.dumps(payload, indent=2, ensure_ascii=False)
    if DRY_RUN:
        logger.info("DRY_RUN=true — not writing.")
        print(json_str)
        return
    OUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    OUT_PATH.write_text(json_str + "\n", encoding="utf-8")
    logger.info("Wrote %s (%d bytes)", OUT_PATH, len(json_str) + 1)


if __name__ == "__main__":
    main()
