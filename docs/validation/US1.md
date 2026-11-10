# US1 — Receive Bark notifications (P1)

**Status: PARTIAL.** The storage/ingest half is verified end-to-end headlessly;
the toast half needs a real APNs push and a person to look at the screen.

## Automated evidence (executed)

* `tests/unit/test_payload_mapping.py` — 18 mapping rules from
  `contracts/push-payload.md`: text, subtitle merging, group fallbacks, delete
  (string and numeric), delete without id, encrypted placeholder, empty alert,
  bare-string alert, unknown fields, invalid UTF-8, duplicate ids, image icon,
  non-HTTP icon, markdown image, 200-char per-line truncation, and "never raises".
* `tests/unit/test_notification_ingest.py` — three payloads → three newest-first
  rows; duplicate `remote_id` ignored; server-scoped ids; delete removes the row;
  empty/malformed payloads not stored; id-less payloads de-duplicated within 5 s;
  notifier failure never loses a row; **the adapter acks all four delivered
  messages including the malformed and delete payloads** (fake courier
  connection, no network).
* `tests/unit/test_supervisor_status.py` — connecting → active → degraded
  transitions, 1 s → 60 s jittered backoff, three consecutive connect failures →
  `invalid` (no retry storm), token rejection → invalidate once → re-activate
  once → `invalid`, and `stop()` cancelling an in-flight connection.
* `tests/unit/test_retention.py` — pruning by age and count.
* Headless app smoke (temporary database, no UI):
  `payload → BoundedEventQueue → ingest thread → SQLite` produced
  `smoke-1 Smoke unread=1`, and the recall payload removed the row.
* "30 pushes in 5 s" row (history half): 30 payloads queued through the app's
  bounded event queue produced **30 rows, 30 unread, 0 dropped** while the process
  stayed responsive.
* Tray unread badge formatting: `tests/unit/test_unread.py` (`9+` cap).

## Still needs a real run (quickstart §3 + failure row)

1. Start the app with a seeded identity/link and `curl` a text push at a local
   `bark-server`; confirm a native toast appears within 10 s with title
   `Deploy done` and body `build 412 is live`.
2. Send 30 pushes in 5 s and confirm the toasts may coalesce but history has 30
   rows, the badge shows `9+`, and the UI stays responsive.
3. Disconnect the network for 60 s and reconnect: tray amber → blue within 60 s,
   no duplicates.

These require APNs delivery, which spike T1 must first prove.
