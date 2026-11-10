"""Activation identity cache: build, persist, load, validate.

Secrets live only in the DPAPI blob (`data/identity.bin`); the database stores a
certificate fingerprint and non-secret identifiers.  A new activation replaces
the blob atomically, and a fingerprint mismatch is treated as "no identity"
rather than as a corrupt row.
"""

from __future__ import annotations

import hashlib
import json
import logging
import time
import uuid
from pathlib import Path

from cryptography import x509

from ..core.errors import ActivationRejected, IdentityMissing, IdentityUnreadable
from ..core.models import ActivationMaterial, DeviceIdentity, DeviceState
from .pypush_client import ActivationError, PypushReceiver, cert_fingerprint

logger = logging.getLogger("bark_pc.identity")

ACTIVATION_RATE_LIMIT_S = 60.0


def generate_device_ids() -> tuple[str, str]:
    """UDID and serial are generated once and reused for the identity's life."""
    return str(uuid.uuid4()), "WindowSerial"


def fingerprint_of_pem(cert_pem: str) -> str:
    certificate = x509.load_pem_x509_certificate(cert_pem.encode("ascii"))
    return cert_fingerprint(certificate)


class IdentityManager:
    """Owns `data/identity.bin` and the matching `device_identity` row."""

    def __init__(
        self,
        *,
        store,
        secret_path: Path,
        db,
        receiver: PypushReceiver | None = None,
        clock=time.monotonic,
    ):
        self.store = store
        self.secret_path = Path(secret_path)
        self.db = db
        self.receiver = receiver or PypushReceiver()
        self.clock = clock
        self._last_attempt_at: float | None = None

    # -- ids --------------------------------------------------------------- #
    def ensure_ids(self) -> tuple[str, str]:
        meta = self.db.get_identity_meta()
        if meta and meta.udid and meta.serial:
            return meta.udid, meta.serial
        udid, serial = generate_device_ids()
        identity = meta or DeviceIdentity()
        identity.udid = udid
        identity.serial = serial
        identity.device_class = "Windows"
        if identity.state == DeviceState.ABSENT:
            identity.state = DeviceState.ABSENT
        self.db.upsert_identity_meta(identity)
        return udid, serial

    # -- persistence ------------------------------------------------------- #
    def save(self, material: ActivationMaterial) -> DeviceIdentity:
        fingerprint = fingerprint_of_pem(material.cert_pem)
        payload = json.dumps(material.to_json(), separators=(",", ":")).encode("utf-8")
        self.store.save(self.secret_path, payload)
        identity = self.db.get_identity_meta() or DeviceIdentity()
        identity.udid = material.udid or identity.udid
        identity.serial = material.serial or identity.serial
        identity.cert_fingerprint = fingerprint
        identity.state = DeviceState.ACTIVE
        identity.activated_at = identity.activated_at or int(time.time() * 1000)
        identity.last_error = None
        self.db.upsert_identity_meta(identity)
        logger.info("identity saved (fingerprint %s…)", fingerprint[:8])
        return identity

    def load(self) -> ActivationMaterial | None:
        """Load and validate cached material. Returns None when unusable."""
        meta = self.db.get_identity_meta()
        if not self.secret_path.exists():
            if meta and meta.state == DeviceState.ACTIVE:
                self.db.set_identity_state(DeviceState.ABSENT, last_error="identity file missing")
            return None
        try:
            raw = self.store.load(self.secret_path)
            data = json.loads(raw.decode("utf-8"))
            material = ActivationMaterial.from_json(data)
        except IdentityMissing:
            return None
        except (IdentityUnreadable, ValueError) as exc:
            # Never delete the blob: the user may be migrating the folder.
            logger.warning("identity blob unreadable: %s", exc)
            self.db.set_identity_state(DeviceState.ABSENT, last_error=str(exc))
            raise
        if not material.cert_pem or not material.key_pem:
            logger.warning("identity blob is incomplete; treating as absent")
            self.db.set_identity_state(DeviceState.ABSENT, last_error="incomplete identity")
            return None
        try:
            fingerprint = fingerprint_of_pem(material.cert_pem)
        except Exception as exc:  # pragma: no cover - malformed certificate
            logger.warning("identity certificate cannot be parsed: %s", exc)
            self.db.set_identity_state(DeviceState.ABSENT, last_error=str(exc))
            return None
        if meta is None or meta.cert_fingerprint != fingerprint:
            logger.warning("identity fingerprint mismatch; treating as absent")
            self.db.set_identity_state(DeviceState.ABSENT, last_error="fingerprint mismatch")
            return None
        return material

    def clear(self) -> None:
        try:
            self.secret_path.unlink(missing_ok=True)
        except OSError as exc:  # pragma: no cover - defensive
            logger.warning("could not remove identity blob: %s", exc)
        self.db.set_identity_state(DeviceState.ABSENT, last_error="identity removed")

    # -- activation -------------------------------------------------------- #
    def can_activate(self) -> bool:
        if self._last_attempt_at is None:
            return True
        return (self.clock() - self._last_attempt_at) >= ACTIVATION_RATE_LIMIT_S

    @property
    def seconds_until_activation(self) -> float:
        if self._last_attempt_at is None:
            return 0.0
        remaining = ACTIVATION_RATE_LIMIT_S - (self.clock() - self._last_attempt_at)
        return max(0.0, remaining)

    async def activate(
        self, *, udid: str | None = None, serial: str | None = None
    ) -> ActivationMaterial:
        if not self.can_activate():
            wait = int(self.seconds_until_activation) + 1
            logger.warning("activation throttled: retry in %ss", wait)
            raise ActivationRejected(f"Please wait {wait}s before retrying activation.")
        self._last_attempt_at = self.clock()
        udid = udid or self.ensure_ids()[0]
        serial = serial or self.db.get_identity_meta().serial
        logger.info("activation: starting (udid=%s, serial=%s)", udid, serial)
        meta = self.db.get_identity_meta()
        if meta is not None:
            meta.state = DeviceState.ACTIVATING
            meta.last_error = None
            self.db.upsert_identity_meta(meta)
        try:
            material = await self.receiver.activate(udid=udid, serial=serial)
        except ActivationError as exc:
            logger.warning("activation: rejected by Apple (%s)", exc)
            self.db.set_identity_state(DeviceState.INVALID, last_error="activation rejected")
            raise
        except Exception as exc:
            logger.warning("activation: failed (%s: %s)", type(exc).__name__, exc)
            self.db.set_identity_state(DeviceState.INVALID, last_error=str(exc))
            raise
        logger.info("activation: certificate stored (udid=%s)", material.udid)
        self.save(material)
        return material

    async def fetch_base_token(self, material: ActivationMaterial) -> str:
        token = await self.receiver.fetch_base_token(material)
        material.base_token_hex = token
        self.save(material)
        return token

    def invalidate_token(self) -> None:
        """Drop the cached base token before a reconnect/re-activation."""
        try:
            material = self.load()
        except IdentityUnreadable:
            return
        if material is None:
            return
        material.base_token_hex = None
        self.save(material)

    def update_token(self, base_token_hex: str) -> None:
        if not base_token_hex:
            return
        try:
            material = self.load()
        except IdentityUnreadable:
            return
        if material is None or material.base_token_hex == base_token_hex:
            return
        material.base_token_hex = base_token_hex
        self.save(material)
        logger.info("base push token refreshed (%s…)", base_token_hex[:8])

    def mark_active(self) -> None:
        self.db.set_identity_state(
            DeviceState.ACTIVE,
            last_error=None,
            last_used_at=int(time.time() * 1000),
        )


def material_fingerprint(material: ActivationMaterial) -> str:
    return hashlib.sha256(material.cert_pem.encode("utf-8")).hexdigest()
