# Implementation Plan: Bark Desktop Client for Windows

**Branch**: `001-bark-desktop-client` | **Date**: 2026-11-10 | **Spec**: [spec.md](./spec.md)

**Input**: Feature specification from `specs/001-bark-desktop-client/spec.md`

## Summary

Build a per-user, tray-first Windows application that receives Bark push notifications by acting as an Apple push client: a `pypush`-based adapter performs anonymous device activation, mints a scoped token for the Bark topic, and streams notifications from the Apple courier; the token is registered with a Bark-compatible server so existing senders (iPhone app, curl, scripts) keep working unchanged. Received messages become native Windows toasts, are stored in a local SQLite history, and images referenced by pushes are fetched, cached, and viewable. The app is pure Python with a Tkinter/ttk UI, `pystray` tray, `windows-toasts` toasts, and ships as a self-contained portable onedir build with no installer and no admin rights.

Two research findings shape the plan:

1. **No Apple account is needed.** `pypush` 2.x activation posts to Apple's device-activation endpoint and returns a device certificate, private key, and push token. The spec has been corrected accordingly (Story 2, FR-003, Edge Cases, Assumptions, Risks).
2. **`pypush` is SSPL-licensed and explicitly unstable.** It is isolated behind one adapter module (`push/pypush_client.py`) and consumed unmodified as a pinned dependency.

## Technical Context

**Language/Version**: Python 3.11 (baseline; 3.10+ acceptable), 64-bit, `pyproject.toml`-managed

**Primary Dependencies**:
- `pypush==2.0.2` (APNs courier client + device activation) with its transitive `anyio`, `httpx`, `cryptography`
- `Pillow` (image decode/thumbnail), `pystray` (tray), `windows-toasts` + `winrt-*` (native toasts), `segno` (QR rendering for the setup wizard)
- Standard library: `tkinter`/`ttk` (UI), `sqlite3` (history), `winreg` (autostart, URI scheme), `ctypes` (DPAPI, named mutex), `logging`

**Storage**: SQLite (WAL) at `data/bark.db`; image cache files under `data/images/`; settings in `data/settings.json`; DPAPI-encrypted identity blob at `data/identity.bin`

**Testing**: Unit tests only (explicit user instruction): `pytest` with `anyio` support, `httpx.MockTransport` instead of live HTTP, temp SQLite files, and fakes for the courier stream, notifier, and secret store, so the suite is headless and offline. No contract or integration suites; external behavior is proved by the manual validation tasks in `quickstart.md` with results recorded under `docs/validation/`.

**Target Platform**: Windows 10 21H2+ and Windows 11, x64 (ARM64 supported only if Python and all wheels are available)

**Project Type**: Single-project desktop application with a platform-integration boundary

**Performance Goals**: idle working set < 150 MB (measured 44.5 MB with Tk, Pillow, pystray, and `windows-toasts` all loaded; `pypush` imports add ~16 MB over a bare interpreter), idle CPU < 1%, notification shown within 10 s p95 of server acceptance, image thumbnail < 3 s when host RTT < 1 s, warm start < 3 s

**Constraints**: no admin rights, no Windows service, no installer, no auto-update; offline backfill explicitly excluded; SSPL dependency; upstream `pypush` API may change; toasts must work from an unpackaged portable process

**Scale/Scope**: one user profile, one active device identity, one active Bark link, 500 notifications / 30 days retention, 200 MB image cache, 4 UI surfaces (history window, setup wizard, settings dialog, image viewer)

## Constitution Check

*GATE: Must pass before Phase 0 research. Re-check after Phase 1 design.*

`.specify/memory/constitution.md` contains only template placeholders — no principles are ratified, so no project-specific gates are enforced. The following default gates are applied and must hold:

| Gate | Status | Evidence |
|------|--------|----------|
| Spec has no unresolved `[NEEDS CLARIFICATION]` | PASS | 4 clarifications resolved in `spec.md` |
| Every FR is testable | PASS | FR-001..FR-038 each map to a scenario or validation step in `quickstart.md` |
| New dependencies justified | PASS | Dependency table in `research.md` §R1–R3, R11; each is minimal for its job and measured against the memory budget |
| Platform-specific code isolated | PASS | `platform/base.py` protocols; only `platform/windows/*` references Win32/WinRT |
| Credentials and message content protected | PASS | DPAPI-at-rest (R8), redacted logging (R16), no content leaves the machine |
| No scope creep beyond the spec | PASS | Encrypted pushes, backfill, service mode, macOS/Linux, auto-update all excluded |
| Test strategy covers external failure modes | PASS | Unit tests with fakes for activation, courier, and server (mocked transport); manual `quickstart.md` scenarios cover real toasts, APNs, and servers |

**Post-Phase 1 re-check**: still PASS. The design adds no new external services; the only new artifact-level obligation is the `barkpc:` per-user URI registration, which is HKCU-only and documented in `contracts/local-ipc.md`.

## Project Structure

### Documentation (this feature)

