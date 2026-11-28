"""Anonymous APNs activation signed by the bark-supabase `apns-device` service.

The desktop builds the activation-info plist and its CSR key, sends the plist to
the `apns-device` Edge Function (which drives Apple's apsd signer on a macOS
GitHub runner), then submits the signed plist to Albert for a device
certificate.  The private key never leaves this machine.

The workflow defaults mirror the `macos-15` runner identity reported by its
logs: ``VirtualMac2,1``, macOS ``15.8.2`` build ``24H232``.
"""

from __future__ import annotations

import base64
import hashlib
import json
import logging
import plistlib
import re
import time
import uuid
from collections.abc import Callable

import anyio
import httpx
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import NameOID

from ..core.errors import ActivationRejected, ActivationUnavailable
from ..core.http_debug import alog_request, alog_response
from ..core.models import ActivationMaterial

logger = logging.getLogger("bark_pc.pypush.apns_device")

ALBERT_URL = "https://albert.apple.com/deviceservices/deviceActivation?device={device_class}"

# Defaults from the macos-15 arm64 workflow-runner logs.  The activation-info
# must look like the Mac whose apsd certificate chain signs it.
DEFAULT_DEVICE_CLASS = "MacOS"
DEFAULT_PRODUCT_TYPE = "VirtualMac2,1"
DEFAULT_PRODUCT_VERSION = "15.8.2"
DEFAULT_BUILD_VERSION = "24H232"
DEFAULT_SERIAL = "ZJLGNW7555"

DEFAULT_POLL_INTERVAL_S = 2.0
DEFAULT_SIGN_TIMEOUT_S = 600.0
DEFAULT_ALBERT_TIMEOUT_S = 60.0

# Apple answers activation with an XML plist on success and with an HTML
# wizard page containing `<b>reason</b>` on failure.  Only the reason is logged.
_BOLD_TEXT = re.compile(r"<b>(.*?)</b>", re.DOTALL)
# Activation answers wrap a multi-line plist in <Protocol>.  A rejection ships
# the same wrapper around an empty <dict/> plus a `<b>reason</b>` on the page.
_PROTOCOL_BLOCK = re.compile(r"<Protocol>(.*)</Protocol>", re.DOTALL)
_WHITESPACE = re.compile(r"\s+")


def _is_empty_protocol(body: str) -> bool:
    """True when Apple replies with a `<Protocol>` block but no activation record."""
    return bool(_PROTOCOL_BLOCK.search(body)) and "device-activation" not in body


def _summarize(text: str, limit: int = 400) -> str:
    """Collapse an upstream body to one bounded, log-safe line."""
    collapsed = _WHITESPACE.sub(" ", text or "").strip()
    if len(collapsed) > limit:
        return collapsed[:limit] + "…"
    return collapsed


def _activation_event_hooks() -> dict[str, list]:
    """Generic HTTP debug hooks plus the activation-specific analysis."""
    return {
        "request": [alog_request, _log_activation_request],
        "response": [alog_response, _log_activation_response],
    }


async def _log_activation_request(request: httpx.Request) -> None:
    logger.debug(
        "activation request: POST %s://%s%s (device=%s, %d bytes)",
        request.url.scheme,
        request.url.host,
        request.url.path,
        request.url.params.get("device", "?"),
        len(request.content),
    )


async def _log_activation_response(response: httpx.Response) -> None:
    # `response` hooks run before the body is read, so pull it into memory here
    # (httpx caches it; the caller can still use `response.text`).
    try:
        body = (await response.aread()).decode("utf-8", errors="replace")
    except Exception as exc:  # pragma: no cover - body already consumed elsewhere
        logger.debug("activation response body unavailable: %s", exc)
        return
    logger.debug("activation response: HTTP %s (%d bytes)", response.status_code, len(body))
    reason = _BOLD_TEXT.search(body)
    if response.status_code >= 400 or reason is not None or _is_empty_protocol(body):
        logger.warning(
            "Apple rejected the activation request (HTTP %s): %s",
            response.status_code,
            _summarize(reason.group(1)) if reason else _summarize(body),
        )
        if _is_empty_protocol(body):
            logger.warning(
                "Apple returned an activation plist without a device certificate "
                '(an empty <dict/>, usually "there is a problem with your device")'
            )
    logger.debug("activation response body: %s", _summarize(body, 2000))


