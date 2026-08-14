# project-help-hollard-dashboard

Public dashboard for the Hollard **AIP** and **SBMAC** help-app programmes.

- Live dashboard: GitHub Pages (after first push)
- Source sheet: `Hollard AIP SBMAC_Download Figures` (sheet ID `1O8VmhNwnGdvjzweNZQDEls2ZzRXP0CdlPxyLyh6C_TY`)
- Refresh cadence: daily, 05:30 SAST (`.github/workflows/sync_dashboard.yml`)
- Monthly pre-population: 05:10 SAST on the 1st (`.github/workflows/prepopulate_month.yml`) — appends a predicted row for the month that just ended (1 Oct adds Sep) to `AIP Stats`, `SBMAC Stats` and `RAF Claims`, then triggers the dashboard sync

## Layout

| Section | SBMAC | AIP | Notes |
|---|---|---|---|
| KPI tiles — top row (Usage / Monthly Downloads / Active Apps) | ✓ | ✓ | Latest month from Stats tab |
| KPI tiles — bottom row (Cumulative Downloads / Cumulative Call-outs / Total Panic Events) | ✓ | ✓ | Programme-to-date totals |
| Active Apps bar chart | ✓ | ✓ | Sourced from Stats `Active Customers` (col C) — chronological sort fixed |
| Daily call-outs line | ✓ | ✓ | Cumulative + # Callouts + # Completed |
| Panic geo-pin map | ✓ | ✓ | Leaflet + marker clustering, default-fitted to South Africa |
| Monthly performance table | ✓ | ✓ | Full Stats schema, newest month first |
| RAF Claims table | shared | shared | Single shared monthly table |

## Tabs read from the source sheet

| Tab | Rows | Notes |
|---|---|---|
| `AIP Stats` | 65 monthly rows from Jan-2021 | Cols J & K (`Ave Usage`, `Downloads`) are empty in source — skipped. Note: `Total Downloads` (col D) and `Monthly Downloads` (col B) hold the same per-month value in the source — neither is cumulative |
| `SBMAC Stats` | 64 monthly rows from Feb-2021 | Same shape as AIP |
| `AIP Daily call outs` | 1,710 daily rows | Stale "Active Apps by Month" pivot in cols F+ is ignored. The `Cumulative` col is mislabelled in source — it actually holds the per-day call-out count, used for the all-time call-out total |
| `SBMAC Daily Call outs` | 1,712 daily rows | Clean 4-col schema (same `Cumulative` quirk) |
| `AIP Panics` | 2,598 valid points | Single col `lat,lon` string |
| `SBMAC Panics` | 1,139 valid points | A few rows have flipped (positive) latitudes |
| `RAF Claims` | 28 monthly rows from Feb-2024 | Shared across both views |

## Local development

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt

# Refresh summary.json from the live sheet
GOOGLE_SHEETS_CREDENTIALS="$(cat /path/to/service-account.json)" \
  .venv/bin/python scripts/export_summary.py

# Preview the dashboard locally
python3 -m http.server 8766 --directory docs
# → http://localhost:8766/
```

`DRY_RUN=true` prints the payload without writing `summary.json`.

## Monthly pre-population

`scripts/prepopulate_month.py` appends a row for each month missing up to and
including the month that just ended (SAST) in the three monthly tabs:

- **Predicted inputs** — rounded 6-month average: `Monthly Downloads` and
  `Usage` on the Stats tabs; `Calls` and `Valid Claims` on RAF Claims.
- **Derived columns** — formulas copied from the row above with relative row
  references shifted down one (`Active Customers`, `Adoption`, `Base size`,
  `Downloads to Base`, `Usage / Downloads`, `Non-Valid Claims`).
- The Month cell gets a note marking the row as a prediction so the real
  figures can be captured over it later. Idempotent — existing months are
  never touched.

Note: in `DRY_RUN=true` back-fill runs, consecutive months log identical
values because nothing is appended between iterations; live runs roll each
appended prediction into the next month's average.

## GitHub setup notes

Required repo secret: `GOOGLE_SHEETS_CREDENTIALS` (service-account JSON for
`project-help-sheets@calendar-sync-493309.iam.gserviceaccount.com`, shared on
the source sheet as an Editor — write access is needed by the monthly
pre-population workflow).

GitHub Pages must be enabled with **Source: GitHub Actions** under
`Settings → Pages`.
