# Encoding and layout pass

**Status: PARTIAL.** Text handling is covered by automated tests; the visual
high-DPI and multi-monitor checks need a desktop session.

## Automated evidence (executed)

`tests/unit/test_encoding.py`:

* CJK (`中文通知内容测试`), RTL Arabic, and emoji (`deploy 🚀 done ✅`) survive
  parsing, `title — subtitle` merging, and a SQLite round-trip.
* An 8 KB stored-body cap is enforced in **bytes**, so 5 000 emoji (20 KB) are
  truncated without producing broken characters, and the body is never emptied.
* Toast truncation is applied per line at 200 characters and never splits a
  character or introduces `U+FFFD`.
* The encrypted placeholder is shown verbatim
  (`Encrypted message — not supported in this version`).
* Timestamps render in one stable local format (`YYYY-MM-DD HH:MM`).
* An empty title still yields the toast title `Bark` (never a blank toast).

`tests/unit/test_payload_mapping.py` also checks invalid UTF-8 decoding with
`errors="replace"` (no exception, message still usable).

## Implementation notes

* `truncate_lines()` applies the 200-character limit independently per line.
* The history detail pane caps rendered text at 8 KB and the list uses a single
  bold tag for unread rows (no layout shift when the image is missing).
* Fonts are set once in `ui/theme.py` (`Segoe UI` / `Consolas`) and scaled from
  the primary monitor DPI (`SetProcessDpiAwareness(2)` before Tk starts, ttk
  scaling derived from `LOGPIXELSX`).

## Still needs a real run (quickstart SC matrix)

1. Push CJK, RTL, emoji and an 8 KB body; confirm no mojibake in toasts or
   history.
2. Verify 100 %, 150 % and 200 % display scaling; confirm controls and the
   history list do not clip.
3. Disconnect a secondary monitor, restart, and confirm the window size is
   restored without being positioned off-screen (only size is persisted).
