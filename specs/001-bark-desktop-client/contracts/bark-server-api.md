# Contract: Bark Server HTTP API (client-side usage)

Scope: the subset of the Bark-compatible server API this client calls. Verified against bark-server `route_register.go`, `apns/apns.go`, and `docs/API_V2.md`. Servers must be Bark-compatible; anything implementing these calls works (public `api.day.app`, self-hosted `bark-server`, compatible workers).

Base URL: user-configured, normalized as `scheme://host[:port][/prefix]` with no trailing slash.

## 1. Register the push token

`POST {base}/register`

Request headers: `Content-Type: application/json; charset=utf-8`

```json
{
  "device_key": "optional-existing-key",
  "device_token": "<lowercase-hex base push token>",
  "device_type": "ios"
}
```

- `device_key` is omitted on first registration (server assigns a UUID key) and supplied when rotating an existing key on the same server.
- Legacy field names `key` / `devicetoken` are accepted by the server; the client always sends the new names.
- `device_type` must be `ios` (default) or `watchos`; the client always sends `ios`.

Success `200`:

```json
{
  "code": 200,
  "message": "success",
  "timestamp": 1762732800,
  "data": {
    "key": "ynJ5Ft4atkMkWeo2PAvFhF",
    "device_key": "ynJ5Ft4atkMkWeo2PAvFhF",
    "device_token": "<echo of the registered token>",
    "device_type": "ios"
  }
}
```

Client obligations:
- Read `data.device_key`, falling back to `data.key`.
- Treat any of the documented failures as `RegistrationRejected` (do not retry blindly):
  - `400 device token is empty`
  - `400 device token is invalid` (token longer than 160 chars)
  - `400 invalid device type: …`
  - `403` when the server has registration closed (`CLOSE_REGISTER=true`)
  - `500 device registration failed: …`
- On success, persist `device_key` and `server_url` atomically; keep the previous link usable until this succeeds.

## 2. Verify a key

`GET {base}/register/{device_key}`

- `200 {"code":200,"message":"success",…}` → key exists on this server.
- `400 {"code":400,"message":"<reason>"}` → key unknown; treat the link as `stale`.

Used at startup (throttled to once per hour) and when the user presses "Check connection".

## 3. Reachability

`GET {base}/ping`

- `200` → server reachable. Any other status or transport error → `ServerUnreachable`.
- This call requires no authentication on the servers we target; if the user's server returns `401`, the client reports "server requires authentication, which is not supported" instead of retrying.

## 4. Self-test push

`POST {base}/push`

```json
{ "device_key": "<our key>", "title": "Bark Desktop", "body": "Test notification", "group": "bark-desktop", "id": "<uuid>" }
```

- `200` with `code: 200` → the server accepted the push for delivery. This proves the whole chain (server → APNs → this client) *if and only if* the message also arrives on the stream; the client waits up to 30 s and reports both facts distinctly.
- `400` / `500` → surface the server's `message` verbatim in the self-test result.

## 5. Self-message route push

`POST {message_route_url}`

Used by the history window's compose bar when the self-message route is `bark`
or `both`. The setting is the full push URL of the target device (device key in
the path), optionally with Bark query parameters:

```
https://api.day.app/<device_key>
http://127.0.0.1:8080/<device_key>?group=ops
```

```json
{ "title": "<host name>", "body": "<composed line>", "group": "bark-desktop", "id": "<hex>" }
```

- Same success/failure semantics as the self-test push (`code: 200`).
- The URL is user-configured and must use `http://` or `https://`; credentials
  and fragments are rejected, query parameters are preserved.
- This route does not depend on the Supabase Realtime connection and never
  sends to a third party other than the URL the user configured.

## Error taxonomy mapping

| Condition | Client error | User-visible message |
|-----------|--------------|----------------------|
| DNS/TLS/connect failure | `ServerUnreachable` | "Cannot reach {host}. Check the address and your network." |
| 401/403 | `ServerRejected` | "The server refused the request ({status}). Registration may be closed." |
| 4xx with Bark `message` | `RegistrationRejected` | The server's message, prefixed with "Server said:" |
| 5xx | `ServerError` | "The server reported an internal error. Try again later." |
| Timeout (10 s) | `ServerTimeout` | "The server did not respond in time." |

## Compatibility notes and limits

- No unregister endpoint exists. Rotating within a server overwrites the key's token; abandoning a server cannot delete the key and must be communicated honestly.
- Server pushes target APNs production with topic `me.fin.bark` (`me.fin.bark.watch` for watchOS). A server that hardcodes a different topic requires the client's `push_topic` override to match, or it will receive nothing.
- The client must not send notification content to any third party; all calls above go only to the user-chosen server.
