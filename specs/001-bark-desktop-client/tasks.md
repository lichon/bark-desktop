---
description: "Task list for Bark Desktop Client for Windows"
---

# Tasks: Bark Desktop Client for Windows

**Input**: Design documents from `specs/001-bark-desktop-client/`

**Prerequisites**: `plan.md`, `spec.md`, `research.md`, `data-model.md`, `contracts/`, `quickstart.md`

**Tests**: Unit tests only, per explicit instruction. There are no contract or integration test suites; every task below that mentions tests targets `tests/unit/` and runs headless with fakes and a mocked HTTP transport. External behavior (real toasts, real APNs, real servers) is verified by the manual validation task at the end of each story, recorded under `docs/validation/`.

**Organization**: Tasks are grouped by user story from `spec.md` so each story is independently implementable and testable.

## Format: `[ID] [P?] [Story] Description`

- **[P]**: different files, no dependency on an incomplete task → safe to run in parallel
- **[Story]**: `US1`–`US5`, mapped to the prioritized stories in `spec.md`
- Every task names an exact file path

## Path Conventions

Single desktop project at the repository root: application package `bark_pc/`, tests in `tests/`, build tooling in `bark_pc/tools/`, validation notes in `docs/validation/`.

---

## Phase 1: Setup (Shared Infrastructure)

**Purpose**: Project skeleton, pinned dependencies, logging, and the three go/no-go spikes from `research.md` §R17.

- [X] T001 Create the package skeleton and project metadata: `pyproject.toml` (project `bark-pc`, `requires-python = ">=3.10"`, package `bark_pc`), `.gitignore` (`data/`, `dist/`, `build/`, `.venv/`, `__pycache__/`), `README.md` stub, and empty packages `bark_pc/{core,push,bark,images,platform/windows,ui,tools}`, `tests/{unit,contract,integration,fixtures/payloads}`, `docs/validation/`
- [X] T002 Pin runtime dependencies in `pyproject.toml`: `pypush==2.0.2`, `Pillow`, `pystray`, `windows-toasts`, `segno` (pure-Python QR, no transitive deps); dev extra: `pytest`, `ruff`; add `[project.scripts] bark-desktop = "bark_pc.__main__:main"`
- [X] T003 [P] Configure `[tool.ruff]` in `pyproject.toml`: `target-version = "py310"`, `line-length = 100`, rules `E,F,I,UP,B`
- [X] T004 [P] Configure `[tool.pytest.ini_options]` in `pyproject.toml`: `testpaths = ["tests"]`, `addopts = "-q"`, markers `windows_only` and `slow`; create `tests/__init__.py` placeholders
- [X] T005 [P] Implement `bark_pc/core/logging.py`: `RotatingFileHandler` at `<app>/logs/app.log` (2 MB × 3), configurable level, and a redaction filter that masks hex runs of ≥64 chars, base64 blobs, `token`/`authorization` values, and notification bodies (FR-026)
- [ ] T006 Create `bark_pc/tools/spike_token.py` to resolve **spike T1**: activate anonymously, connect to `1-courier.push.apple.com`, print the base token (redacted after first 8 chars), `POST /register` it, `POST /push` a self-test, and wait ≤30 s for arrival; record the pass/fail and the APNs reason in `docs/validation/spike_T1.md`
- [ ] T007 [P] Create `bark_pc/tools/spike_toast.py` plus a `--onedir --windowed` PyInstaller smoke build to resolve **spike T0**: show a toast with a hero image from an unpackaged exe; verify visible appearance and source name on Windows 11 and Windows 10 21H2; record whether a per-user Start Menu shortcut/AUMID is required in `docs/validation/spike_T0.md`
- [X] T008 [P] Create `bark_pc/tools/spike_frozen_imports.py` to resolve **spike T2**: import `pypush.apns`, `winrt.windows.ui.notifications`, `pystray`, `tkinter`, `PIL`, `segno` from a frozen build; record required hidden imports in `docs/validation/spike_T2.md`

**Checkpoint**: Dependencies install, spikes have documented outcomes, and T1 has a go/no-go decision.

---

## Phase 2: Foundational (Blocking Prerequisites)

**Purpose**: Portable paths, models, storage, error taxonomy, OS integration protocols, and the app shell every story needs.

**⚠️ CRITICAL**: No user story work can begin until this phase is complete.

