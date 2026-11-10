"""Spike T1 — is the base courier token accepted by APNs via Bark?

Go/no-go gate for the whole product (`research.md` §R6).  Run it against a
Bark-compatible server you control:

    python -m bark_pc.tools.spike_token --server http://127.0.0.1:8080

It activates an anonymous device, prints a redacted base token, registers it with
the server, pushes a self-test message, and waits (default 30 s) for the message
to arrive on the courier stream.  The outcome is appended to
`docs/validation/spike_T1.md`.
"""

from __future__ import annotations

import argparse
import sys
import uuid
from datetime import datetime
from pathlib import Path

import anyio

from bark_pc.bark.server_client import BarkServerClient
from bark_pc.core.errors import BarkError
from bark_pc.core.logging import build_logger
from bark_pc.push.identity import generate_device_ids
from bark_pc.push.payload import parse_payload
from bark_pc.push.pypush_client import PypushReceiver

DEFAULT_TOPIC = "me.fin.bark"


def _record(text: str, path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(text.rstrip() + "\n")


async def run_spike(server: str, topic: str, timeout: float, report: Path) -> int:
    receiver = PypushReceiver()
    udid, serial = generate_device_ids()
    print(f"activating anonymous device (udid {udid})…")
    material = await receiver.activate(udid=udid, serial=serial)

    message_id = str(uuid.uuid4())
    try:
        async with anyio.fail_after(timeout):
            async with receiver._connection(material) as connection:  # noqa: SLF001 - spike
                token = (await connection.base_token).hex()
                print(f"base token: {token[:8]}… ({len(token)} chars)")
                client = BarkServerClient(server)
                try:
                    key = client.register(token)
                    print(f"registered device key: {key}")
                    client.push_test(key, message_id=message_id, body="Spike T1 self-test")
                finally:
                    client.close()

                scoped = await connection.mint_scoped_token(topic)
                async with connection.notification_stream(topic, scoped) as stream:
                    async for command in stream:
                        await connection.ack(command)
                        parsed = parse_payload(command.payload)
                        remote_id = parsed.remote_id
                        if remote_id == message_id:
                            print("PASS: the self-test push arrived on the stream")
                            _record(
                                f"## {datetime.now().isoformat(timespec='seconds')} — PASS\n"
                                f"- server: `{server}`\n- topic: `{topic}`\n"
                                f"- token prefix: `{token[:8]}…`\n"
                                f"- device key: `{key}`\n"
                                f"- arrival: within {timeout:.0f}s\n",
                                report,
                            )
                            return 0
                        print(f"  (other push received: {parsed.outcome.value})")
    except TimeoutError:
        print("FAIL: no push arrived before the timeout")
        _record(
            f"## {datetime.now().isoformat(timespec='seconds')} — FAIL (timeout)\n"
            f"- server: `{server}`\n- topic: `{topic}`\n"
            f"- The server accepted the push but nothing arrived on the stream.\n"
            f"- Check the server log for the APNs reason (BadDeviceToken / "
            f"DeviceTokenNotForTopic) and compare `push_topic`.\n",
            report,
        )
        return 1
    except BarkError as exc:
        print(f"FAIL: {exc}")
        _record(
            f"## {datetime.now().isoformat(timespec='seconds')} — FAIL (server)\n"
            f"- server: `{server}`\n- reason: {exc}\n",
            report,
        )
        return 1
    except Exception as exc:  # activation/connection failures
        print(f"FAIL: {type(exc).__name__}: {exc}")
        _record(
            f"## {datetime.now().isoformat(timespec='seconds')} — FAIL (transport)\n"
            f"- server: `{server}`\n- reason: {type(exc).__name__}: {exc}\n",
            report,
        )
        return 1
    return 1


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--server", required=True, help="Bark-compatible server base URL")
    parser.add_argument("--topic", default=DEFAULT_TOPIC)
    parser.add_argument("--timeout", type=float, default=30.0)
    parser.add_argument(
        "--report",
        type=Path,
        default=Path("docs/validation/spike_T1.md"),
        help="file to append the outcome to",
    )
    parser.add_argument("--log-level", default="info")
    args = parser.parse_args(argv)
    build_logger(None, level=args.log_level, console=True)
    return anyio.run(run_spike, args.server, args.topic, args.timeout, args.report)


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
