# Final validation matrix (spec SC-001 … SC-012)

**Status: PARTIAL.** Everything that can be verified without external services is
verified; the rows marked BLOCKED need real APNs traffic (spike T1), a clean VM,
or a long run. See the individual records in this folder.

| Criterion | Status | Evidence / what remains |
|-----------|--------|--------------------------|
| SC-001 setup < 5 min | BLOCKED | Wizard is three steps and has no account prompt; timing needs a live activation. `US2.md` |
| SC-002 95 % of pushes within 10 s | BLOCKED | Requires live delivery. Supervisor/receiver latency path is implemented. |
| SC-003 7 days continuous | BLOCKED | `soak.md` |
| SC-004 closing the window never stops delivery | PARTIAL | Close is `withdraw()` + receiver on its own thread; the packaged exe stayed alive in the tray and answered instance commands. Live push after close still to be observed. |
| SC-005 image < 3 s, text never blocked | PARTIAL | Text is presented immediately; image fetching runs on 2 workers behind a queue with caps. Fetch/cache timings measured only against a mocked transport. |
| SC-006 idle CPU < 1 %, memory < 150 MB | PARTIAL | **58 MB working set / 40.3 MB private** measured for the packaged exe; CPU observed at 0 % but not averaged over an hour. `perf.md` |
| SC-007 reconnect within 60 s | PARTIAL | Backoff caps at 60 s and stall detection fires at 90 s; live network drop not exercised. |
| SC-008 history intact across restart | **PASS** | `tests/unit/test_history_repo.py::test_persistence_across_close_and_reopen` (rows, order, read state). |
| SC-009 tray actions ≤ 2 interactions | **PASS** (by inspection) | Open history (double-click), mark all read, test, copy key, settings, setup, quit are all top-level tray items; no nested menus. |
| SC-010 < 1 in 20 pushes need intervention | BLOCKED | Requires a live failure-injection session (`quickstart.md` §10). |
| SC-011 zero-install, delete-one-folder removal | PARTIAL | The packaged onedir build runs without Python installed (it started here from `dist/`); only opt-in HKCU entries (autostart, `barkpc:`) survive folder deletion, and both are removable from Settings. Clean-VM confirmation pending. |
| SC-012 moved folder repaired | PARTIAL | Repair decision unit-tested; the interactive prompt was not exercised. |

## Spike outcomes

* **T0 (toast from unpackaged exe)**: tool ready, visual check pending →
  `spike_T0.md`.
* **T1 (base token accepted by APNs via Bark)**: tool ready, live gate pending →
  `spike_T1.md`. **This is the one unverified premise of the design.**
* **T2 (frozen imports)**: **PASS** — all 18 modules import in a frozen build;
  `tkinter.ttk` needs an explicit hidden import, now pinned in the build script →
  `spike_T2.md`.

## Deviations from `spec.md` / `plan.md`

1. **Hero images arrive as a second, updated toast.** The first toast is shown
   immediately (text latency is never traded for image latency); when a late fetch
   succeeds within 30 s the toast is replaced with the hero-image version. The
   spec's "hero image in the toast" holds for cached and quickly fetched images.
2. **Toasts are created on the Tk thread** (marshalled through `post_to_ui`) to
   avoid WinRT apartment issues; the receiver only enqueues, so acks are never
   delayed.
3. **Diagnostics export** writes `bark-pc-diagnostics.zip` into a folder chosen by
   the user (not a single file picker), which keeps the three log files together.
4. **`barkpc:` registration happens after the first successful setup**, not at
   first launch, so a portable copy that is never configured leaves no trace in
   the registry.

## Automated suite

```
python -m pytest        # 182 passed
python -m ruff check    # All checks passed
python -m ruff format --check
```

Manual steps to finish: `quickstart.md` §2–§10 on a clean Windows 11 VM, then
Windows 10 21H2, with a local `bark-server`, recording the results in the
per-story records above and replacing this status with PASS/BLOCKED per row.