- [X] T009 Implement `bark_pc/core/config.py`: resolve the portable app folder from the executable location (frozen) or package parent (source), create `data/`, `data/images/`, `data/logs/`; load/validate `data/settings.json` with the defaults and ranges from `data-model.md` §Settings (invalid values fall back and log); write atomically (temp + `os.replace`)
- [X] T010 [P] Implement `bark_pc/core/models.py`: dataclasses/enums `DeviceIdentity` (`state` ∈ `absent|activating|active|invalid`), `BarkLink` (`state` ∈ `unlinked|registering|linked|stale|abandoned`), `Notification`, `ImageCacheEntry` (`state` ∈ `pending|cached|unavailable|too_large|not_image`), `ConnectionStatus` (`starting|connecting|active|degraded|invalid|silenced`), `PushStatus`; redacting `__repr__` on any type holding secrets
- [X] T011 Implement `bark_pc/core/db.py` schema v1 + migration runner per `data-model.md`: tables `device_identity` (`CHECK (id = 1)`), `bark_link` (`CHECK (id = 1)`), `notification`, `image_cache`, `schema_migrations`; unique index on `(source_server, remote_id)` **where `remote_id IS NOT NULL`**; indexes on `received_at DESC`, `read_at IS NULL`; timestamps as UTC epoch milliseconds; `PRAGMA journal_mode=WAL`, `foreign_keys=ON`
- [X] T012 Implement `bark_pc/core/db.py` repository API: `insert_notification` (ignore duplicates, return existing id), `list_notifications(limit, offset)`, `mark_read(id|all)`, `mark_unread`, `delete_notification`, `clear_history`, `unread_count`, `prune(retention_count, retention_days)` (hard-delete the oldest beyond limits), `get_link`/`upsert_link`/`mark_link_state`, `get_identity_meta`/`upsert_identity_meta`, image-cache row CRUD and `image_cache_bytes()`
- [X] T013 [P] Implement `bark_pc/core/bus.py`: thread-safe outbound queue for receiver events, `post_to_ui(callable)` helper that marshals through the Tk `after` scheduler, and status-event dataclasses
- [X] T014 [P] Implement `bark_pc/core/errors.py`: typed errors with user-facing messages per `contracts/bark-server-api.md` and `contracts/pypush-adapter.md` (`ServerUnreachable`, `ServerTimeout`, `RegistrationRejected`, `ActivationRejected`, `ActivationUnavailable`, `TokenRejected`, `StreamLost`, `IdentityMissing`, `IdentityUnreadable`)
- [X] T015 [P] Define `bark_pc/platform/base.py` protocols: `Tray`, `Notifier`, `Autostart`, `SecretStore`, `SingleInstance`, `Opener`, `UriHandler`; no Win32 imports outside `platform/windows/`
- [X] T016 Implement `bark_pc/platform/windows/secrets.py`: DPAPI `CryptProtectData`/`CryptUnprotectData` via `ctypes` with `CRYPTPROTECT_UI_FORBIDDEN` (never `LOCAL_MACHINE`), atomic blob writes, and `IdentityUnreadable` on decrypt failure (FR-004, FR-035)
- [X] T017 Implement `bark_pc/platform/windows/single_instance.py` per `contracts/local-ipc.md`: mutex `Local\BarkPcClient.{sha1(lowercased SID)[:16]}`, `data/instance.json` (`pid`, `port`, `token`, `started_at`), loopback one-line JSON listener with 2 s timeout and token check, stale-file recovery (FR-022)
- [X] T018 Implement `bark_pc/platform/windows/tray.py` minimal tray: `pystray` icon started with `run_detached()`, static menu items `Open` and `Quit`, icon assets under `assets/icons/` (16/20/24/32 px), and a task that re-adds the icon after Explorer restarts
- [X] T019 Implement `bark_pc/ui/app_shell.py`: `SetProcessDpiAwareness(2)` before Tk init, hidden root window, close-to-tray default, `post_to_ui` implementation, ordered shutdown (stop receiver → close DB → remove `instance.json`), and a single quit confirmation path
- [X] T020 Implement `bark_pc/app.py` and `bark_pc/__main__.py` composition root: config → logging → DB → single instance → shell → tray → receiver thread placeholder; graceful shutdown on SIGINT/exit; no console window when frozen
- [X] T021 Create `tests/conftest.py` fakes and fixtures: `FakePushReceiver` (scripted payloads and status transitions), `FakeNotifier` (records calls), `InMemorySecretStore`, `FakeBarkServer` built on `httpx.MockTransport`, temp portable dirs, and payload fixture loader
- [X] T022 [P] Write `tests/unit/test_retention.py` (prune by count and by age, newest kept, oldest deleted first) and `tests/unit/test_settings.py` (defaults, out-of-range fallback, atomic write leaves no temp files)

**Checkpoint**: Foundation ready — `python -m bark_pc` starts to a tray icon with no stories implemented.

---

## Phase 3: User Story 1 — Receive Bark notifications on the Windows desktop (Priority: P1) 🎯 MVP CORE

**Goal**: Text pushes addressed to the device key appear as native Windows toasts while the app runs windowless, and are recorded in history.

