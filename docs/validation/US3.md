# US3 — Review recent notifications (P2)

**Status: PARTIAL.** Repository behaviour and unread/badge accounting are fully
unit-tested; the visual walk-through of the Tk surfaces needs a desktop session
and real pushes.

## Automated evidence (executed)

* `tests/unit/test_history_repo.py` — newest-first ordering with a deterministic
  tie-break, 100-row paging (105 rows → 100 + 5, disjoint ids), mark read
  one/all, mark unread, delete, delete-by-remote-id, clear, server-scoped
  `find_by_remote_id`, persistence across close/reopen, 8 KB body / 4 KB raw caps,
  idempotent schema migration, and soft-deleted rows hidden from the list.
* `tests/unit/test_unread.py` — unread excludes read and soft-deleted rows, is
  zero after clear, consistent across arbitrary read order, and tracks
  inserts/deletes; badge caps at `9+`.
* `tests/unit/test_encoding.py` — CJK, RTL and emoji survive parsing and a
  database round-trip; truncation never splits a character; timestamps render in
  one stable local format.
* `HistoryWindow` implements: toolbar (Mark all read / Refresh / counts), list
  columns `unread | time | title | group`, keyboard `Up/Down/Enter/Delete`,
  100-row paging with *Load more*, empty state with a hint to copy the device key,
  detail pane with selectable body, *Open link* (http(s) only), *Copy content*,
  mark read/unread, delete, 320 px thumbnail slot, and a *Retry image* action.
  Deleting a row also dismisses the matching toast (`Notifier.remove`), and
  `delete` pushes refresh the open window live.
* Retention runs at startup and hourly (`AppMaintenance`), keeping the newest
  rows and logging counts only.
* Settings offers *Clear history* with a confirmation and reports the exact count.

## Still needs a real run (quickstart §5–§6)

1. Push five notifications, dismiss the toasts, open history from the tray:
   all five newest-first with unread dots; *Mark all read* clears them.
2. Delete one, restart the app, confirm the remaining four persist with the
   correct read state.
3. Send a push then a `delete` push with the same `id`: the row disappears from
   history and from the notification centre.

The Tk surfaces were verified to construct in the packaged build (the first-run
wizard and tray appear at startup), but their interaction paths are not part of
the automated suite.
