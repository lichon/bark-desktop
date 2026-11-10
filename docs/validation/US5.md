# US5 — Tray-first, unobtrusive operation (P3)

**Status: PARTIAL.** The packaged exe was run on Windows 11 and the single-instance
protocol, clean shutdown and idle footprint were verified; sign-out/sign-in,
Explorer-restart recovery and the 24 h measurement need a real interactive session.

## Automated evidence (executed)

* **Packaged exe run** (`dist/BarkDesktopClient/BarkDesktopClient.exe`):
  * started in **0.44 s** (to `data/instance.json`), created
    `data/bark.db`, `data/images/`, `data/logs/`, stayed alive in the tray with the
    setup wizard open, and used **58 MB** working set / 40 MB private bytes.
  * a second launch with `barkpc:open` exited **0.31 s** after forwarding the
    command; exactly one `BarkDesktopClient.exe` remained and the log recorded
    `forwarded open to the running instance`.
  * the maintenance-tooling `quit` command returned `{"ok":true}`, the process
    exited with code 0 and `data/instance.json` was removed.
* `tests/unit/test_single_instance_protocol.py` — `focus`/`open?id=` messaging,
  unknown commands, invalid tokens, malformed/non-UTF-8 input, numeric ids,
  stale-record detection with an injected port checker, record round-trip and
  idempotent cleanup, per-user mutex naming.
* `tests/unit/test_autostart.py` — writes a quoted absolute path to the HKCU
  `Run` value, disables by deleting the value, refuses empty/relative paths,
  detects a moved folder (`repair`/`ignore`), and reads a manually written value.
* Tray state rendering: distinct colours for
  `starting|connecting|active|degraded|invalid|silenced`, a separate silenced
  glyph, unread badge overlay capped at `9+`, tooltip with server/status/unread,
  double-click opens the history window, *Settings…*, *Setup…*, a
  relaunch watchdog that re-asserts the icon, and a confirmation before quitting.
* Command-line handling accepts a `barkpc:` URI or `--focus`, forwards it to the
  running instance and exits without starting a second receiver.
* Silence mode suppresses toasts while still recording, auto-expires, and shows
  the `silenced` tray state; hide-preview renders
  `["Bark Desktop Client", "New notification"]`.

## Still needs a real run (quickstart §7–§8)

1. Enable *Start with Windows*, sign out and back in: a tray-only process with
   no elevation prompt; Task Manager → Startup shows `BarkDesktopClient`.
2. Move the portable folder, launch from the new path: offer to repair or
   disable the stale entry.
3. Delete the folder after quitting: only the optional autostart entry and the
   `barkpc:` registration remain, both removable via *Remove all data*.
4. Restart Explorer: the tray icon reappears within a minute (watchdog logs
   `tray icon re-asserted` at debug level).
5. Idle measurement for 1 h and 24 h:
   `python -m bark_pc.tools.measure_idle --duration 3600` (fails the run above
   150 MB working set or 1 % average CPU).
