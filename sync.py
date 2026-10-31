#!/usr/bin/env python3
"""Sync GitHub issues into a Google Sheet: new issues are added, changed ones updated in place.

Safe to re-run on a schedule: unchanged cells are not rewritten, rows are never deleted,
and extra columns you add to the sheet yourself (notes, owner, priority...) are left alone.
"""
import argparse
import os
import sys
from dataclasses import dataclass, field
from pathlib import Path

import gspread
import requests
from gspread.utils import ValueRenderOption, rowcol_to_a1
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

KEY = "number"
COLUMNS = ["number", "type", "title", "state", "author", "labels", "comments",
           "created_at", "updated_at", "closed_at", "url"]
# gspread's own default location: outside the repo, so the key can't be committed by accident
DEFAULT_CREDS = os.environ.get("GOOGLE_CREDENTIALS",
                               str(Path.home() / ".config" / "gspread" / "service_account.json"))


# --- 1. Fetch: the only GitHub-specific part. Swap these two functions for any other API. ---

def flatten(issue: dict) -> dict:
    """Turn one nested API record into a flat row keyed by COLUMNS."""
    return {
        "number": issue["number"],
        "type": "pull request" if "pull_request" in issue else "issue",
        "title": issue["title"],
        "state": issue["state"],
        "author": (issue.get("user") or {}).get("login", ""),
        "labels": ", ".join(label["name"] for label in issue.get("labels", [])),
        "comments": issue.get("comments", 0),
        "created_at": issue.get("created_at") or "",
        "updated_at": issue.get("updated_at") or "",
        "closed_at": issue.get("closed_at") or "",
        "url": issue["html_url"],
    }


def http_session(token=None) -> requests.Session:
    """Session that retries rate limits and server errors with backoff."""
    s = requests.Session()
    retry = Retry(total=5, backoff_factor=1, status_forcelist=[429, 500, 502, 503, 504],
                  allowed_methods=["GET"], respect_retry_after_header=True)
    s.mount("https://", HTTPAdapter(max_retries=retry))
    s.headers["Accept"] = "application/vnd.github+json"
    if token:
        s.headers["Authorization"] = f"Bearer {token}"
    return s


def fetch_issues(session, repo: str, since=None, max_pages=10) -> list:
    url = f"https://api.github.com/repos/{repo}/issues"
    params = {"state": "all", "per_page": 100, "sort": "updated", "direction": "desc"}
    if since:
        params["since"] = since if "T" in since else f"{since}T00:00:00Z"
    records = []
    for page in range(1, max_pages + 1):
        r = session.get(url, params=params, timeout=30)
        if r.status_code == 403 and r.headers.get("X-RateLimit-Remaining") == "0":
            sys.exit("GitHub rate limit hit (60 requests/hour without a token). "
                     "Set GITHUB_TOKEN or try again later.")
        if r.status_code == 404:
            sys.exit(f"Repo '{repo}' not found (or private and no GITHUB_TOKEN set).")
        r.raise_for_status()
        records += [flatten(i) for i in r.json()]
        if "next" not in r.links:
            break
        url, params = r.links["next"]["url"], None  # the next link already carries the query
    else:
        print(f"Note: stopped after {max_pages} pages. Use --since for incremental syncs "
              f"or raise --max-pages.")
    return records


# --- 2. Plan: work out the minimum set of cell writes. Pure function, no network. ---

@dataclass
class Plan:
    header: list
    header_changed: bool
    first_new_row: int
    updates: list = field(default_factory=list)   # (row, col, value), 1-based like Sheets
    new_rows: list = field(default_factory=list)
    rows_updated: int = 0
    unchanged: int = 0


def cell(value) -> str:
    return "" if value is None else str(value)


