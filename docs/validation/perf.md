# Performance measurements

Environment: Windows 11, 64-bit, Python 3.11.2, packaged
`--onedir --windowed` build (`dist/BarkDesktopClient`), no Bark link (first-run
wizard open), no push traffic.

## Executed

| Metric | Target | Measured | Notes |
|--------|--------|----------|-------|
| Warm start → ready | < 3 s | **0.44 s / 0.58 s / 0.73 s** (three runs) | ready = `data/instance.json` written and the instance listener accepting commands |
| Idle working set | < 150 MB | **58.0 MB** | sampled 5 × 1 s after 8 s uptime; constant |
| Idle private bytes | — | **40.3 MB** | same samples |
| Portable bundle size | — | 59 MB onedir, 28 MB zip | includes Tk, Pillow, pypush, WinRT |
| Ingestion of a 30-push burst | history must keep all 30, app responsive | 30 rows, 30 unread, **0 dropped events** | headless app harness through the bounded queue + ingest thread |

CPU sampling was not run for an hour in this environment; the packaged process
was observed idle at 0 % in the sampling loop. Use
`python -m bark_pc.tools.measure_idle --duration 3600` for the formal SC-006
check (it exits non-zero above 150 MB or 1 % average CPU).

## Not measurable here

| Metric | Why | How to measure |
|--------|-----|----------------|
| Notification shown within 10 s p95 (SC-002) | requires live APNs delivery | 20 pushes from `quickstart.md` §3, record arrival deltas |
| Image thumbnail < 3 s with RTT < 1 s (SC-005) | requires a real image host and a push | `quickstart.md` §4, time thumbnail appearance |
| 7-day / 24 h stability (SC-003) | requires a long run | `soak.md` procedure |

The unit suite itself runs in ~2 s (182 tests, no network), which keeps the
development loop fast.
