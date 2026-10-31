# api-sheets-sync

Keep a Google Sheet in sync with an API — re-run it as often as you like and it only changes what
actually changed.

The demo pulls GitHub issues and pull requests into a sheet. The fetch step is one small function,
so the same pattern works for Shopify orders, Stripe payments, a CRM, or any JSON API.

## Why not just "clear the sheet and paste everything"?

Because that's what breaks the moment a real team uses the sheet. This version:

| | Clear-and-paste script | This sync |
|---|---|---|
| Someone adds a "Notes" or "Owner" column | Wiped on the next run | Left alone |
| An issue is closed upstream | Whole sheet rewritten | Just `state`, `updated_at` and `closed_at` cells change |
| Nothing changed since last run | Rewrites everything anyway | Writes nothing |
| 2,000 rows | Risks hitting Google's write quota | One batched write request per run |
| API returns the same record twice while paging | Duplicate row | One row |
| API adds a new field you start syncing | Columns shift | New column added at the end |
| GitHub rate limit or server error | Crashes | Retries with backoff, then a clear message |

Rows are matched on a key column (`number`), so each issue is always one row.

## Try it without Google (dry run)

```bash
pip install -r requirements.txt
python sync.py psf/requests --dry-run --max-pages 2
```

```console
Fetched: 200 | New: 200 | Updated: 0 (0 cells) | Unchanged: 0
  + 7638 | issue | ZIP extraction tests do not catch predic | open | OpenForge888
  + 7413 | pull request | Fix HTTPDigestAuth uri field truncated b | open | zons-zhaozhy
  ...
Dry run: nothing written.
```

## Sync to a real sheet

Real output against a live sheet:

```console
$ python sync.py psf/requests --sheet-id 1AbC...xYz --tab Issues --max-pages 2
Fetched: 200 | New: 200 | Updated: 0 (0 cells) | Unchanged: 0
Done: 2 range(s) written in 1 request.

$ python sync.py psf/requests --sheet-id 1AbC...xYz --tab Issues --max-pages 2
Fetched: 200 | New: 0 | Updated: 0 (0 cells) | Unchanged: 200
Sheet already up to date.

$ python sync.py psf/requests --sheet-id 1AbC...xYz --tab Issues --since 2026-10-01
Fetched: 29 | New: 0 | Updated: 0 (0 cells) | Unchanged: 29
Sheet already up to date.
```

To check it against your own sheet, `python tests/live_check.py --sheet-id <id>` creates a new tab
and checks the first sync, a no-change re-run, and that a hand-added column survives.

| Option | What it does |
|---|---|
| `repo` | `owner/name` of any public repo (private repos need `GITHUB_TOKEN`) |
| `--sheet-id` | The long ID in the sheet URL, between `/d/` and `/edit` |
| `--tab` | Worksheet tab to sync into. Created if missing. Default `Issues` |
| `--creds` | Service account JSON. Default `$GOOGLE_CREDENTIALS`, then `~/.config/gspread/service_account.json` |
| `--since` | Only fetch issues updated since this date. Use it for fast scheduled runs |
| `--max-pages` | Cap on API pages, 100 issues each. Default 10 |
| `--dry-run` | Show what would change and write nothing. Works with or without `--sheet-id` |

Environment variables: `GITHUB_TOKEN` (optional, raises the GitHub limit from 60 to 5,000
requests an hour), `GOOGLE_CREDENTIALS` (path to the service account JSON).

## Google setup (one-off, about 5 minutes)

1. In [Google Cloud Console](https://console.cloud.google.com/), create a project and enable the
   **Google Sheets API** and **Google Drive API**.
2. Go to *IAM & Admin → Service Accounts*, create a service account, then *Keys → Add key → JSON*.
   Save the file as `~/.config/gspread/service_account.json`. Keeping it outside the project folder
   means it can't be committed by accident. (Or put it anywhere and set `GOOGLE_CREDENTIALS`.)
3. Open your Google Sheet, click **Share**, and add the `client_email` from that JSON file as an
   **Editor**.
4. Copy the sheet ID from its URL and pass it as `--sheet-id`.

## Run it on a schedule

Cron, every hour:

```cron
0 * * * * cd /path/to/api-sheets-sync && python sync.py owner/repo --sheet-id 1AbC...xYz --since $(date -u -v-2d +\%F)
```

(`date -v-2d` is macOS. On Linux use `date -u -d '2 days ago' +\%F`.)

## Adapting it to another API

Only the top section of [`sync.py`](sync.py) knows about GitHub:

- `flatten()` turns one API record into a flat row whose keys are `COLUMNS`.
- `fetch_issues()` handles auth and paging and returns a list of those rows.

Change those two functions plus `KEY` and `COLUMNS`, and the planning and writing logic is reused
unchanged.

## How it works

1. **Fetch** every record from the API, following pagination.
2. **Plan**: read the sheet once and compare it with the records, cell by cell. Build a list of
   only the cells that differ, plus any new rows. `plan_sync()` is a pure function, which is why
   it can be tested without Google.
3. **Apply** everything in a single `batch_update` call, growing the sheet first if needed.

Values are written raw, so `007` stays `007` and timestamps stay as ISO text
(`2024-03-05T12:00:00Z`). Rows deleted upstream are kept in the sheet, never removed.

## Tests

```bash
pip install -r requirements-dev.txt
pytest
```

The tests run a full sync against an in-memory fake worksheet: first sync, a re-run with no
changes, a closed issue, a user-added column, the sheet running out of rows, paging, and rate
limits.
