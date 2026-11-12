# Bluetooth audio receiver validation

Covers the A2DP sink feature: `bark_pc/bluetooth` (WinRT backend, worker thread,
settings state) and the *Settings → Bluetooth* tab. It uses the Windows
Bluetooth stack through `Windows.Media.Audio.AudioPlaybackConnection`, the same
API as the open-source
[AudioPlaybackConnector](https://github.com/ysc3839/AudioPlaybackConnector);
no bumble, codec, decoder or separate adapter is involved.

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
* **Dialog** — the Bluetooth tab exists, loads and returns the saved fields,
  refuses to enable the feature without a chosen device, and maps duplicate
  device names to unique combo labels.
* **Capability probe** — `bluetooth_support()` reports an OS/API reason instead
  of raising when the WinRT projection is missing. On this machine
  (`Windows 11`, WinRT 3.2.1) `list_sink_devices_sync()` returns `([], "")`
  until a phone with an A2DP sink interface is paired.

Whole suite: `python -m pytest` — all passing, headless, offline.

`python -m ruff check bark_pc tests` — clean.

## Interactive check (run manually, not in this container)

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
