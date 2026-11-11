"""Shared fixtures and fakes for the headless unit test suite.

Everything here avoids real sockets, real APNs and real Windows shell APIs.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import anyio
import httpx
import pytest

from bark_pc.core.config import Paths
from bark_pc.core.db import Database
from bark_pc.core.models import ActivationMaterial, Notification

FIXTURE_DIR = Path(__file__).parent / "fixtures" / "payloads"


# --------------------------------------------------------------------------- #
# Portable folders + storage
# --------------------------------------------------------------------------- #


def pytest_configure(config):
    config.addinivalue_line("markers", "windows_only: test only meaningful on Windows")


@pytest.fixture
def portable_paths(tmp_path: Path) -> Paths:
    paths = Paths(app_dir=tmp_path / "BarkDesktopClient")
    paths.ensure_dirs()
    return paths


@pytest.fixture
def settings_store(portable_paths: Paths):
    from bark_pc.core.config import SettingsStore

    return SettingsStore(portable_paths)


@pytest.fixture
def temp_db(portable_paths: Paths):
    database = Database(portable_paths.db_path)
    try:
        yield database
    finally:
        database.close()


@pytest.fixture
def db(temp_db: Database) -> Database:
    return temp_db


# --------------------------------------------------------------------------- #
# Payload fixtures
# --------------------------------------------------------------------------- #


def load_payload(name: str) -> bytes:
    path = FIXTURE_DIR / name
    return path.read_bytes()


def load_payload_json(name: str) -> dict:
    return json.loads((FIXTURE_DIR / name).read_text(encoding="utf-8"))


@pytest.fixture
def payload_loader():
    return load_payload


# --------------------------------------------------------------------------- #
# Fakes
# --------------------------------------------------------------------------- #


class FakeNotifier:
    """Records `show`/`remove` calls instead of touching WinRT."""

    def __init__(self):
        self.shown: list[Notification] = []
        self.removed: list[Notification] = []
        self.fail = False

    def show(self, notification: Notification) -> bool:
        if self.fail:
            raise RuntimeError("notifier exploded")
        self.shown.append(notification)
        return True

    def remove(self, notification: Notification) -> None:
        self.removed.append(notification)


class FakePushReceiver:
    """Scripted receiver: raises the given errors or emits statuses, then blocks."""

    def __init__(self, script=None, payloads=None, token: str = "ab" * 32):
        self.script = list(script or [])
        self.payloads = list(payloads or [])
        self.token = token
        self.calls: list[dict] = []
        self.stopped = False
        self.acks: list[bytes] = []

    async def run(self, *, material, topic, on_message, on_status, on_token=None, on_activity=None):
        self.calls.append({"material": material, "topic": topic})
        if on_token is not None:
            on_token(self.token)
        for step in self.script:
            if isinstance(step, BaseException):
                raise step
            if callable(step):
                step(on_status)
        for payload in self.payloads:
            on_message(payload)
        await anyio.sleep_forever()

    async def stop(self) -> None:
        self.stopped = True


class InMemorySecretStore:
    """Deterministic stand-in for DPAPI (base64 + prefix)."""

    PREFIX = b"MEMSECRET:"

    def protect(self, data: bytes) -> bytes:
        import base64

        return self.PREFIX + base64.b64encode(data)

    def unprotect(self, data: bytes) -> bytes:
        import base64

        if not data.startswith(self.PREFIX):
            from bark_pc.core.errors import IdentityUnreadable

            raise IdentityUnreadable()
        return base64.b64decode(data[len(self.PREFIX) :])

    def save(self, path: Path, data: bytes) -> None:
        from bark_pc.core.config import atomic_write_bytes

        atomic_write_bytes(path, self.protect(data))

    def load(self, path: Path) -> bytes:
        from bark_pc.core.errors import IdentityMissing

        if not Path(path).exists():
            raise IdentityMissing()
        return self.unprotect(Path(path).read_bytes())


class FakeBarkServer:
    """`httpx.MockTransport` based fake with a request log."""

    def __init__(self, handler=None):
        self.requests: list[httpx.Request] = []
        self.handler = handler or self.default_handler

    def default_handler(self, request: httpx.Request) -> httpx.Response:
        if request.url.path == "/register" and request.method == "POST":
            body = json.loads(request.content.decode("utf-8"))
            return httpx.Response(
                200,
                json={
                    "code": 200,
                    "message": "success",
                    "data": {
                        "key": "newkey123",
                        "device_key": "newkey123",
                        "device_token": body.get("device_token"),
                        "device_type": body.get("device_type", "ios"),
                    },
                },
            )
        if request.url.path.startswith("/register/"):
            return httpx.Response(200, json={"code": 200, "message": "success"})
        if request.url.path == "/ping":
            return httpx.Response(200, text="pong")
        if request.url.path == "/push":
            return httpx.Response(200, json={"code": 200, "message": "success"})
        return httpx.Response(404, json={"code": 404, "message": "not found"})

    def transport(self) -> httpx.MockTransport:
        def handle(request: httpx.Request) -> httpx.Response:
            self.requests.append(request)
            return self.handler(request)

        return httpx.MockTransport(handle)


@pytest.fixture
def fake_notifier() -> FakeNotifier:
    return FakeNotifier()


@pytest.fixture
def fake_db_server() -> FakeBarkServer:
    return FakeBarkServer()


@pytest.fixture
def secret_store() -> InMemorySecretStore:
    return InMemorySecretStore()


# --------------------------------------------------------------------------- #
# Activation material (self-signed certificate, no Apple involved)
# --------------------------------------------------------------------------- #


def make_activation_material(
    udid: str = "11111111-2222-3333-4444-555555555555",
    serial: str = "BARKSERIAL0001",
    token: str | None = "cd" * 32,
    days_valid: int = 30,
) -> ActivationMaterial:
    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import rsa
    from cryptography.x509.oid import NameOID

    key = rsa.generate_private_key(public_exponent=65537, key_size=1024)
    subject = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "bark-test")])
    now = datetime.now(timezone.utc)
    certificate = (
        x509.CertificateBuilder()
        .subject_name(subject)
        .issuer_name(subject)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - timedelta(minutes=1))
        .not_valid_after(now + timedelta(days=days_valid))
        .sign(key, hashes.SHA256())
    )
    cert_pem = certificate.public_bytes(serialization.Encoding.PEM).decode("ascii")
    key_pem = key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    ).decode("ascii")
    return ActivationMaterial(
        cert_pem=cert_pem,
        key_pem=key_pem,
        base_token_hex=token,
        udid=udid,
        serial=serial,
    )
