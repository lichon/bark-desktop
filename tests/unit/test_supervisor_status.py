"""Supervisor status transitions, backoff, give-up rules and stop semantics."""

from __future__ import annotations

import logging
import time

import anyio
import pytest

from bark_pc.core.errors import ActivationRejected, CourierRejected, StreamLost, TokenRejected
from bark_pc.core.models import PushStatus
from bark_pc.push.supervisor import BackoffPolicy, PushSupervisor

from ..conftest import FakePushReceiver, make_activation_material

MATERIAL = make_activation_material()


def build(receiver, *, statuses, sleeps, random_value=0.5, **kwargs):
    async def fake_sleep(delay):
        sleeps.append(delay)

    return PushSupervisor(
        receiver,
        material_getter=lambda: MATERIAL,
        topic="me.fin.bark",
        on_message=lambda payload: None,
        on_status=lambda status, reason: statuses.append((status, reason)),
        sleep=fake_sleep,
        sleep_step=1000.0,
        random_fn=lambda: random_value,
        clock=lambda: 0.0,
        **kwargs,
    )


def test_backoff_grows_exponentially_and_caps_at_sixty_seconds():
    policy = BackoffPolicy()
    delays = [policy.next_delay(lambda: 0.5) for _ in range(8)]
    assert delays == [1.0, 2.0, 4.0, 8.0, 16.0, 32.0, 60.0, 60.0]


def test_backoff_jitter_stays_within_twenty_percent():
    low = BackoffPolicy().next_delay(lambda: 0.0)
    high = BackoffPolicy().next_delay(lambda: 1.0)
    assert 0.8 <= low <= 1.0
    assert 1.0 <= high <= 1.2
    assert low != high


def test_reset_restarts_the_sequence():
    policy = BackoffPolicy()
    policy.next_delay(lambda: 0.5)
    policy.next_delay(lambda: 0.5)
    policy.reset()
    assert policy.next_delay(lambda: 0.5) == 1.0


def test_degraded_schedules_a_growing_reconnect_delay():
    statuses: list[tuple[PushStatus, str | None]] = []
    sleeps: list[float] = []
    receiver = FakePushReceiver(script=[StreamLost("connection reset")])
    supervisor = build(receiver, statuses=statuses, sleeps=sleeps)

    anyio.run(supervisor.run_loop, 3)

    assert sleeps == [1.0, 2.0]  # third attempt returns without sleeping
    assert len(receiver.calls) == 3
    outcomes = [status for status, _reason in statuses]
    assert outcomes.count(PushStatus.DEGRADED) >= 2
    assert outcomes[-1] == PushStatus.DEGRADED


def test_three_consecutive_connect_failures_end_in_invalid_without_a_retry_storm():
    statuses: list[tuple[PushStatus, str | None]] = []
    sleeps: list[float] = []
    receiver = FakePushReceiver(script=[CourierRejected("rejected")])
    supervisor = build(receiver, statuses=statuses, sleeps=sleeps, max_connect_failures=3)

    anyio.run(supervisor.run_loop, 10)

    assert len(receiver.calls) == 3
    assert sleeps == [1.0, 2.0]
    assert statuses[-1][0] == PushStatus.INVALID
    assert "rejected" in (statuses[-1][1] or "")


def test_activation_rejections_also_count_towards_giving_up():
    statuses: list[tuple[PushStatus, str | None]] = []
    receiver = FakePushReceiver(script=[ActivationRejected("apple said no")])
    supervisor = build(receiver, statuses=statuses, sleeps=[], max_connect_failures=2)

    anyio.run(supervisor.run_loop, 10)

    assert len(receiver.calls) == 2
    assert statuses[-1][0] == PushStatus.INVALID


def test_token_rejection_invalidates_once_reactivates_once_then_gives_up():
    statuses: list[tuple[PushStatus, str | None]] = []
    calls = {"invalidate": 0, "reactivate": 0}

    async def reactivate():
        calls["reactivate"] += 1

    receiver = FakePushReceiver(script=[TokenRejected("token rejected")])
    supervisor = build(
        receiver,
        statuses=statuses,
        sleeps=[],
        on_invalidate_token=lambda: calls.__setitem__("invalidate", calls["invalidate"] + 1),
        on_reactivate=reactivate,
    )

    anyio.run(supervisor.run_loop, 10)

    assert calls == {"invalidate": 1, "reactivate": 1}
    assert len(receiver.calls) == 3
    assert statuses[-1][0] == PushStatus.INVALID


def test_missing_material_turns_invalid_immediately():
    statuses: list[tuple[PushStatus, str | None]] = []
    receiver = FakePushReceiver()
    supervisor = PushSupervisor(
        receiver,
        material_getter=lambda: None,
        topic="me.fin.bark",
        on_message=lambda payload: None,
        on_status=lambda status, reason: statuses.append((status, reason)),
        sleep=lambda delay: None,
        clock=lambda: 0.0,
    )

    anyio.run(supervisor.run_loop)

    assert receiver.calls == []
    assert statuses[-1][0] == PushStatus.INVALID
    assert "identity" in (statuses[-1][1] or "").lower()


