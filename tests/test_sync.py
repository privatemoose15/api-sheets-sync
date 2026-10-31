import pytest
from gspread.utils import a1_to_rowcol

import sync


class FakeWorksheet:
    """Just enough of gspread.Worksheet to run a sync against a list of lists."""

    def __init__(self, grid=None, rows=1000, cols=26):
        self.grid = [list(r) for r in (grid or [])]
        self.row_count, self.col_count = rows, cols
        self.write_calls = 0

    def get_all_values(self, **_):
        return [r[:] for r in self.grid]

    def add_rows(self, n):
        self.row_count += n

    def add_cols(self, n):
        self.col_count += n

    def batch_update(self, data, raw=True):
        self.write_calls += 1
        for d in data:
            r0, c0 = a1_to_rowcol(d["range"])
            for i, values in enumerate(d["values"]):
                for j, v in enumerate(values):
                    self._set(r0 + i, c0 + j, v)

    def _set(self, r, c, v):
        assert r <= self.row_count and c <= self.col_count, "write outside grid"
        while len(self.grid) < r:
            self.grid.append([])
        row = self.grid[r - 1]
        row.extend([""] * (c - len(row)))
        row[c - 1] = v


def issue(number, state="open", title=None, pr=False, **extra):
    raw = {"number": number, "title": title or f"Issue {number}", "state": state,
           "user": {"login": "ann"}, "labels": [{"name": "bug"}], "comments": 0,
           "created_at": "2024-03-01T09:00:00Z", "updated_at": "2024-03-01T09:00:00Z",
           "closed_at": None, "html_url": f"https://github.com/o/r/issues/{number}", **extra}
    if pr:
        raw["pull_request"] = {}
    return sync.flatten(raw)


def run(ws, records):
    plan = sync.plan_sync(ws.get_all_values(), records)
    sync.apply(ws, plan)
    return plan


def test_first_sync_writes_header_and_rows_in_one_call():
    ws = FakeWorksheet()
    plan = run(ws, [issue(1), issue(2, pr=True)])
    assert ws.grid[0] == sync.COLUMNS
    assert [r[0] for r in ws.grid[1:]] == [1, 2]
    assert ws.grid[2][1] == "pull request"
    assert len(plan.new_rows) == 2 and ws.write_calls == 1


def test_rerun_with_no_changes_writes_nothing():
    ws = FakeWorksheet()
    run(ws, [issue(1), issue(2)])
    plan = run(ws, [issue(1), issue(2)])
    assert plan.unchanged == 2 and ws.write_calls == 1


def test_changed_issue_updates_only_changed_cells():
    ws = FakeWorksheet()
    run(ws, [issue(1), issue(2)])
    closed = issue(2, state="closed", updated_at="2024-03-05T12:00:00Z",
                   closed_at="2024-03-05T12:00:00Z")
    plan = run(ws, [closed, issue(3)])
    assert sorted(c for _, c, _ in plan.updates) == [4, 9, 10]   # state, updated_at, closed_at
    assert len(ws.grid) == 4                                      # updated in place, 3 appended
    assert ws.grid[2][3] == "closed"


def test_users_own_columns_survive():
    ws = FakeWorksheet()
    run(ws, [issue(1)])
    ws.grid[0].insert(3, "notes")             # user inserts a column mid-sheet
    ws.grid[1].insert(3, "chase Ann")
    run(ws, [issue(1, title="Renamed"), issue(2)])
    assert ws.grid[1][3] == "chase Ann"
    assert ws.grid[1][2] == "Renamed"
    assert ws.grid[2][3] == ""                # new row leaves user column blank


def test_new_api_column_is_appended_to_header():
    ws = FakeWorksheet([sync.COLUMNS[:-1], [1, "issue", "Issue 1"]])
    run(ws, [issue(1)])
    assert ws.grid[0][-1] == "url"
    assert ws.grid[1][-1] == "https://github.com/o/r/issues/1"


def test_duplicate_records_from_pagination_add_one_row():
    ws = FakeWorksheet()
    run(ws, [issue(1), issue(1)])
    assert len(ws.grid) == 2


def test_grows_sheet_when_full():
    ws = FakeWorksheet(rows=2, cols=5)
    run(ws, [issue(1), issue(2), issue(3)])
    assert ws.row_count == 4 and ws.col_count == len(sync.COLUMNS)


class FakeResponse:
    def __init__(self, body, next_url=None, status=200, headers=None):
        self.body, self.status_code, self.headers = body, status, headers or {}
        self.links = {"next": {"url": next_url}} if next_url else {}

    def json(self):
        return self.body

    def raise_for_status(self):
        assert self.status_code < 400


class FakeSession:
    def __init__(self, *responses):
        self.responses, self.calls = list(responses), []

    def get(self, url, params=None, timeout=None):
        self.calls.append((url, params))
        return self.responses.pop(0)


def raw_issue(n):
    return {"number": n, "title": "t", "state": "open", "user": {"login": "ann"},
            "html_url": f"https://github.com/o/r/issues/{n}"}


def test_fetch_follows_pagination():
    s = FakeSession(FakeResponse([raw_issue(1)], next_url="https://api.github.com/page2"),
                    FakeResponse([raw_issue(2)]))
    records = sync.fetch_issues(s, "o/r", since="2024-06-01")
    assert [r["number"] for r in records] == [1, 2]
    assert s.calls[0][1]["since"] == "2024-06-01T00:00:00Z"
    assert s.calls[1] == ("https://api.github.com/page2", None)


def test_fetch_rate_limit_gives_clear_error():
    s = FakeSession(FakeResponse({}, status=403, headers={"X-RateLimit-Remaining": "0"}))
    with pytest.raises(SystemExit, match="rate limit"):
        sync.fetch_issues(s, "o/r")
