## 2026-11-10T19:02:54 — frozen import smoke test
- frozen: True
- executable: `C:\Users\lc\repos\bark-pc\dist\spike\SpikeT2\SpikeT2.exe`
- modules checked: 18
- failures: 1
  - `tkinter.ttk`: ModuleNotFoundError: No module named 'tkinter.ttk'
- required hidden imports: tkinter.ttk

## 2026-11-10T19:03:21 — frozen import smoke test
- frozen: True
- executable: `C:\Users\lc\repos\bark-pc\dist\spike\SpikeT2\SpikeT2.exe`
- modules checked: 18
- failures: 0


## Conclusion

**PASS.** All 18 modules used by the packaged app import inside a frozen
PyInstaller build, including `pypush.apns`, all four `winrt.*` namespaces used by
`windows-toasts`, `pystray._win32`, Tk, Pillow/ImageTk, segno and the anyio
asyncio backend.

The first run failed on exactly one module — `tkinter.ttk` — because
`bark_pc/ui/*` imports it inside functions, so PyInstaller cannot see it by
static analysis. `bark_pc/tools/build_portable.py` therefore pins these explicit
hidden imports:

```
tkinter, tkinter.ttk, tkinter.font, tkinter.filedialog, tkinter.messagebox,
winrt.windows.ui.notifications, winrt.windows.foundation,
winrt.windows.foundation.collections, winrt.windows.data.xml.dom,
pypush.apns, pypush.apns.albert, pypush.apns.lifecycle, pypush.apns.transport,
pystray._win32, anyio._backends._asyncio, PIL._tkinter_finder
```

The second run (after adding them) reported 0 failures. The packaged
`BarkDesktopClient.exe` built with the same options starts, creates
`data/`, writes `data/instance.json`, and exits cleanly on the instance `quit`
command (see `perf.md` and `US5.md`).
