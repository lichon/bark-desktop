#!/usr/bin/env python
"""End-to-end: sign through apns-device, then activate against a local Albert.

Runs real HTTP against a local server that mimics the Supabase function's
two-call protocol and Apple's activation response.  No network, no macOS runner.

    .venv/Scripts/python scripts/e2e/apns_device_activate.py
"""

from __future__ import annotations

import base64
import datetime
import json
import plistlib
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs

import anyio
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import NameOID

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from bark_pc.push import apns_device  # noqa: E402

UDID = "E2E90000-0000-4000-8000-000000000001"
TOKEN = "device-token"
SIGNATURE = b"apsd-signature"
CHAIN = b"apsd-chain"


def certificate_pem() -> str:
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "e2e-device")])
    certificate = (
        x509.CertificateBuilder()
        .subject_name(name)
        .issuer_name(name)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(datetime.datetime(2020, 1, 1))
        .not_valid_after(datetime.datetime(2040, 1, 1))
        .sign(key, hashes.SHA256())
    )
    return certificate.public_bytes(serialization.Encoding.PEM).decode("ascii")


class Handler(BaseHTTPRequestHandler):
    polls = 0
    activation_info = b""
    albert_payload: dict = {}
    cert_pem = ""

    def log_message(self, *args) -> None:  # keep the script quiet
        pass

    def _body(self, status: int, body: bytes, content_type: str) -> None:
        self.send_response(status)
        self.send_header("content-type", content_type)
        self.send_header("content-length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _json(self, status: int, payload: dict) -> None:
        self._body(status, json.dumps(payload).encode(), "application/json")

    def do_POST(self) -> None:
        length = int(self.headers.get("content-length") or 0)
        body = self.rfile.read(length)
        if self.path == "/sign":
            assert self.headers.get("authorization") == f"Bearer {TOKEN}"
            request = json.loads(body)
            Handler.activation_info = base64.b64decode(request["activation_info"], validate=True)
            return self._json(
                202,
                {
                    "code": 202,
                    "message": "workflow dispatched",
                    "data": {"id": 7, "status": "queued"},
                },
            )
        if self.path.startswith("/albert"):
            form = parse_qs(body.decode())
            Handler.albert_payload = plistlib.loads(form["activation-info"][0].encode())
            record = {
                "device-activation": {
                    "activation-record": {"DeviceCertificate": Handler.cert_pem.encode()}
                }
            }
            protocol = plistlib.dumps(record).decode("utf-8")
            return self._body(
                200, f"<Document><Protocol>{protocol}</Protocol></Document>".encode(), "text/xml"
            )
        self.send_error(404)

    def do_GET(self) -> None:
        if self.path != "/sign/7":
            return self.send_error(404)
        Handler.polls += 1
        if Handler.polls == 1:
            return self._json(
                202,
                {
                    "code": 202,
                    "message": "workflow running",
                    "data": {"id": 7, "status": "in_progress"},
                },
            )
        return self._json(
            200,
            {
                "code": 200,
                "message": "success",
                "data": {
                    "id": 7,
                    "status": "completed",
                    "conclusion": "success",
                    "mode": "sign",
                    "signature": base64.b64encode(SIGNATURE).decode(),
                    "certificate_chain": base64.b64encode(CHAIN).decode(),
                    "error": None,
                },
            },
        )


def main() -> int:
    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    port = server.server_address[1]
    apns_device.ALBERT_URL = f"http://127.0.0.1:{port}/albert?device={{device_class}}"
    Handler.cert_pem = certificate_pem()

    signer = apns_device.ApnsDeviceSigner(
        url=f"http://127.0.0.1:{port}/sign", token=TOKEN, poll_interval=0.01
    )
    material = anyio.run(lambda: signer.activate(udid=UDID, serial=apns_device.DEFAULT_SERIAL))
    server.shutdown()

    info = plistlib.loads(Handler.activation_info)
    checks = {
        "polled until completed": Handler.polls == 2,
        "MacOS device class": info["DeviceClass"] == "MacOS",
        "runner product type": info["ProductType"] == "VirtualMac2,1",
        "runner product version": info["ProductVersion"] == "15.8.2",
        "runner build": info["BuildVersion"] == "24H232",
        "runner serial": info["SerialNumber"] == "ZJLGNW7555",
        "caller UDID": info["UniqueDeviceID"] == UDID,
        "CSR data present": isinstance(info["DeviceCertRequest"], bytes),
        "signature forwarded": Handler.albert_payload["FairPlaySignature"] == SIGNATURE,
        "chain forwarded": Handler.albert_payload["FairPlayCertChain"] == CHAIN,
        "activation-info forwarded": Handler.albert_payload["ActivationInfoXML"]
        == Handler.activation_info,
        "certificate stored": "BEGIN CERTIFICATE" in material.cert_pem,
        "private key stored": "BEGIN PRIVATE KEY" in material.key_pem,
        "no base token yet": material.base_token_hex is None,
    }
    for name, ok in checks.items():
        print(f"{'PASS' if ok else 'FAIL'}: {name}")
    failures = [name for name, ok in checks.items() if not ok]
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
