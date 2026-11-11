# Text-to-speech validation

Covers the spoken-notification feature: `bark_pc/tts` (provider registry, spoken
text, worker thread) and `bark_pc/platform/windows/sapi.py` (SAPI 5 through raw
COM). Provider default is `windows`; nobody else is required for the feature to
work.

## Automated evidence

`python -m pytest tests/unit/test_tts.py` — 27 tests, no audio and no network:

* **Spoken text** — title+body joined, body optional, subtitle-only pushes,
  `Hide preview text` collapses to "New notification", markdown images/links and
  bare URLs removed, whitespace flattened, clipping on a word boundary,
  empty notifications stay silent.
* **Settings** — defaults (`tts_enabled` off, `tts_provider` = `windows`), save +
  load round trip, every invalid value (boolean, provider name, rate, volume,
  character budget) falls back to its default with a warning.
* **Factory** — `windows` builds the SAPI-backed provider on Windows and degrades
  to `NullTtsProvider` elsewhere; `none` is silent; unknown names fall back to the
  default provider.
* **Worker** — speak/stop/configure reach the engine on its own thread, the engine
  is closed on shutdown, a burst keeps only the newest lines, a missing engine
  disables speech instead of raising, and one failing utterance does not kill the
  worker.
* **Windows engine** — `SapiEngine` opens, sets `Rate`/`Volume`, purges and closes
  (skipped when no SAPI voice is registered, or when the machine has no audio
  endpoint — a CI runner, a server — and SAPI refuses to `Speak`); the
  `EXCEPINFO` struct is checked against the native layout so a refusal reports
  SAPI's own `scode` and description instead of a bare `0x80020009`, and the
  `VARIANT` struct stride is asserted to be 24 bytes on x64 / 16 bytes on x86,
  which is what makes `DISPPARAMS` argument passing correct.

Whole suite: `python -m pytest` — 408 tests, headless, offline, all passing.

`python -m ruff check bark_pc tests` — clean.

## Interactive check (run manually, not in this container)

```python
from bark_pc.tts import build_tts_provider, speech_text
from bark_pc.core.models import Notification

p = build_tts_provider("windows", rate=0, volume=80)
print(p.available(), p.speak(speech_text(
    Notification(title="Deploy done", body="build 412 is live"))))
p.shutdown()
```

Expected: `True True` and one spoken sentence through the default Windows voice.

Not yet exercised on a real desktop (requires listening):

| Check | Status |
|-------|--------|
| Voice is audible with the system default output device | pending |
| *Settings → Test voice* speaks with the dialog's unsaved rate/volume | pending |
| Tray menu *Speak notifications* toggles and survives a restart | pending |
| A push burst is heard as the newest message only | pending |
| `Hide preview text` says only "New notification" | pending |
| No voice installed → log line, toasts still delivered | pending |
| Portable build (`BarkDesktopClient.exe`) speaks without extra DLLs | pending |
