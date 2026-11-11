"""Receiver supervision: status transitions, backoff, and stall detection.

The adapter owns a single long-lived courier connection and reports nothing about
its own keepalive; the supervisor therefore infers connection health, applies
capped exponential backoff with jitter, and decides when the link needs user
action (`invalid`) instead of retrying forever.
"""

from __future__ import annotations

import logging
import random
import threading
import time
from dataclasses import dataclass, field
from inspect import Parameter, signature

import anyio

from ..core.errors import (
    ActivationRejected,
    BarkError,
    CourierRejected,
    TokenRejected,
)
from ..core.models import ActivationMaterial, PushStatus

logger = logging.getLogger("bark_pc.supervisor")

STALL_TIMEOUT_S = 90.0
MAX_CONNECT_FAILURES = 3


@dataclass
class BackoffPolicy:
    """1 s → 60 s capped exponential backoff with ±20 % jitter."""

    initial: float = 1.0
    maximum: float = 60.0
    jitter_ratio: float = 0.2
    attempt: int = field(default=0, init=False)

    def reset(self) -> None:
        self.attempt = 0

    def next_delay(self, random_fn=random.random) -> float:
        base = min(self.maximum, self.initial * (2**self.attempt))
        self.attempt += 1
        jitter = base * self.jitter_ratio * (random_fn() * 2.0 - 1.0)
        return max(0.0, min(self.maximum, base + jitter))


def _accepts_keyword(func, name: str) -> bool:
    """True when *func* takes *name* (or ``**kwargs``); older receivers lack it."""
    try:
        parameters = signature(func).parameters
    except (TypeError, ValueError):  # pragma: no cover - builtins and partials
        return False
    if name in parameters:
        return True
    return any(parameter.kind is Parameter.VAR_KEYWORD for parameter in parameters.values())


def _running_asyncio_loop():
    """The current event loop, or None when a non-asyncio backend is in use."""
    try:
        import asyncio

        return asyncio.get_running_loop()
    except RuntimeError:  # pragma: no cover - trio backend or no loop
        return None


