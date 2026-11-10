# Spike T0 — toast from an unpackaged, portable process

**Question**: does a toast actually appear, with the correct source name and a
hero image, when shown from an unpackaged `--onedir --windowed` build?

**Status: BLOCKED (visual confirmation pending).** The tool and the packaged
build exist; the check needs a person looking at a clean Windows 11 VM and a
Windows 10 21H2 machine.

## Tool

```bash
python -m bark_pc.tools.spike_toast --image path\to\image.png
# from the packaged build, the same module can be run with the venv interpreter
```

It shows a text toast and a hero-image toast, prints what the WinRT API returned,
appends the result to this file, and waits six seconds so the toast can be seen.

Related evidence already gathered:

* `windows-toasts` 1.3.1 imports and `InteractableWindowsToaster` constructs in
  the frozen build (spike T2).
* The packaged app starts, creates its tray icon and setup wizard, and shuts down
  cleanly (see `perf.md`); toast construction is not exercised by that run because
  no push has been received yet.

## Procedure on a clean machine

1. Copy `dist/BarkDesktopClient/` to the machine and run `BarkDesktopClient.exe`.
2. Tray menu → *Test notification* (needs a registered device key), or run the
   spike tool above.
3. Confirm: the toast appears; the source name is **Bark Desktop Client**; the
   hero image is visible; notifications are enabled for the app in Windows
   Settings → Notifications.
4. Repeat on Windows 10 21H2.

## If nothing appears

### 2026-11-10 — first real failure observed (not a visual run)

A live push reached the app and `show_toast()` returned normally, but Windows
reported an asynchronous failure. Recorded from the app log:

```
WARNING bark_pc.toast: toast failed: <ToastFailedEventArgs object at 0x…>
```

Root cause of the unreadable line: `windows_toasts` 1.3.1 forwards the raw WinRT
`ToastFailedEventArgs`, which exposes `error_code` (an `HResult`), not
`error_message`; the old handler fell back to `str(event)`.

The code behind it, captured by calling `WindowsToastNotifier.show()` directly
and reading `event.error_code`:

```
error_code = 0x803E0114 (WPN_E_NOTIFICATION_TYPE_DISABLED)
HKCU\Software\Microsoft\Windows\CurrentVersion\PushNotifications\ToastEnabled = 0
```

So the toast path itself was never exercised on this machine: notifications are
turned off for the user. The app now logs the HRESULT plus its cause, and shows
the reason in the tray tooltip and the history toolbar.

### Procedure when toasts are enabled

1. Create a per-user Start Menu shortcut with an explicit AUMID and set the
   toast `notifierAUMID` to it (per-user, no admin rights; permitted by
   `contracts/local-ipc.md` §5).
2. Re-test; if the API reports success but nothing is shown, check Focus Assist
   and the per-app notification toggle before changing code.

Record the outcome below when the check has actually been performed.
