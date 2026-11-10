# Phase 0 Research — Bark Desktop Client for Windows

Date: 2026-11-10. All measurements were taken on the development machine (Windows 11, Python 3.11.2, 64-bit) unless noted. RSS is working-set/private, sampled via `Get-Process`.

## R1. GUI toolkit — Tkinter + ttk

**Decision**: Use the standard-library `tkinter` + `ttk` widgets for all windows (history, setup wizard, settings, image viewer), styled with a small custom ttk theme.

**Rationale**:
- Measured: bare interpreter 12.2 MB → `tkinter` root window 24.4–25.7 MB working set (Tk root + ttk widgets ≈ 26 MB). Nothing else in the candidate set is close.
- Zero dependencies and no extra binaries in the portable bundle (important because the app ships as a zip folder).
- Adequate for the required surfaces: a two-pane list/detail window, a 3-step wizard, modal dialogs, and an image viewer with zoom/save.
- Tk must own the main thread, which is compatible with `pystray` (see R2) and with the async receiver living in a worker thread.

**Alternatives considered**:
- **PySide6/Qt**: best widgets and built-in tray, but Qt libraries add roughly 60–120 MB RSS and a large bundle; rejected against the < 150 MB memory goal.
- **wxPython**: native Win32 controls and a good task-bar icon, but adds ~40–60 MB RSS and a sizeable compiled package for no functional gain over Tk here.
- **pywebview/WebView2**: heavier (WebView2 processes) and breaks "no embedded browser engine".
- **Dear PyGui / Electron / web UI**: no native tray story (DPG) or unacceptable footprint (Electron).

**Risk**: Tk's look is dated and DPI handling needs explicit work (`ctypes.windll.shcore.SetProcessDpiAwareness`, ttk scaling). Mitigated in `ui/theme.py`; acceptable for a utility app.

## R2. System tray — pystray

**Decision**: `pystray` 0.19.x, icon started with `run_detached()` so the Tk main loop keeps ownership of the main thread; menu actions marshal to Tk via `root.after`.

**Rationale**: 0.2 MB on disk, ~1 MB RSS, uses the Win32 shell API directly (no `pywin32`), supports dynamic menus (unread count, connection state) and icon updates. Verified import and icon construction in a spike.

**Alternatives considered**: `wx.Adv.TaskBarIcon` (requires all of wxPython), Qt `QSystemTrayIcon` (requires Qt), custom `Shell_NotifyIcon` via ctypes (reinvents pystray).

**Risk**: two event loops (Tk + pystray) must never touch each other's widgets; all cross-thread UI work goes through one `post_to_ui()` helper. Tray icon must be re-added after Explorer restarts — verified pattern exists in pystray; add a recovery task.

## R3. Windows toast notifications — windows-toasts 1.3.1

**Decision**: `windows-toasts` with `InteractableWindowsToaster` (needed for hero/inline images), `Toast(["title","body"])`, and images supplied as `ToastDisplayImage.fromPath(path, position=ToastImagePosition.Hero)` or `ToastImagePosition.AppLogo`. Click-through uses the toast `launch_action` with the message URL, or a `barkpc:` URI when the message has no URL.

**Rationale**: Wraps WinRT toast APIs with a small API surface (0.1 MB) plus `winrt-runtime` and `winrt-Windows.UI.Notifications` (2.5 MB). Spike result: `show_toast()` returned success with a hero image on this machine; a plain `WindowsToaster` rejects hero placement (warning) and only supports app-logo images, so the interactable variant is required.

**Alternatives considered**: raw `winrt` calls (more XML plumbing for the same result), PowerShell `New-BurntToastNotification` (spawns a process per toast, ~1 s latency, dependency on a module), `pystray` balloon tips (legacy, not native toasts, poor image support), Win32 `Shell_NotifyIcon` balloons (same).

**Open risk (spike T0)**: unpackaged, portable apps normally need an AppUserModelID backed by a Start Menu shortcut for toasts to appear reliably and to show a sensible source name. The spike showed no shortcut was created and no AUMID registry key was registered, yet the API reported success. **Display must be verified visually on a clean Windows VM** before the UI work is considered done; fallbacks: write a per-user Start Menu shortcut at startup (allowed — per-user, no admin), or use `ToastSystemButton`-free plain toasts with the process AUMID.

## R4. Push reception — pypush 2.0.2 APNs client

**Decision**: Depend on `pypush==2.0.2`, used only through `bark_pc/push/pypush_client.py`, following the library's own CLI flow:

```text
cert, key = await apns.activate(device_class="Windows", udid=..., serial=...)
async with apns.create_apns_connection(cert, key, token=cached_base_token,
                                       courier="1-courier.push.apple.com") as conn:
    token = await conn.mint_scoped_token("me.fin.bark")
    async with conn.notification_stream("me.fin.bark", token) as stream:
        async for n in stream:
            await conn.ack(n)
            yield n.payload      # bytes, JSON
```