```text
specs/001-bark-desktop-client/
├── plan.md              # This file
├── spec.md              # Feature specification
├── research.md          # Phase 0: decisions, measurements, risks
├── data-model.md        # Phase 1: entities, tables, state machines
├── quickstart.md        # Phase 1: end-to-end validation guide
├── contracts/
│   ├── pypush-adapter.md
│   ├── bark-server-api.md
│   ├── push-payload.md
│   └── local-ipc.md
├── checklists/
│   └── requirements.md
└── tasks.md             # Phase 2 output (/speckit.tasks, not created here)
```

### Source Code (repository root)

```text
bark_pc/
├── __main__.py                 # entry point: single instance, composition, run
├── app.py                      # application lifecycle, wiring, shutdown
├── core/
│   ├── config.py               # portable paths, settings load/save, defaults
│   ├── models.py               # Notification, BarkLink, DeviceIdentity, ImageRef
│   ├── db.py                   # schema, migrations, repository, retention pruning
│   ├── bus.py                  # thread-safe event/queue plumbing
│   ├── logging.py              # rotating log, redaction filter
│   └── errors.py               # user-facing error taxonomy
├── push/
│   ├── pypush_client.py        # ONLY module importing pypush.apns
│   ├── identity.py             # activation cache: save/load/validate
│   ├── payload.py              # APNs/Bark payload → Notification
│   └── supervisor.py           # reconnect/backoff state machine, status reporting
├── bark/
│   ├── server_client.py        # register / key check / ping
│   └── links.py                # device key lifecycle, share link + QR payload
├── images/
│   ├── fetcher.py              # bounded HTTP fetch with timeouts/size caps
│   └── cache.py                # content-addressed cache + LRU eviction
├── platform/
│   ├── base.py                 # Tray, Notifier, Autostart, SecretStore,
│   │                           # SingleInstance, Opener, UriHandler protocols
│   └── windows/
│       ├── tray.py             # pystray icon, menu, unread badge state
│       ├── toast.py            # windows-toasts rendering, images, click-through
│       ├── autostart.py        # HKCU Run key, stale-path detection/repair
│       ├── secrets.py          # DPAPI protect/unprotect via ctypes
│       ├── single_instance.py  # named mutex + loopback focus socket
│       └── uri_scheme.py       # HKCU\Software\Classes\barkpc registration
├── ui/
│   ├── app_shell.py            # Tk root, hidden-window lifecycle, thread marshalling
│   ├── history_window.py       # list + detail pane, read/delete/clear
│   ├── setup_wizard.py         # activation → registration → key/QR
│   ├── settings_dialog.py      # server, retention, images, autostart, silence
│   ├── image_viewer.py         # full-size view, save, copy
│   └── theme.py                # ttk styling, DPI handling
└── tools/
    └── build_portable.py       # PyInstaller onedir build script

tests/
├── fixtures/payloads/          # APNs payload samples per contracts/push-payload.md
└── unit/
    ├── test_payload_mapping.py        # payload → Notification rules
    ├── test_notification_ingest.py    # parse → repo → notifier (temp DB)
    ├── test_supervisor_status.py      # status/backoff transitions
    ├── test_bark_server_client.py     # request/response shapes via MockTransport
    ├── test_links.py                  # link lifecycle and rotation rules
    ├── test_identity.py               # activation cache + DPAPI failure paths
    ├── test_history_repo.py           # ordering, paging, read state, persistence
    ├── test_unread.py                 # unread counts and badge formatting
    ├── test_image_fetcher.py          # fetch caps, cache hit, eviction
    ├── test_image_cache.py            # hashing, thumbnails, accounting
    ├── test_autostart.py              # Run-key writes and stale-path detection
    ├── test_single_instance_protocol.py # focus/open message handling
    ├── test_retention.py
    └── test_settings.py
```

**Structure Decision**: Single desktop project. The only platform-conditional code lives under `platform/`, selected at import time by `app.py`; `push/`, `bark/`, `images/`, and `core/` are OS-agnostic so macOS/Linux can be added later by implementing the `platform/base.py` protocols.

## Complexity Tracking

| Violation | Why Needed | Simpler Alternative Rejected Because |
|-----------|------------|-------------------------------------|
| Async `anyio` receiver thread inside a synchronous Tk app | `pypush` is async-only and owns a long-lived courier connection with its own reconnect/keepalive tasks | Running the whole UI on anyio would force an async GUI toolkit and raise memory; polling in the UI thread would block the event loop |
| `barkpc:` URI scheme registration | Toast click-through must reach a running background app without a foreground window | COM toast activator requires packaging/registration far beyond scope; plain `launch` of the target URL cannot restore the app for messages without a URL |
| Two UI processes' worth of toolkit (Tk + pystray message loop) | Tk must own the main thread while the tray needs a Win32 message loop independent of Tk | A single `pystray.run()` loop in the main thread would make Tk secondary and unstable; Qt/wx would solve it at ~2–3× memory |
| Cached activation blob on disk | Re-activating on every start would hammer Apple's activation service and risk blocking | In-memory-only identity would require activation on every launch and break offline restarts |
