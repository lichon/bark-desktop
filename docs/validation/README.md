# Validation records

This folder holds the evidence for the validation tasks in
`specs/001-bark-desktop-client/tasks.md`. Automated checks can be re-run at any
time; the rows marked **BLOCKED** require a real Bark server, real APNs traffic,
a clean Windows VM, or a multi-hour run and were therefore not executed in the
development container.

| Record | Covers | Status |
|--------|--------|--------|
| [spike_T0.md](spike_T0.md) | Toast display from an unpackaged exe | BLOCKED — tool ready, visual check pending |
| [spike_T1.md](spike_T1.md) | Base courier token accepted by APNs via Bark | BLOCKED — tool ready, needs Apple + Bark server |
| [spike_T2.md](spike_T2.md) | Frozen imports | **PASS** (ticket `tkinter.ttk` requires an explicit hidden import) |
| [US1.md](US1.md) | Receive text pushes, history, toasts, burst | PARTIAL — ingest verified; toasts need real pushes |
| [US2.md](US2.md) | Setup, activation, registration, self-test | PARTIAL — logic unit-tested; live run blocked |
| [US3.md](US3.md) | History, unread, recall, tray actions | PARTIAL — repository/UI logic covered; manual walk-through pending |
| [US4.md](US4.md) | Images, cache, viewer | PARTIAL — fetch/cache unit-tested with a mocked transport |
| [US5.md](US5.md) | Tray-first operation, autostart, single instance | PARTIAL — packaged-exe run verified; sign-out and 24 h run pending |
| [TTS.md](TTS.md) | Spoken notifications (SAPI provider, settings, worker) | PARTIAL — logic and engine smoke-tested; listening check pending |
| [Bluetooth.md](Bluetooth.md) | Bluetooth audio receiver / A2DP sink (WinRT, settings, worker) | PARTIAL — logic tested; real phone connection pending |
| [perf.md](perf.md) | Warm start, idle footprint, latency | PARTIAL — start/memory measured; toast latency needs APNs |
| [encoding.md](encoding.md) | CJK/RTL/emoji, truncation, DPI | PARTIAL — text verified by tests; visual DPI pending |
| [security.md](security.md) | Secrets, logging, URL/URI handling | **PASS** (review + greps, see file) |
| [soak.md](soak.md) | 24 h soak, sleep/resume | BLOCKED |
| [final.md](final.md) | SC-001 … SC-012 matrix | PARTIAL — see matrix |

## Environment used for the executed checks

* Windows 11, Python 3.11.2 (64-bit), `pypush==2.0.2`, Pillow 13.0.0,
  pystray 0.19.5, windows-toasts 1.3.1, PyInstaller 6.22.3.
* Unit suite: `python -m pytest` — 405 tests, headless, offline.
* Portable build: `python -m bark_pc.tools.build_portable` →
  `dist/BarkDesktopClient/` (59 MB) + `dist/BarkDesktopClient-windows-x64.zip`
  (28 MB).
