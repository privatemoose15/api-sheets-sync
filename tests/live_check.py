#!/usr/bin/env python3
"""End-to-end check against a real Google Sheet (the unit tests use a fake one).

Creates a new tab named livecheck-<timestamp> and never deletes anything:

    python tests/live_check.py --sheet-id <id> [--repo psf/requests]
"""
import argparse
import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from gspread.utils import ValueRenderOption  # noqa: E402

import sync  # noqa: E402

failures = 0


def check(label, ok, detail=""):
    global failures
    failures += not ok
    print(f"{'PASS' if ok else 'FAIL'}  {label}" + (f"  -> {detail}" if not ok else ""))


def read(ws):
    return ws.get_all_values(value_render_option=ValueRenderOption.unformatted)


def main():
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("--sheet-id", required=True)
    p.add_argument("--repo", default="psf/requests")
    p.add_argument("--creds", default=sync.DEFAULT_CREDS)
    args = p.parse_args()

    records = sync.fetch_issues(sync.http_session(), args.repo, max_pages=1)
    tab = f"livecheck-{datetime.now():%Y%m%d-%H%M%S}"
    ws = sync.open_worksheet(args.creds, args.sheet_id, tab, create=True)
    print(f"Fetched {len(records)} issues from {args.repo}, writing to new tab '{tab}'\n")

    # 1. First sync fills the empty tab
    sync.apply(ws, sync.plan_sync(read(ws), records))
    grid = read(ws)
    check("1. header written", grid[0] == sync.COLUMNS, grid[0])
    unique = len({r[sync.KEY] for r in records})  # paging can return an issue twice
    check("1. one row per issue", len(grid) == unique + 1, f"{len(grid) - 1} rows, {unique} issues")

    # 2. Same data again: what Sheets hands back must compare equal to what we wrote,
    #    otherwise every scheduled run would rewrite the whole sheet
    plan = sync.plan_sync(grid, records)
    check("2. re-run finds nothing to change", not plan.updates and not plan.new_rows,
          f"{len(plan.updates)} cell diffs, e.g. {plan.updates[:3]}")

    # 3. A user adds their own column mid-sheet, and someone overwrites a synced cell
    ws.insert_cols([["notes", "keep me"]], col=4)
    ws.update_acell("E2", "TAMPERED")  # 'state' of the first issue, shifted right by 'notes'
    plan = sync.plan_sync(read(ws), records)
    sync.apply(ws, plan)
    grid = read(ws)
    check("3. only the overwritten cell is rewritten", len(plan.updates) == 1 and not plan.new_rows,
          plan.updates)
    check("3. overwritten cell restored", grid[1][4] == records[0]["state"], grid[1][4])
    check("3. user's own column kept", grid[0][3] == "notes" and grid[1][3] == "keep me",
          [grid[0][3], grid[1][3]])

    print(f"\n{'All checks passed' if not failures else f'{failures} check(s) FAILED'}. "
          f"Tab '{tab}' is left in the sheet to look at; delete it when done.")
    sys.exit(1 if failures else 0)


if __name__ == "__main__":
    main()
