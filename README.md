# Bark Desktop Client

A cross-platform, tray-first desktop client that receives
[Bark](https://github.com/Finb/Bark) push notifications and turns them into
native desktop notifications, with a local searchable history, image previews,
and recall (delete) support.

* **No Apple account is used.** The app performs an anonymous Apple device
  activation and receives pushes the same way the Bark iOS app does.
* **Your pushes keep working unchanged.** Existing senders (the Bark iPhone app,
  `curl`, scripts, shortcuts) keep using the same server and device key.
* **Portable by default.** Unpack a folder and run the app; no installer and no
  admin rights are required on Windows.

## Platform support

The receiver, SQLite history, image cache, Supabase Realtime transport, routing
and UI are platform-neutral. Each operating system is served by a backend that
implements the traits in `bark_pc/platform/base.py` — tray, notifications,
secret storage, autostart, single instance, screen-lock state and the system
voice. Only `bark_pc/platform/<os>/` imports OS-specific APIs.

| Platform | Status | Backend |
|----------|--------|---------|
| Windows 10/11 | **Supported** | `bark_pc/platform/windows/` (Win32/WinRT, DPAPI, SAPI 5) |
| macOS | Planned | menu-bar tray, Notification Center, Keychain, system voice |
| Linux | Planned | StatusNotifier tray, libnotify, Secret Service, speech-dispatcher |

The shared layers are OS-agnostic and are tested headlessly on every platform
(`windows_only` tests are skipped elsewhere), but there is no supported way to
run the full app on macOS or Linux yet: the composition root still imports the
Windows backend. Windows is therefore the reference backend and the only
platform with a runnable packaged build today. The packaging script itself is
platform-aware (see [Packaged builds](#packaged-builds)), so each host produces
its own artifact as soon as the matching backend lands.

## Quick start (Windows)

1. Unpack `BarkDesktopClient-windows-x64.zip` anywhere you can write to (for
   example `%LOCALAPPDATA%\Programs\BarkDesktopClient`).
2. Run `bark.exe`. The first launch opens the setup wizard; later launches
   open the History window. The tray icon appears either way.
3. Choose the Bark server:
   * **Public server** — `https://api.day.app` (default), or
   * **Self-hosted** — for example `http://127.0.0.1:8080`.
4. Optionally tick **Supabase Realtime** and paste the project URL
   (`https://<project-ref>.supabase.co`), the project's anon/publishable key and
   a room name. The app then connects to the Bark server **and** the Realtime
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

On macOS and Linux the setup flow will be the same once a platform backend
lands. Today those platforms are supported for development and tests only: the
full app has no backend to load there.

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
| Project URL | `https://<project-ref>.supabase.co` (a bare `<ref>.supabase.co` works too) |
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
the APNs transport. Loopback projects (`http://127.0.0.1:54321`) use plain `ws://`;
hosted projects use `wss://`. *Check connection* verifies the project/key through
Supabase's REST endpoint. *Test notification* / the wizard's self-test only
pushes through the Bark server, never into the room, so one test never shows up
twice. The history window's bottom compose bar sends a one-line message this
machine writes; *Settings → Message route* chooses the route (the Realtime room,
a Bark URL, or both, see below). Its *Send* button is enabled while at least one
selected route is usable — or while this device still has its own device key to
fall back to (see *Routing self messages* below) — and Enter sends. The button
wears the tray icon's colours: blue (the icon's online ring) while a message can
be sent, grey (the icon's offline ring) and disabled while it cannot. When the
Realtime route is used, the app's own frame goes out with this machine's host
name as `sender`
(the name a room's other participants see), and the id of every message it
publishes is remembered for a few minutes. The app's own receiver sees that
broadcast too, but recognises it by that id — not by the `sender` label, which
anyone can reuse — so it is stored in the history **already read**, and no toast
is shown and no speech is spoken for it. Messages from every other sender behave
as usual.

Nothing Apple-specific happens for the Realtime side: it reuses the device
activation performed for the Bark server, and toggling it never re-registers
that key. Turning it off does not affect Bark delivery.

## Routing self messages

The compose bar at the bottom of the history window sends messages this machine
writes. *Settings → Message route → Send compose-bar messages to* chooses where
they go:

| Route | Needs | What happens |
|-------|-------|--------------|
| Supabase Realtime room (default) | Realtime enabled and joined | Broadcasts the line into the room, as described above. |
| Bark URL | a Bark push URL | POSTs the line to that URL as a normal Bark push. |
| Both | either one | Uses every route that is currently available. |
| *nothing usable* | a linked device key | Pushes the line to this device's own device key, so the compose bar works before any route is configured. |

The selected route is always tried first. When it cannot deliver — the default
Realtime room is off and no Bark URL was entered, or the chosen route is
temporarily unavailable — the message is pushed straight to this device through
the device key registered during setup, exactly like a normal Bark push to
`https://api.day.app/<device_key>`. That makes "send to self" the out-of-the-box
behaviour: leaving *Bark URL* empty is valid and simply selects this fallback.
Only a machine with neither a usable route nor a device key (an install that
never linked a server, or one linked through Supabase only) keeps *Send*
disabled.

The Bark URL is the full push URL of the target device, device key included —
for example `https://api.day.app/<device_key>` or a self-hosted equivalent such
as `http://127.0.0.1:8080/<device_key>`. Query parameters are kept, so
`?group=ops&level=timeSensitive` works like any other Bark URL. The client POSTs
`{title, body, group, id}` as JSON to exactly that URL: the title is this
machine's host name, the group is `bark-desktop`, and the body is the composed
line. No Supabase project is needed for this route, so the *Send* button works
while the URL is configured even if the Realtime room is off or disconnected.

*Both* is useful when the room's other participants should see the message and
the phone should receive it as a Bark push. If one route fails, the other is
still used, and the tray reports which route failed.

## Routing incoming messages to webhooks

The client can also forward incoming messages to any HTTP webhook. Open
*Routing…* in the Messages window (next to *Settings*) or tray menu → *Message
routing…*, add a rule and press *Save*. Every rule has two optional match
conditions, a URL and an optional request body:

| Setting | Meaning | Empty means |
|---------|---------|-------------|
| Message group | The push's `group` field, or the Supabase room name (case-insensitive) | any message |
| Title contains | A case-insensitive substring of the title or subtitle | any message |
| Webhook URL | `http://` or `https://`; may contain `{placeholders}`; custom headers are supported | — |
| Method | `GET` (default), `POST`, `PUT` or `PATCH`; GET sends no body, the others send the body below | `GET` |
| Body format | `JSON` (default) or `Form (x-www-form-urlencoded)`; sets the `Content-Type` header unless one is configured | `JSON` |
| Body | A template with `{placeholders}`; unknown placeholders and JSON that is invalid after filling are rejected on save | no body |

Both conditions must match when both are set. A rule with no conditions is a
catch-all. Several rules can match the same message; each one receives the
notification. Matching happens for messages received from the network; the
compose bar's own messages and delete/recall pushes are never forwarded.

Senders can add a group without changing the rest of their push:

```bash
curl -s -X POST http://127.0.0.1:8080/push -H 'Content-Type: application/json' \
  -d '{"device_key":"<key>","group":"ops","title":"Deploy done","body":"build 412"}'
```

A rule with message group `ops` then forwards that message. Bark's `group`
field (and, for Supabase rooms, the room name that becomes the group) is the
only category used for matching, so existing senders need no changes.

The client never appends fields or query parameters to the webhook URL. That
URL is used exactly as configured, except for explicit placeholders:

| Placeholder | Value |
|-------------|-------|
| `{id}`, `{remote_id}` | Local and sender-provided message ids |
| `{group}` | The message group / Realtime room |
| `{title}`, `{subtitle}`, `{display_title}` | Message title fields |
| `{body}` | Message body |
| `{url}`, `{image_url}` | The push URL and image URL |
| `{source}` | The receiving server or room |
| `{received_at}`, `{received_at_iso}` | Receipt time (epoch milliseconds / ISO 8601) |

Placeholder values are URL-encoded, so they cannot change the URL structure.
Unknown placeholders are rejected when the rule is saved. For example:

```
https://example.com/hook/{group}?title={title}&body={body}
```

`GET` (the default) sends a request to that URL with no body. `POST`, `PUT` and
`PATCH` send the rule's *Body*, which is a template in the selected format.
Message text is **not** included automatically: the body is exactly what you
write, so only the placeholders you use are sent. The same placeholder names
are available in the body, plus `{payload}` for the sender's raw custom JSON.
An empty body sends no body at all.

A JSON body must stay valid JSON once the placeholders are filled, and
placeholder values are escaped for you:

```json
{
  "text": "{display_title}: {body}",
  "group": "{group}",
  "received_at": {received_at},
  "payload": {payload},
  "link": "{url}"
}
```

Quoted placeholders (`"{body}"`, as above) become JSON strings; `{received_at}`
may be used unquoted as a number and `{payload}` must be unquoted so the raw
payload object is inserted. The `Content-Type: application/json` header is added
unless the rule configures its own. A missing value becomes an empty string
(or `null` when used unquoted).

A form body is sent verbatim except that placeholder values are
`application/x-www-form-urlencoded`-encoded:

```
text={display_title}&descr={body}&ts={received_at}
```

The `Content-Type: application/x-www-form-urlencoded` header is added unless the
rule configures its own, and a missing value becomes an empty field. Bodies
written by older versions (which used a *Message content* option) are converted
to the equivalent JSON template when `routes.json` is loaded.

Webhook calls run on a background worker with a 10-second timeout, so a slow
endpoint never delays the toast or the history. A failing webhook is logged and
dropped; local delivery is unaffected. Rules live in `routes.json` in the data
folder and take effect as soon as *Save* is pressed.

## Tray and windows

| Action | Where |
|--------|-------|
| Messages | Double-click the tray icon, or tray menu → *Messages* |
| Mark all read | Tray menu → *Mark all read* |
| Send a test notification | Tray menu → *Test notification* |
| Read notifications aloud | Tray menu → *Speak notifications* (toggle) |
| Copy the device key | Tray menu → *Copy device key* |
| Settings | Tray menu → *Settings…* |
| Message routing | Messages window → *Routing…*, or tray menu → *Message routing…* |
| Re-run setup | Tray menu → *Setup…* |
| Quit | Tray menu → *Quit* |

Closing the history window does **not** stop delivery — the app keeps running in
the tray. The tray icon colour shows the connection state: blue = connected,
amber = reconnecting, red = action required, moon = silenced.

Start at login is opt-in (on Windows, Settings → *Start with Windows*). It writes
a per-user `Run` entry; no admin prompt is involved, and it is visible in Task
Manager → Startup. Other backends will expose the same option and map it to a
`LaunchAgent` on macOS and an XDG autostart entry on Linux. If you move the
installation folder, the app offers to repair the entry on the next launch.

The Tk windows are shared by all platforms. Where the platform differs is the
shell integration: tray icon, notifications, URI registration and login start.

## Spoken notifications

The client can also read new notifications out loud. It is off by default; turn
it on in *Settings → Text-to-speech → Speak notifications aloud*, or toggle
*Speak notifications* in the tray menu.

* **Provider:** on Windows, the **Windows system voice** (SAPI 5 — the same
  voices as Narrator and *Settings → Time & language → Speech*). `Off (no voice)`
  disables speech. Speech is rendered locally: no audio file, no extra download
  and no network access are involved. The speech layer is provider-neutral; the
  macOS and Linux backends will plug their system voice into the same
  `TtsProvider` interface.
* **Rate** (`-10…10`) and **volume** (`0…100`) are handed straight to the voice.
  *Test voice* speaks a sample line with the values currently in the dialog.
* **Only the newest message is spoken.** A new notification interrupts the
  current sentence instead of queueing behind it, so a burst never replays as a
  backlog.
* With *Hide preview text* on, the voice says only "New notification";
  silenced notifications are never spoken.
* **Nothing is spoken while the screen is locked.** A locked workstation means
  the user is away, so a read-out would only be overheard. On Windows the check
  is the session flags (`WTSSessionInfoEx`) with `OpenInputDesktop` as a
  fallback; other backends will use their own screen-lock APIs. Untick
  *Settings → Text-to-speech → Only read aloud while the screen is unlocked* to
  be read to while locked. Speech already in progress when the screen locks
  finishes its sentence.
* Bodies are trimmed to 300 characters and markdown/images/URLs are stripped
  first, so a link-heavy push is readable rather than spelled out.

Speech is presentation only: if the system has no voice installed, the app logs
`text-to-speech unavailable` and notifications carry on as before.

## Bluetooth audio receiver (A2DP sink)

Windows 10 (version 2004, build 19041) and later can act as a Bluetooth
speaker, but Windows does not let the user start that mode on its own. The
client adds the missing switch in *Settings → Bluetooth*: tick **Use this PC as
a Bluetooth speaker (A2DP sink)**, choose the phone, and press **Connect** (or
just save). Audio from the phone then plays on whatever output device Windows
currently uses — headphones plugged into the PC included.

This uses the same Windows API as the open-source
[AudioPlaybackConnector](https://github.com/ysc3839/AudioPlaybackConnector)
(`Windows.Media.Audio.AudioPlaybackConnection`). The Windows Bluetooth stack
does the A2DP work; the client only enables and opens the connection, so there
is no codec, audio decoder, virtual audio device or separate Bluetooth adapter
involved.

1. Pair the phone once in **Windows Settings → Bluetooth & devices** (or use
   the *Windows Bluetooth settings* button in the dialog).
2. Tick the option, pick the device, and save. *Refresh* re-reads the list if
   the phone was paired while the dialog was open.
3. The status line turns to **Receiving audio** while the phone streams.
   **Disconnect** releases the connection so the phone can use its own speaker
   again; the tray icon and push receiving keep working either way.

Requirements and caveats:

* Windows 10 2004 or newer, and a Bluetooth adapter/driver that supports A2DP
  sink. Windows' own driver normally does; very old adapters may not expose the
  interface, in which case no device appears in the list.
* The PC becomes an audio *receiver*: volume and play/pause stay under the
  phone's control. The Windows output volume still applies.
* Android phones are the usual source; any device that can stream A2DP works.
  iOS does not offer a way to send system audio to an A2DP sink.
* The saved device is reconnected at the next launch while the option is on.
  If Windows has since unpaired the phone, the status line reports the error and
  *Refresh* shows the current device list.

## iPhone notifications (ANCS over USB Bluetooth)

iOS only shares its Notification Center through Apple's Notification Center
Service (ANCS). The client consumes it by driving a **separate USB Bluetooth
adapter** with [Bumble](https://github.com/google/bumble), so new alerts arrive
in the same history/toast/speech/webhook path as Bark pushes.

This is independent of the A2DP sink above (same *Settings → Bluetooth* tab):
both can be on at the same time. The Windows Bluetooth stack keeps handling
audio while Bumble owns the USB dongle.

Requirements:

* A USB Bluetooth LE adapter (Bluetooth 4.0 or newer). While the option is on,
  Bumble uses that adapter exclusively. A CSR8510-class dongle (the common
  “USB Bluetooth 4.0 adapter”) needs no firmware upload and is the most
  predictable choice; some Intel/Realtek radios need firmware files Bumble has
  to find on disk.
* On Windows, bind the adapter to the WinUSB driver with
  [Zadig](https://zadig.akeo.ie/) (*Options → List All Devices*, pick the dongle,
  choose **WinUSB**, then *Replace Driver*). The normal Windows Bluetooth driver
  cannot be shared with Bumble, so use a second dongle (or keep the built-in
  radio for Windows audio and pairing).
* An iPhone with Bluetooth on and notification previews allowed.

Steps:

1. Open *Settings → Bluetooth → iPhone notifications*, press **Refresh**,
   choose the USB adapter, tick the option, and save.
2. On the iPhone open **Settings → Bluetooth**: the PC appears as
   **Bark Desktop** under *Other Devices* / *Accessories*. Tap it there so the
   iPhone connects, then allow pairing and notifications.
3. The status line changes to **Receiving notifications from …**. New alerts are
   stored with the source `ancs`; removing one on the iPhone leaves the history
   row alone.
4. The iPhone connects on its own from then on: it is bonded, the receiver
   keeps advertising, and the iPhone reconnects when it needs the accessory.
   **Restart receiver** re-runs advertising if the link is ever lost.

The accessory also publishes **HID over GATT** (service
`0x1812`) and advertises it, so iOS sees a keyboard it knows how to list. ANCS
alone is often enough for the notification handshake but iOS may never show the
device in Settings until it has a profile like HID to pair with — this is the
well-tested trick from the DIY-watch community. The keyboard is not used to type
anything unless asked to; the service only exists to make the accessory
visible and to bring up a bonded, encrypted link.

### Command-line probe

`python -m bark_pc.tools.ancs_connect` walks the same steps outside the GUI
(and can also scan in the opposite direction): it lists the USB adapters (or
takes `--adapter usb:VVVV:PPPP`), scans, and prints **every** Bluetooth device
it hears with its name, address and signal strength — Apple or not, so an unnamed or non-Apple device is never hidden. It
then asks which one to use, pairs, subscribes and prints every notification it
receives. Selecting a non-iPhone is allowed, but the ANCS subscription step will
explain that the device does not expose Apple's notification service.

| Option | Effect |
|--------|--------|
| `--list-adapters` | print the Bumble selectors and exit |
| `--dump-advertising` | print the advertising PDU and scan response, element by element, and verify the 0x15 ANCS solicitation and the HID service (no dongle needed) |
| `--no-hid` | drop the HID over GATT keyboard (`0x1812`) and its advertising entry; HID is on by default because iOS only reliably lists profiles it knows |
| `--appearance 0x03C1` | advertised appearance; default `0x03C1` (Keyboard) with HID, `0x00C0` (*Generic Watch*) without, `0x0007` is *Eye-glasses* (see note below) |
| `--mitm` | request passkey entry instead of Just Works; the passkey is printed and typed on the iPhone |
| `--no-bonding` | do not store pairing keys (the iPhone must pair on every connect) |
| `--peer AA:BB:…` | skip the picker and connect to that address |
| `--scan-seconds` / `--scan-rounds` | size of each scan window and how many to try |
| `--seconds 60` | stop listening after a time limit instead of Ctrl+C |
| `--json` | one JSON object per notification (for scripts) |
| `--non-interactive` | never prompt; take the best-ranked adapter and iPhone |

The probe shares `<data>\bluetooth\ancs-keys.json`, so a bond made with it is
reused by the app (and vice versa). Stop the app's receiver first: a dongle can
only be opened by one process at a time.

Four things an iPhone checks before it offers the notification service, all
visible from the probe:

1. **A profile iOS lists (HID).** The advertising PDU must carry AD type `0x03`
   with service `0x1812` (*HID over GATT*) and the PC must serve that service.
   This is the community-verified way to make iOS show the accessory at all;
   without it the iPhone tends to ignore the device until a bond exists.
2. **The 0x15 element.** The advertising PDU must carry AD type `0x15`
   (128-bit *service solicitation*) with Apple's ANCS UUID
   `7905F431-B5CE-4E99-A40F-4B1E122D00D0`. A `0x06`/`0x07` UUID list means the
   opposite relationship (a service this device offers) and is not enough.
   `--dump-advertising` prints both checks.
3. **Appearance.** Cosmetic only: it selects the icon iOS shows.
   `--appearance HEX` overrides the default. Note that the Bluetooth Assigned
   Numbers assign *Generic Watch* to `0x00C0` (category `0x03` Watch); `0x0007`
   is *Eye-glasses*, so an accessory advertising `0x0007` is still a valid ANCS
   consumer but is labelled as glasses. With HID the default is `0x03C1`
   (Keyboard).
4. **Security.** ANCS needs an encrypted, bonded link. The default is Just Works
   + bonding, which is all a display-less accessory can complete; `--mitm` asks
   for passkey entry instead, and the negotiated `encrypted`/`authenticated`/
   `key` values are printed after subscribing.
5. **Identity address.** The accessory advertises a fixed static random address
   (`F0:F1:F2:F3:F4:F5`) and distributes *that same address* as its SMP identity
   address (`identity_address_type=RANDOM`), with a local IRK persisted in
   `bluetooth/ancs-irk.bin`. iOS keys the Settings entry and the LTK on the
   identity address the pairing reported, so this is what lets a bonded iPhone
   reconnect after the receiver restarts; a mismatch leaves the entry only
   usable after *Forget This Device*.

Notes and caveats:

* The PC is the BLE peripheral: it advertises the ANCS solicitation and the
  iPhone connects. On that same link the PC is the ANCS client (GATT client) and
  the iPhone the ANCS server, which is the direction a smartwatch uses. iOS
  initiates afterwards, so the accessory never has to scan or dial.
* Only Notification Source and Data Source are consumed. Positive/negative
  actions (accepting a call, replying) are not performed.
* Notifications that already existed when the connection was established are
  ignored, so reconnecting does not duplicate history.
* Pairing keys are cached in `<data>\bluetooth\ancs-keys.json`, so the iPhone
  normally asks only once. Deleting that file (or using *Remove all data*)
  makes it ask again.
* Upgrading from a build that predates the identity-address fix needs one
  **Forget This Device** on the iPhone: those bonds were recorded against the
  dongle's public BD_ADDR instead of `F0:F1:F2:F3:F4:F5`, so iOS could never
  find the accessory again. After that single re-pair, reconnects reuse the
  bond.
* The adapter is opened through libusb, so Windows does not show it as a
  Bluetooth radio while the receiver is running. Stop the option (or the app)
  before using that dongle for anything else.
* ANCS needs an iPhone; Android has no equivalent service. Android notifications
  continue to arrive through the normal Bark push path.

## Where data lives

History, images, logs and settings are kept in the per-user application-data
folder:

| Platform | Folder |
|----------|--------|
| Windows | `%LOCALAPPDATA%\BarkDesktopClient\` |
| macOS | `~/Library/Application Support/BarkDesktopClient/` |
| Linux | `$XDG_DATA_HOME/BarkDesktopClient/` (default `~/.local/share/BarkDesktopClient/`) |

The layout inside the folder is the same everywhere:

```
bark.db  settings.json  routes.json  identity.bin
images/  logs/
```

On first launch a legacy `data\` folder next to the executable is moved there
automatically (existing application-data content is never overwritten). For a
fully portable layout set `BARK_PC_HOME` — data then stays next to the exe in
`data\`. Set `BARK_PC_DATA` to use a different data folder. In a macOS `.app`
bundle the executable lives inside `Contents/MacOS`, so set `BARK_PC_HOME` to a
folder next to the `.app` when you want data to travel with the bundle.

## Privacy

* The Bark device key (in `bark.db`) and the Supabase auth key (in
  `settings.json`) are credentials: anyone with them can send to your link or
  room. Treat them like the push URL.
* Notification content stays on this machine. The only outbound connections are
  Apple's activation service, Apple's courier, the Bark server you chose, the
  Supabase project and its Realtime WebSocket when the optional room is enabled,
  the Bark URL configured for self messages, the webhook URLs you route
  messages to, and the image URLs contained in your own pushes.
* Webhook rules (`routes.json`) may contain an `Authorization` header or a
  token in the webhook URL. Treat that file as a credential too.
* The activation identity (`identity.bin`) is encrypted with the platform's
  per-user secret store. On Windows that is DPAPI, scoped to the current
  account: it cannot be read by another Windows account, even if the folder is
  copied. The `SecretStore` interface is what the macOS (Keychain) and Linux
  (Secret Service) backends implement.
* Logs (`logs/app.log`) are rotated at 2 MB × 3 and redact tokens, certificates
  and notification bodies.
* Set the log level in *Settings → Maintenance → Log level* (`debug`, `info`,
  `warning` or `error`). `debug` also records every outbound HTTP request and
  response, including the `httpx`/`httpcore` details of libraries that create
  their own clients.
* *Settings → Remove all data* deletes the identity, history, images, logs,
  settings, webhook rules, the autostart entry and the `barkpc:` registration.

## Troubleshooting

The rows below describe the Windows backend; the other platforms will get
equivalent checks as their backends land.

| Symptom | Likely cause | What to do |
|---------|--------------|------------|
| Registration succeeds but pushes never arrive | The server uses a different APNs topic, or the push token is not accepted | Check `push_topic` in Settings against the server's hardcoded topic (`me.fin.bark` by default). Server logs show the APNs reason. With log level `debug`, a delivered push logs `stream: delivering message (N bytes)` followed by `stored notification <id>`; a message that carries neither the base nor the scoped token also logs `stream: message on <topic> carries an unexpected token`. |
| Toast API succeeds but nothing appears | Notifications disabled for the app, Focus Assist on, or an unpackaged-toast quirk | Windows Settings → System → Notifications; try *Test notification*. A refused toast now logs `toast failed: 0x…: <cause>` and is repeated in the tray tooltip and the history toolbar (`toasts blocked`). `0x803E0114` = notifications are off for this user (`HKCU\Software\Microsoft\Windows\CurrentVersion\PushNotifications\ToastEnabled` is `0`); `0x803E0207` = Do Not Disturb dropped it; `0x80070490` = no app registration (AUMID) for the toast source. |
| Every activation fails with an Apple error | Activation endpoint blocked, changed, or rate-limited | Check the log; wait a minute before retrying. |
| Tray icon disappears after restarting Explorer | Shell notification-area reset | The app re-adds it within a minute. |
| Images never load | Host blocks non-browser user agents, TLS failure, or a non-image content type | The log records the URL hash and the reason; try a different host. |
| Notifications are not read aloud | Speech is off, the screen is locked, the system has no voice installed, or the speech API is unavailable | Enable *Settings → Text-to-speech → Speak notifications aloud* and press *Test voice*. While the screen is locked the app stays silent by design (see *Only read aloud while the screen is unlocked*). The log records `text-to-speech unavailable (… provider): …`; install a voice in the OS speech settings. |
| App will not start after moving the folder | The autostart entry points at the old path | Launch it manually once and accept the repair prompt. |

Diagnostics for a bug report: *Settings → Export diagnostics* writes a zip with
the redacted logs and a settings snapshot.

## Development

The project is a plain Python package and runs on any OS for tests and linting.

Windows:

```bat
py -3.11 -m venv .venv
.venv\Scripts\pip install -e ".[dev]"
.venv\Scripts\python -m pytest
.venv\Scripts\python -m ruff check bark_pc tests
.venv\Scripts\python -m bark_pc
.venv\Scripts\python -m bark_pc.tools.build_portable
```

macOS / Linux:

```bash
python3.11 -m venv .venv
.venv/bin/pip install -e ".[dev]"
.venv/bin/python -m pytest          # headless unit suite, no network
.venv/bin/python -m ruff check bark_pc tests
.venv/bin/python -m bark_pc         # Windows backend required; not usable yet
```

The test suite is headless and platform-tagged (`windows_only`), so the shared
layers can be exercised on macOS and Linux while the platform backends are
built.

## Packaged builds

The portable build script selects the platform, tray backend, icon format and
archive type for the host, so the command is the same everywhere:

```bash
python -m bark_pc.tools.build_portable          # build + archive
python -m bark_pc.tools.build_portable --no-zip # folder only, no archive
python -m bark_pc.tools.build_portable --target windows --archive zip
```

| Host | Bundle in `dist/` | Archive |
|------|-------------------|---------|
| Windows | `BarkDesktopClient/` (contains `bark.exe`) | `BarkDesktopClient-windows-<arch>.zip` |
| macOS | `BarkDesktopClient.app` | `BarkDesktopClient-macos-<arch>.tar.gz` |
| Linux | `BarkDesktopClient/` (contains `bark`) | `BarkDesktopClient-linux-<arch>.tar.gz` |

`<arch>` is `x64`, `arm64` or `x86`. POSIX archives are tar so executable bits
and symlinks survive; Windows uses zip so Explorer can unpack it. `--archive`
accepts `auto`, `zip`, `tar.gz` or `none`. PyInstaller cannot cross-compile, so
`--target` must match the host; it exists for CI to assert what it is building.
Every bundle contains `NOTICE`, `README.md` and `docs/quickstart.md` (inside
`Contents/Resources/` for the macOS `.app`).

On macOS and Linux the bundle currently contains the shared layers plus the
platform-neutral pystray/Tk code, and the build prints a warning that the native
notification/secret/autostart backend is still missing. Only the Windows build
starts end to end today.

Layout: `bark_pc/core` (config, models, SQLite repository, logging), `bark_pc/push`
(APNs adapter, supervisor, payload parsing, identity cache), `bark_pc/bark`
(server client and link lifecycle), `bark_pc/images` (fetch + cache), `bark_pc/tts`
(provider-neutral text-to-speech), `bark_pc/ui` (Tk surfaces), and
`bark_pc/platform` (the platform contracts plus one backend per OS). Only
`bark_pc/platform/<os>/` may import that OS's APIs, and only
`bark_pc/push/pypush_client.py` imports `pypush`.

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
