"""Link lifecycle: refusal keeps the old link, rotation, honest abandonment."""

from __future__ import annotations

import pytest

from bark_pc.bark.links import (
    ABANDON_NOTE,
    LinkManager,
    share_payload,
    validate_device_key,
)
from bark_pc.core.errors import BarkError, LinkInvalid, RegistrationRejected
from bark_pc.core.models import BarkLink, LinkState, now_ms

SERVER_A = "http://127.0.0.1:8080"
SERVER_B = "http://127.0.0.1:9090"


class FakeClient:
    def __init__(self, url, outcomes, log, on_register=None):
        self.url = url
        self.outcomes = outcomes
        self.log = log
        self.on_register = on_register

    def register(self, device_token, device_key=None, device_type="ios"):
        if self.on_register is not None:
            self.on_register(self)
        self.log.append({"url": self.url, "token": device_token, "device_key": device_key})
        outcome = self.outcomes.pop(0) if self.outcomes else "server-issued-key"
        if isinstance(outcome, BaseException):
            raise outcome
        return outcome

    def verify_key(self, key):
        outcome = self.outcomes.pop(0) if self.outcomes else True
        if isinstance(outcome, BaseException):
            raise outcome
        return outcome

    def close(self):
        return None


def make_manager(db, outcomes, *, on_register=None, log=None):
    log = log if log is not None else []
    factory = lambda url: FakeClient(url, outcomes, log, on_register)  # noqa: E731
    return LinkManager(db, client_factory=factory), log


def seed_link(db, *, url: str = SERVER_A, key: str = "oldKey123", state=LinkState.LINKED):
    link = BarkLink(
        server_url=url,
        device_key=key,
        state=state,
        created_at=now_ms() - 10_000,
        updated_at=now_ms() - 5_000,
        last_verified_at=now_ms() - 5_000,
    )
    db.upsert_link(link)
    return link


def test_register_from_scratch_creates_a_linked_row(db):
    manager, log = make_manager(db, ["freshKey"])
    link = manager.register(SERVER_A, "ab" * 32)

    assert link.state == LinkState.LINKED
    assert link.device_key == "freshKey"
    assert link.server_url == SERVER_A
    assert link.last_error is None
    assert log[0]["device_key"] is None  # no key to rotate yet
    assert db.get_link().device_key == "freshKey"


def test_refusal_keeps_the_previous_linked_row_usable(db):
    seed_link(db, key="workingKey")
    manager, _ = make_manager(db, [RegistrationRejected("server said no")])

    with pytest.raises(BarkError):
        manager.register(SERVER_A, "ab" * 32)

    link = db.get_link()
    assert link.state == LinkState.LINKED
    assert link.device_key == "workingKey"
    assert link.server_url == SERVER_A
    assert link.last_error and "no" in link.last_error
    assert link.is_usable


def test_refusal_without_a_previous_link_leaves_a_stale_row(db):
    manager, _ = make_manager(db, [RegistrationRejected("nope")])
    with pytest.raises(BarkError):
        manager.register(SERVER_A, "ab" * 32)

    link = db.get_link()
    assert link.state == LinkState.STALE
    assert link.device_key == ""
    assert link.last_error


def test_same_server_rotation_reuses_the_key_and_persists_the_new_mapping(db):
    seed_link(db, key="oldKey123")
    manager, log = make_manager(db, ["rotatedKey456"])
    link = manager.rotate("ab" * 32)

    assert log[0]["device_key"] == "oldKey123"
    assert link.device_key == "rotatedKey456"
    assert link.state == LinkState.LINKED
    assert db.get_link().device_key == "rotatedKey456"


def test_switch_server_marks_the_old_link_abandoned_and_never_claims_revocation(db):
    seed_link(db, url=SERVER_A, key="oldKey123")
    observed_states = []
    manager, log = make_manager(
        db,
        ["newServerKey"],
        on_register=lambda client: observed_states.append(db.get_link().state),
    )

    result = manager.switch_server(SERVER_B, "ab" * 32)

    assert observed_states == [LinkState.ABANDONED]  # abandoned while registering
    assert result.abandoned is not None
    assert result.abandoned.state == LinkState.ABANDONED
    assert result.abandoned.abandoned_at is not None
    assert result.abandoned.device_key == "oldKey123"
    assert result.note == ABANDON_NOTE
    assert "revoked" in result.note  # explicitly denies claiming revocation
    assert result.link.server_url == SERVER_B
    assert db.get_link().server_url == SERVER_B
    assert log[0]["device_key"] is None  # a different server gets a new key


def test_failed_switch_restores_the_previous_link(db):
    seed_link(db, url=SERVER_A, key="stillWorking")
    manager, _ = make_manager(db, [RegistrationRejected("cannot switch")])

    with pytest.raises(BarkError):
        manager.switch_server(SERVER_B, "ab" * 32)

    link = db.get_link()
    assert link.server_url == SERVER_A
    assert link.device_key == "stillWorking"
    assert link.state == LinkState.LINKED


def test_switch_to_the_same_server_is_not_an_abandonment(db):
    seed_link(db, url=SERVER_A, key="sameKey")
    manager, log = make_manager(db, ["sameKey"])
    result = manager.switch_server(SERVER_A, "ab" * 32)
    assert result.abandoned is None
    assert result.note is None
    assert log[0]["device_key"] == "sameKey"


def test_verify_updates_state(db):
    seed_link(db)
    manager, _ = make_manager(db, [True])
    assert manager.verify() is True
    link = db.get_link()
    assert link.state == LinkState.LINKED
    assert link.last_verified_at is not None

    manager, _ = make_manager(db, [False])
    assert manager.verify() is False
    assert db.get_link().state == LinkState.STALE


def test_verify_transport_failure_does_not_change_link_state(db):
    seed_link(db)
    manager, _ = make_manager(db, [BarkError("network down")])
    with pytest.raises(BarkError):
        manager.verify()
    assert db.get_link().state == LinkState.LINKED
    assert db.get_link().last_error


@pytest.mark.parametrize(
    "key",
    ["", " ", "with space", "with/slash", "x" * 161, "emoji😀"],
)
def test_invalid_device_keys_are_rejected(key):
    with pytest.raises(LinkInvalid):
        validate_device_key(key)


@pytest.mark.parametrize("key", ["abc", "A-b_c.d~e", "x" * 160])
def test_valid_device_keys_are_accepted(key):
    assert validate_device_key(key) == key


def test_share_payload_is_the_push_url(db):
    link = seed_link(db, key="shareKey")
    assert share_payload(link) == f"{SERVER_A}/shareKey"
    assert LinkManager.push_url(link) == f"{SERVER_A}/shareKey"