**Rationale**:
- It is the only maintained Python implementation that both activates as an Apple device and receives courier pushes; the user mandated it.
- `activate()` requires **no Apple account**: it signs an activation payload with the bundled FairPlay key, posts to `albert.apple.com/deviceservices/deviceActivation`, and returns a device certificate + RSA key. Passwords and 2FA are never involved. The spec was corrected accordingly.
- The library already caches and reuses a base token (`create_apns_connection(token=...)`, "Fix reconnection with a cached base token") and keeps a 30 s keepalive plus exponential-backoff reconnect, which we can rely on while still supervising it ourselves.
- Import cost measured at +16 MB RSS over baseline (28.1 MB total with `httpx`, `cryptography`, `anyio` loaded).

**Alternatives considered**: implementing APNs courier protocol in-house (months of reverse engineering, actively changing), using an iOS-side bridge (defeats the purpose), push relay over HTTP (not Bark), Bark server polling (`/messages` backfill was explicitly declined; polling also breaks the "live delivery" model).

**Risks**:
- **License**: pypush is SSPL-licensed and was purchased by Beeper. Bundling it into a distributed build imposes SSPL obligations (source availability for the combined work). Mitigation: keep the adapter boundary, ship the dependency unmodified with its license text, keep this repository's own source public, and record the licensing decision as a release blocker to resolve before publishing binaries.
- **Stability**: the project states it is unstable until 3.0.0. The adapter pins the version, wraps all exceptions in our own error taxonomy, and supports replacing the adapter wholesale.
- **Activation abuse control**: cache the activation blob (R8) and rate-limit retries; never activate on every launch.

## R5. Device registration with the Bark server

**Decision**: Register by `POST {server}/register` with a JSON body `{"device_key": <optional>, "device_token": <base_token_hex>, "device_type": "ios"}`, parse `data.device_key`/`data.key` from the response, and validate with `GET {server}/register/{device_key}`. Use `GET {server}/ping` for reachability. Always call the same server the user selected (public or self-hosted).

**Rationale**: Confirmed against bark-server source (`route_register.go`):
- `POST /register` parses `DeviceInfo` with `device_key`, `device_token`, `device_type`, plus legacy `key`/`devicetoken`; generates a UUID key when `device_key` is empty; rejects empty tokens and tokens longer than 160 chars; rejects device types other than `ios`/`watchos`; returns `{"code":200,"data":{"key","device_key","device_token","device_type"}}`.
- `GET /register/:device_key` returns success/failure for an existing key, which is our cheap liveness check.
- The V1-compatible `GET /register?devicetoken=...&key=...` exists and is useful for servers that only implement the old API.

**Limitation found**: bark-server exposes **no unregister/delete endpoint**. Re-registering the same key with a new token overwrites the mapping, which gives us same-server rotation. When the user abandons a server, the old key remains mapped to a token we no longer control; the UI must state this plainly (FR-032 is satisfiable for rotation, and truthfully reported for abandonment). The token becomes useless once we stop acknowledging/delivering on it, but the key itself cannot be destroyed remotely.

## R6. Token semantics (highest technical risk — spike T1)

**Decision**: Register the **base** push token (`conn.base_token.hex()`, the token assigned in the courier `ConnectAck`) as the Bark `device_token`, and use `mint_scoped_token("me.fin.bark")` only for the receive-side filter. Validate end-to-end before building any UI.

**Rationale**: bark-server pushes with its own provider key to `api.push.apple.com`, topic `me.fin.bark` (hardcoded in `apns/apns.go`, with `me.fin.bark.watch` for watchOS). The device token it stores must therefore be the token APNs recognises for that device — i.e. the base token, which is exactly what `ConnectAck` returns. Scoped tokens are a courier-side subscription concept, not the APNs device token.

**Why it is a risk**: if the base token only works for courier connections and not for provider pushes, `POST /register` will succeed but pushes will fail with `BadDeviceToken`/`DeviceTokenNotForTopic`. Cheap detection: register, then push through the selected server and watch the courier stream; if nothing arrives within 30 s, log the server-side rejection (the server returns the APNs reason) and stop.

**Fallbacks if T1 fails**: (a) activate with the sandbox courier and a sandbox-capable server — rejected, Bark's public server is production-only; (b) register the scoped token instead — same test decides; (c) document the limitation and require a self-hosted server whose topic can be changed. Decide only from measurement.

## R7. Local storage — SQLite (WAL) + files

**Decision**: `sqlite3` with WAL journaling for notifications and links; JSON for settings; content-addressed files for images; DPAPI blob for secrets.

