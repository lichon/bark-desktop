# Bark Desktop Client for Windows

A tray-first Windows app that receives [Bark](https://github.com/Finb/Bark) push
notifications and turns them into native Windows toasts, with a local searchable
history, image previews, and recall (delete) support.

* **No installer, no admin rights.** Unpack a folder, run `bark_pc.exe`.
* **No Apple account is used.** The app performs an anonymous Apple device
  activation and receives pushes the same way the Bark iOS app does.
* **Your pushes keep working unchanged.** Existing senders (the Bark iPhone app,
  `curl`, scripts, shortcuts) keep using the same server and device key.

## Quick start

1. Unpack `BarkDesktopClient.zip` anywhere you can write to (for example
   `%LOCALAPPDATA%\Programs\BarkDesktopClient`).
2. Run `bark_pc.exe`. The first launch opens the setup wizard; later launches
   open the History window. The tray icon appears either way.
3. Choose the Bark server:
   * **Public server** — `https://api.day.app` (default), or
   * **Self-hosted** — for example `http://127.0.0.1:8080`.
4. Optionally tick **Supabase Realtime** and enter the project address
   (`sb://<project-ref>.supabase.co`), the project's anon/publishable key and a
   room name. The app then connects to the Bark server **and** the Realtime
   room at the same time; the room is an extra transport, not a replacement.
5. The wizard activates the device and registers a key with the Bark server. No
   Apple ID, password or 2FA prompt appears at any point.
6. Copy the displayed **push URL** (or scan the QR code) and use it as the push
   target. Keep it private: anyone who has it can send you notifications. If
   Supabase Realtime is enabled, the displayed room name is what senders use
   there.

The wizard can also send a self-test push through the Bark server and reports
two facts separately: whether the server accepted it, and whether it arrived on
this device. The optional Realtime room is not used for the self-test.

## Sending pushes

Text:

```bash
curl -s -X POST http://127.0.0.1:8080/push \
  -H 'Content-Type: application/json' \
  -d '{"device_key":"<key>","title":"Deploy done","body":"build 412 is live","group":"ops"}'
```

Image (the image is fetched and cached locally):

```bash
curl -s -X POST http://127.0.0.1:8080/push -H 'Content-Type: application/json' \
  -d '{"device_key":"<key>","title":"Chart","body":"daily revenue","icon":"https://picsum.photos/600/300"}'
```

Recall a message:

```bash
curl -s -X POST http://127.0.0.1:8080/push -H 'Content-Type: application/json' \
  -d '{"device_key":"<key>","delete":"1","id":"the-message-id"}'
```

With the public server, replace the host with `https://api.day.app`. Multiple
keys can be comma-separated, exactly as with the iOS app.

## Supabase Realtime rooms (optional, alongside a Bark server)

The Bark server is always configured. Supabase Realtime is an optional second
transport: enable it in **Setup** (or *Settings → Supabase Realtime*) and the app
runs both receivers concurrently, so a push arrives whichever side it was sent
to. Enter:

| Field | Value |
|-------|-------|
| Project address | `sb://<project-ref>.supabase.co` (or `sb://https://<ref>.supabase.co`) |
| Supabase auth key | the project's anon or publishable key (`sb_publishable_…`) |
| Room name | the room to join, for example `alerts` |

Turning the option off stops the Realtime connection but leaves the Bark link
running. The project address, key and room are kept so re-enabling is one click.

The client joins the same channel shape as the companion `use-supabase.ts` hook:
`room:<room-name>:messages`. A frame is a `ChannelMessage` (the shape
`newMessage` produces), and the text lives in `content`:

```json
{
  "id": "…",
  "type": "message",
  "content": "build 412 is live",
  "timestamp": "2026-01-01T00:00:00.000Z",
  "sender": "Ana"
}
```

When the client sends one, `content` is the notification body as a plain string:
only the message text goes over the wire, never a push envelope. A string is
always read as that body (it is not decoded as JSON). A sender that has a full
Bark payload can put it in `content` as an object instead, and it is used
verbatim:

```json
{
  "aps": { "alert": { "title": "Deploy done", "body": "build 412 is live" } },
  "group": "ops",
  "id": "build-412"
}
```

When a message's content has no title, the message `sender` is used as the
notification title — so a sender that only sends its text and its name still
shows a sensible title. When it has no `group`, the Realtime room name becomes
the group (toasts and the history's *Group* line), so one room's messages stay
together; an explicit `group` in the content always wins.

The client authenticates the WebSocket with the key as the `apikey` query
parameter, sends the Phoenix heartbeat, and reconnects with the same backoff as
the APNs transport. Loopback projects (`sb://127.0.0.1:54321`) use plain `ws://`;
hosted projects use `wss://`. *Check connection* verifies the project/key through
Supabase's REST endpoint. *Test notification* / the wizard's self-test only
pushes through the Bark server, never into the room, so one test never shows up
twice. The history window's bottom compose bar broadcasts a one-line message
into the room: its *Send* button is enabled only while the Realtime transport is
connected, and Enter sends. The button wears the tray icon's colours: blue (the
icon's online ring) while the room is joined, grey (the icon's offline ring) and
disabled while it is not. The app's own receiver sees that broadcast too, but
recognises it as its own message (the broadcast `sender` is the desktop): it is
stored in the history **already read**, and no toast is shown and no speech is
spoken for it. Messages from every other sender behave as usual.

Nothing Apple-specific happens for the Realtime side: it reuses the device
activation performed for the Bark server, and toggling it never re-registers
that key. Turning it off does not affect Bark delivery.

## Tray and windows

| Action | Where |
|--------|-------|
| Messages | Double-click the tray icon, or tray menu → *Messages* |
| Mark all read | Tray menu → *Mark all read* |
| Send a test notification | Tray menu → *Test notification* |
| Read notifications aloud | Tray menu → *Speak notifications* (toggle) |
| Copy the device key | Tray menu → *Copy device key* |
| Settings | Tray menu → *Settings…* |
| Re-run setup | Tray menu → *Setup…* |
| Quit | Tray menu → *Quit* |

Closing the history window does **not** stop delivery — the app keeps running in
the tray. The tray icon colour shows the connection state: blue = connected,
amber = reconnecting, red = action required, moon = silenced.

Start with Windows is opt-in (Settings → *Start with Windows*). It writes a
per-user `Run` entry; no admin prompt is involved, and it is visible in Task
Manager → Startup. If you move the installation folder, the app offers to repair
the entry on the next launch.

## Spoken notifications

The client can also read new notifications out loud. It is off by default; turn
it on in *Settings → Text-to-speech → Speak notifications aloud*, or toggle
*Speak notifications* in the tray menu.

* **Provider:** the **Windows system voice** (SAPI 5 — the same voices as
  Narrator and *Settings → Time & language → Speech*). `Off (no voice)`
  disables speech. Speech is rendered locally by the Windows voice: no audio
  file, no extra download and no network access are involved.
* **Rate** (`-10…10`) and **volume** (`0…100`) are handed straight to the voice.
  *Test voice* speaks a sample line with the values currently in the dialog.
* **Only the newest message is spoken.** A new notification interrupts the
  current sentence instead of queueing behind it, so a burst never replays as a
  backlog.
* With *Hide preview text* on, the voice says only "New notification";
  silenced notifications are never spoken.
* Bodies are trimmed to 300 characters and markdown/images/URLs are stripped
  first, so a link-heavy push is readable rather than spelled out.

Speech is presentation only: if Windows has no voice installed, the app logs
`text-to-speech unavailable` and notifications carry on as before.

## Where data lives

History, images, logs and settings are kept in the per-user AppData folder:

```
%LOCALAPPDATA%\BarkDesktopClient\
  bark.db  settings.json  identity.bin
  images\  logs\
```

On first launch a legacy `data\` folder next to the executable is moved there
automatically (existing AppData content is never overwritten). For a fully
portable layout set `BARK_PC_HOME` — data then stays next to the exe in `data\`.
Set `BARK_PC_DATA` to use a different data folder.

## Privacy

* The Bark device key (in `bark.db`) and the Supabase auth key (in
  `settings.json`) are credentials: anyone with them can send to your link or
  room. Treat them like the push URL.
* Notification content stays on this machine. The only outbound connections are
  Apple's activation service, Apple's courier, the Bark server you chose, the
  Supabase project and its Realtime WebSocket when the optional room is enabled,
  and the image URLs contained in your own pushes.
* The activation identity
  (`%LOCALAPPDATA%\BarkDesktopClient\identity.bin`) is encrypted with Windows
  DPAPI. It cannot be read by another Windows account, even if the folder is
  copied.
* Logs (`%LOCALAPPDATA%\BarkDesktopClient\logs\app.log`) are rotated at 2 MB × 3
  and redact tokens, certificates and notification bodies.
* Set the log level to `debug` (Settings → Log level) to also record every
  outbound HTTP request and response, including the `httpx`/`httpcore` details
  of libraries that create their own clients.
* *Settings → Remove all data* deletes the identity, history, images, logs,
  settings, the autostart entry and the `barkpc:` registration.

## Troubleshooting

| Symptom | Likely cause | What to do |
|---------|--------------|------------|
| Registration succeeds but pushes never arrive | The server uses a different APNs topic, or the push token is not accepted | Check `push_topic` in Settings against the server's hardcoded topic (`me.fin.bark` by default). Server logs show the APNs reason. With log level `debug`, a delivered push logs `stream: delivering message (N bytes)` followed by `stored notification <id>`; a message that carries neither the base nor the scoped token also logs `stream: message on <topic> carries an unexpected token`. |
| Toast API succeeds but nothing appears | Notifications disabled for the app, Focus Assist on, or an unpackaged-toast quirk | Windows Settings → System → Notifications; try *Test notification*. A refused toast now logs `toast failed: 0x…: <cause>` and is repeated in the tray tooltip and the history toolbar (`toasts blocked`). `0x803E0114` = notifications are off for this user (`HKCU\Software\Microsoft\Windows\CurrentVersion\PushNotifications\ToastEnabled` is `0`); `0x803E0207` = Do Not Disturb dropped it; `0x80070490` = no app registration (AUMID) for the toast source. |
| Every activation fails with an Apple error | Activation endpoint blocked, changed, or rate-limited | Check `%LOCALAPPDATA%\BarkDesktopClient\logs\app.log`; wait a minute before retrying. |
| Tray icon disappears after restarting Explorer | Shell notification-area reset | The app re-adds it within a minute. |
| Images never load | Host blocks non-browser user agents, TLS failure, or a non-image content type | The log records the URL hash and the reason; try a different host. |
| Notifications are not read aloud | Speech is off, Windows has no voice installed, or SAPI is unavailable | Enable *Settings → Text-to-speech → Speak notifications aloud* and press *Test voice*. The log records `text-to-speech unavailable (windows provider): …`; install a voice under *Settings → Time & language → Speech → Manage voices*. |
| App will not start after moving the folder | The autostart entry points at the old path | Launch it manually once and accept the repair prompt. |

Diagnostics for a bug report: *Settings → Export diagnostics* writes a zip with
the redacted logs and a settings snapshot.

## Development

```bash
py -3.11 -m venv .venv
.venv\Scripts\pip install -e ".[dev]"
.venv\Scripts\python -m pytest          # headless unit suite, no network
.venv\Scripts\python -m ruff check bark_pc tests
.venv\Scripts\python -m bark_pc         # run from source
.venv\Scripts\python -m bark_pc.tools.build_portable
```

Layout: `bark_pc/core` (config, models, SQLite repository, logging), `bark_pc/push`
(APNs adapter, supervisor, payload parsing, identity cache), `bark_pc/bark`
(server client and link lifecycle), `bark_pc/images` (fetch + cache), `bark_pc/tts`
(provider-neutral text-to-speech), `bark_pc/ui`
(Tk surfaces), `bark_pc/platform/windows` (tray, toasts, DPAPI, single instance,
autostart, URI scheme, SAPI voice). Only `bark_pc/platform/windows/` imports
Win32/WinRT, and only `bark_pc/push/pypush_client.py` imports `pypush`.

Design documents live in `specs/001-bark-desktop-client/` (`spec.md`, `plan.md`,
`data-model.md`, `contracts/`, `quickstart.md`, `tasks.md`). Validation records
live in `docs/validation/`.

## Changes

We use [Conventional Commits](https://www.conventionalcommits.org/). Versioning
follows semantic versioning with the current minor pinned in `pyproject.toml`.

## Licensing

The application's own code is MIT-licensed. Bundled third-party components keep
their own licenses; see `NOTICE`. Notably, `pypush` is **SSPL-licensed**, which
imposes source-availability obligations on distributed builds — review `NOTICE`
before publishing binaries.
