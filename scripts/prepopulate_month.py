#!/usr/bin/env python3
"""
scripts/prepopulate_month.py

Appends a pre-populated row for each missing month (up to the current
month, SAST) to the three monthly tabs of the Hollard source sheet:

  AIP Stats / SBMAC Stats — predicts `Monthly Downloads` (B) and
    `Usage` (H) as the rounded 6-month average; every other populated
    column is a formula copied from the row above with relative row
    references shifted down one.
  RAF Claims — predicts `Calls` (B) and `Valid Claims` (C) the same
    way; `Non-Valid Claims` (D) is a copied formula.

The Month cell gets a cell note flagging the row as a prediction so
whoever captures the real figures knows to overwrite the inputs.

Runs idempotently: a month that already has a row is never touched.

Env:
  GOOGLE_SHEETS_CREDENTIALS  service-account JSON (required; the
                             account must be an *Editor* on the sheet)
  DRY_RUN=true               print planned rows, write nothing
"""
import json
import logging
import os
import re
import statistics
import sys
from datetime import datetime, timedelta, timezone

import gspread
from google.oauth2.service_account import Credentials

logger = logging.getLogger(__name__)

SHEET_ID = "1O8VmhNwnGdvjzweNZQDEls2ZzRXP0CdlPxyLyh6C_TY"
SCOPES   = ["https://www.googleapis.com/auth/spreadsheets"]
DRY_RUN  = os.environ.get("DRY_RUN", "").lower() == "true"
SAST     = timezone(timedelta(hours=2))

MONTH_SHORT = ["Jan","Feb","Mar","Apr","May","Jun",
               "Jul","Aug","Sep","Oct","Nov","Dec"]
MONTH_IDX   = {m: i for i, m in enumerate(MONTH_SHORT, start=1)}

AVERAGE_WINDOW = 6

# tab name -> 0-based indexes of raw input columns to predict
TABS = {
    "AIP Stats":   {"predict": [1, 7]},   # Monthly Downloads, Usage
    "SBMAC Stats": {"predict": [1, 7]},
    "RAF Claims":  {"predict": [1, 2]},   # Calls, Valid Claims
}

NOTE = ("Auto pre-populated prediction ({}-month average). "
        "Replace the predicted values with actuals when available.")

CELL_REF = re.compile(r"(\$?[A-Z]{1,3})(\$?)(\d+)")


def _auth():
    creds_json = os.environ.get("GOOGLE_SHEETS_CREDENTIALS")
    if not creds_json:
        raise RuntimeError("GOOGLE_SHEETS_CREDENTIALS env var not set")
    creds = Credentials.from_service_account_info(
        json.loads(creds_json), scopes=SCOPES,
    )
    return gspread.authorize(creds)


def _parse_month(s):
    """'Jun-2026' → (2026, 6). None if unparseable."""
    s = (s or "").strip()
    if "-" not in s:
        return None
    mon, year = s.split("-", 1)
    mon = mon.strip().capitalize()[:3]
    if mon not in MONTH_IDX or not year.strip().isdigit():
        return None
    return (int(year), MONTH_IDX[mon])


def _to_number(v):
    s = str(v or "").strip().replace(",", "").replace(" ", "")
    if not s or s in ("-", "—", "null"):
        return None
    try:
        return float(s)
    except ValueError:
        return None


def _shift_formula(formula, delta=1):
    """Shift every relative row reference in a formula down by delta."""
    def bump(m):
        col, row_abs, row = m.groups()
        if row_abs == "$":
            return m.group(0)
        return f"{col}{int(row) + delta}"
    return CELL_REF.sub(bump, formula)


def _months_between(start, end):
    """Exclusive of start, inclusive of end. start/end = (year, month)."""
    out, (y, m) = [], start
    while (y, m) < end:
        m += 1
        if m > 12:
            y, m = y + 1, 1
        out.append((y, m))
    return out


def _predict(rows, col):
    """Rounded mean of the last AVERAGE_WINDOW numeric values in col."""
    vals = [n for r in rows if (n := _to_number(r[col] if col < len(r) else "")) is not None]
    window = vals[-AVERAGE_WINDOW:]
    if not window:
        raise RuntimeError(f"no numeric history in column index {col}")
    return int(round(statistics.mean(window)))


def _build_row(prev_values, prev_formulas, predict_cols, data_rows, year, month):
    """New row: =DATE month, predicted inputs, shifted formulas, else blank."""
    width = len(prev_values)
    row = []
    for col in range(width):
        formula = prev_formulas[col] if col < len(prev_formulas) else ""
        if col == 0:
            row.append(f"=DATE({year},{month},1)")
        elif isinstance(formula, str) and formula.startswith("="):
            row.append(_shift_formula(formula))
        elif col in predict_cols:
            row.append(_predict(data_rows, col))
        else:
            row.append("")
    return row


def _add_note(ws, row_idx):
    """Attach the prediction note to the Month cell (col A) of row_idx."""
    ws.spreadsheet.batch_update({
        "requests": [{
            "updateCells": {
                "range": {
                    "sheetId": ws.id,
                    "startRowIndex": row_idx - 1,
                    "endRowIndex": row_idx,
                    "startColumnIndex": 0,
                    "endColumnIndex": 1,
                },
                "rows": [{"values": [{"note": NOTE.format(AVERAGE_WINDOW)}]}],
                "fields": "note",
            }
        }]
    })


def process_tab(sh, tab, predict_cols, today):
    ws = sh.worksheet(tab)
    values = ws.get_all_values()
    if len(values) < 2:
        raise RuntimeError(f"{tab}: no data rows")

    data_rows  = values[1:]
    last_month = None
    for r in reversed(data_rows):
        last_month = _parse_month(r[0])
        if last_month:
            break
    if not last_month:
        raise RuntimeError(f"{tab}: could not parse any month in column A")

    missing = _months_between(last_month, (today.year, today.month))
    if not missing:
        logger.info("%s: up to date (last month %s-%s)", tab, *last_month)
        return 0

    added = 0
    for (year, month) in missing:
        last_row_idx  = len(ws.get_all_values())
        prev_values   = ws.row_values(last_row_idx)
        prev_formulas = ws.get(
            f"A{last_row_idx}:{gspread.utils.rowcol_to_a1(1, len(prev_values))[:-1]}{last_row_idx}",
            value_render_option="FORMULA",
        )[0]
        new_row = _build_row(prev_values, prev_formulas, predict_cols,
                             ws.get_all_values()[1:], year, month)
        label = f"{MONTH_SHORT[month - 1]}-{year}"
        if DRY_RUN:
            logger.info("%s: DRY RUN — would append %s: %s", tab, label, new_row)
            continue
        ws.append_row(new_row, value_input_option="USER_ENTERED",
                      insert_data_option="INSERT_ROWS",
                      table_range=f"A1:{gspread.utils.rowcol_to_a1(last_row_idx, len(prev_values))}")
        _add_note(ws, last_row_idx + 1)
        logger.info("%s: appended predicted row %s: %s", tab, label, new_row)
        added += 1
    return added


def main():
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    today = datetime.now(SAST)
    gc = _auth()
    sh = gc.open_by_key(SHEET_ID)

    total, failures = 0, []
    for tab, cfg in TABS.items():
        try:
            total += process_tab(sh, tab, cfg["predict"], today)
        except Exception as exc:
            logger.error("%s: FAILED — %s", tab, exc)
            failures.append(tab)

    if failures:
        logger.error("Pre-population failed for: %s", ", ".join(failures))
        sys.exit(1)
    logger.info("Done — %d row(s) appended.", total)


if __name__ == "__main__":
    main()
