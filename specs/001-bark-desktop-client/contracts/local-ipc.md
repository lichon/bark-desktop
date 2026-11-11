# Contract: Local Windows Integration

All registrations in this file are **per-user (HKCU)** and require no elevation. Anything that would touch `HKLM`, a machine-wide service, or an installer is out of scope (spec FR-034, FR-036).

## 1. Single instance

- Mutex name: `Local\BarkPcClient.{sha1(lowercased user SID)[:16]}`
- Created with `CreateMutexW` at startup. `ERROR_ALREADY_EXISTS` → this process is a secondary instance.
- The primary instance writes `data/instance.json`:

```json
{ "pid": 12345, "port": 51823, "token": "<32-byte hex>", "started_at": 1762732800000 }
```

- The secondary instance connects to `127.0.0.1:<port>`, sends one JSON line, then exits:

```json
{ "token": "<token from instance.json>", "command": "focus" }
```

- Commands: `focus` (restore and focus the window), `open` with `{"notification_id": 42}` (open the history window at an entry), `quit` (used only by maintenance tooling).
- The listener binds `127.0.0.1` only, accepts one request per connection, times out after 2 s, and requires the token; an invalid token is answered with `{"ok":false}` and logged.
- A stale `instance.json` (port closed) is treated as "no primary instance": delete it and continue startup.

## 2. URI scheme `barkpc:`

Registered under `HKCU\Software\Classes\barkpc`:

| Value | Data |
|-------|------|
| `(Default)` | `URL:Bark Desktop Client` |
| `URL Protocol` | `""` |
| `DefaultIcon\(Default)` | `"<app dir>\bark.exe",0` |
| `shell\open\command\(Default)` | `"<app dir>\bark.exe" "%1"` |

Grammar:

```text
barkpc:open                       → focus the history window
barkpc:open?id=<notification_id>  → focus the history window at that entry
barkpc:setup                      → open the setup wizard
```

- Unknown or malformed URIs focus the main window and log a warning; they never raise a dialog.
- Registration happens on first successful setup and is idempotent (re-writes the same values). A "Remove all data" action deletes the `barkpc` key.
- The scheme is used as the toast `launch_action` for messages without an `http(s)` URL, so clicks restore the app even when it is hidden.

## 3. Autostart

`HKCU\Software\Microsoft\Windows\CurrentVersion\Run`

| Value name | Data |
|------------|------|
| `BarkDesktopClient` | `"<absolute path to bark.exe>"` (quoted) |

- Written only when the user enables "Start with Windows"; deleted when disabled or when "Remove all data" runs.
- `settings.autostart_path` stores the path that was written. On startup, if `autostart_enabled` is true and the stored path differs from the current exe path, the UI offers "Repair startup entry" (one confirmation, then rewrite) or "Disable" (FR-037).
- The app never writes a Run entry pointing at a temporary or missing path.

## 4. Credential protection

- Secret file: `data/identity.bin`, content = `CryptProtectData(utf8_json, description="Bark Desktop Client", flags=CRYPTPROTECT_UI_FORBIDDEN)`.
- Scope: current user (no `CRYPTPROTECT_LOCAL_MACHINE`), so copying the portable folder to another account or machine yields undecryptable bytes; the client detects this, treats the identity as `absent`, and asks the user to re-activate.
- Unprotect failure is never fatal and never deletes the file automatically (the user may be mid-migration); the UI offers "Re-activate device" and "Remove all data".

## 5. Toast presentation

- Source name: fixed `Bark Desktop Client`; an AUMID/shortcut may be created per-user if spike T0 shows it is required for display.
- Text fields: `[title (or "Bark"), body]`; when `hide_preview_text` is on: `["Bark Desktop Client", "New notification"]`.
- Image: `ToastImagePosition.Hero` for the message image; `ToastImagePosition.AppLogo` for the Bark/Bark-server logo if no message image exists.
- Click: `launch_action` = message `url` when `http(s)`, else `barkpc:open?id=<notification_id>`.
- Never more than one toast per message; bursts may be coalesced by the OS but history always records every message (FR-018).

## 6. Display and DPI

- Process is per-monitor DPI aware (`SetProcessDpiAwareness(2)` before Tk starts); ttk scaling derived from the primary monitor DPI.
- Tray icon sizes: 16, 20, 24, 32 px variants; the unread badge is a separate overlay drawn with Pillow, never a second icon.
- Window geometry is persisted in settings only as size (not position) to avoid restoring onto a disconnected monitor.