**Independent Test**: With a pre-seeded identity and link fixture (no activation), start the app, replay `tests/fixtures/payloads/text.json` through the receiver, and confirm a notifier call with the correct title/body and one history row, while the window is closed.

- [X] T023 [P] [US1] Write `tests/unit/test_payload_mapping.py` covering all fixtures in `contracts/push-payload.md`: `text`, `text_with_subtitle`, `group`, `delete`, `encrypted`, `empty_alert`, `string_alert`, `unknown_fields`, `invalid_utf8`, `duplicate_id`; assert lower-cased/stringified custom fields, `title — subtitle` merging, `delete == "1"`, ciphertext placeholder text, ignore-on-empty-alert, and tolerance of a bare-string `aps.alert`
- [X] T024 [P] [US1] Write `tests/unit/test_notification_ingest.py` with `FakeNotifier` and a temp SQLite file: three parsed payloads produce exactly three rows newest-first, one ack per message, a duplicate `remote_id` is ignored, and a `delete` outcome removes the matching row
- [X] T025 [P] [US1] Implement `bark_pc/push/payload.py`: decode UTF-8 with `errors="replace"`, return a `ParseOutcome` (`message | delete | empty | encrypted | malformed`) plus a `Notification` draft; apply the mapping table, 200-char toast truncation rule metadata, markdown-image extraction hook, and never raise on unknown or missing fields (FR-009, FR-012, FR-013)
- [X] T026 [US1] Implement `bark_pc/push/pypush_client.py` per `contracts/pypush-adapter.md`: `activate()` → `ActivationMaterial`, `run()` using `apns.create_apns_connection` with the cached base token and `courier="1-courier.push.apple.com"`, `mint_scoped_token(topic)`, `notification_stream`, `ack()` for every message, mapped error taxonomy, secret-redacted reprs, `stop()`; this is the only module importing `pypush` (FR-002)
- [X] T027 [US1] Implement `bark_pc/push/supervisor.py`: dedicated thread running an `anyio` event loop, single live connection, status transitions `connecting → active → degraded`/(`invalid`), backoff 1 s → 60 s with jitter, re-activate only on `TokenRejected` once more before `invalid`, and `last_connected_at` persistence (FR-011, FR-027)
- [X] T028 [US1] Implement `bark_pc/platform/windows/toast.py` text path: `InteractableWindowsToaster("Bark Desktop Client")`, text fields `[title or "Bark", body]`, per-line truncation at 200 chars, `launch_action` = message URL when `http(s)` else a no-op stub, `on_activated`/`on_failed` logging only; tolerate `UnsupportedOSVersionException` by logging (FR-010)
- [X] T029 [US1] Wire the receive path in `bark_pc/app.py`: supervisor → `payload.parse` → `db.insert_notification` → `toast.show` → unread counter; delete outcomes call `db.delete_notification`; encrypted/empty outcomes follow `contracts/push-payload.md`; never let a UI exception kill the stream (FR-008, FR-009, FR-012)
- [X] T030 [US1] Guard the hot path in `bark_pc/push/supervisor.py` + `bark_pc/core/bus.py`: bounded queue (drop-oldest with a counter, never block acks), exception isolation per message, and a "receiver stalled" status after 90 s without a keepalive
- [X] T031 [P] [US1] Write `tests/unit/test_supervisor_status.py` with a stub `FakePushReceiver`: `degraded` schedules a reconnect with growing, jittered backoff; three consecutive connect failures end in `invalid` with no retry storm; `stop()` cancels in-flight tasks
- [ ] T032 [US1] Run and record `quickstart.md` §3 (text push) plus the "30 pushes in 5 s" failure-path row on Windows; save observations to `docs/validation/US1.md` (all 30 in history, toasts may coalesce, unread = 30, UI responsive)

**Checkpoint**: US1 works end-to-end with a seeded identity; text delivery, history rows, and toasts are verifiable.

---

## Phase 4: User Story 2 — Set up the client as a Bark device (Priority: P1)

**Goal**: A first-run wizard activates an anonymous device identity, registers its token with the chosen Bark server, and yields a usable device key plus QR code — with no Apple account involved.

**Independent Test**: On a clean portable folder, complete the wizard against a local `bark-server`, confirm `GET /register/{key}` returns success and the displayed push URL delivers to the running app.

> **Implementation note**: US2 is what makes US1 runnable end-to-end. Although listed after US1 by spec priority, build US2's activation/registration path before claiming a working MVP.

