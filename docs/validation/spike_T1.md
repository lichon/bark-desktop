# Spike T1 — is the base courier token accepted by APNs via Bark?

**Question**: after anonymous activation, does registering the **base** courier
token with a Bark-compatible server make providers pushes arrive on the client
stream? This is the go/no-go gate for the product premise (`research.md` §R6).

**Status: BLOCKED.** The tool is implemented and unit-tested at the adapter
boundary, but the decision requires outbound access to `albert.apple.com`,
`*.courier.push.apple.com`, and a Bark-compatible server, plus a real APNs
provider push. Those are external services and were not exercised from this
development environment.

## Tool

```bash
python -m bark_pc.tools.spike_token --server http://127.0.0.1:8080 --timeout 30
```

It performs exactly the production flow:

1. `apns.activate(device_class="Windows", udid=…, serial=…)` — anonymous, no
   Apple account.
2. `create_apns_connection(..., courier="1-courier.push.apple.com")` and read
   `conn.base_token` (printed redacted after eight characters).
3. `POST /register` on the selected server with `device_token=<base token hex>`.
4. `POST /push` a self-test with a UUID `id`.
5. Watch the courier stream (scoped token for `me.fin.bark`) for that `id` within
   the timeout, acking every message.

The result is appended to this file: PASS (with token prefix, key, and arrival
time) or FAIL (with the reason to check in the server log).

## Decision rule

* **PASS** → keep the base-token design; proceed with the MVP validation.
* **FAIL with a server-side APNs reason** (`BadDeviceToken`,
  `DeviceTokenNotForTopic`) → per `research.md` §R6, re-evaluate the token choice
  (scoped token) or document self-hosted-only support with a matching
  `push_topic`. Do not guess: decide from the recorded reason.
* **FAIL during activation** → Apple's activation endpoint is unreachable or
  rejected the request; record the `<b>…</b>` error text and retry later.

## What is covered without the live run

* The adapter's imports and error mapping (`tests/unit/test_notification_ingest.py`,
  `tests/unit/test_supervisor_status.py`).
* Registration request/response shapes (`tests/unit/test_bark_server_client.py`).
* Status transitions and token-rejection handling (`tests/unit/test_supervisor_status.py`).
* Frozen-build imports of `pypush.apns` (spike T2).

The live spike itself remains the last unverified premise of the design.
