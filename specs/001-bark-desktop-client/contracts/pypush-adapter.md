# Contract: pypush APNs Adapter

`bark_pc/push/pypush_client.py` is the **only** module allowed to import `pypush`. Everything else depends on the interface below, so the upstream library (SSPL, unstable until 3.0.0) can be replaced without touching receiving, storage, or UI code.

Target: `pypush==2.0.2` (`pypush.apns`).

## Upstream surface used

```python
from pypush import apns

cert, key = await apns.activate(device_class="Windows", udid=... , serial=...)
async with apns.create_apns_connection(
    cert, key,
    token=cached_base_token_or_None,
    courier="1-courier.push.apple.com",
) as conn:
    scoped = await conn.mint_scoped_token(topic)
    async with conn.notification_stream(topic, scoped) as stream:
        async for cmd in stream:
            await conn.ack(cmd)
            cmd.payload        # bytes → UTF-8 JSON
```

Notes verified from source:
- `activate()` performs anonymous device activation against `albert.apple.com` and returns `(x509.Certificate, rsa.RSAPrivateKey)`. No Apple account, password, or 2FA exists on this path.
- `create_apns_connection(..., token=...)` accepts a cached base token and refreshes it from `ConnectAck` when the server issues a new one.
- The connection runs its own keepalive (30 s) and exponential-backoff reconnect; it does not surface those states to callers, so the adapter must infer and report them.
- `notification_stream` filters by topic and (scoped) token; `ack()` must be called for each message. The adapter does **not** use `notification_stream`: its `command.token == scoped_token` check drops every real Bark push, because the Bark server holds the device's *base* token (that is what `run_setup` and `tools/spike_token.py` register). Instead the adapter installs the topic filter itself (`connection._filter([topic])`), subscribes to the broadcast stream with a topic-only filter (`message_filter`), and logs the token prefix at debug level when a message carries neither the base nor the scoped token.
- `apns.activate()` defaults the sandbox flag to `False`; the client pins `courier="1-courier.push.apple.com"` for production Bark pushes and must not auto-fallback to a sandbox courier.
- `pypush` 2.0.2 logs `WARNING:Unexpected field with packet ID N in packet …` (module-level, i.e. on the root logger, whole packet included) for every APNs `SendMessage` field its field table lacks. Apple sends field 24 — the notification UUID, a string copy of the payload `id` — on every push, and the parser skips unknown fields, so delivery is unaffected; the warning is per-message noise that also embeds payload bytes. `core.logging` therefore keeps the root logger bridged to the app log at **every** level (at info level `logging.warning()` would otherwise call `basicConfig()` and print the unredacted packet to stderr), and rewrites that one warning into a body-free debug line (`courier: SendMessage field 24 is not mapped by pypush (ignored)`). Dropping it at info level is deliberate; it must not be "fixed" by patching the pinned dependency.

## Interface implemented by the adapter

```python
class PushReceiver(Protocol):
    async def activate(self, *, udid: str, serial: str) -> ActivationMaterial: ...
    async def run(self, *, material: ActivationMaterial, topic: str,
                  on_message: Callable[[bytes], None],
                  on_status: Callable[[PushStatus, str | None], None],
                  on_token: Callable[[str], None] | None = None,
                  on_activity: Callable[[], None] | None = None) -> None: ...
    async def stop(self) -> None: ...
```

- `activate()` → `ActivationMaterial(cert_pem, key_pem, base_token_hex, udid, serial)`. Raises `ActivationError(reason)` on failure; never returns partial material.
- `run()` is long-lived: it connects, mints the scoped token, streams, acks, and calls `on_message(raw_bytes)` for each notification. Any exception terminates `run()`; the supervisor decides whether to reconnect, re-activate, or mark the link invalid. Delivery is decided by **topic**, never by token: `me.fin.bark` messages addressed to the base token (what Bark servers use) and to the scoped token both reach `on_message`.
- `on_activity()` fires for **every inbound courier command**, not just notifications: `pypush` sends a keepalive and receives `KeepAliveAck` every 30 s (`pypush/apns/lifecycle.py`, `_ping_task`), and the adapter taps the broadcast stream (`_receive_stream(filters.ALL)`) to report them. The supervisor's stall watchdog therefore measures link liveness, not topic traffic: a quiet topic still produces acks every 30 s, so a 90 s gap means three missed keepalives. This matters because `pypush`'s own `_receive(KeepAliveAck)` has no timeout — a half-open socket blocks it forever and never triggers pypush's internal reconnect.
- `stop()` closes the connection and cancels tasks without raising; called on quit and before re-activation.
- Callbacks are invoked from the receiver thread; they must be cheap and must not raise (exceptions are logged and swallowed, never allowed to break the stream).

## Status reporting (adapter → supervisor)

| Status | Emitted when | Supervisor reaction |
|--------|--------------|---------------------|
| `connecting` | before activation/connect attempt | tray amber |
| `active` | first successful `ConnectAck` + filter applied | tray normal, persist `last_connected_at` |
| `degraded(reason)` | stream error, timeout, ping failure, courier close | backoff reconnect (1 s → 60 s, jittered); tray amber |
| `invalid(reason)` | activation rejected, certificate rejected, repeated auth failures | stop retrying, tray red, offer re-activation |
| `stopped` | `stop()` completed | none |

## Error taxonomy

| Condition | Adapter error | Classification |
|-----------|---------------|----------------|
| HTTP error from Apple's activation endpoint, `<b>…</b>` error text | `ActivationRejected(message)` | permanent until user retries; rate-limited |
| TLS/DNS/timeout during activation | `ActivationUnavailable(message)` | retryable with backoff |
| Connect ack non-zero status | `CourierRejected(status)` | treat as `invalid` after 3 attempts |
| Stream closed / keepalive missed | `StreamLost(reason)` | `degraded`, reconnect |
| Token rejected by courier | `TokenRejected` | invalidate cached token, reconnect once, then re-activate once, then `invalid` |
| Unexpected payload decode error | `PayloadMalformed` | log and continue (never kill the stream) |

## Invariants the adapter must uphold

1. Exactly one live courier connection per process; reconnects happen inside `run()` or via supervisor restart, never concurrently.
2. An ack is sent for every message delivered to `on_message`, even if the message is later discarded as a duplicate.
3. Tokens and certificates are never logged; `repr`/`__str__` of `ActivationMaterial` redacts secrets.
4. Activation material is reused across restarts; re-activation only happens on explicit user action or `TokenRejected`.
5. No hidden threads: all concurrency comes from the caller's `anyio` task group.

## Test seam

Tests substitute a `FakePushReceiver` that replays JSON payloads from fixtures, emits scripted status transitions, and records acks. No test may contact `albert.apple.com` or a courier.