- [X] T033 [P] [US2] Write `tests/unit/test_bark_server_client.py` using `httpx.MockTransport` (no sockets): `POST /register` success reads `data.device_key`, legacy `data.key` fallback works, an empty token is rejected client-side, a token longer than 160 chars is rejected **before** the request, `403` closed registration → `ServerRejected`, `500` → `ServerError`, `GET /register/{key}` ok/fail, `GET /ping` ok/fail, `POST /push` ok/fail (contract: `contracts/bark-server-api.md`)
- [X] T034 [P] [US2] Write `tests/unit/test_links.py` with a fake server client: registration refusal keeps the previous `linked` row usable, a server switch marks the old link `abandoned` and never claims revocation, same-server rotation overwrites the key mapping and persists the new key atomically
- [X] T035 [P] [US2] Implement `bark_pc/bark/server_client.py` with `httpx`: normalize `server_url` (`https` default, no trailing slash, no embedded credentials, reject non-`http(s)`), 10 s timeout, `register(device_token, device_key=None, device_type="ios")`, `verify_key`, `ping`, `push_test`; map every failure to `core/errors.py` (FR-024, FR-030, FR-031)
- [X] T036 [P] [US2] Implement `bark_pc/push/identity.py`: build/load/save `ActivationMaterial` through `SecretStore` (`cert_pem`, `key_pem`, `udid`, `serial`, `base_token_hex`), compute `cert_fingerprint` = SHA-256 of DER cert, reuse only when fingerprint matches the DB row, atomic replace on success, discard partial material on failure, and rate-limit activation to ≥60 s between attempts (FR-002, FR-004, FR-007)
- [X] T037 [US2] Implement `bark_pc/bark/links.py`: link lifecycle transitions (`unlinked → registering → linked | stale`, `linked → stale | abandoned`), `device_key` validation (non-empty, ≤160 chars, URL-safe), `push_url` = `{server_url}/{device_key}`, share payload = the same URL, and an explicit note string for abandonment ("the server cannot delete keys; this key will stop receiving when the device stops acknowledging")
- [X] T038 [US2] Implement `bark_pc/ui/setup_wizard.py` 3-step flow: (1) server choice with public default `https://api.day.app` + "self-hosted" address field, (2) activation → registration progress with the status labels from `data-model.md` §ConnectionStatus and no account prompt anywhere (FR-003), (3) result: device key, server address, push URL, QR, copy buttons; plus "Retry", "Change server", and a confirmation dialog for "Replace identity" (FR-001, FR-005, FR-007)
- [X] T039 [US2] Render the QR with `segno` in `bark_pc/ui/setup_wizard.py`: encode the push URL, render to PNG via `Pillow`, display at 240 px, and offer "Save PNG"/"Copy URL"
- [X] T040 [US2] Implement the self-test push in `bark_pc/bark/links.py` + `bark_pc/ui/setup_wizard.py`: generate a UUID `id`, `POST /push`, then wait ≤30 s on the receiver for that `id`; report two distinct outcomes ("server accepted" / "received on this device") with the server's message on failure (FR-006)
- [X] T041 [US2] Wire first-run detection in `bark_pc/app.py`: open the wizard when identity is `absent` or link is `unlinked`; while unresolved set tray state `invalid` and suppress toasts; on success start the receiver without restarting the app
- [X] T042 [US2] Add a `Setup…` and `Registration status` entry to the tray menu in `bark_pc/platform/windows/tray.py` so the wizard is reachable after first run
- [X] T043 [US2] Write `tests/unit/test_identity.py`: fingerprint mismatch → treated as `absent`, decrypt failure → `IdentityUnreadable` without deleting the blob, atomic replace leaves no partial file, activation rate limit enforced
- [ ] T044 [US2] Run and record `quickstart.md` §2 + §10 rows for invalid server URL, `CLOSE_REGISTER=true`, and corrupted `data/identity.bin`; save to `docs/validation/US2.md` (previous link stays active on failure, history/settings intact)

**Checkpoint**: A clean machine reaches a registered device key and receives a self-test push without any Apple credentials.

---

## Phase 5: User Story 3 — Review recent notifications from the tray (Priority: P2)

**Goal**: A persistent, glanceable history with unread state, delete/recall support, and tray quick actions.

**Independent Test**: Push five notifications, dismiss the toasts, open history from the tray, confirm all five newest-first with unread dots, mark all read, delete one, restart the app, and confirm the remaining four persist with correct read state.

