# Security and privacy pass

**Status: PASS** for the reviewable code paths. Live-service behaviours (Apple
activation, real server traffic) are outside this review.

## Checks performed

| Area | Requirement | Evidence |
|------|-------------|----------|
| Secrets at rest | DPAPI only, per-user, no `LOCAL_MACHINE` | `platform/windows/secrets.py` uses `CryptProtectData`/`CryptUnprotectData` with `CRYPTPROTECT_UI_FORBIDDEN` only; no local-machine scope. Unit test asserts the blob is ciphertext (no `BEGIN CERTIFICATE`/`BEGIN PRIVATE KEY`). |
| Secrets in logs | tokens, certificates and bodies redacted | `core/logging.py` filter masks PEM blocks, 64+ hex runs, base64 blobs, `token=`/`authorization` values, bearer tokens and JSON secret fields; every handler (file and console) carries the filter. `raw_json`/`body`/`title` are excluded from diagnostics snapshots. |
| Secrets in exceptions | `ActivationMaterial.__repr__`/`__str__` redact cert, key and token | `core/models.py`; the token shows only its first 8 characters. |
| Logs on disk | rotated 2 MB × 3 under `data/logs/` | `build_logger` with `RotatingFileHandler`. |
| Outbound network | only chosen server, Apple endpoints, image URLs | `grep -rn "httpx.Client\|webbrowser.open\|os.startfile"`: server client (user URL), image fetcher (push image URLs), pypush (Apple). No analytics, no telemetry. |
| URL opening | `http(s)` only | `app.open_url` refuses anything else; `toast.is_openable_url` only accepts http(s) before using a URL as `launch_action`. |
| URI input | validated, never raises dialogs | `parse_barkpc_uri` returns `valid=False` for unknown schemes/commands/non-numeric ids; the IPC listener rejects malformed JSON and wrong tokens. |
| Process execution | no shells, no eval | `grep -rn "shell=True\|os.system"` → no matches. |
| Image fetching | size/type/redirect caps | 10 MB cap (declared `Content-Length` and streaming), `Content-Type: image/*` required, max 2 redirects, non-http(s) refused; failures become cache states. |
| Untrusted payloads | never fatal, never executed | `parse_payload` returns an outcome and never raises; markdown is not rendered; URL/icon values are only stored. |
| Local IPC | loopback only, single line, token checked, 2 s timeout | `platform/windows/single_instance.py` binds `127.0.0.1`, one request per connection, `secrets.compare_digest` token check, size-capped read. |
| Autostart / URI scheme | HKCU only, no elevation | `autostart.py` writes `HKCU\…\Run`; `uri_scheme.py` writes `HKCU\Software\Classes\barkpc`; both removable via *Remove all data*. |
| Platform isolation | Win32/WinRT only under `platform/windows/` | `grep -rn "winreg\|ctypes.windll\|winrt\|windows_toasts\|pystray" bark_pc --include=*.py` outside `platform/windows/` and `tools/` → no matches. |
| Dependency isolation | only the adapter imports pypush | `grep -rn "pypush" bark_pc --include=*.py` → only `bark_pc/push/pypush_client.py`. |
| Data removal | identity, DB, images, logs, settings, autostart, URI key, instance file | `BarkApp.remove_all_data`, reporting exactly what was removed. |
| Privacy modes | hide preview text / silence while recording | `hide_preview_text` renders a neutral notification; `silence_until` suppresses toasts but always stores the message. |

## Residual risks (documented, not code defects)

* `pypush` is SSPL-licensed and unstable before 3.0.0; it is pinned and isolated,
  and the licensing decision is a release blocker (see `NOTICE`).
* Anonymous activation relies on Apple's device-activation service remaining
  available; retries are rate-limited to one attempt per minute and the identity
  is cached so ordinary restarts do not activate again.
* The Bark server has no unregister endpoint, so abandoning a server cannot delete
  the old key; the UI states this explicitly and never claims revocation.
* The portable folder's database and image cache are not encrypted (they contain
  message content, not credentials); they live in the user's own profile folder.
