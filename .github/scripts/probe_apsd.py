"""Drive Apple's push identity stack (apsd) on a macOS host and show what its
signer produces for an anonymous APNs activation.

Two stages, reported independently:

* static — ``ApplePushService.framework/apsd`` links into a plain host process.
* sign — apsd signs our ``activation-info`` plist (needs the bundled agent,
  ``.github/scripts/apsd-agent.js``).

Prints the activation-info, the apsd signature and the certificate chain.
Never talks to Apple: submitting the signature to ``albert.apple.com`` is
skipped on purpose. Needs root (Frida injection) on macOS. This answers a CI
question; it is not part of the application, which uses the anonymous Windows
activation path.

    sudo python probe_apsd.py [--agent apsd-agent.js]
"""

from __future__ import annotations

import argparse
import base64
import json
import plistlib
import re
import subprocess
import sys
import tempfile
import uuid
from collections.abc import Callable
from pathlib import Path

# Run in the injected host, which has apsd.framework dlopen()ed. Deliberately
# bridge-free (Frida 17 has no global `ObjC`); the ObjC surface is exercised by
# the agent in the `sign` stage.
PROBE_JS = """
rpc.exports = {
    describe() {
        const module = Process.findModuleByName("apsd");
        return JSON.stringify({ module: module === null ? null : module.path });
    },
};
"""


def sysctl(name: str) -> str:
    return subprocess.run(
        ["sysctl", "-n", name], capture_output=True, text=True, check=False
    ).stdout.strip()


def io_platform(key: str) -> str:
    out = subprocess.run(
        ["ioreg", "-rd1", "-c", "IOPlatformExpertDevice"],
        capture_output=True,
        text=True,
        check=False,
    ).stdout
    match = re.search(rf'"{key}"\s*=\s*"?([^"\n]+)"?', out)
    return match.group(1).strip() if match else ""


def device_identity() -> dict[str, str]:
    """The identity apsd binds the activation to."""
    return {
        "hardware_version": sysctl("hw.model"),
        "software_version": sysctl("kern.osproductversion"),
        "software_build_id": sysctl("kern.osversion"),
        "serial_number": io_platform("IOPlatformSerialNumber"),
        "unique_device_id": io_platform("IOPlatformUUID"),
    }


def activation_info(device: dict[str, str]) -> bytes:
    """The `activation-info` plist apsd signs for a device activation."""
    from cryptography.hazmat.primitives.asymmetric import rsa

    key = rsa.generate_private_key(public_exponent=65537, key_size=1024)
    return plistlib.dumps(
        {
            "ActivationRandomness": str(uuid.uuid4()).upper(),
            "ActivationState": "Unactivated",
            "BuildVersion": device["software_build_id"],
            "DeviceCertRequest": certificate_request(key),
            "DeviceClass": "MacOS",
            "ProductType": device["hardware_version"],
            "ProductVersion": device["software_version"],
            "SerialNumber": device["serial_number"],
            "UniqueDeviceID": device["unique_device_id"],
        }
    )


def certificate_request(key) -> bytes:
    """CSR for the push key, SHA-1 signed like apsd's own."""
    from cryptography import x509
    from cryptography.exceptions import UnsupportedAlgorithm
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.x509.oid import NameOID

    request = x509.CertificateSigningRequestBuilder().subject_name(
        x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "Client Push Certificate")])
    )
    try:
        signed = request.sign(key, hashes.SHA1())
        return signed.public_bytes(serialization.Encoding.PEM)
    except UnsupportedAlgorithm:
        return _sha1_request_via_openssl(key)


def _sha1_request_via_openssl(key) -> bytes:
    """`cryptography` refuses SHA-1 signatures; the openssl CLI still allows it."""
    from cryptography.hazmat.primitives import serialization

    with tempfile.TemporaryDirectory() as directory:
        key_path = Path(directory, "push-key.pem")
        request_path = Path(directory, "push-cert-request.pem")
        key_path.write_bytes(
            key.private_bytes(
                serialization.Encoding.PEM,
                serialization.PrivateFormat.TraditionalOpenSSL,
                serialization.NoEncryption(),
            )
        )
        key_path.chmod(0o600)
        subprocess.run(
            [
                "openssl", "req", "-new", "-sha1", "-key", str(key_path),
                "-out", str(request_path), "-subj", "/CN=Client Push Certificate",
            ],
            check=True,
            capture_output=True,
        )
        return request_path.read_bytes()


def describe_certificate(certificate) -> str:
    from cryptography.hazmat.primitives import hashes

    valid_from = certificate.not_valid_before_utc
    valid_to = certificate.not_valid_after_utc
    return (
        f"{certificate.subject.rfc4514_string()} "
        f"| issuer {certificate.issuer.rfc4514_string()} "
        f"| serial {certificate.serial_number:x} "
        f"| valid {valid_from:%Y-%m-%d}..{valid_to:%Y-%m-%d} "
        f"| sha256 {certificate.fingerprint(hashes.SHA256()).hex()}"
    )