- [X] T045 [P] [US3] Write `tests/unit/test_history_repo.py` against a temp SQLite file: ordering, paging (100 + load more), mark read/unread, delete, clear, unread count, and persistence across a close/reopen cycle
- [X] T046 [P] [US3] Write `tests/unit/test_unread.py`: unread count excludes soft/hard-deleted and read rows, badge cap formatting (`9+`), and marking read in a different order stays consistent
- [X] T047 [P] [US3] Implement `bark_pc/ui/history_window.py` list pane: newest-first, columns `time | title | group | unread dot`, 320 px thumbnail slot, keyboard navigation (`Up/Down/Enter/Delete`), paging at 100 rows, empty state ("No notifications yet" + hint to copy the device key)
- [X] T048 [P] [US3] Implement `bark_pc/ui/history_window.py` detail pane: title, subtitle, body (selectable), group, received time, image area placeholder, "Open link" (default browser via `webbrowser.open` only for `http(s)`), "Copy content", mark read/unread, delete
- [X] T049 [US3] Implement unread badge rendering in `bark_pc/platform/windows/tray.py`: Pillow overlay with count (cap `9+`), tooltip showing server + last message title, and a separate "silenced" glyph slot for US5
- [X] T050 [US3] Implement the full tray menu in `bark_pc/platform/windows/tray.py`: `Open history`, `Mark all read`, `Test notification`, `Copy device key`, `Settings…`, `Setup…`, `Quit` with ≤2 interactions each (FR-019, SC-009)
- [X] T051 [US3] Implement recall behavior in `bark_pc/ui/history_window.py` + `bark_pc/platform/windows/toast.py`: `delete` payload removes the row and dismisses the matching toast via `windows-toasts` `remove_toast`/group removal; also remove it from the open window live (FR-012)
- [X] T052 [US3] Implement retention execution in `bark_pc/core/db.py` + `bark_pc/app.py`: prune at startup and hourly using `history_retention_count` (50–10 000) and `history_retention_days` (1–365), keep newest, log counts only (FR-018)
- [X] T053 [US3] Implement `Clear history` with confirmation and "Remove all data" entry point in `bark_pc/ui/settings_dialog.py` (full data wipe logic completed in T080), returning the exact counts removed (FR-020)
- [X] T054 [US3] Harden history rendering in `bark_pc/ui/history_window.py`: emoji/CJK/RTL text, 8 KB body cap, 200-char toast preview, DPI-scaled fonts, and no layout shift when a row's image is missing
- [ ] T055 [US3] Run and record `quickstart.md` §5–§6 and the restart scenario; save to `docs/validation/US3.md`

**Checkpoint**: History is durable, ordered, and fully operable from the tray without opening a window first.

---

## Phase 6: User Story 4 — Receive and view image notifications (Priority: P2)

**Goal**: Image-referencing pushes render thumbnails, open full size, cache by URL, and never block or fail the text delivery.

**Independent Test**: Push a reachable image and an unreachable image; the first shows a hero image in the toast and an openable thumbnail, the second still delivers text with an "image unavailable" placeholder, and a repeat of the first renders instantly with no new download.

- [X] T056 [P] [US4] Write `tests/unit/test_image_fetcher.py` using `httpx.MockTransport` (no local server): a cached URL causes zero transport calls, a body over 10 MB yields `too_large`, `text/html` yields `not_image`, the redirect cap is enforced at 2, and LRU eviction respects the cache limit without deleting files referenced by retained notifications
- [X] T057 [P] [US4] Write `tests/unit/test_image_cache.py`: URL normalization/hash stability, thumbnail width 320 with preserved aspect, corrupt image → `unavailable`, cache accounting matches file sizes
- [X] T058 [P] [US4] Implement `bark_pc/images/fetcher.py`: 2 worker threads, `httpx` with 10 s connect / 20 s read / 10 MB cap / 2 redirects / `Content-Type: image/*` required, browser-like UA, `url_hash = sha256(normalized_url)`, errors mapped to cache states, no blocking of the receiver (FR-014, FR-016)
- [X] T059 [P] [US4] Implement `bark_pc/images/cache.py`: content-addressed files under `data/images/`, 320 px thumbnails, `last_used_at` updates, LRU eviction to `image_cache_mb` (20–2 000), orphan purge older than 7 days, and the `image_cache` table as the accounting source (FR-017)
- [X] T060 [US4] Render thumbnails and placeholders in `bark_pc/ui/history_window.py`: `pending` spinner-free placeholder, `unavailable`/`too_large`/`not_image` labels with a "Retry" action for `unavailable`, and 320 px `ImageTk` thumbnails
- [X] T061 [US4] Implement `bark_pc/ui/image_viewer.py`: full-size window, fit-to-window, zoom 25–400 %, save-as (default `<title>-<timestamp>.png`), copy to clipboard via `Pillow` → BMP → `CF_DIB`, and a "Show in folder" action using `os.startfile` on the containing directory
- [X] T062 [US4] Wire images into toasts in `bark_pc/platform/windows/toast.py`: hero placement for message images, `AppLogo` fallback icon, temp file lifecycle with cleanup after the toast is dismissed or 10 minutes elapse, and graceful degradation if placement is unsupported (spike T0 findings)
- [X] T063 [US4] Extract markdown image URLs in `bark_pc/push/payload.py` per `contracts/push-payload.md` rule 4: when `icon` is absent and the body contains `![alt](url)`, use the first `http(s)` URL as `image_url` while storing the text verbatim
- [X] T064 [US4] Prioritize image fetching in `bark_pc/images/fetcher.py`: bounded queue with priority for the currently visible history page and the newest notification, coalescing duplicate URL requests, cancel-on-quit
- [ ] T065 [US4] Run and record `quickstart.md` §4 including the unreachable-image case and repeat-push cache behavior; save to `docs/validation/US4.md`

