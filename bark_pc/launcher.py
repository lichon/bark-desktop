"""Frozen-build entry point.

PyInstaller runs its entry script as `__main__` outside any package, so a file
using relative imports (`bark_pc/__main__.py`) cannot be the entry.  This launcher
imports the package absolutely.
"""

from __future__ import annotations

from bark_pc.__main__ import main

if __name__ == "__main__":  # pragma: no cover - exercised in the packaged build
    raise SystemExit(main())