def _certificate_request(private_key: rsa.RSAPrivateKey) -> bytes:
    """CSR for the push key, shaped like the one pypush sent to Albert."""
    request = x509.CertificateSigningRequestBuilder().subject_name(
        x509.Name(
            [
                x509.NameAttribute(NameOID.COUNTRY_NAME, "US"),
                x509.NameAttribute(NameOID.STATE_OR_PROVINCE_NAME, "CA"),
                x509.NameAttribute(NameOID.LOCALITY_NAME, "Cupertino"),
                x509.NameAttribute(NameOID.ORGANIZATION_NAME, "Apple Inc."),
                x509.NameAttribute(NameOID.ORGANIZATIONAL_UNIT_NAME, "iPhone"),
                x509.NameAttribute(NameOID.COMMON_NAME, str(uuid.uuid4())),
            ]
        )
    )
    return request.sign(private_key, hashes.SHA256()).public_bytes(serialization.Encoding.PEM)


def build_activation_info(
    *,
    udid: str,
    serial: str,
    private_key: rsa.RSAPrivateKey,
    device_class: str = DEFAULT_DEVICE_CLASS,
    product_type: str = DEFAULT_PRODUCT_TYPE,
    product_version: str = DEFAULT_PRODUCT_VERSION,
    build_version: str = DEFAULT_BUILD_VERSION,
) -> bytes:
    """The activation-info plist for *udid* and *serial*."""
    return plistlib.dumps(
        {
            "ActivationRandomness": str(uuid.uuid4()),
            "ActivationState": "Unactivated",
            "BuildVersion": build_version,
            "DeviceCertRequest": _certificate_request(private_key),
            "DeviceClass": device_class,
            "ProductType": product_type,
            "ProductVersion": product_version,
            "SerialNumber": serial,
            "UniqueDeviceID": udid,
        }
    )


def _key_to_pem(private_key: rsa.RSAPrivateKey) -> str:
    return private_key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    ).decode("ascii")


def certificate_fingerprint(certificate: x509.Certificate) -> str:
    return hashlib.sha256(certificate.public_bytes(serialization.Encoding.DER)).hexdigest()


