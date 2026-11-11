# Contract: APNs Push Payload → Notification

Source of truth: bark-server `apns/apns.go` (payload construction) and `docs/API_V2.md` (push fields). The payload arrives as UTF-8 JSON bytes from the courier stream.

## Envelope shape

```json
{
  "aps": {
    "alert": { "title": "Deploy", "subtitle": "prod", "body": "build 412 done" },
    "sound": "minuet",
    "thread-id": "ops",
    "mutable-content": 1,
    "content-available": 1          // present for delete/background pushes
  },
  "icon": "https://example.com/chart.png",
  "group": "ops",
  "url": "https://ci.example.com/build/412",
  "id": "c0ffee-uuid",
  "level": "timeSensitive",
  "badge": "3",
  "delete": "1"                      // present only on recall pushes
}
```

Server behavior to rely on:
- `aps.alert` is an object with `title`, `subtitle`, `body`; any may be absent.
- Every custom push field is added to the payload **lower-cased and stringified** (`pl.Custom(strings.ToLower(k), fmt.Sprintf("%v", v))`). So `autoCopy` arrives as `autocopy`, `isArchive` as `isarchive`, and integers/booleans arrive as strings.
- `group` is duplicated into `aps.thread-id` when present.
- Delete pushes are sent as background pushes (`content-available`) with the custom `delete` field and the message `id`; they have no alert.
- `ciphertext` appears instead of the plaintext fields for encrypted pushes.
- Total payload is bounded at 4 KB by the server.

## Mapping (authoritative)

| Payload | Type after stringification | Notification field | Notes |
|---------|----------------------------|--------------------|-------|
| `aps.alert.title` | string | `title` | Truncated to 200 chars for toast; stored in full |
| `aps.alert.subtitle` | string | `subtitle` | Displayed as `title — subtitle` when both exist |
| `aps.alert.body` | string | `body` | Plain text; markdown not rendered in v1 |
| `aps.thread-id` | string | `group_name` | Fallback when custom `group` is missing |
| `group` | string | `group_name` | Preferred over `thread-id` when both exist |
| `url` | string | `url` | Only `http`/`https` are clickable; anything else is stored but not opened |
| `icon` | URL string | `image_url` | Non-`http(s)` values are ignored |
| `id` | string | `remote_id` | Also the de-duplication key; delete pushes reference it |
| `delete` | `"1"` | — | Removes the notification with matching `remote_id` |
| `bark_desktop_self` | `"1"` | `from_self` | Internal marker the Supabase transport adds when a broadcast's `id` is one this process published recently; such rows are stored read, not toasted, not spoken |
| `level` | string | `level` | `active` / `timeSensitive` / `passive` / `critical`; stored only |
| `badge` | numeric string | — | Ignored (no desktop badge equivalent) |
| `sound` | string | — | Ignored (toast sound default) |
| `copy`, `autocopy` | string | — | Stored in `raw_json`; clipboard action is v2 |
| `isarchive`, `ttl` | string | — | Stored only; archive semantics not applicable locally |
| `ciphertext` | base64 string | `body` = "Encrypted message — not supported in this version" | Never decrypted; counted as delivered |
| `aps.mutable-content` | number | — | Ignored |
| any other key | any | `raw_json` | Ignored for display (forward compatibility) |

## Classification rules

1. **Delete push**: `delete == "1"` (string or number, tolerate both). Requires `id`; without `id` the push is logged and ignored.
2. **Encrypted push**: `ciphertext` present → placeholder notification, `raw_json` retained.
3. **Empty alert**: no title, no subtitle, no body, and not a delete → ignore for the UI, log at debug level (silent pushes carry no user value here).
4. **Markdown image**: if `body` contains a markdown image (`![alt](url)`) and no `icon`, use the first image URL as `image_url`. Text is stored verbatim otherwise.
5. **Unsupported/unknown fields**: never fatal (FR-013). Parsing must survive missing `aps`, non-object `alert` (a bare string is treated as `body`), and invalid UTF-8 (decode with `errors="replace"`).

## De-duplication

- Primary key: `(source_server, remote_id)`. A repeat insert is ignored, not errored.
- Without `remote_id`: hash the raw bytes with SHA-256 and ignore repeats observed within 5 seconds.
- Acks always happen even when a message is dropped as a duplicate.

## Fixtures for tests

Location: `tests/fixtures/payloads/*.json`, one file per case: `text.json`, `text_with_subtitle.json`, `image_icon.json`, `markdown_image.json`, `group.json`, `delete.json`, `encrypted.json`, `empty_alert.json`, `string_alert.json`, `unknown_fields.json`, `invalid_utf8.bin`, `duplicate_id.json`. Each maps to an assertion in `tests/unit/test_payload_mapping.py`.
