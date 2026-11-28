"""The only module allowed to import `pypush`.

`pypush` 2.x is SSPL-licensed and explicitly unstable; keeping it behind this
adapter (see `contracts/pypush-adapter.md`) means the rest of the application can
survive an upstream API change or a library swap.

Activation is anonymous and two-stage: the desktop builds the activation-info
and its CSR key, `apns-device` signs the plist on a macOS runner, and the signed
plist is submitted to Apple's device-activation endpoint.  No Apple account,
password or 2FA is involved.  `pypush` is still the courier transport.
"""

from __future__ import annotations

import hashlib
import logging
import time
from collections.abc import Callable
from contextlib import asynccontextmanager

import anyio
import httpx
from cryptography import x509
from cryptography.hazmat.primitives import serialization
from pypush import apns
from pypush.apns import filters, protocol

from ..core.errors import (
    ActivationRejected,
    ActivationUnavailable,
    CourierRejected,
    PayloadMalformed,
    StreamLost,
    TokenRejected,
)
from ..core.models import ActivationMaterial, PushStatus
from . import apns_device as _apns_device
from .apns_device import ApnsDeviceSigner, certificate_fingerprint

# Re-exported for callers and tests that used the old activation hooks.
_log_activation_request = _apns_device._log_activation_request
_log_activation_response = _apns_device._log_activation_response
_activation_event_hooks = _apns_device._activation_event_hooks

logger = logging.getLogger("bark_pc.pypush")

COURIER = "1-courier.push.apple.com"
DEFAULT_TOPIC = "me.fin.bark"

class ActivationError(ActivationRejected):
    """Backwards-compatible alias used by tools and older call sites."""


def cert_fingerprint(certificate: x509.Certificate) -> str:
    return certificate_fingerprint(certificate)


def _token_prefix(token: bytes | None, length: int = 8) -> str:
    """Short, log-safe token identifier (`None` stays readable)."""
    if not token:
        return "none"
    return f"{bytes(token).hex()[:length]}…"


def message_filter(
    topic: str,
    *,
    base_token: bytes | None = None,
    scoped_token: bytes | None = None,
):
    """Broadcast filter for `SendMessageCommand`s belonging to *topic*.

    `APNsConnection.notification_stream(topic, token)` also matches
    `command.token == token`, i.e. it expects the provider to push to the
    *scoped* token minted for the connection.  A Bark server is registered with
    the device's **base** token instead (see `tools/spike_token.py`), so every
    real push carries the base token, fails that comparison, and is dropped by
    `pypush` while the courier keeps broadcasting it — the log shows
    `Broadcasting SendMessageCommand(... topic='me.fin.bark' ...)` and nothing
    reaches the app.

    Match on the topic only.  The token is reported at debug level so that an
    unexpected provider token is visible without hiding the message.
    """
    topic_hash = hashlib.sha1(topic.encode("utf-8")).digest()

    def matches(command):
        if not isinstance(command, protocol.SendMessageCommand):
            return None
        command_topic = getattr(command, "topic", None)
        if command_topic != topic:
            # Unknown topics stay raw SHA-1 hashes; compare the digest too.
            if not isinstance(command_topic, (bytes, bytearray)):
                return None
            if bytes(command_topic) != topic_hash:
                return None
        token = getattr(command, "token", None)
        if token not in (base_token, scoped_token):
            logger.debug(
                "stream: message on %s carries an unexpected token (%s; base %s, scoped %s)",
                topic,
                _token_prefix(token),
                _token_prefix(base_token),
                _token_prefix(scoped_token),
            )
        return command

    return matches


def _load_material(material: ActivationMaterial):
    certificate = x509.load_pem_x509_certificate(material.cert_pem.encode("ascii"))
    private_key = serialization.load_pem_private_key(
        material.key_pem.encode("ascii"), password=None
    )
    return certificate, private_key


