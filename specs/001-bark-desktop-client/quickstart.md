# Quickstart & Validation Guide

Purpose: prove end-to-end behavior of the Bark desktop client on Windows without reading the implementation. Every step has an observable outcome; the validation matrix at the end maps steps to spec success criteria.

## Prerequisites

- Windows 10 21H2+ or Windows 11 (x64), signed-in user with a writable folder (no admin rights needed anywhere).
- Python 3.11 for development runs; the portable build needs nothing installed.
- A Bark-compatible server you control one way or another:
  - public `https://api.day.app` (simplest), or
  - local `bark-server` for deterministic tests:

```bash
docker run -d --name bark -p 8080:8080 -v "$PWD/bark-data:/data" ghcr.io/finb/bark-server
curl -s http://127.0.0.1:8080/ping
```

- Outbound HTTPS to `albert.apple.com`, `*.courier.push.apple.com`, and the chosen server.

## 1. Run from source

```bash
git clone <repo> && cd bark-pc
py -3.11 -m venv .venv
.venv\Scripts\pip install -e ".[dev]"
.venv\Scripts\python -m bark_pc
```

Expected: a setup wizard opens; the tray icon appears; no console window is required (when launched with `pythonw`).

## 2. First-run setup

1. Choose the server (public or `http://127.0.0.1:8080`) → **Continue**.
2. The wizard performs activation and registration, showing progress states.
3. On success it displays the device key, the server address, the push URL, and a QR code, each copyable.

Expected:
- No Apple ID, password, or 2FA prompt appears at any point.
- Closing the wizard leaves the app in the tray; the tooltip shows the linked server and "Connected".
- Re-running setup does not create a second key unless the user explicitly chooses "Replace identity".

## 3. Send a text push

```bash
curl -s -X POST http://127.0.0.1:8080/push \
  -H 'Content-Type: application/json' \
  -d '{"device_key":"<key>","title":"Deploy done","body":"build 412 is live","group":"ops"}'
```

Expected: a Windows toast appears within 10 s with title `Deploy done` and body `build 412 is live`; the tray unread badge increments; the entry is listed newest-first when the history window is opened; the server shows `code: 200`.

## 4. Send an image push

```bash
curl -s -X POST http://127.0.0.1:8080/push \
  -H 'Content-Type: application/json' \
  -d '{"device_key":"<key>","title":"Chart","body":"daily revenue","icon":"https://picsum.photos/600/300"}'
```

Expected: toast shows a hero image; the history row shows a thumbnail; opening the entry and choosing "Open image" shows the full-size image with save/copy actions. Repeat the same push: no second download is logged and the thumbnail renders immediately.

Unreachable image check:

```bash
curl -s -X POST http://127.0.0.1:8080/push -H 'Content-Type: application/json' \
  -d '{"device_key":"<key>","title":"Broken image","body":"still delivered","icon":"https://127.0.0.1:9/nope.png"}'
```

Expected: text is delivered normally, the entry shows an "image unavailable" placeholder, no error dialog, and no stall in receiving subsequent messages.

## 5. Send a delete/recall push

```bash
curl -s -X POST http://127.0.0.1:8080/push -H 'Content-Type: application/json' \
  -d '{"device_key":"<key>","title":"Temporary","body":"will be recalled","id":"test-recall-1"}'
# then
curl -s -X POST http://127.0.0.1:8080/push -H 'Content-Type: application/json' \
  -d '{"device_key":"<key>","delete":"1","id":"test-recall-1"}'
```

Expected: the first entry appears, then disappears from history and from the notification center.

## 6. Tray and single-instance behavior

- Close the window → process stays alive in the tray, and step 3 still delivers.
- Double-click the tray icon → history window restored and focused.
- Launch the exe again → no second process; the existing window focuses.
- Tray menu → "Test notification" → the client sends a push to itself and reports both "server accepted" and "received on device" separately (with a distinct message if only the first is true).

## 7. Autostart and portable behavior

- Tray → Settings → enable "Start with Windows" → sign out and back in → the app is running in the tray with no elevated prompt.
- Move the portable folder, then launch from the new location → the client detects the stale autostart entry and offers repair/disable.
- Delete the folder after quitting → nothing remains except the optional autostart entry and `barkpc:` registration, both removable from Settings → "Remove all data".

## 8. Idle footprint check

```powershell
$p = Get-Process bark_pc    # or python (dev runs)
Start-Sleep 3600
Get-Process bark_pc | Select-Object WorkingSet64,PrivateMemorySize64,CPU
```

