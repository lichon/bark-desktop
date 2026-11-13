"""Scan for Bluetooth devices over BLE (or advertise), then connect through ANCS.

    python -m bark_pc.tools.ancs_connect
    python -m bark_pc.tools.ancs_connect --mode advertise
    python -m bark_pc.tools.ancs_connect --dump-advertising
    python -m bark_pc.tools.ancs_connect --list-adapters
    python -m bark_pc.tools.ancs_connect --adapter usb:0A12:0001 --scan-seconds 12
    python -m bark_pc.tools.ancs_connect --peer AA:BB:CC:DD:EE:FF --seconds 60
    python -m bark_pc.tools.ancs_connect --mode advertise --appearance 0x0007 --mitm
    python -m bark_pc.tools.ancs_connect --dump-advertising --no-hid

The tool is a diagnostic companion to *Settings -> Bluetooth -> iPhone
notifications*: it opens the
same dedicated USB Bluetooth adapter with Bumble and offers both connection
directions:

* ``--mode scan`` (default) — scan the airwaves, show every device it hears
  with its name (not just Apple ones), ask which one to use, connect to it and
  subscribe. A non-iPhone can be selected, but it will fail at the ANCS
  subscription step with an explanation.
* ``--mode advertise`` — act like a small ANCS accessory: advertise under the
  name "Bark Desktop" and let the iPhone connect from *Settings -> Bluetooth*,
  the same way the app's peripheral mode works.

Either way the tool pairs, subscribes to Notification Source + Data Source and
prints every notification it receives. Stop the app's ANCS receiver first: one
dongle can only be opened by one process at a time.

Three checks are built in, because an iPhone silently ignores an accessory that
fails any of them:

* ``--dump-advertising`` prints the advertising PDU and scan response element by
  element and says whether the 0x15 **service solicitation** carries the ANCS
  UUID (a 0x06/0x07 UUID list is not equivalent). It needs no dongle.
* ``--appearance HEX`` changes the advertised appearance (default 0x03C1 keyboard
  with HID, 0x00C0 Generic Watch without). Cosmetic only: it selects the iOS
  icon, never the ANCS handshake.
* ``--no-hid`` drops the HID over GATT keyboard (0x1812). HID is on by default
  because iOS only reliably lists accessories that expose a profile it knows;
  without it the ANCS solicitation alone is often invisible in Settings.
* ``--mitm`` / ``--no-bonding`` change the link security. The default is Just
  Works + bonding; ``--mitm`` uses passkey entry, prints the passkey here and
  the iPhone has to type it. Either way the link security actually negotiated is
  printed after subscribing.
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import json
import logging
import sys
from collections.abc import Callable, Sequence
from datetime import datetime
from pathlib import Path

from bark_pc.bluetooth.advertising import HID_APPEARANCE_KEYBOARD
from bark_pc.bluetooth.ancs import (
    ANCS_ADVERTISED_NAME,
    ANCS_APPEARANCE,
    AncsError,
    AncsNotification,
    AncsPeerInfo,
    iphone_score,
)
from bark_pc.bluetooth.usb_adapters import (
    UsbBluetoothAdapter,
    list_usb_bluetooth_adapters,
)
from bark_pc.core.config import resolve_paths

logger = logging.getLogger("bark_pc.ancs.probe")

SCAN_SECONDS = 8.0
SCAN_ROUNDS = 5
MODE_SCAN = "scan"
MODE_ADVERTISE = "advertise"


# -- presentation --------------------------------------------------------- #
def format_adapters(adapters: Sequence[UsbBluetoothAdapter], error: str = "") -> str:
    """Numbered adapter list for `--list-adapters` and the picker."""
    if not adapters:
        return error or (
            "No USB Bluetooth adapter found. Plug in a BLE dongle and bind the "
            "WinUSB driver with Zadig."
        )
    width = max(len(adapter.selector) for adapter in adapters)
    lines = [
        f"{index:>2}. {adapter.selector:<{width}}  {adapter.name}"
        for index, adapter in enumerate(adapters, start=1)
    ]
    if error:
        lines.append(error)
    return "\n".join(lines)


def rank_devices(peers: Sequence[AncsPeerInfo]) -> list[AncsPeerInfo]:
    """Order every scanned device: iPhone-like first, then the rest by signal.

    Unlike the app's iPhone filter this never drops a device, so the probe can
    show all Bluetooth devices in range, Apple or not.
    """
    return sorted(peers, key=lambda peer: (iphone_score(peer), peer.rssi), reverse=True)


def format_peers(peers: Sequence[AncsPeerInfo]) -> str:
    """Numbered list of every device seen in a scan, with names and flags."""
    ranked = rank_devices(list(peers))
    if not ranked:
        return "No Bluetooth device found in range."
    name_width = max(len(peer.name or "(no name)") for peer in ranked)
    address_width = max(len(peer.address) for peer in ranked)
    lines = []
    for index, peer in enumerate(ranked, start=1):
        flags = [
            flag
            for flag, marked in (
                ("Apple", peer.apple),
                ("ANCS", peer.has_ancs),
                ("not connectable", not peer.connectable),
            )
            if marked
        ]
        lines.append(
            f"{index:>2}. {(peer.name or '(no name)'):<{name_width}}  "
            f"{peer.address:<{address_width}}  rssi {peer.rssi:>4}  "
            f"{' '.join(flags)}".rstrip()
        )
    return "\n".join(lines)


def parse_selection(answer: str, count: int) -> int:
    """Turn a typed answer into a 0-based index; empty means the first entry."""
    text = (answer or "").strip()
    if count < 1:
        raise ValueError("There is nothing to select.")
    if not text:
        return 0
    if not text.isdigit():
        raise ValueError(f"Enter a number between 1 and {count}.")
    index = int(text) - 1
    if not 0 <= index < count:
        raise ValueError(f"Enter a number between 1 and {count}.")
    return index


def prompt_selection(
    count: int,
    *,
    what: str,
    ask: Callable[[str], str] | None = None,
    tell: Callable[[str], None] = print,
    attempts: int = 3,
) -> int:
    """Ask for a 1-based choice until it parses; raise SystemExit otherwise."""
    ask = ask or input
    prompt = f"Select the {what} [1-{count}] (1): "
    for _ in range(attempts):
        try:
            return parse_selection(ask(prompt), count)
        except ValueError as exc:
            tell(str(exc))
    raise SystemExit("No selection made.")


def format_notification(notification: AncsNotification) -> str:
    """One human-readable line per ANCS notification."""
    stamp = datetime.now().strftime("%H:%M:%S")
    app = notification.app_name or notification.app_id or "iPhone"
    title = notification.display_title
    body = (notification.body or notification.subtitle or "").strip()
    if body and body != title:
        text = f"{title} - {body}"
    else:
        text = title
    category = f" [{notification.category}]" if notification.category else ""
    return f"[{stamp}] {app}: {text} (uid {notification.uid}){category}"


def notification_json(notification: AncsNotification) -> str:
    """Machine-readable form, useful when the probe feeds another script."""
    return json.dumps(
        {
            "uid": notification.uid,
            "app_id": notification.app_id,
            "app_name": notification.app_name,
            "title": notification.title,
            "subtitle": notification.subtitle,
            "body": notification.body,
            "category": notification.category,
            "date_ms": notification.date_ms,
        },
        ensure_ascii=False,
    )


def format_security(summary: dict[str, object]) -> str:
    """One block describing bonding/MITM, on the configured side and the link."""
    configured = [
        "bonding=" + ("on" if summary.get("bonding") else "off"),
        "MITM=" + ("on" if summary.get("mitm_requested") else "off (Just Works)"),
        "HID=" + ("keyboard 0x1812" if summary.get("hid") else "off"),
    ]
    lines = ["Security requested: " + ", ".join(configured)]
    if not summary.get("connected"):
        lines.append("Link: not connected")
        return "\n".join(lines)
    lines.append(
        "Link: "
        f"role={summary.get('role') or '?'} "
        f"encrypted={'yes' if summary.get('encrypted') else 'no'} "
        f"authenticated={'yes' if summary.get('authenticated') else 'no'} "
        f"SC={'yes' if summary.get('secure_connections') else 'no'} "
        f"key={summary.get('key_size')}B"
    )
    if summary.get("mitm_requested"):
        if summary.get("authenticated"):
            lines.append("MITM: the link is authenticated - the passkey matched on both sides.")
        else:
            lines.append(
                "MITM: requested but the link is NOT authenticated - the iPhone "
                "most likely fell back to Just Works (check that it typed the passkey)."
            )
    if summary.get("peer_mitm_requested"):
        lines.append("The iPhone also asked for MITM protection.")
    return "\n".join(lines)


def _appearance_value(text: str) -> int:
    """argparse type for a 16-bit appearance, accepting 0x00C0 or 192."""
    try:
        value = int(text, 0)
    except (TypeError, ValueError):
        raise argparse.ArgumentTypeError(f"{text!r} is not a number (try 0x00C0)") from None
    if not 0 <= value <= 0xFFFF:
        raise argparse.ArgumentTypeError("Appearance must be between 0x0000 and 0xFFFF.")
    return value


def dump_advertising(appearance: int | None = None, *, hid: bool = True) -> str:
    """The exact advertising PDU and scan response, decoded, without a dongle."""
    from bark_pc.bluetooth.ancs_backend import ancs_advertising_data

    return ancs_advertising_data(appearance, hid=hid).describe()


# -- flow ----------------------------------------------------------------- #
def _pick_adapter(args: argparse.Namespace) -> UsbBluetoothAdapter:
    """An explicit --adapter skips enumeration (enumeration may fail on driver issues)."""
    if args.adapter:
        selector = args.adapter.strip()
        if not selector.casefold().startswith("usb:"):
            raise SystemExit("--adapter expects a Bumble USB selector such as usb:0A12:0001.")
        return UsbBluetoothAdapter(selector=selector, name=selector, vendor_id=0, product_id=0)

    adapters, error = list_usb_bluetooth_adapters()
    if not adapters:
        raise SystemExit(format_adapters(adapters, error))
    if len(adapters) == 1:
        print(f"Using the only USB Bluetooth adapter: {adapters[0].label}")
        return adapters[0]

    print(format_adapters(adapters))
    if not args.interactive:
        print(f"Selected {adapters[0].label} automatically.")
        return adapters[0]
    return adapters[prompt_selection(len(adapters), what="USB Bluetooth adapter")]


async def _scan(backend, seconds: float) -> list[AncsPeerInfo]:
    """One scan window listing every device; the backend caps it at SCAN_SECONDS."""
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    timer = loop.call_later(max(0.5, seconds), stop.set)
    try:
        return await backend.scan(stop, include_all=True)
    finally:
        timer.cancel()


async def _choose_peer(
    backend,
    *,
    scan_seconds: float,
    rounds: int,
    preselected: str,
    interactive: bool,
) -> AncsPeerInfo | None:
    """Scan until a candidate is picked; None when the user gives up."""
    if preselected:
        print(f"Using the address from --peer: {preselected}")
        return AncsPeerInfo(address=preselected)

    for attempt in range(1, rounds + 1):
        print(
            f"Scanning for Bluetooth devices for up to {scan_seconds:g}s "
            f"(round {attempt}/{rounds}) - open Settings > Bluetooth on the iPhone, "
            "keep its screen on and put it near this PC."
        )
        peers = await _scan(backend, scan_seconds)
        candidates = rank_devices(peers)

        if not candidates:
            print("No Bluetooth device found in range.")
            if not interactive:
                continue
            if input("Scan again? [Y/n] ").strip().casefold() in ("", "y", "yes"):
                continue
            return None

        print()
        print(f"Found {len(candidates)} device(s) in range (all devices shown):")
        print(format_peers(candidates))
        if not interactive:
            # Non-interactive runs take the best-ranked device.
            peer = candidates[0]
            print(f"Selected {peer.label} ({peer.address}) automatically.")
            return peer
        try:
            index = prompt_selection(len(candidates), what="device")
        except SystemExit:
            return None
        peer = candidates[index]
        if not peer.connectable:
            print(
                f"{peer.label} is not advertising as connectable; the connection "
                "may fail. Keep the device awake and try again."
            )
        return peer
    return None


async def _listen(disconnected: asyncio.Event, seconds: float) -> str:
    """Wait for a drop, an optional time limit, or Ctrl+C."""
    waiters = {asyncio.ensure_future(disconnected.wait()): "disconnected"}
    if seconds > 0:
        waiters[asyncio.ensure_future(asyncio.sleep(seconds))] = "timeout"
    done, pending = await asyncio.wait(waiters, return_when=asyncio.FIRST_COMPLETED)
    for task in pending:
        task.cancel()
    await asyncio.gather(*pending, return_exceptions=True)
    return waiters[next(iter(done))]


async def _serve(backend, peer: AncsPeerInfo, args: argparse.Namespace) -> str:
    """Pair, subscribe and print notifications until a drop/limit.

    Returns `_listen`'s reason, or "failed" when the ANCS setup did not work.
    """
    disconnected = asyncio.Event()

    def on_notification(notification: AncsNotification) -> None:
        print(
            notification_json(notification) if args.json else format_notification(notification),
            flush=True,
        )

    print("Pairing and subscribing to Apple notifications...")
    try:
        await backend.subscribe(on_notification, disconnected.set)
    except AncsError as exc:
        print(f"Subscribe failed: {exc}")
        return "failed"

    print()
    print(format_security(backend.security_summary()))
    print()
    print("Connected. Allow the pairing prompt on the iPhone if it appears.")
    print("Listening for iPhone notifications - press Ctrl+C to stop.")
    reason = await _listen(disconnected, args.seconds)
    if reason == "disconnected":
        print("The iPhone disconnected.")
    elif reason == "timeout":
        print(f"Finished after {args.seconds:g}s.")
    return reason


async def _run_central(backend, args: argparse.Namespace) -> int:
    """Scan, pick a device and connect to it (BLE central role)."""
    peer = await _choose_peer(
        backend,
        scan_seconds=args.scan_seconds,
        rounds=args.scan_rounds,
        preselected=args.peer.strip(),
        interactive=args.interactive,
    )
    if peer is None:
        print("No device selected.")
        return 1

    print(f"Connecting to {peer.label} ({peer.address})...")
    try:
        await backend.connect(peer)
    except AncsError as exc:
        print(f"Connect failed: {exc}")
        return 1

    return 0 if await _serve(backend, peer, args) != "failed" else 1


async def _run_advertise(backend, args: argparse.Namespace) -> int:
    """Advertise a peripheral ANCS accessory and wait for iOS to connect."""
    report = backend.advertising_report
    if report is not None:
        print(report.describe())
        print()
    print(f'Advertising as "{ANCS_ADVERTISED_NAME}"...')
    print(
        "On the iPhone: Settings > Bluetooth > tap "
        f'"{ANCS_ADVERTISED_NAME}" under Other Devices / Accessories, then allow '
        "pairing and notifications. Press Ctrl+C to stop."
    )
    print()

    stop = asyncio.Event()
    timer = None
    if args.wait_seconds > 0:
        timer = asyncio.get_running_loop().call_later(args.wait_seconds, stop.set)
    try:
        while True:
            try:
                peer = await backend.wait_for_connection(stop)
            except AncsError as exc:
                print(f"Connection failed: {exc}")
                return 1
            if peer is None:
                if args.wait_seconds > 0:
                    print(f"No device connected within {args.wait_seconds:g}s.")
                return 0

            print(f"{peer.label} connected ({peer.address}).")
            reason = await _serve(backend, peer, args)
            if reason != "disconnected":
                return 0 if reason != "failed" else 1
            print("Advertising again - the iPhone can reconnect from Settings > Bluetooth.")
            print()
    finally:
        if timer is not None:
            timer.cancel()


async def _run(args: argparse.Namespace) -> int:
    from bark_pc.bluetooth.ancs_backend import BumbleAncsBackend

    adapter = _pick_adapter(args)
    keystore = (
        Path(args.keystore).expanduser()
        if args.keystore
        else (resolve_paths().data_dir / "bluetooth" / "ancs-keys.json")
    )

    print(f"Adapter: {adapter.label if adapter.vendor_id else adapter.selector}")
    print(f"Pairing keys: {keystore}")
    print(format_security({"bonding": args.bonding, "mitm_requested": args.mitm, "hid": args.hid}))
    print()

    backend = BumbleAncsBackend(
        log=logger,
        keystore_path=keystore,
        adapter=adapter.selector,
        appearance=args.appearance,
        mitm=args.mitm,
        bonding=args.bonding,
        hid=args.hid,
    )
    backend.on_passkey = lambda number, digits: print(
        f"Pairing passkey: {number:0{digits}d} - type it on the iPhone.", flush=True
    )
    try:
        await backend.start()
    except AncsError as exc:
        print(f"Could not open the adapter: {exc}")
        return 1

    try:
        if args.mode == MODE_ADVERTISE:
            return await _run_advertise(backend, args)
        return await _run_central(backend, args)
    finally:
        with contextlib.suppress(Exception, asyncio.CancelledError):
            await backend.close()


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "--list-adapters",
        action="store_true",
        help="list the USB Bluetooth adapters Bumble can open and exit",
    )
    parser.add_argument(
        "--adapter",
        default="",
        help="Bumble selector (usb:VVVV:PPPP), skipping adapter enumeration",
    )
    parser.add_argument(
        "--mode",
        choices=(MODE_SCAN, MODE_ADVERTISE),
        default=MODE_SCAN,
        help=(
            "scan and connect to a device (default), or advertise as "
            f'"{ANCS_ADVERTISED_NAME}" and let the iPhone connect from its '
            "Bluetooth settings"
        ),
    )
    parser.add_argument(
        "--peer",
        default="",
        help="scan mode: connect straight to this BLE address, skipping the picker",
    )
    parser.add_argument(
        "--scan-seconds",
        type=float,
        default=SCAN_SECONDS,
        help=f"length of one scan window (Bumble caps it at {SCAN_SECONDS:g}s)",
    )
    parser.add_argument(
        "--scan-rounds",
        type=int,
        default=SCAN_ROUNDS,
        help="how many scan windows to try before giving up",
    )
    parser.add_argument(
        "--seconds",
        type=float,
        default=0.0,
        help="stop listening after this many seconds (0 = until Ctrl+C)",
    )
    parser.add_argument(
        "--wait-seconds",
        type=float,
        default=0.0,
        help=(
            "advertise mode: give up when no iPhone connects within this many "
            "seconds (0 = wait until Ctrl+C)"
        ),
    )
    parser.add_argument(
        "--keystore",
        default="",
        help="pairing-key file (default: the app's bluetooth/ancs-keys.json)",
    )
    parser.add_argument("--json", action="store_true", help="print notifications as JSON lines")
    parser.add_argument(
        "--non-interactive",
        action="store_true",
        help="never prompt: take the best-ranked iPhone and adapter",
    )
    parser.add_argument(
        "--appearance",
        type=_appearance_value,
        default=None,
        metavar="HEX",
        help=(
            "16-bit Appearance advertised with the accessory. Default: "
            f"0x{HID_APPEARANCE_KEYBOARD:04X} (Keyboard) with --hid, otherwise "
            f"0x{ANCS_APPEARANCE:04X} (Generic Watch). 0x0007 is Eye-glasses in "
            "the Bluetooth Assigned Numbers, not Generic Watch. Cosmetic only: "
            "it changes the iOS icon, never the ANCS handshake."
        ),
    )
    parser.add_argument(
        "--no-hid",
        action="store_true",
        help=(
            "do not publish/advertise the HID over GATT keyboard (0x1812). "
            "HID is what makes iOS list the accessory in Settings -> Bluetooth; "
            "drop it to test the ANCS-only handshake"
        ),
    )
    parser.add_argument(
        "--mitm",
        action="store_true",
        help=(
            "ask for man-in-the-middle protection (passkey entry). The passkey "
            "is printed here and has to be typed on the iPhone; without this "
            "flag the accessory uses Just Works, which no display-less device "
            "can avoid"
        ),
    )
    parser.add_argument(
        "--no-bonding",
        action="store_true",
        help="do not store bonding keys (the iPhone then has to pair on every connect)",
    )
    parser.add_argument(
        "--dump-advertising",
        action="store_true",
        help=(
            "print the advertising PDU and scan response as bytes, with the "
            "0x15 ANCS service solicitation check, then exit (no dongle needed)"
        ),
    )
    parser.add_argument("-v", "--verbose", action="store_true", help="debug logging")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.WARNING,
        format="%(levelname)s %(name)s: %(message)s",
    )
    args.interactive = not args.non_interactive and sys.stdin.isatty()
    args.bonding = not args.no_bonding
    args.hid = not args.no_hid

    if args.dump_advertising:
        try:
            print(dump_advertising(args.appearance, hid=args.hid))
        except AncsError as exc:
            print(f"Cannot build the advertising data: {exc}")
            return 1
        return 0

    if args.list_adapters:
        print(format_adapters(*list_usb_bluetooth_adapters()))
        return 0
    if args.scan_rounds < 1:
        print("--scan-rounds must be at least 1.")
        return 2
    if args.mode == MODE_ADVERTISE and args.peer:
        print("--peer is ignored in advertise mode; the iPhone chooses the connection.")
    try:
        return asyncio.run(_run(args))
    except KeyboardInterrupt:
        print("\nStopped.")
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