class PushSupervisor:
    """Runs the receiver on its own thread and publishes typed status events."""

    def __init__(
        self,
        receiver,
        *,
        material_getter,
        topic: str,
        on_message,
        on_status,
        on_token=None,
        on_invalidate_token=None,
        on_reactivate=None,
        backoff: BackoffPolicy | None = None,
        sleep=None,
        clock=time.monotonic,
        random_fn=random.random,
        stall_timeout: float = STALL_TIMEOUT_S,
        max_connect_failures: int = MAX_CONNECT_FAILURES,
        sleep_step: float = 0.25,
    ):
        self.receiver = receiver
        self.material_getter = material_getter
        self.topic = topic
        self.on_message = on_message
        self.on_status = on_status
        self.on_token = on_token
        self.on_invalidate_token = on_invalidate_token
        self.on_reactivate = on_reactivate
        self.backoff = backoff or BackoffPolicy()
        self._sleep = sleep or anyio.sleep
        self.clock = clock
        self.random_fn = random_fn
        self.stall_timeout = stall_timeout
        self.watchdog_interval = min(15.0, max(1.0, self.stall_timeout / 3))
        self.max_connect_failures = max_connect_failures
        self.sleep_step = max(0.01, float(sleep_step))

        self._stop_requested = threading.Event()
        # APNs needs the activated device material; Supabase Realtime does not.
        self._requires_material = bool(getattr(receiver, "needs_material", True))
        self._receiver_reports_activity = _accepts_keyword(
            getattr(receiver, "run", None), "on_activity"
        )
        self._thread: threading.Thread | None = None
        self._cancel_scope = None
        self._loop = None  # asyncio loop of the receiver thread (for thread-safe cancel)
        self._last_activity = clock()
        self._connect_failures = 0
        self._token_rejections = 0
        self.status = PushStatus.STOPPED

    # -- public API -------------------------------------------------------- #
    def start(self) -> None:
        if self._thread is not None and self._thread.is_alive():
            return
        self._stop_requested.clear()
        self._thread = threading.Thread(
            target=self._thread_main, name="bark-push-receiver", daemon=True
        )
        self._thread.start()

    def stop(self, timeout: float = 5.0) -> None:
        self._stop_requested.set()
        self._request_cancellation()
        thread, self._thread = self._thread, None
        if thread is not None:
            thread.join(timeout=timeout)
            if thread.is_alive():  # pragma: no cover - daemon thread, abandoned
                logger.warning("receiver thread still running after %.1f s", timeout)
        self._emit(PushStatus.STOPPED, None)

    def _request_cancellation(self) -> None:
        """Cancel the in-flight connection from the *event loop* thread.

        `CancelScope.cancel()` needs a running loop (it reads the current task),
        so calling it from the shutdown thread raises `RuntimeError` — which the
        old `except Exception: pass` swallowed.  The connection was therefore
        never cancelled, the receiver stayed blocked in its socket read, and the
        caller burned its whole join timeout (~5 s of slow quit).  Handing the
        cancel to the loop delivers it at the receiver's next await instead.
        """
        scope = self._cancel_scope
        if scope is None:
            return
        loop = self._loop
        try:
            if loop is not None and loop.is_running():
                loop.call_soon_threadsafe(self._safe_cancel, scope)
                return
        except RuntimeError:  # pragma: no cover - loop already closed
            pass
        self._safe_cancel(scope)

    @staticmethod
    def _safe_cancel(scope) -> None:
        try:
            scope.cancel()
        except Exception:  # pragma: no cover - already closed
            pass

    @property
    def is_running(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def join(self, timeout: float | None = None) -> None:
        if self._thread is not None:
            self._thread.join(timeout=timeout)

    # -- thread plumbing --------------------------------------------------- #
    def _thread_main(self) -> None:  # pragma: no cover - exercised via async loop tests
        try:
            anyio.run(self.run_loop)
        except Exception:
            logger.exception("supervisor thread crashed")
            self._emit(PushStatus.INVALID, "internal error")
        finally:
            self._loop = None

    def _emit(self, status: PushStatus, reason: str | None = None) -> None:
        self.status = status
        self._last_activity = self.clock()
        if status == PushStatus.ACTIVE:
            self._connect_failures = 0
            self._token_rejections = 0
            self.backoff.reset()
        try:
            self.on_status(status, reason)
        except Exception:  # pragma: no cover - callback must not break supervision
            logger.exception("on_status callback failed")

    def _note_activity(self) -> None:
        self._last_activity = self.clock()

    # -- main loop --------------------------------------------------------- #
    async def run_loop(self, max_attempts: int | None = None) -> None:
        """Supervision loop; returns when stopped or when the link turns invalid."""
        self._loop = _running_asyncio_loop()
        attempts = 0
        while not self._stop_requested.is_set():
            attempts += 1
            material = self._material() if self._requires_material else None
            if self._requires_material and material is None:
                self._emit(PushStatus.INVALID, "Device identity missing")
                return

            self._emit(PushStatus.CONNECTING, None)
            try:
                await self._run_connection(material)
                # Normal return means the receiver closed without error.
                if not self._stop_requested.is_set():
                    self._emit(PushStatus.DEGRADED, "stream closed")
                    await self._sleep_with_stop(self.backoff.next_delay(self.random_fn))
                continue
            except TokenRejected as exc:
                if not await self._handle_token_rejection(exc):
                    return
                continue
            except (ActivationRejected, CourierRejected) as exc:
                self._connect_failures += 1
                if self._connect_failures >= self.max_connect_failures:
                    logger.warning("giving up after %s failures: %s", self._connect_failures, exc)
                    self._emit(PushStatus.INVALID, str(exc))
                    return
                self._emit(PushStatus.DEGRADED, str(exc))
                if max_attempts is not None and attempts >= max_attempts:
                    return
                await self._sleep_with_stop(self.backoff.next_delay(self.random_fn))
                continue
            except BarkError as exc:
                if self._stop_requested.is_set():
                    return
                self._emit(PushStatus.DEGRADED, str(exc))
                if max_attempts is not None and attempts >= max_attempts:
                    return
                await self._sleep_with_stop(self.backoff.next_delay(self.random_fn))
                continue
            except anyio.get_cancelled_exc_class():
                if self._stop_requested.is_set():
                    return
                self._emit(PushStatus.DEGRADED, "stream cancelled")
                await self._sleep_with_stop(self.backoff.next_delay(self.random_fn))
                continue

        self._emit(PushStatus.STOPPED, None)

    async def _run_connection(self, material: ActivationMaterial | None) -> None:
        failure: BaseException | None = None
        async with anyio.create_task_group() as task_group:
            self._cancel_scope = task_group.cancel_scope
            task_group.start_soon(self._watchdog, task_group.cancel_scope)

            def handle_message(payload: bytes) -> None:
                self._note_activity()
                try:
                    self.on_message(payload)
                except Exception:  # pragma: no cover - isolated per message
                    logger.exception("message handler failed")

            def handle_status(status: PushStatus, reason: str | None) -> None:
                self._note_activity()
                self._emit(status, reason)

            run_kwargs = {
                "material": material,
                "topic": self.topic,
                "on_message": handle_message,
                "on_status": handle_status,
                "on_token": self.on_token,
            }
            if self._receiver_reports_activity:
                run_kwargs["on_activity"] = self._note_activity

            try:
                await self.receiver.run(**run_kwargs)
            except BaseException as exc:  # noqa: BLE001 - re-raised below
                failure = exc
            finally:
                self._cancel_scope = None
                task_group.cancel_scope.cancel()
        if failure is not None:
            raise failure

    async def _watchdog(self, cancel_scope) -> None:
        """Reconnect when *nothing* arrives for `stall_timeout`.

        `pypush` sends a keepalive and receives its ack every 30 s, and the
        adapter reports every inbound command through `on_activity`, so an idle
        topic still produces traffic.  Silence therefore means three missed
        keepalives: a half-open socket that `pypush` never notices, because its
        own ack wait has no timeout.
        """
        # Wait in short slices so an in-loop shutdown is noticed promptly even if
        # a cross-thread cancel cannot reach the blocked read.
        step = min(1.0, self.watchdog_interval)
        while True:
            waited = 0.0
            while waited < self.watchdog_interval and not self._stop_requested.is_set():
                await anyio.sleep(step)
                waited += step
            if self._stop_requested.is_set():
                cancel_scope.cancel()
                return
            if self.clock() - self._last_activity > self.stall_timeout:
                logger.warning("no courier traffic for %.0f s; reconnecting", self.stall_timeout)
                cancel_scope.cancel()
                return

    async def _handle_token_rejection(self, exc: TokenRejected) -> bool:
        """Invalidate once, re-activate once, then give up. Returns False to stop."""
        self._token_rejections += 1
        if self._token_rejections == 1:
            logger.info("push token rejected; invalidating cached token")
            if self.on_invalidate_token is not None:
                try:
                    self.on_invalidate_token()
                except Exception:  # pragma: no cover - callback isolation
                    logger.exception("invalidate-token callback failed")
            self._emit(PushStatus.DEGRADED, str(exc))
            await self._sleep_with_stop(self.backoff.next_delay(self.random_fn))
            return True
        if self._token_rejections == 2 and self.on_reactivate is not None:
            logger.info("token rejected again; re-activating the device once")
            try:
                await self.on_reactivate()
            except Exception as reactivation_error:
                logger.warning("re-activation failed: %s", reactivation_error)
                self._emit(PushStatus.INVALID, str(reactivation_error))
                return False
            self._emit(PushStatus.DEGRADED, str(exc))
            return True
        self._emit(PushStatus.INVALID, str(exc))
        return False

    async def _sleep_with_stop(self, delay: float) -> None:
        """Sleep, but wake immediately when `stop()` is called."""
        remaining = delay
        while remaining > 0 and not self._stop_requested.is_set():
            step = min(self.sleep_step, remaining)
            await self._sleep(step)
            remaining -= step

    # -- helpers ----------------------------------------------------------- #
    def _material(self) -> ActivationMaterial | None:
        try:
            return self.material_getter()
        except Exception:  # pragma: no cover - defensive
            logger.exception("material_getter failed")
            return None


class FakePushReceiver:
    """Scripted receiver used by unit tests (never touches the network)."""

    def __init__(self, script=None, payloads=None):
        self.script = list(script or [])
        self.payloads = list(payloads or [])
        self.calls: list[dict] = []
        self.stopped = False

    async def run(self, *, material, topic, on_message, on_status, on_token=None, on_activity=None):
        self.calls.append({"material": material, "topic": topic})
        if on_token is not None:
            on_token("ab" * 32)
        for step in self.script:
            if isinstance(step, BaseException):
                raise step
            if callable(step):
                step(on_status)
        for payload in self.payloads:
            on_message(payload)
        await anyio.sleep_forever()  # stay connected until cancelled

    async def stop(self) -> None:
        self.stopped = True