Expected: working set under 150 MB, average CPU under 1% over the hour, unread badge still accurate.

## 9. Portable build check

```bash
python -m bark_pc.tools.build_portable
```

Expected on Windows: `dist/BarkDesktopClient/` contains `bark.exe` and
`_internal/`, and the script writes `dist/BarkDesktopClient-windows-<arch>.zip`;
zipping and unpacking to a clean folder on a machine without Python still
starts, activates, registers, and receives a push. Verify license notices are
present for pypush (SSPL), pystray (LGPL-3.0), Pillow, and windows-toasts.

On macOS the same command produces `dist/BarkDesktopClient.app` and a `.tar.gz`;
on Linux it produces the onedir folder and a `.tar.gz`. Those archives are only
smoke checks until the platform backend is implemented and will not start yet.

## 10. Failure-path checks

| Action | Expected |
|--------|----------|
| Disconnect network for 60 s, then reconnect | Tray shows degraded, then returns to connected within 60 s without user action; no duplicates |
| Stop the Bark server, then use "Test notification" | "Cannot reach …" with the host named; link stays `linked`; retry works after restarting the server |
| Set an invalid server URL | Registration fails with the transport/server message; the previous working link remains active |
| Point at a server with registration closed (`CLOSE_REGISTER=true`) | "The server refused the request (403)…" and a link to change servers |
| Corrupt/remove `data/identity.bin` while keeping the server link | App reports "Device identity missing", offers "Re-activate device"; history and settings are intact |
| Send 30 pushes in 5 s | All 30 are in history, toasts may be coalesced, unread count is 30, app stays responsive |
| Put the machine to sleep 5 min, wake, push | Message arrives after resume; the UI shows "last connected" time; nothing is backfilled |

## Validation matrix

| Spec criterion | How to validate |
|----------------|-----------------|
| SC-001 setup < 5 min | Time steps 1–2 including the first delivered test push |
| SC-002 95% within 10 s | Repeat step 3 twenty times, record arrival deltas |
| SC-003 7 days continuous | Soak run with hourly pushes; check logs for restarts |
| SC-004 window close never stops delivery | Step 6 first bullet |
| SC-005 image < 3 s, text never blocked | Step 4, both cases |
| SC-006 idle CPU/memory | Step 8 |
| SC-007 reconnect within 60 s | Step 10 row 1 |
| SC-008 history intact across restart | Push several, restart, compare list order and content |
| SC-009 tray actions ≤ 2 interactions | Step 6 menu walk-through |
| SC-010 < 1 in 20 require intervention | Aggregate of the failure-path table |
| SC-011 zero-install, delete-one-folder removal | Steps 7 and 9 |
| SC-012 moved folder repaired | Step 7 second bullet |

## Pre-implementation spikes (must pass before UI work)

1. **T0 – toast from unpackaged exe**: build a minimal `--onedir --windowed` exe that shows a toast with an image on a clean Windows 11 VM and on Windows 10 21H2. If nothing appears, add the per-user Start Menu shortcut/AUMID step described in `research.md` §R3.
2. **T1 – token accepted by APNs via Bark**: register the base courier token, `POST /push`, and confirm arrival on the stream within 30 s. This de-risks the entire product (see `research.md` §R6).
3. **T2 – frozen-import smoke test**: the built exe imports `pypush`, `winrt.*`, `pystray`, and Tk; add hidden imports as needed.

## Troubleshooting

| Symptom | Likely cause | Check |
|---------|--------------|-------|
| Registration succeeds, pushes never arrive | Base token not accepted for provider pushes (T1) or topic mismatch | Server logs the APNs reason (`BadDeviceToken`, `DeviceTokenNotForTopic`); compare `push_topic` with the server's hardcoded topic |
| Toast API returns success but nothing shows | Missing AUMID/shortcut, notifications disabled for the user, Focus Assist | Windows Settings → Notifications; run spike T0 |
| Every activation fails with an Apple error | Activation endpoint blocked/changed or request rate-limited | `data/logs/app.log` for the `<b>…</b>` error text; wait before retrying |
| Tray icon disappears after Explorer restarts | Shell notification area reset | Confirm the tray recovery task logs "re-added" |
| Images never load | Host blocks non-browser UA, TLS failure, or `Content-Type` not `image/*` | Log entry includes the URL hash and reason; use a different host |
| App will not start after moving the folder | Stale autostart entry points at the old path | Launch manually once; accept the repair prompt |