**Checkpoint**: Image notifications are complete, cached, and fail gracefully without affecting text delivery.

---

## Phase 7: User Story 5 — Tray-first, unobtrusive operation (Priority: P3)

**Goal**: The app behaves as a daily background utility: starts at sign-in (optional), lives in the tray, exposes status and quick actions, handles single-instance focus and toast click-through, and can be silenced.

**Independent Test**: Enable start-with-Windows, sign out/in, confirm a tray-only process with no admin prompt; double-click restores the window; a second launch focuses the first; a move of the portable folder produces the repair prompt; idle CPU/memory stay within budget.

- [X] T066 [P] [US5] Write `tests/unit/test_autostart.py` using a `HKCU\Software\BarkDesktopClient\TestRun` mirror key: writes a quoted path, disables by deleting the value, detects a stale stored path, and never writes an empty or relative path
- [X] T067 [P] [US5] Write `tests/unit/test_single_instance_protocol.py`: `focus`/`open?id=` message encoding and parsing, invalid token refused, malformed JSON rejected without raising, and stale `instance.json` recovery using an injected port-reachability check (no real mutex or socket)
- [X] T068 [P] [US5] Implement `bark_pc/platform/windows/autostart.py` per `contracts/local-ipc.md` §3: `HKCU\...\CurrentVersion\Run` value `BarkDesktopClient` with the quoted absolute exe path, enable/disable, `autostart_path` tracking, and a repair decision payload (`repair | disable | ignore`) for a moved folder (FR-021, FR-037)
- [X] T069 [P] [US5] Implement `bark_pc/platform/windows/uri_scheme.py` per `contracts/local-ipc.md` §2: idempotent `HKCU\Software\Classes\barkpc` registration (`URL Protocol`, `DefaultIcon`, `shell\open\command`), parsing/validation of `barkpc:open`, `barkpc:open?id=<n>`, `barkpc:setup`, and complete removal on request
- [X] T070 [US5] Handle command-line input in `bark_pc/app.py`: accept a `barkpc:` URI or `--focus`, forward to the running instance through the single-instance socket, open the requested surface, and exit without starting a second receiver (FR-022)
- [X] T071 [US5] Implement `bark_pc/ui/settings_dialog.py`: server URL + `push_topic` override, retention count/days, image cache MB, toasts enabled, hide preview text, silence-until picker, autostart toggle with repair prompt, "Check connection", "Export diagnostics", "Remove all data" (FR-021, FR-023, FR-025, FR-032)
- [X] T072 [US5] Implement silence and privacy modes in `bark_pc/core/config.py` + `bark_pc/app.py`: `silence_until` suppresses toasts while still recording (tray `silenced` glyph, auto-expire), and `hide_preview_text` renders `["Bark Desktop Client", "New notification"]` (FR-023)
- [X] T073 [US5] Complete tray state rendering in `bark_pc/platform/windows/tray.py`: distinct icons for `starting|connecting|active|degraded|invalid|silenced`, `last connected` tooltip line, double-click restores, and Explorer-restart recovery verified
- [X] T074 [US5] Implement quit semantics in `bark_pc/app.py` + `bark_pc/ui/app_shell.py`: confirmation, receiver cancel with 5 s timeout, DB close, `instance.json` removal, no orphan threads/processes; second launch never spawns a duplicate receiver (FR-008, FR-022)
- [X] T075 [US5] Create `bark_pc/tools/measure_idle.py`: sample working set/private bytes/CPU every 30 s for a configurable duration, write CSV + summary, and fail with a non-zero exit if working set >150 MB or average CPU >1 % (SC-006)
- [ ] T076 [US5] Run and record `quickstart.md` §6–§8 (tray actions, autostart, moved folder, idle measurement at 1 h and 24 h); save to `docs/validation/US5.md`

**Checkpoint**: All five stories work independently; the app is a proper tray-first background utility.

---

## Phase 8: Polish & Cross-Cutting Concerns

**Purpose**: Portability, packaging, diagnostics, privacy hardening, and full validation.