def test_stop_cancels_an_in_flight_connection():
    statuses: list[tuple[PushStatus, str | None]] = []
    receiver = FakePushReceiver()  # blocks forever once connected
    supervisor = PushSupervisor(
        receiver,
        material_getter=lambda: MATERIAL,
        topic="me.fin.bark",
        on_message=lambda payload: None,
        on_status=lambda status, reason: statuses.append((status, reason)),
    )
    supervisor.start()

    deadline = time.monotonic() + 5
    while not receiver.calls and time.monotonic() < deadline:
        time.sleep(0.01)
    assert receiver.calls, "receiver never started"

    supervisor.stop(timeout=5.0)

    assert supervisor.is_running is False
    assert statuses[-1][0] == PushStatus.STOPPED


def test_stop_does_not_wait_for_the_join_timeout():
    """Quit must not stall: the cancel is delivered on the receiver's loop."""
    receiver = FakePushReceiver()  # blocks forever once connected
    supervisor = PushSupervisor(
        receiver,
        material_getter=lambda: MATERIAL,
        topic="me.fin.bark",
        on_message=lambda _payload: None,
        on_status=lambda _status, _reason: None,
    )
    supervisor.start()

    deadline = time.monotonic() + 5
    while not receiver.calls and time.monotonic() < deadline:
        time.sleep(0.01)
    assert receiver.calls, "receiver never started"

    started = time.monotonic()
    supervisor.stop(timeout=5.0)
    elapsed = time.monotonic() - started

    assert supervisor.is_running is False
    assert elapsed < 1.0, f"stop took {elapsed:.2f} s"


@pytest.mark.parametrize("error", [StreamLost("lost"), CourierRejected("nope")])
def test_error_reasons_are_reported(error):
    statuses: list[tuple[PushStatus, str | None]] = []
    receiver = FakePushReceiver(script=[error])
    supervisor = build(receiver, statuses=statuses, sleeps=[], max_connect_failures=5)
    anyio.run(supervisor.run_loop, 1)
    reasons = [reason for _status, reason in statuses if reason]
    assert any(str(error) in reason for reason in reasons)


# --------------------------------------------------------------------------- #
# Stall watchdog: silence means a dead link, keepalive traffic means a quiet one
# --------------------------------------------------------------------------- #


class KeepaliveReceiver:
    """Stays connected and reports inbound keepalive acks as activity."""

    def __init__(self, ping_interval: float = 0.01):
        self.ping_interval = ping_interval
        self.calls = 0

    async def run(self, *, material, topic, on_message, on_status, on_token=None, on_activity=None):
        self.calls += 1
        on_status(PushStatus.ACTIVE, None)
        assert on_activity is not None, "supervisor must request an activity callback"
        while True:
            await anyio.sleep(self.ping_interval)
            on_activity()


class SilentReceiver:
    """Stays 'connected' but never produces traffic (half-open socket)."""

    def __init__(self):
        self.calls = 0

    async def run(self, *, material, topic, on_message, on_status, on_token=None, on_activity=None):
        self.calls += 1
        on_status(PushStatus.ACTIVE, None)
        await anyio.sleep_forever()


def watchdog_supervisor(receiver, **kwargs):
    supervisor = PushSupervisor(
        receiver,
        material_getter=lambda: MATERIAL,
        topic="me.fin.bark",
        on_message=lambda payload: None,
        on_status=lambda status, reason: None,
        stall_timeout=kwargs.pop("stall_timeout", 0.12),
        backoff=BackoffPolicy(initial=0.01, maximum=0.05),
        **kwargs,
    )
    supervisor.watchdog_interval = 0.02
    return supervisor


def run_for(supervisor, seconds: float) -> None:
    async def scenario():
        async with anyio.create_task_group() as group:
            group.start_soon(supervisor.run_loop)
            await anyio.sleep(seconds)
            supervisor.stop()

    anyio.run(scenario)


def test_keepalive_activity_prevents_stall_reconnects(caplog):
    receiver = KeepaliveReceiver()
    supervisor = watchdog_supervisor(receiver, stall_timeout=0.12)

    with caplog.at_level(logging.WARNING, logger="bark_pc.supervisor"):
        run_for(supervisor, 0.4)

    assert receiver.calls == 1
    assert "reconnecting" not in caplog.text


def test_silence_beyond_the_stall_timeout_reconnects(caplog):
    receiver = SilentReceiver()
    supervisor = watchdog_supervisor(receiver, stall_timeout=0.05)

    with caplog.at_level(logging.WARNING, logger="bark_pc.supervisor"):
        run_for(supervisor, 0.4)

    assert receiver.calls >= 2
    assert "no courier traffic" in caplog.text
