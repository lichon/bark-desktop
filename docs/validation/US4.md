# US4 — Receive and view images (P2)

**Status: PARTIAL.** Fetch caps, cache semantics, eviction and accounting are
unit-tested with a mocked HTTP transport; a real host and a real APNs push are
needed for the visual check.

## Automated evidence (executed)

* `tests/unit/test_image_fetcher.py` — a cached URL causes **zero** transport
  calls; a body over 10 MB yields `too_large`; `text/html` yields `not_image`;
  non-HTTP URLs are never fetched; the redirect cap is enforced at 2 (original +
  two redirects = 3 requests, then `unavailable`); a valid image is cached and
  reused (one request for two fetches); a corrupt body yields `unavailable`; an
  HTTP 404 yields `unavailable` (never an exception); duplicate queued requests
  are coalesced into one fetch; results travel back through the callback;
  **LRU eviction never deletes a file referenced by a retained notification**.
* `tests/unit/test_image_cache.py` — URL normalization (case, default ports,
  fragments, query order) and stable SHA-256 keys; 320 px thumbnails preserving
  aspect, never upscaled; accounting matches the actual file sizes; corrupt data
  raises `InvalidImage`; failed states recorded; caching attaches the image to
  matching notification rows; orphan purge removes only unreferenced, stale
  entries; `touch` updates LRU state.
* The ingest path submits image URLs from the ingest thread (never blocking the
  receiver) with priority 10 for the newest push; the history page and explicit
  retries use higher priorities. The fetcher's two worker threads are shut down on
  quit.
* Toast integration: a hero image is attached from the cached file when available
  and the toast is re-shown once within 30 s of receipt when a late fetch
  succeeds; a missing image degrades to a text-only toast with no error dialog.
* `ImageViewer` provides fit-to-window, zoom 25–400 %, *Save as…* (default
  `<title>-<timestamp>.png`), clipboard copy (BMP → CF_DIB), and *Show in folder*.

## Still needs a real run (quickstart §4)

1. Push `{"title":"Chart","body":"daily revenue","icon":"https://picsum.photos/600/300"}`:
   hero image in the toast, thumbnail in history, full-size viewer with save/copy.
2. Repeat the same push: no second download; the thumbnail renders immediately.
3. Push an unreachable image URL: text still delivered, "image unavailable"
   placeholder, no error dialog, no stall in receiving later messages.
