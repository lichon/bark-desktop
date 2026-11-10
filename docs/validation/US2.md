# US2 — Register the client as a Bark device (P1)

**Status: PARTIAL.** Activation/registration/QR/self-test logic is unit-tested;
the live wizard run needs Apple's activation endpoint and a Bark server.

## Automated evidence (executed)

* `tests/unit/test_bark_server_client.py` — `POST /register` success reads
  `data.device_key` with legacy `data.key` fallback; the request body carries
  `device_token` + `device_type: "ios"` and `device_key` only when rotating;
  empty and >160-character tokens are rejected **before** any request; `403`
  → `ServerRejected`; `500` → `ServerError`; `400` messages surface verbatim;
  HTTP-200-with-error-code is rejected; `GET /register/{key}` ok/unknown;
  `GET /ping` ok/HTTP error/`401`; `POST /push` ok/failure; transport and timeout
  mapping.
* `tests/unit/test_links.py` — a refused registration leaves the previous
  `linked` row usable (old key preserved, error recorded); a refused first
  registration leaves a `stale` row; same-server rotation sends the existing
  `device_key` and persists the new key in one write; switching servers marks the
  old link `abandoned` **while the new registration is in flight**, returns the
  abandonment note ("… never claims the key was revoked"), and restores the old
  link if the switch fails; device-key validation (1–160 URL-safe characters).
* `tests/unit/test_identity.py` — anonymous activation material is cached;
  fingerprint mismatch → treated as absent; unreadable blob raises
  `IdentityUnreadable` **without deleting it**; atomic replace leaves no partial
  file; the blob is encrypted (never plaintext PEM); activation is rate-limited
  to one attempt per 60 s; a failed activation marks the identity `invalid` and
  discards partial material.
* No Apple account prompt exists anywhere: the wizard has no credential fields,
  and `PypushReceiver.activate` calls only `apns.activate(device_class="Windows")`.

## Still needs a real run (quickstart §2 + §10)

1. Clean portable folder → wizard → public or local server → device key, push
   URL, QR displayed; `GET /register/{key}` returns success.
2. Re-running setup does not create a second key unless *Replace identity* is
   chosen.
3. Invalid server URL: registration fails with the transport message and the
   previous working link stays active.
4. `CLOSE_REGISTER=true`: "The server refused the request (403)…" with a link to
   change servers.
5. Corrupted `data/identity.bin`: "Device identity missing", offer
   re-activation, history and settings intact.
6. Self-test reports "server accepted" and "received on this device" distinctly.

The QR rendering path (`segno` → PNG → Pillow → `ImageTk`) and the self-test
timeout logic are implemented but only exercised with the mocked server.
