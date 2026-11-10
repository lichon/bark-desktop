"""Entry point: `python -m bark_pc`, `bark-desktop`, and the packaged exe."""

from __future__ import annotations

import sys

from .app import BarkApp


def main(argv: list[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)

    # A `barkpc:` URI or --focus is forwarded to the running instance by
    # `BarkApp.run` before any window is created.
    uri = next((a for a in args if a.lower().startswith("barkpc:")), None)
    if "--focus" in args and uri is None:
        uri = "barkpc:open"

    app = BarkApp(argv=args)
    try:
        return app.run(initial_uri=uri)
    except KeyboardInterrupt:  # pragma: no cover - interactive dev runs
        app.shutdown()
        return 130


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