**Rationale**: stdlib, no server, robust to crashes, easy retention queries (`DELETE ... WHERE received_at < ? LIMIT`), and cheap for 500 rows. JSON settings stay human-readable and diffable. Images belong on disk, not in BLOBs, to keep the DB small and make the viewer trivial.

**Alternatives considered**: JSON-only store (rewrite amplification, corruption risk on crash), TinyDB (same, extra dep), leveldb/rocksdb (binary dep), SQLite + SQLAlchemy (unnecessary weight for ~4 tables).

## R8. Credential protection — Windows DPAPI

**Decision**: Encrypt the activation blob (certificate, private key, UDID/serial, base token) with `CryptProtectData` (`CRYPTPROTECT_UI_FORBIDDEN`, per-user scope) through `ctypes`, stored as `data/identity.bin`. Never encrypt the database; it holds message content, not credentials, and the folder is already per-user.

**Rationale**: No dependency, ties secrets to the Windows user account, survives portable folder copies by design (another user cannot decrypt). Matches FR-035: state sits in the app folder, secrets are unreadable to other accounts.

**Alternatives considered**: `keyring` (extra dep, may prompt or fail on portable setups, stores outside the folder — violates portability), plaintext (unacceptable), custom AES with a bundled key (security theatre).

## R9. Single instance — named mutex + loopback focus socket

**Decision**: `CreateMutexW` on `Local\BarkPcClient-<user SID hash>` for exclusivity; the first instance listens on a random loopback port recorded in `data/instance.json`; a second instance sends `focus` and exits.

**Rationale**: stdlib `ctypes` only, releases automatically on crash (unlike a lock file), and the loopback socket gives us the "focus existing window" behavior required by FR-022 and the `barkpc:` URI handler.

**Alternatives considered**: lock file (stale after crash), `FindWindow` + `WM_COPYDATA` (fragile with Tk), localhost-only HTTP server (more surface than needed; a single framed line is enough).

## R10. Autostart — HKCU Run key

**Decision**: `HKCU\Software\Microsoft\Windows\CurrentVersion\Run` value `BarkDesktopClient` holding the quoted exe path; store the path we wrote in settings so a move can be detected and repaired (FR-037).

**Rationale**: per-user, no admin, no shortcut COM plumbing; user-visible in Task Manager's Startup tab, which is what users expect and can disable.

**Alternatives considered**: Startup-folder `.lnk` (needs COM/`pywin32` or a `.cmd` shim), Task Scheduler (admin-ish, opaque), Windows service (excluded by decision).

**Risk**: portable folders move; a stale Run entry launches nothing and Windows silently drops it. Detection happens at next manual launch (compare stored vs current path) — that is the FR-037 behavior.

## R11. Portable packaging — PyInstaller onedir

**Decision**: PyInstaller `--onedir --windowed` producing `BarkDesktopClient/` with `bark_pc.exe` plus `_internal/`; ship as a zip. Include license notices for pypush (SSPL), pystray (LGPL-3.0), Pillow (HPND), windows-toasts (MIT).

**Rationale**: onedir starts fastest and lets a portable folder be moved as a unit; windowed mode avoids a console; hidden imports for `winrt.*` and pypush are explicit in the spec file. Approximate payload: Python runtime ~15 MB, site-packages ~62 MB in the spike venv (includes test-only extras), so expect a ~45–70 MB zip.

**Alternatives considered**: `--onefile` (extracts to temp on every launch, slower start, AV false positives), embeddable Python + `pip install` at first run (needs network, breaks "no separately installed runtime"), Nuitka (compile time and WinRT/pypush compatibility risk), MSIX (needs signing and packaging).

**Risk**: `winrt` uses namespace packages; PyInstaller needs explicit `--hidden-import` entries per WinRT module. Verify early with a smoke build (spike T2).

## R12. Image pipeline

**Decision**: Fetch with `httpx` (already a dependency) in a bounded worker pool: 10 s connect, 20 s read, 10 MB cap, `Content-Type` must start with `image/`, follow at most 2 redirects. Validate/decode with Pillow, store under `data/images/<sha256-of-url>.<ext>`, generate a 320 px-wide thumbnail for lists, and evict least-recently-used files when the cache exceeds 200 MB.

**Rationale**: Reuses the existing HTTP stack; caps prevent a malicious or broken URL from freezing the app or filling the disk; URL-addressed cache satisfies FR-017 dedupe; Pillow is required anyway for tray icons and the viewer.

**Alternatives considered**: `requests` (extra dep for no gain), `urllib` (no easy timeouts/redirect caps, TLS ergonomics), caching in SQLite (bloats DB), downloading at toast time synchronously (blocks the receiver).

**Risk**: some hosts hotlink-block or serve HTML with a 200; the `Content-Type` + decode check turns that into the "image unavailable" state required by FR-016.