def split_der_chain(blob: bytes) -> list[bytes]:
    """Apple hands back the certificate chain as concatenated DER SEQUENCEs."""
    chunks: list[bytes] = []
    offset = 0
    while offset < len(blob):
        if blob[offset] != 0x30:
            raise ValueError(f"expected a DER SEQUENCE at offset {offset}")
        start = offset
        offset += 1
        length = blob[offset]
        offset += 1
        if length & 0x80:
            count = length & 0x7F
            length = int.from_bytes(blob[offset : offset + count], "big")
            offset += count
        offset += length
        if offset > len(blob):
            raise ValueError("DER SEQUENCE runs past the end of the blob")
        chunks.append(blob[start:offset])
    return chunks


def print_chain(label: str, blob: bytes) -> int:
    """Print every certificate in a DER chain; returns how many there were."""
    from cryptography import x509

    print(f"{label}: {len(blob)} bytes", flush=True)
    try:
        certificates = [
            x509.load_der_x509_certificate(der) for der in split_der_chain(blob)
        ]
    except Exception as exc:  # noqa: BLE001 - the raw blob is the fallback
        print(f"  not a DER chain ({exc}); base64: {base64.b64encode(blob).decode()}", flush=True)
        return 0
    for index, certificate in enumerate(certificates, 1):
        print(f"  [{index}] {describe_certificate(certificate)}", flush=True)
    return len(certificates)


def run_stage(name: str, step: Callable[[], str]) -> bool:
    try:
        print(f"{name}: OK - {step()}", flush=True)
        return True
    except Exception as exc:  # noqa: BLE001 - any failure is a result here
        print(f"{name}: FAIL - {exc}", flush=True)
        return False


# Apple's push/identity code, dlopen()ed into our own process so the signer can
# be driven there instead of inside the system's platform-signed daemons. These
# paths are resolved by dyld, not stat()ed: on recent macOS most of them exist
# only inside the dyld shared cache and have no file on disk.
DAEMONS = [
    "/System/Library/Frameworks/CFNetwork.framework/CFNetwork",
    "/System/Library/PrivateFrameworks/ApplePushService.framework/apsd",
    (
        "/System/Library/PrivateFrameworks/IDS.framework/identityservicesd.app"
        "/Contents/MacOS/identityservicesd"
    ),
]
CORE_FOUNDATION = "/System/Library/Frameworks/CoreFoundation.framework/CoreFoundation"


def serve() -> int:
    """Internal mode: become the injectable host, then idle on a run loop."""
    import ctypes

    for path in DAEMONS:
        try:
            ctypes.CDLL(path, mode=ctypes.RTLD_GLOBAL)
        except OSError as exc:
            print(f"cannot load {path}: {exc}", file=sys.stderr, flush=True)
            return 1
    print("ready", flush=True)
    ctypes.CDLL(CORE_FOUNDATION).CFRunLoopRun()
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--serve", action="store_true", help="internal: load the daemons and idle"
    )
    parser.add_argument("--agent", type=Path, help="bundled apsd-agent.js")
    args = parser.parse_args()
    if args.serve:
        return serve()

    import frida

    device = device_identity()
    print(
        f"identity: {device['hardware_version']} {device['software_version']} "
        f"({device['software_build_id']}) serial={device['serial_number']} "
        f"udid={device['unique_device_id']}",
        flush=True,
    )

    host = subprocess.Popen(
        [sys.executable, "-I", str(Path(__file__).resolve()), "--serve"],
        stdout=subprocess.PIPE,
        text=True,
    )
    failures = 0
    try:
        if host.stdout is None or host.stdout.readline().strip() != "ready":
            print("static: FAIL - host process did not load the daemon frameworks", flush=True)
            return 1

        try:
            session = frida.attach(host.pid)
            probe = session.create_script(PROBE_JS)
            probe.load()
            described = json.loads(probe.exports_sync.describe())
        except Exception as exc:  # noqa: BLE001 - injection failure is a result
            print(f"static: FAIL - cannot instrument the host process: {exc}", flush=True)
            return 1

        info: dict[str, bytes] = {}
        signed: dict[str, bytes] = {}

        def static() -> str:
            if described["module"] is None:
                raise RuntimeError("apsd's Mach-O is not loaded in the host process")
            return f"apsd loaded from {described['module']}"

        def sign() -> str:
            if args.agent is None:
                raise RuntimeError("no agent built, cannot sign")
            info["body"] = activation_info(device)
            agent = session.create_script(args.agent.read_text())
            agent.load()
            result = agent.exports_sync.sign(
                base64.b64encode(info["body"]).decode()
            )
            signed["signature"] = base64.b64decode(result["signature"])
            signed["chain"] = base64.b64decode(result["certificate_chain"])

            print("-- activation-info (to be apsd signed) --", flush=True)
            print(info["body"].decode("utf-8", "replace"), flush=True)
            print("-- apsd signature --", flush=True)
            print(f"bytes: {len(signed['signature'])}", flush=True)
            print(f"base64: {result['signature']}", flush=True)
            print("-- apsd certificate chain --", flush=True)
            count = print_chain("chain", signed["chain"])
            return (
                f"signature {len(signed['signature'])} bytes, "
                f"chain {len(signed['chain'])} bytes ({count} certificates)"
            )

        failures += 0 if run_stage("static", static) else 1
        failures += 0 if run_stage("sign", sign) else 1
    finally:
        host.terminate()

    print(f"verdict: {'signed' if failures == 0 else 'failed'}", flush=True)
    return 0 if failures == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