- [X] T077 [P] Implement diagnostics export in `bark_pc/ui/settings_dialog.py` + `bark_pc/core/logging.py`: zip the three log files plus a redacted settings snapshot (no keys, tokens, bodies) to a user-chosen folder (FR-026)
- [X] T078 [P] Complete "Remove all data" in `bark_pc/platform/windows/*` + `bark_pc/core/config.py`: delete `identity.bin`, `bark.db`, `images/`, `logs/`, `settings.json`, the autostart value, the `barkpc` key, and `instance.json`; report exactly what was removed (FR-004)
- [X] T079 [P] Add scheduled maintenance in `bark_pc/app.py`: startup + hourly retention prune, hourly link `verify_key` (throttled, jittered), daily orphan image purge, with all failures logged and never blocking the receiver
- [X] T080 [P] Write `README.md` user guide: install/unpack, setup, sending pushes (curl examples for text/image/delete), tray actions, troubleshooting table, privacy statement, and the "no Apple account is used" explanation; add `NOTICE` entries for pypush (SSPL), pystray (LGPL-3.0), Pillow (HPND), windows-toasts (MIT), segno (BSD)
- [X] T081 Implement `bark_pc/tools/build_portable.py`: PyInstaller `--onedir --windowed --name BarkDesktopClient` with explicit `--hidden-import` entries from spike T2 (`winrt.*`, `pypush.apns`, `anyio` backends), version resource, icon, `NOTICE`/licence inclusion, and zip output to `dist/`
- [ ] T082 Validate the portable build per `quickstart.md` §9 in a clean Windows VM with no Python installed: start, activate, register, receive a toast with an image, open history, then delete the folder and confirm only the optional autostart entry and `barkpc` key remain (SC-011)
- [X] T083 [P] Security pass over `bark_pc/`: no secrets or bodies in logs, DPAPI-only persistence, URL open restricted to `http(s)`, no `shell=True`, URI inputs validated, image fetches size/type capped, and no outbound calls except the chosen server, Apple endpoints, and image URLs (FR-004, FR-026)
- [ ] T084 [P] Robustness soak: 24 h run with hourly pushes and a mid-run network drop + sleep/resume; verify memory trend is flat, no duplicate rows, no unhandled exceptions in `data/logs/app.log`, and reconnect within 60 s (SC-003, SC-007); record in `docs/validation/soak.md`
- [ ] T085 [P] Performance measurements recorded in `docs/validation/perf.md`: warm start <3 s, 20-push p95 toast latency, image thumbnail <3 s, idle CPU/memory within T075 thresholds (SC-002, SC-005, SC-006)
- [ ] T086 [P] Text/encoding pass in `bark_pc/ui/history_window.py` + `bark_pc/platform/windows/toast.py`: CJK, RTL, emoji, 8 KB body, 200-char truncation, no mojibake in toasts or history; verify high-DPI at 100/150/200 % and multi-monitor window restore; record in `docs/validation/encoding.md`
- [X] T087 Code quality pass: `ruff` clean, public modules type-hinted, no dead code, `pypush` imports confined to `bark_pc/push/pypush_client.py` (grep check), and `platform/windows/` is the only module importing Win32/WinRT
- [ ] T088 Final end-to-end validation of the full matrix in `quickstart.md` (SC-001 … SC-012) recorded in `docs/validation/final.md`, including the three spike outcomes and any deviations from `spec.md`

---

## Dependencies & Execution Order

### Phase dependencies

- **Setup (Phase 1)**: no dependencies; T006 (spike T1) is a **go/no-go gate** for the whole feature
- **Foundational (Phase 2)**: depends on Setup; blocks all stories
- **User Stories (Phase 3–7)**: all depend on Phase 2; can proceed in parallel if staffed
- **Polish (Phase 8)**: depends on the stories being delivered

### User story dependencies

- **US1 (P1)**: needs Phase 2 only for its unit tests; a real end-to-end run needs US2's identity + link, so US2's T035–T037 should be completed before the first manual validation of US1
- **US2 (P1)**: independent of US1 (verifiable via `GET /register/{key}` and the self-test push); shares the receiver stream with US1 for T040
- **US3 (P2)**: depends on US1 for rows to display, but its repository/view-model tests run on seeded data without US1
- **US4 (P2)**: depends on US1's payload `image_url` and US3's list pane for thumbnails; toast image work (T062) depends on T028
- **US5 (P3)**: depends on the tray from Phase 2 and the settings surface; otherwise independent

### Within each story

- Tests (T023/T024, T033/T034, T045/T046, T056/T057, T066/T067) are unit tests that must pass before the phase's manual validation task
- Models/parsers before services, services before wiring, wiring before manual validation
- Manual validation task is last in each phase

### Parallel opportunities