def plan_sync(grid: list, records: list, key=KEY, columns=COLUMNS) -> Plan:
    """Compare what's in the sheet (grid, header first) with fresh records."""
    header = list(grid[0]) if grid else []
    added = [c for c in columns if c not in header]
    header += added
    col = {}
    for i, name in enumerate(header):
        col.setdefault(name, i)

    def value_at(row, name):
        i = col[name]
        return row[i] if i < len(row) else ""

    existing = {}
    for r, row in enumerate(grid[1:], start=2):
        k = cell(value_at(row, key))
        if k and k not in existing:
            existing[k] = (r, row)

    plan = Plan(header, bool(added), first_new_row=max(len(grid), 1) + 1)
    seen = set()
    for rec in records:
        k = cell(rec[key])
        if k in seen:  # pagination can return a record twice if it changes mid-fetch
            continue
        seen.add(k)
        if k not in existing:
            row = [""] * len(header)
            for c in columns:
                row[col[c]] = rec[c]
            plan.new_rows.append(row)
            continue
        r, row = existing[k]
        changes = [(r, col[c] + 1, rec[c]) for c in columns
                   if cell(value_at(row, c)) != cell(rec[c])]
        plan.updates += changes
        plan.rows_updated += bool(changes)
        plan.unchanged += not changes
    return plan


# --- 3. Apply: one batched write, so a big sync stays inside Google's quota. ---

def apply(ws, plan: Plan) -> int:
    last_row = plan.first_new_row + len(plan.new_rows) - 1
    if last_row > ws.row_count:
        ws.add_rows(last_row - ws.row_count)
    if len(plan.header) > ws.col_count:
        ws.add_cols(len(plan.header) - ws.col_count)

    data = []
    if plan.header_changed:
        data.append({"range": "A1", "values": [plan.header]})
    data += [{"range": rowcol_to_a1(r, c), "values": [[v]]} for r, c, v in plan.updates]
    if plan.new_rows:
        data.append({"range": rowcol_to_a1(plan.first_new_row, 1), "values": plan.new_rows})
    if data:
        ws.batch_update(data, raw=True)
    return len(data)


def open_worksheet(creds: str, sheet_id: str, tab: str, create: bool):
    try:
        gc = gspread.service_account(filename=creds)
        sh = gc.open_by_key(sheet_id)
    except FileNotFoundError:
        sys.exit(f"No Google credentials at '{creds}'. See README: 'Google setup'.")
    except gspread.SpreadsheetNotFound:
        sys.exit("Sheet not found. Check --sheet-id, and share the sheet (Editor) with the "
                 "service account's client_email from the credentials JSON.")
    try:
        return sh.worksheet(tab)
    except gspread.WorksheetNotFound:
        return sh.add_worksheet(tab, rows=1000, cols=len(COLUMNS)) if create else None


def main():
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("repo", help="owner/name, e.g. psf/requests")
    p.add_argument("--sheet-id", help="The long ID in the sheet's URL (between /d/ and /edit)")
    p.add_argument("--tab", default="Issues", help="Worksheet tab name (created if missing)")
    p.add_argument("--creds", default=DEFAULT_CREDS,
                   help="Service account JSON (default: $GOOGLE_CREDENTIALS, "
                        "else ~/.config/gspread/service_account.json)")
    p.add_argument("--since", help="Only issues updated since this date, e.g. 2024-06-01")
    p.add_argument("--max-pages", type=int, default=10, help="100 issues per page (default 10)")
    p.add_argument("--dry-run", action="store_true", help="Show what would change, write nothing")
    args = p.parse_args()
    if not args.sheet_id and not args.dry_run:
        p.error("--sheet-id is required (or use --dry-run to preview without Google)")

    session = http_session(os.environ.get("GITHUB_TOKEN"))
    records = fetch_issues(session, args.repo, args.since, args.max_pages)

    ws = None
    if args.sheet_id:
        ws = open_worksheet(args.creds, args.sheet_id, args.tab, create=not args.dry_run)
    grid = ws.get_all_values(value_render_option=ValueRenderOption.unformatted) if ws else []
    plan = plan_sync(grid, records)

    print(f"Fetched: {len(records)} | New: {len(plan.new_rows)} | "
          f"Updated: {plan.rows_updated} ({len(plan.updates)} cells) | Unchanged: {plan.unchanged}")
    if args.dry_run:
        for row in plan.new_rows[:5]:
            print("  + " + " | ".join(cell(v)[:40] for v in row[:5]))
        print("Dry run: nothing written.")
        return
    writes = apply(ws, plan)
    print(f"Done: {writes} range(s) written in 1 request." if writes else "Sheet already up to date.")


if __name__ == "__main__":
    main()
