# Soak and resilience run

**Status: BLOCKED.** A 24 h (and the 7-day SC-003) run needs a machine that stays
up, live APNs traffic, and a network that can be interrupted on purpose.

## Procedure

1. Register the client against a Bark-compatible server and confirm text and
   image pushes arrive.
2. Start the packaged build and leave it running in the tray.
3. Send one push per hour (a scheduled `curl` against the server).
4. After 2 h: disconnect the network for 5 minutes, reconnect, push again.
5. After 4 h: put the machine to sleep for 5 minutes, resume, push again.
6. Sample the process every 30 s:

   ```bash
   python -m bark_pc.tools.measure_idle --pid <pid> --duration 86400 --interval 30 \
       --csv docs/validation/soak-idle.csv
   ```

7. Review at the end:
   * working-set trend is flat (no leak);
   * `data/logs/app.log` has no unhandled exceptions or tracebacks;
   * history has exactly one row per push (no duplicates, no gaps);
   * reconnects after the network drop and after resume complete within 60 s;
   * the tray badge matches the unread count.

## What is already verified without the long run

* Reconnect policy: capped 1 s → 60 s jittered backoff, three consecutive connect
  failures → `invalid`, no retry storm; token rejection → invalidate → re-activate
  once → `invalid` — `tests/unit/test_supervisor_status.py`.
* Stall watchdog: a receiver that goes 90 s without status/message activity is
  cancelled and reconnected — implemented in
  `PushSupervisor._watchdog`.
* Bounded ingest queue drops the oldest event rather than blocking acks; a
  30-push burst kept all 30 rows with 0 drops.
* Idempotent storage: `(source_server, remote_id)` unique index ignores
  redeliveries; id-less payloads de-duplicated within 5 s.

Record the outcome below after a real run.
