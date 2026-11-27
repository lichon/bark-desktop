"""Drive Apple's push identity stack (apsd) on a macOS host and report whether an
anonymous APNs activation can succeed there.

Three stages, reported independently:

* static — ``ApplePushService.framework/apsd`` links into a plain host process.
* sign — apsd's FairPlay signer produces a signature over our ``activation-info``
  plist (needs Idun's built Frida agent, ``mac-helper/agent``).
* albert — ``albert.apple.com`` accepts the signature and returns a device push
  certificate.

Needs root (Frida injection) on macOS. This answers a CI question; it is not part
of the application, which uses the anonymous Windows activation path.

    sudo python probe_apsd.py --host idun-mac-host.py [--agent idun-mac-agent.js]
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
import urllib.parse
import urllib.request
import uuid
from collections.abc import Callable
from pathlib import Path
from typing import Any

ALBERT = "https://albert.apple.com/deviceservices/deviceActivation?device=MacOS"

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


def cfnetwork_version() -> str:
    info = "/System/Library/Frameworks/CFNetwork.framework/Resources/Info"
    out = subprocess.run(
        ["defaults", "read", info, "CFBundleVersion"],
        capture_output=True,
        text=True,
        check=False,
    ).stdout.strip()
    return out or "unknown"


def device_identity() -> dict[str, str]:
    """The identity Apple binds the push certificate to, as apsd reports it."""
    return {
        "hardware_version": sysctl("hw.model"),
        "software_version": sysctl("kern.osproductversion"),
        "software_build_id": sysctl("kern.osversion"),
        "serial_number": io_platform("IOPlatformSerialNumber"),
        "unique_device_id": io_platform("IOPlatformUUID"),
        "push_user_agent": (
            f"apsd (unknown version) CFNetwork/{cfnetwork_version()} "
            f"Darwin/{sysctl('kern.osrelease')}"
        ),
    }


def activation_info(device: dict[str, str]) -> bytes:
    """`activation-info` plist, shaped exactly like idun's ApnsActivation."""
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


def post_to_albert(
    info: bytes, signature: bytes, chain: bytes, user_agent: str
) -> str:
    body = plistlib.dumps(
        {
            "ActivationInfoComplete": True,
            "ActivationInfoXML": info,
            "FairPlayCertChain": chain,
            "FairPlaySignature": signature,
        }
    )
    request = urllib.request.Request(
        ALBERT,
        data=urllib.parse.urlencode({"activation-info": body.decode()}).encode(),
        headers={"User-Agent": user_agent, "Content-Type": "application/x-www-form-urlencoded"},
    )
    with urllib.request.urlopen(request, timeout=120) as response:
        return response.read().decode("utf-8", "replace")


def describe_certificate(pem: bytes) -> str:
    from cryptography import x509

    certificate = x509.load_pem_x509_certificate(pem)
    valid_from = certificate.not_valid_before_utc
    valid_to = certificate.not_valid_after_utc
    return (
        f"{certificate.subject.rfc4514_string()} "
        f"valid {valid_from:%Y-%m-%d}..{valid_to:%Y-%m-%d}"
    )


def activation_record(page: str) -> dict[str, Any] | None:
    match = re.search("<Protocol>(.*?)</Protocol>", page, re.S)
    if match is None:
        return None
    record = plistlib.loads(match.group(1).strip().encode())
    return record.get("device-activation", {}).get("activation-record")


def run_stage(name: str, step: Callable[[], str]) -> bool:
    try:
        print(f"{name}: OK - {step()}", flush=True)
        return True
    except Exception as exc:  # noqa: BLE001 - any failure is a result here
        print(f"{name}: FAIL - {exc}", flush=True)
        return False


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--host", required=True, type=Path, help="idun-mac-host.py")
    parser.add_argument("--agent", type=Path, help="built idun-mac-agent.js")
    args = parser.parse_args()

    import frida

    device = device_identity()
    print(
        f"identity: {device['hardware_version']} {device['software_version']} "
        f"({device['software_build_id']}) serial={device['serial_number']} "
        f"udid={device['unique_device_id']}",
        flush=True,
    )

    host = subprocess.Popen(
        [sys.executable, "-I", str(args.host)], stdout=subprocess.PIPE, text=True
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
            result = agent.exports_sync.fairplay_sign(
                base64.b64encode(info["body"]).decode()
            )
            signed["signature"] = base64.b64decode(result["signature"])
            signed["chain"] = base64.b64decode(result["certificate_chain"])
            return (
                f"signature {len(signed['signature'])} bytes, "
                f"chain {len(signed['chain'])} bytes"
            )

        def albert() -> str:
            if "signature" not in signed:
                raise RuntimeError("no signature to submit")
            page = post_to_albert(
                info["body"], signed["signature"], signed["chain"], device["push_user_agent"]
            )
            record = activation_record(page)
            certificate = (record or {}).get("DeviceCertificate")
            if not certificate:
                errors = re.findall(r"<b>(.*?)</b>", page, re.S)
                raise RuntimeError(f"no activation record; page says {errors[:2]}")
            return describe_certificate(certificate)

        failures += 0 if run_stage("static", static) else 1
        failures += 0 if run_stage("sign", sign) else 1
        failures += 0 if run_stage("albert", albert) else 1
    finally:
        host.terminate()

    print(f"verdict: {'usable' if failures == 0 else 'unusable'}", flush=True)
    return 0 if failures == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
