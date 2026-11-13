# Bluetooth validation

Covers `bark_pc/bluetooth`, the *Settings → Bluetooth* tab and both Bluetooth
features:

* the **A2DP sink**, which uses the Windows Bluetooth stack through
  `Windows.Media.Audio.AudioPlaybackConnection` (the same API as the
  [AudioPlaybackConnector](https://github.com/ysc3839/AudioPlaybackConnector));
* the **ANCS receiver**, which uses Bumble + libusb to drive a dedicated USB
  dongle (see further below).

The two features are independent: no codec, decoder or shared adapter is
involved in either.

## Automated evidence

`python -m pytest tests/unit/test_bluetooth.py tests/ui/test_settings_dialog.py`
— no radio, no audio, no network:

* **Settings** — Bluetooth is off by default; the saved device id and name
  survive a save/load round trip; a non-boolean `bluetooth_enabled` and an
  over-long device name fall back to defaults with a warning.
* **Status text** — every `SinkStatus` produces a readable line, including a
  connect timeout and the "waiting for a device" state.
* **Worker** — connects to the saved device on its own thread, reports
  `CONNECTED`, disconnects back to `READY`, survives a connect failure with the
  Windows error in `state.error`, reacts to a remote close, can connect without
  the saved `enabled` flag, and starts/stops when settings are applied. A
  failing device enumeration is reported but does not prevent connecting to the
  saved device.
* **Dialog** — the Bluetooth tab exists (A2DP sink and *iPhone notifications
  (ANCS)* sections together) loads and returns the saved fields, refuses to
  enable the feature without a chosen device, and maps duplicate device names
  to unique combo labels.
* **Capability probe** — `bluetooth_support()` reports an OS/API reason instead
  of raising when the WinRT projection is missing. On this machine
  (`Windows 11`, WinRT 3.2.1) `list_sink_devices_sync()` returns `([], "")`
  until a phone with an A2DP sink interface is paired.

Whole suite: `python -m pytest` — all passing, headless, offline.

`python -m ruff check bark_pc tests` — clean.

# iPhone notifications (ANCS) validation

Covers `bark_pc/bluetooth/ancs.py`, `ancs_backend.py`, `advertising.py`,
`hid.py`, `usb_adapters.py`, and the *iPhone notifications (ANCS)* section of the
*Settings → Bluetooth* tab. Bumble opens a dedicated
USB Bluetooth adapter through libusb; the receiver can either scan for the
iPhone (central, default) or advertise an accessory (peripheral). No test opens a
dongle.

## Automated evidence

`python -m pytest tests/unit/test_ancs.py tests/unit/test_ancs_connect.py
 tests/unit/test_ancs_backend.py tests/unit/test_advertising.py
 tests/unit/test_hid.py tests/unit/test_usb_adapters.py
 tests/ui/test_settings_dialog.py`:

* **Settings** — ANCS is off by default; the adapter selector and adapter name
  survive a save/load round trip; a non-USB transport (for example a
  `tcp-client:` spec) is rejected so a tampered settings file cannot make the
  app open a network transport.
* **Mapping** — ANCS attributes map to the normal `Notification` fields, the
  iPhone's notification UID plus date forms the dedupe id, and the synthesized
  Bark payload is stored once even when the iPhone sends it twice.
* **Advertisement filtering** — named iPhones/iPads score highest, unknown
  Apple devices and ANCS advertisers are kept, and AirPods/Watches/Macs and
  non-connectable advertisements are filtered out (the diagnostic probe can
  include every device instead).
* **Worker** — advertises after opening the adapter, reports `CONNECTED` when
  the iPhone connects, forwards notifications, returns to `ADVERTISING` after
  the link drops, accepts a reconnect without reopening the adapter, reports
  start failures in `ERROR`, restarts when the adapter changes, and stops when
  the option is disabled.
* **USB discovery** — Bluetooth-class devices and composite devices with a
  Bluetooth interface are found; drives and hubs are not; identical dongles get
  stable `serial`/`#N` selectors. Enumeration and the support probe never raise.
* **Dialog** — the ANCS controls are on their own tab, saved values are kept
  even while the adapter list is unavailable, the mode switch updates the
  guidance text and saved candidate, a newly selected adapter is included in the
  saved candidate, and enabling the option without an adapter is refused.

* **CLI probe** — the adapter and device lists are numbered, every scanned
  device is listed (non-Apple and non-connectable ones included, marked as
  such), an empty selection means the first entry, out-of-range answers are
  rejected, a non-interactive run takes the best-ranked device, a preselected
  address skips the scan, and a non-USB `--adapter` value is refused. The
  appearance argument accepts hex or decimal and rejects values outside 16 bits,
  and the security block reports bonding/MITM plus an unauthenticated
  (Just Works) fallback instead of claiming MITM succeeded.
* **Advertising packet** — the accessory's PDU is decoded element by element:
  the ANCS UUID appears in a **0x15** service solicitation (not in a 0x06/0x07
  UUID list), HID `0x1812` appears in the **0x03** 16-bit service list whenever
  HID is on, the appearance is read little-endian and named from the Assigned
  Numbers (`0x03C1` = Keyboard, `0x00C0` = Generic Watch, `0x0007` =
  Eye-glasses), the 31-byte legacy budget is enforced (29 bytes with HID),
  truncated elements are reported instead of raising, and the scan response
  carries only the local name.
* **HID over GATT** — the service UUID is `0x1812` and it carries HID
  Information (4 octets, bcdHID 1.11, normally connectable), a complete
  65-byte boot-keyboard report map, HID Control Point, Protocol Mode, input and
  output Report characteristics with Report Reference descriptors, and the boot
  keyboard in/out reports. A real `bumble.gatt_server.Server` accepts it and
  auto-adds the two CCCDs (21 attributes). Report output updates the LED state
  and calls the observer, unsupported characters are skipped rather than
  guessed, and typing sends a press plus a release to both the Report and the
  boot-keyboard characteristics.
* **Security configuration** — the default pairing config is Just Works
  (`mitm=False`) with bonding on; `mitm=True` switches the delegate to passkey
  display and forwards the number to the caller; `security_summary()` reports
  encrypted/authenticated/SC/key-size/peer-MITM from the live link and reports
  `connected=False` instead of guessing when there is no link.

`python -m pytest` — whole suite passing, headless, offline.

`python -m ruff check bark_pc tests` — clean.

## ANCS interactive check (run manually, not in this container)

1. Plug in a spare BLE 4.0+ USB dongle. On Windows, bind it to WinUSB with Zadig
   (the Windows driver must not be installed/claimed).
2. *Settings → Bluetooth → iPhone notifications*, press **Refresh** next to
   *USB Bluetooth adapter*:
   the dongle appears. Select it, tick *Receive iPhone notifications…*, save. The
   status shows *Advertising*.
3. On the iPhone: **Settings → Bluetooth** → tap **Bark Desktop** under Other
   Devices/Accessories → accept pairing → allow notifications. The PC status
   becomes *Receiving notifications from <iPhone name>*.
4. Trigger a message/calendar alert. It must appear as a Windows toast, in the
   history with source `ancs`, and through TTS/webhooks if those are enabled.
5. Lock the iPhone / walk out of range: the status returns to *Advertising*;
   bring it back (tap the accessory again) and it reconnects. Existing alerts
   must not be re-added to the history.
6. Restart the app with the option saved: advertising resumes without the
   adapter selection being lost. Unplug the dongle and restart: the status shows
   a driver/not-found error rather than crashing.
7. Optional cross-check without the GUI: stop the receiver, then run
   `python -m bark_pc.tools.ancs_connect --mode advertise` (see the README). It
   must find the same dongle, advertise the same accessory and print incoming
   notifications; the bond it creates is reused by the app afterwards.
8. Optional packet check, no dongle needed:
   `python -m bark_pc.tools.ancs_connect --dump-advertising`. The elements must
   be `0x01 Flags`, `0x15 128-bit service solicitation 7905f431-…-4b1e122d00d0`,
   `0x03 16-bit service UUIDs (complete) 0x1812`, then `0x19 Appearance
   0x03C1 (Keyboard)` — 29 of the 31 legacy bytes. The verdict lines must read
   *ANCS solicited through 0x15* and *HID advertised*; if ANCS reads *NOT
   solicited* the iPhone never offers the service, and if HID is missing iOS may
   never list the accessory at all. `--no-hid` shows the ANCS-only packet for
   comparison (`0x00C0` appearance, 25 bytes).
9. Optional appearance/security experiment: run the accessory through the CLI
   instead of the GUI, for example
   `python -m bark_pc.tools.ancs_connect --mode advertise --appearance 0x0007`
   or `… --mitm`. The tool prints the PDU it advertises, the passkey when MITM
   asks for one, and the negotiated `encrypted`/`authenticated`/`key` values
   after subscribing. Note `0x0007` is *Eye-glasses* in the Bluetooth Assigned
   Numbers; *Generic Watch* is `0x00C0`, so the swap is cosmetic and does not
   change whether ANCS works.

Known limitations:

* ANCS is iPhone-only. Notification actions (accept/decline/reply) are not
  implemented; only the `added`/`modified` events are read.
* The HID keyboard is a vehicle for pairing, not an input feature: it types
  nothing unless `HidKeyboard.type_text()` is called, and the app never calls
  it. iOS may show the accessory as a keyboard in Settings → Bluetooth; that is
  expected and is what keeps it listed.
* The app always advertises the spec's *Generic Watch* appearance (`0x00C0`) and
  always pairs with Just Works + bonding. The CLI can change both
  (`--appearance`, `--mitm`) for experiments; a display-less accessory cannot
  complete MITM without showing a passkey somewhere.
* The PC is the BLE peripheral, so it cannot connect first: the iPhone has to
  be the one tapping the accessory in Settings → Bluetooth. If iOS never lists
  *Bark Desktop*, check that the HID service is advertised (see the packet check
  above).
* A WinUSB-bound dongle disappears from Windows' Bluetooth devices while the
  receiver is running; use a dedicated dongle for ANCS.
* Pre-existing notifications are skipped on purpose, so history is not replayed
  after every reconnect.

## A2DP sink interactive check (run manually, not in this container)

1. Pair an Android phone in **Windows Settings → Bluetooth & devices**.
2. Open *Settings → Bluetooth*, press **Refresh**; the phone appears in
   **Paired device**.
3. Tick **Use this PC as a Bluetooth speaker**, select the phone, press
   **Connect**. The status line must reach **Receiving audio from …**.
4. Play music on the phone: it should come out of the PC's current output
   device. Change the Windows output device; audio follows it.
5. Press **Disconnect**: the phone's speaker takes over, the PC's status returns
   to **Waiting for a device**, and push notifications keep arriving.
6. Reconnect, then restart the app: with the option saved, the connection is
   re-opened at launch.
7. Unpair the phone in Windows Settings and press **Refresh**: the list empties
   and the status suggests pairing again.

Known limitations:

* Windows 10 build 19041+ is required; the WinRT API is absent on older builds.
* The source device controls play/pause and (absolute) volume. The PC only
  enables/opens/releases the connection.
* iOS cannot stream system audio to an A2DP sink.