class ApnsDeviceSigner:
    """Activate a device through the signed `apns-device` workflow."""

    def __init__(
        self,
        *,
        url: str,
        token: str,
        device_class: str = DEFAULT_DEVICE_CLASS,
        product_type: str = DEFAULT_PRODUCT_TYPE,
        product_version: str = DEFAULT_PRODUCT_VERSION,
        build_version: str = DEFAULT_BUILD_VERSION,
        poll_interval: float = DEFAULT_POLL_INTERVAL_S,
        sign_timeout: float = DEFAULT_SIGN_TIMEOUT_S,
        albert_timeout: float = DEFAULT_ALBERT_TIMEOUT_S,
        sleep: Callable[[float], object] = anyio.sleep,
        clock: Callable[[], float] = time.monotonic,
    ):
        self.url = (url or "").strip().rstrip("/")
        self.token = (token or "").strip()
        self.device_class = device_class
        self.product_type = product_type
        self.product_version = product_version
        self.build_version = build_version
        self.poll_interval = poll_interval
        self.sign_timeout = sign_timeout
        self.albert_timeout = albert_timeout
        self._sleep = sleep
        self.clock = clock

    @property
    def configured(self) -> bool:
        return bool(self.url and self.token)

    async def activate(self, *, udid: str, serial: str) -> ActivationMaterial:
        if not self.configured:
            raise ActivationUnavailable(
                "APNs device signing is not configured. Set the apns-device URL "
                "and key in Settings → Maintenance."
            )
        started = time.perf_counter()

        def elapsed() -> float:
            return time.perf_counter() - started

        private_key = rsa.generate_private_key(public_exponent=65537, key_size=1024)
        activation_info = build_activation_info(
            udid=udid,
            serial=serial,
            private_key=private_key,
            device_class=self.device_class,
            product_type=self.product_type,
            product_version=self.product_version,
            build_version=self.build_version,
        )
        logger.debug("activation: activation-info is %d bytes", len(activation_info))

        try:
            signature, chain = await self._sign(activation_info)
            logger.debug(
                "activation: signed (%d-byte signature, %d-byte chain)",
                len(signature),
                len(chain),
            )
            certificate = await self._submit_albert(activation_info, signature, chain)
        except ActivationRejected as exc:
            logger.warning("activation: rejected after %.1fs (%s)", elapsed(), exc)
            logger.debug("activation failure details", exc_info=True)
            raise
        except ActivationUnavailable as exc:
            logger.warning("activation: failed after %.1fs (%s)", elapsed(), exc)
            logger.debug("activation failure details", exc_info=True)
            raise
        except Exception as exc:  # noqa: BLE001 - mapped for the UI below
            logger.warning("activation: failed after %.1fs (%s)", elapsed(), exc)
            logger.debug("activation failure details", exc_info=True)
            raise ActivationUnavailable(f"activation failed: {exc}") from exc

        material = ActivationMaterial(
            cert_pem=certificate.public_bytes(serialization.Encoding.PEM).decode("ascii"),
            key_pem=_key_to_pem(private_key),
            base_token_hex=None,
            udid=udid,
            serial=serial,
        )
        return material

    # -- signing service --------------------------------------------------- #
    def _headers(self) -> dict[str, str]:
        return {
            "authorization": f"Bearer {self.token}",
            "accept": "application/json",
            "content-type": "application/json",
        }

    async def _sign(self, activation_info: bytes) -> tuple[bytes, bytes]:
        payload = json.dumps({"activation_info": base64.b64encode(activation_info).decode("ascii")})
        timeout = httpx.Timeout(self.sign_timeout)
        async with httpx.AsyncClient(
            timeout=timeout, event_hooks={"request": [alog_request], "response": [alog_response]}
        ) as client:
            response = await client.post(self.url, content=payload, headers=self._headers())
            data = self._envelope(response)
            run_id = data.get("id")
            if not isinstance(run_id, int):
                raise ActivationUnavailable("apns-device did not return a workflow run id")
            logger.debug("activation: signing workflow %s", data.get("run_url") or run_id)

            deadline = self.clock() + self.sign_timeout
            while self.clock() < deadline:
                await self._sleep(self.poll_interval)
                response = await client.get(
                    f"{self.url}/{run_id}", headers=self._headers(), timeout=timeout
                )
                data = self._envelope(response)
                if response.status_code == 200:
                    return self._signature(data)
            raise ActivationUnavailable("timed out waiting for the apns-device signature")

    def _envelope(self, response: httpx.Response) -> dict:
        try:
            body = response.json()
        except ValueError as exc:
            raise ActivationUnavailable(
                f"apns-device returned HTTP {response.status_code} with a non-JSON body"
            ) from exc
        if not isinstance(body, dict):
            raise ActivationUnavailable("apns-device returned an invalid response")
        data = body.get("data")
        if not isinstance(data, dict):
            data = {}
        if response.status_code not in (200, 202):
            message = body.get("message") or f"HTTP {response.status_code}"
            raise ActivationUnavailable(f"apns-device signing failed: {message}")
        return data

    @staticmethod
    def _signature(data: dict) -> tuple[bytes, bytes]:
        error = data.get("error")
        if isinstance(error, str) and error:
            raise ActivationUnavailable(f"apns-device signing failed: {error}")
        signature = data.get("signature")
        chain = data.get("certificate_chain")
        if not isinstance(signature, str) or not isinstance(chain, str):
            raise ActivationUnavailable("apns-device returned no signature")
        try:
            return base64.b64decode(signature, validate=True), base64.b64decode(
                chain, validate=True
            )
        except ValueError as exc:
            raise ActivationUnavailable("apns-device returned a malformed signature") from exc

    # -- Apple activation -------------------------------------------------- #
    async def _submit_albert(
        self, activation_info: bytes, signature: bytes, chain: bytes
    ) -> x509.Certificate:
        payload = plistlib.dumps(
            {
                "ActivationInfoComplete": True,
                "ActivationInfoXML": activation_info,
                "FairPlayCertChain": chain,
                "FairPlaySignature": signature,
            }
        ).decode("utf-8")
        async with httpx.AsyncClient(
            timeout=self.albert_timeout, event_hooks=_activation_event_hooks()
        ) as client:
            response = await client.post(
                ALBERT_URL.format(device_class=self.device_class),
                data={"activation-info": payload},
            )
        text = response.text
        if response.status_code >= 400:
            reason = _BOLD_TEXT.search(text)
            raise ActivationUnavailable(
                f"Albert returned HTTP {response.status_code}: "
                f"{_summarize(reason.group(1)) if reason else _summarize(text)}"
            )
        if _is_empty_protocol(text):
            raise ActivationRejected(
                "Apple returned an activation record without a device certificate. "
                "Try again in a minute."
            )
        protocol = _PROTOCOL_BLOCK.search(text)
        if protocol is None:
            reason = _BOLD_TEXT.search(text)
            raise ActivationRejected(
                _summarize(reason.group(1)) if reason else "Apple rejected the activation."
            )
        try:
            record = plistlib.loads(protocol.group(1).encode("utf-8"))
            certificate_pem = record["device-activation"]["activation-record"]["DeviceCertificate"]
            if isinstance(certificate_pem, str):
                certificate_pem = certificate_pem.encode("ascii")
            return x509.load_pem_x509_certificate(certificate_pem)
        except (KeyError, TypeError, ValueError, plistlib.InvalidFileException) as exc:
            raise ActivationRejected(
                "Apple returned an activation record without a device certificate."
            ) from exc
