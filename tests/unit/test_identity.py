"""Activation cache: fingerprint validation, DPAPI failure paths, rate limiting."""

from __future__ import annotations

import json

import anyio
import pytest

from bark_pc.core.errors import ActivationRejected, IdentityUnreadable
from bark_pc.core.models import DeviceState
from bark_pc.push.identity import IdentityManager, fingerprint_of_pem

from ..conftest import InMemorySecretStore, make_activation_material


class FakeReceiver:
    def __init__(self, material=None, error: BaseException | None = None):
        self.material = material
        self.error = error
        self.activations = 0
        self.token_requests = 0

    async def activate(self, *, udid, serial):
        self.activations += 1
        if self.error is not None:
            raise self.error
        material = self.material or make_activation_material()
        material.udid = udid
        material.serial = serial
        return material

    async def fetch_base_token(self, material):
        self.token_requests += 1
        return "ef" * 32


def make_manager(tmp_path, db, *, receiver=None, clock=None):
    clock = clock or [0.0]
    manager = IdentityManager(
        store=InMemorySecretStore(),
        secret_path=tmp_path / "secret" / "identity.bin",
        db=db,
        receiver=receiver or FakeReceiver(),
        clock=lambda: clock[0],
    )
    return manager, clock


def test_generated_ids_are_stable(db, tmp_path):
    manager, _ = make_manager(tmp_path, db)
    first = manager.ensure_ids()
    second = manager.ensure_ids()
    assert first == second
    assert first[0] != first[1]


def test_save_and_load_roundtrip(db, tmp_path):
    manager, _ = make_manager(tmp_path, db)
    material = make_activation_material()
    manager.save(material)

    loaded = manager.load()
    assert loaded is not None
    assert loaded.cert_pem == material.cert_pem
    assert loaded.key_pem == material.key_pem
    assert loaded.base_token_hex == material.base_token_hex
    meta = db.get_identity_meta()
    assert meta.state == DeviceState.ACTIVE
    assert meta.cert_fingerprint == fingerprint_of_pem(material.cert_pem)
    assert meta.activated_at is not None


def test_blob_is_encrypted_not_plaintext(db, tmp_path):
    manager, _ = make_manager(tmp_path, db)
    manager.save(make_activation_material())
    raw = (tmp_path / "secret" / "identity.bin").read_bytes()
    assert raw.startswith(InMemorySecretStore.PREFIX)
    assert b"BEGIN CERTIFICATE" not in raw
    assert b"BEGIN PRIVATE KEY" not in raw


def test_fingerprint_mismatch_is_treated_as_absent(db, tmp_path):
    manager, _ = make_manager(tmp_path, db)
    manager.save(make_activation_material())
    meta = db.get_identity_meta()
    meta.cert_fingerprint = "deadbeef"
    db.upsert_identity_meta(meta)

    assert manager.load() is None
    assert db.get_identity_meta().state == DeviceState.ABSENT
    assert (tmp_path / "secret" / "identity.bin").exists()  # never deleted automatically


def test_missing_blob_returns_none_and_marks_absent(db, tmp_path):
    manager, _ = make_manager(tmp_path, db)
    manager.save(make_activation_material())
    (tmp_path / "secret" / "identity.bin").unlink()

    assert manager.load() is None
    assert db.get_identity_meta().state == DeviceState.ABSENT


def test_decrypt_failure_raises_without_deleting_the_blob(db, tmp_path):
    manager, _ = make_manager(tmp_path, db)
    manager.save(make_activation_material())
    path = tmp_path / "secret" / "identity.bin"
    path.write_bytes(b"corrupted-not-dpapi")

    with pytest.raises(IdentityUnreadable):
        manager.load()
    assert path.exists()
    assert db.get_identity_meta().state == DeviceState.ABSENT


def test_atomic_replace_leaves_no_partial_file(db, tmp_path):
    manager, _ = make_manager(tmp_path, db)
    manager.save(make_activation_material())
    manager.save(make_activation_material(token="aa" * 32))

    leftovers = [path for path in (tmp_path / "secret").iterdir() if path.name != "identity.bin"]
    assert leftovers == []
    assert (
        json.loads(InMemorySecretStore().load(tmp_path / "secret" / "identity.bin"))[
            "base_token_hex"
        ]
        == "aa" * 32
    )


def test_activation_is_rate_limited_to_one_attempt_per_minute(db, tmp_path):
    receiver = FakeReceiver(material=make_activation_material(token=None))
    manager, clock = make_manager(tmp_path, db, receiver=receiver)

    material = anyio.run(lambda: manager.activate())
    assert material.base_token_hex is None
    assert receiver.activations == 1

    with pytest.raises(ActivationRejected):
        anyio.run(lambda: manager.activate())
    assert receiver.activations == 1

    clock[0] = 61.0
    anyio.run(lambda: manager.activate())
    assert receiver.activations == 2
    assert manager.can_activate() is False  # the second attempt just happened


def test_activation_failure_marks_identity_invalid(db, tmp_path):
    receiver = FakeReceiver(error=ActivationRejected("Apple said no"))
    manager, _ = make_manager(tmp_path, db, receiver=receiver)
    with pytest.raises(ActivationRejected):
        anyio.run(lambda: manager.activate())
    assert db.get_identity_meta().state == DeviceState.INVALID
    assert not (tmp_path / "secret" / "identity.bin").exists()


def test_fetch_base_token_persists_the_token(db, tmp_path):
    manager, _ = make_manager(tmp_path, db)
    material = anyio.run(lambda: manager.activate())
    token = anyio.run(lambda: manager.fetch_base_token(material))
    assert token == "ef" * 32
    assert manager.load().base_token_hex == "ef" * 32


def test_update_token_and_invalidate_token(db, tmp_path):
    manager, _ = make_manager(tmp_path, db)
    material = make_activation_material(token=None)
    manager.save(material)

    manager.update_token("12" * 32)
    assert manager.load().base_token_hex == "12" * 32

    manager.invalidate_token()
    loaded = manager.load()
    assert loaded is not None
    assert loaded.base_token_hex is None
    assert loaded.cert_pem == material.cert_pem


def test_mark_active_sets_last_used(db, tmp_path):
    manager, _ = make_manager(tmp_path, db)
    manager.save(make_activation_material())
    manager.mark_active()
    meta = db.get_identity_meta()
    assert meta.state == DeviceState.ACTIVE
    assert meta.last_used_at is not None


def test_clear_removes_the_blob(db, tmp_path):
    manager, _ = make_manager(tmp_path, db)
    manager.save(make_activation_material())
    manager.clear()
    assert not (tmp_path / "secret" / "identity.bin").exists()
    assert db.get_identity_meta().state == DeviceState.ABSENT