- Setup: T003, T004, T005, T007, T008 run in parallel after T001–T002
- Foundational: T010, T013, T014, T015 in parallel; T016/T017 in parallel; T022 parallel with T018–T021
- US1: T023, T024, T025 in parallel; T026 and T028 in parallel after T025
- US2: T033, T034, T035, T036 in parallel
- US3: T045, T046, T047, T048 in parallel
- US4: T056, T057, T058, T059 in parallel
- US5: T066, T067, T068, T069 in parallel
- Polish: T077–T080 and T083–T086 largely parallel

---

## Parallel Example: User Story 2

```bash
# Unit tests and the two independent clients:
Task: "Write tests/unit/test_bark_server_client.py per contracts/bark-server-api.md"
Task: "Write tests/unit/test_links.py"
Task: "Implement bark_pc/bark/server_client.py"
Task: "Implement bark_pc/push/identity.py"

# Then, sequentially:
Task: "Implement bark_pc/bark/links.py"        # uses server_client + identity
Task: "Implement bark_pc/ui/setup_wizard.py"   # uses links
Task: "Render the QR with segno in the wizard"
Task: "Implement the self-test push"
```

## Parallel Example: User Story 4

```bash
Task: "Write tests/unit/test_image_fetcher.py"
Task: "Write tests/unit/test_image_cache.py"
Task: "Implement bark_pc/images/fetcher.py"
Task: "Implement bark_pc/images/cache.py"

# Then:
Task: "Render thumbnails/placeholders in history_window.py"
Task: "Implement ui/image_viewer.py"
Task: "Wire images into toasts"
```

---

## Implementation Strategy

### MVP scope

**MVP = Phase 1 + Phase 2 + US2 (T033–T044) + US1 (T023–T032).** The two P1 stories are inseparable in practice: US2 produces the identity and key, US1 turns arrivals into toasts and history. Deliver this before touching P2/P3.

1. Complete Setup, and **stop if spike T1 fails** — the product premise depends on the base token being accepted by APNs via Bark.
2. Complete Foundational.
3. Build US2 (activation → registration → key/QR → self-test), validate against a local `bark-server`.
4. Build US1 (stream → parse → toast → history), validate with real pushes.
5. **STOP and validate the MVP** with `quickstart.md` §1–§3.

### Incremental delivery

1. MVP (US2 + US1) → demo: real Bark pushes on the Windows desktop.
2. Add US3 → demo: durable history, recall, tray actions.
3. Add US4 → demo: image notifications and viewer.
4. Add US5 → demo: autostart, single instance, silence, status.
5. Polish → portable zip, diagnostics, privacy review, SC matrix.

### Notes

- Every task names a concrete file; an implementer should not need to re-read the spec to start.
- Constraints quoted from `data-model.md` (retention ranges, `≤160` char keys, cache 20–2 000 MB, state enums, partial unique index) are binding, not suggestions.
- Do not add dependencies beyond T002 without checking the memory budget in `research.md` §Dependency footprint.
- Commit after each task or logical pair; never leave the receiver thread un-stoppable between commits.

---

## Implementation status (updated 2026-11-10)

Automated work is complete and the suite is green:

* `python -m pytest` → **182 passed** (headless, offline, fakes for APNs/server).
* `python -m ruff check bark_pc tests` → clean; `ruff format --check` → clean.
* Portable build verified: `dist/BarkDesktopClient/` (59 MB) + zip (28 MB); the
  packaged exe starts in 0.44 s, uses 58 MB working set, answers the single-instance
  protocol and exits cleanly.

Tasks left unchecked are **validation tasks that need external services or a human
in front of the screen**, not missing implementation:

| Task | What it needs | Record |
|------|---------------|--------|
| T006 (spike T1: token accepted by APNs via Bark) | Outbound Apple activation + a Bark server + a real push | `docs/validation/spike_T1.md` |
| T007 (spike T0: toast from unpackaged exe) | Visual check on Windows 11 and Windows 10 21H2 | `docs/validation/spike_T0.md` |
| T032, T044, T055, T065, T076 (per-story manual validation) | Live pushes, local `bark-server`, sign-out, 1 h/24 h runs | `docs/validation/US*.md` |
| T082 (clean-VM portable build) | Clean Windows VM without Python | `docs/validation/final.md` (SC-011) |
| T084 (24 h soak) | Long run with network drops/sleep-resume | `docs/validation/soak.md` |
| T085 (latency/CPU measurements) | Live APNs delivery and a 1 h CPU sample | `docs/validation/perf.md` |
| T086 (visual DPI/encoding pass) | 100/150/200 % scaling, multi-monitor | `docs/validation/encoding.md` |
| T088 (final SC matrix) | The runs above | `docs/validation/final.md` |

Everything else in Phases 1–8 is implemented, tested, and documented.