## R13. Payload parsing and de-duplication

**Decision**: Parse `notification.payload` as UTF-8 JSON produced by bark-server:

| Source | Field | Destination |
|--------|-------|-------------|
| `aps.alert.title` | string | `Notification.title` |
| `aps.alert.subtitle` | string | merged into title (toast has 2 text lines) |
| `aps.alert.body` | string | `Notification.body` |
| `aps.thread-id` / custom `group` | string | `Notification.group` |
| custom `url` | string | `Notification.url` (click-through) |
| custom `icon` | URL | `Notification.image_url` |
| custom `id` | string | `Notification.remote_id` (also APNs `apns-collapse-id`) |
| custom `delete` | `"1"`/`1` | delete by `remote_id` (FR-012) |
| custom `ciphertext` | base64 | unsupported → placeholder entry (R14) |
| custom `level`, `badge`, `sound`, `copy`, `autoCopy`, `ttl` | any | stored raw, not acted on in v1 |
| unknown keys | any | preserved in `Notification.raw` JSON, ignored for display (FR-013) |

De-duplicate on `(server, remote_id)` when present, else on a SHA-256 of the raw payload plus a 5 s window, so acks/redeliveries cannot create duplicates.

**Rationale**: Fields verified against `apns/apns.go` (custom params are lower-cased and stringified) and the V2 push API table. Bark renders markdown on-device; the desktop client treats markdown as plain text in v1 (noted in Assumptions).

## R14. Encrypted pushes in v1

**Decision**: Detect `ciphertext` and create a notification whose body reads "Encrypted message — not supported in this version", store the raw payload, and count it as delivered. No decryption, no retry.

**Rationale**: Spec excludes encrypted pushes; silently dropping a message would break the "never drop" expectation, and showing ciphertext would be noise.

## R15. Threading and lifecycle

**Decision**: Main thread runs Tk. A `push-receiver` worker thread runs an `anyio` event loop that owns activation, connection, and the notification stream. A small image worker pool (2 threads) fetches thumbnails. DB access is serialized through a single connection guarded by a lock (writes are rare). Tray callbacks post UI work via `root.after(0, ...)`.

**Rationale**: `pypush` is async-only, Tk is single-threaded, and pystray needs its own message loop — three loops are unavoidable; keeping exactly one owner per loop avoids deadlocks. The receiver reports typed status events (`connecting`, `active`, `degraded(reason)`, `invalid`) for the tray icon.

## R16. Logging and redaction

**Decision**: `logging` with `RotatingFileHandler` at `data/logs/app.log` (2 MB × 3). A redaction filter drops DPAPI blobs, tokens (any 64+ hex run), `Authorization`-style values, and message bodies; log event ids only. Export from settings copies the logs to a user-chosen folder (FR-026).

**Rationale**: Debuggability for reconnect/registration problems without turning logs into a leak of notification content.

## R17. Verification spikes that must precede UI work

| Spike | Question | Success criterion | Fallback |
|-------|----------|-------------------|----------|
| **T0** | Do toasts from an unpackaged portable exe actually display with correct app name and image? | Toast visibly appears on a clean Windows 11 VM and on Windows 10 21H2 | Create a per-user Start Menu shortcut with an explicit AUMID at first run |
| **T1** | Is the base courier token accepted by `api.push.apple.com` through bark-server? | `POST /push` → message appears on the client stream within 10 s | Re-evaluate token choice (R6) or document self-hosted-only support |
| **T2** | Does the PyInstaller onedir build import `pypush`, `winrt`, `pystray`, and Tk correctly offline? | Built exe starts, registers, shows a toast, and shows history with no network install | Add hidden imports; switch pystray to ctypes-only path |

## Dependency footprint summary

| Package | Disk (spike) | Why needed |
|---------|--------------|------------|
| `pypush` 2.0.2 (+ `cryptography` 10.5 MB, `httpx` 0.6 MB, `anyio` 1.3 MB, `h2`/`hpack`/`cffi` transitive) | ~13 MB | APNs activation + courier receive |
| `Pillow` 13.0 | 15.5 MB | thumbnails, tray/toast images, viewer |
| `pystray` 0.19.5 | 0.2 MB | tray icon + dynamic menu |
| `windows-toasts` 1.3.1 + `winrt-runtime`/`winrt-Windows.UI.Notifications` | 2.6 MB | native toasts with images |
| `segno` (pure Python) | <0.5 MB | QR code for the device key / push URL without a compiled QR dependency |
| Total site-packages in spike venv (includes pip/setuptools) | 62 MB | portable bundle base |

Measured combined idle footprint with Tk + Pillow + pystray + windows-toasts loaded: **44.5 MB**, leaving comfortable headroom under the 150 MB budget for a 500-item history and the receiver thread.