class PypushReceiver:
    """`PushReceiver` implementation backed by `pypush.apns`."""

    def __init__(
        self,
        *,
        courier: str = COURIER,
        apns_device_config: Callable[[], tuple[str, str]] | None = None,
    ):
        self.courier = courier
        self._apns_device_config = apns_device_config or (lambda: ("", ""))
        self._stop_event: anyio.Event | None = None
        self._stopped = False

    # -- activation -------------------------------------------------------- #
    async def activate(self, *, udid: str, serial: str) -> ActivationMaterial:
        url, token = self._apns_device_config()
        signer = ApnsDeviceSigner(url=url, token=token)
        started = time.perf_counter()

        def elapsed() -> float:
            return time.perf_counter() - started

        logger.info(
            "activation: requesting anonymous device certificate (udid=%s, serial=%s)",
            udid,
            serial,
        )
        try:
            material = await signer.activate(udid=udid, serial=serial)
            fingerprint = cert_fingerprint(
                x509.load_pem_x509_certificate(material.cert_pem.encode("ascii"))
            )
            logger.info(
                "activation: succeeded in %.1fs (cert fingerprint %s…)",
                elapsed(),
                fingerprint[:8],
            )
            return material
        except (ActivationRejected, ActivationUnavailable):
            raise
        except httpx.TimeoutException as exc:
            logger.warning("activation: timed out after %.1fs (%s)", elapsed(), exc)
            raise ActivationUnavailable(f"activation timed out: {exc}") from exc
        except httpx.TransportError as exc:
            logger.warning("activation: transport error after %.1fs (%s)", elapsed(), exc)
            raise ActivationUnavailable(f"activation transport error: {exc}") from exc
        except Exception as exc:  # noqa: BLE001 - activation errors are mapped for the UI
            message = str(exc)
            if "Albert" in message or "activation" in message.lower():
                logger.warning("activation: rejected after %.1fs (%s)", elapsed(), message)
                logger.debug("activation failure details", exc_info=True)
                raise ActivationRejected(message) from exc
            logger.warning("activation: failed after %.1fs (%s)", elapsed(), message)
            logger.debug("activation failure details", exc_info=True)
            raise ActivationUnavailable(message) from exc

    # -- connection helpers ------------------------------------------------ #
    @asynccontextmanager
    async def _connection(self, material: ActivationMaterial):
        certificate, private_key = _load_material(material)
        token = bytes.fromhex(material.base_token_hex) if material.base_token_hex else None
        logger.debug(
            "courier: connecting to %s (cached base token=%s, sandbox=False)",
            self.courier,
            "yes" if token else "no",
        )
        async with apns.create_apns_connection(
            certificate,
            private_key,
            token=token,
            sandbox=False,
            courier=self.courier,
        ) as connection:
            logger.debug("courier: connection established")
            yield connection

    async def fetch_base_token(self, material: ActivationMaterial) -> str:
        """Connect once and return the base push token (hex) from ConnectAck."""
        logger.debug("courier: fetching base push token (udid=%s)", material.udid)
        try:
            async with self._connection(material) as connection:
                token = await connection.base_token
                logger.debug("courier: base push token received (%s…)", token.hex()[:8])
                return token.hex()
        except Exception as exc:
            logger.debug("courier: base push token failed (%s)", exc)
            raise self._map_error(exc) from exc

    # -- streaming --------------------------------------------------------- #
    async def run(
        self,
        *,
        material: ActivationMaterial,
        topic: str = DEFAULT_TOPIC,
        on_message: Callable[[bytes], None],
        on_status: Callable[[PushStatus, str | None], None] | None = None,
        on_token: Callable[[str], None] | None = None,
        on_activity: Callable[[], None] | None = None,
    ) -> None:
        """Connect, mint a scoped token, stream and ack. Raises on any failure.

        `on_activity` fires for every inbound courier command, including the
        `KeepAliveAck` that `pypush` exchanges every 30 s.  Silence therefore
        means the link is dead, not that the topic is quiet.
        """
        status = on_status or (lambda _status, _reason: None)
        self._stopped = False
        logger.debug("stream: starting (topic=%s)", topic)
        status(PushStatus.CONNECTING, None)
        try:
            async with self._connection(material) as connection:
                base_token_bytes = await connection.base_token
                base_token = base_token_bytes.hex()
                logger.debug("stream: base push token %s…", base_token[:8])
                if on_token is not None:
                    try:
                        on_token(base_token)
                    except Exception:  # pragma: no cover - callback must not break the stream
                        logger.exception("on_token callback failed")
                scoped = await connection.mint_scoped_token(topic)
                logger.debug(
                    "stream: scoped token minted for topic %s (%s)",
                    topic,
                    _token_prefix(scoped),
                )
                async with anyio.create_task_group() as group:
                    if on_activity is not None:
                        group.start_soon(self._mirror_activity, connection, on_activity)
                    stream_failure: BaseException | None = None
                    try:
                        async with self._notification_stream(
                            connection,
                            topic,
                            base_token=base_token_bytes,
                            scoped=scoped,
                        ) as stream:
                            status(PushStatus.ACTIVE, None)
                            logger.info("stream: active (topic=%s)", topic)
                            async for command in stream:
                                logger.debug(
                                    "stream: delivering message (%d bytes)",
                                    len(command.payload or b""),
                                )
                                try:
                                    on_message(command.payload)
                                except Exception:
                                    logger.exception("on_message callback failed")
                                finally:
                                    # An ack is sent even when the payload is dropped.
                                    await connection.ack(command)
                    except BaseException as exc:  # noqa: BLE001 - re-raised below
                        stream_failure = exc
                    finally:
                        group.cancel_scope.cancel()
                # Leave the task group first so the original error is not
                # wrapped in an ExceptionGroup (the supervisor maps its type).
                if stream_failure is not None:
                    raise stream_failure
        except anyio.get_cancelled_exc_class():
            raise
        except Exception as exc:
            mapped = self._map_error(exc)
            logger.info("push stream ended: %s", mapped)
            raise mapped from exc

    @asynccontextmanager
    async def _notification_stream(
        self,
        connection,
        topic: str,
        *,
        base_token: bytes | None,
        scoped: bytes | None,
    ):
        """Messages on *topic*, whichever of this device's tokens they carry.

        The scoped token is still minted (upstream's own client does the same,
        and it keeps the topic registered) but is not used as a routing filter;
        see `message_filter`.
        """
        async with connection._filter([topic]), connection._receive_stream(  # noqa: SLF001
            message_filter(topic, base_token=base_token, scoped_token=scoped),
        ) as stream:
            yield stream

    async def _mirror_activity(self, connection, on_activity: Callable[[], None]) -> None:
        """Report every inbound courier command as connection activity."""
        async with connection._receive_stream(filters.ALL, backlog=False) as commands:
            async for _command in commands:
                try:
                    on_activity()
                except Exception:  # pragma: no cover - callback isolation
                    logger.exception("on_activity callback failed")

    async def stop(self) -> None:
        """Cancel in-flight work; safe to call when nothing is running."""
        self._stopped = True
        if self._stop_event is not None:
            self._stop_event.set()
        await anyio.sleep(0)

    # -- error mapping ----------------------------------------------------- #
    def _map_error(self, exc: BaseException) -> BaseException:
        if isinstance(
            exc,
            (
                TokenRejected,
                ActivationRejected,
                ActivationUnavailable,
                StreamLost,
                CourierRejected,
                PayloadMalformed,
            ),
        ):
            return exc
        if isinstance(exc, httpx.TimeoutException):
            return ActivationUnavailable(f"timeout: {exc}")
        if isinstance(exc, httpx.TransportError):
            return ActivationUnavailable(f"transport error: {exc}")
        text = str(exc).lower()
        if "token" in text and ("reject" in text or "invalid" in text):
            return TokenRejected(str(exc))
        if "assert" in type(exc).__name__.lower():
            return CourierRejected(str(exc))
        return StreamLost(f"{type(exc).__name__}: {exc}")


def get_receiver() -> PypushReceiver:
    return PypushReceiver()
