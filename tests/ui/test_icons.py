"""The window icon must be set on the Tk root (no Tk feather in the taskbar)."""

from __future__ import annotations

import tkinter as tk

import pytest

from bark_pc.ui import icons


@pytest.fixture
def root():
    try:
        window = tk.Tk()
    except tk.TclError:  # pragma: no cover - no display (headless CI)
        pytest.skip("Tk is not available in this environment")
    window.withdraw()
    try:
        yield window
    finally:
        window.destroy()


def test_window_icon_covers_every_size_windows_asks_for(root):
    images = icons.window_icon_images(root)

    assert [int(image.width()) for image in images] == list(icons.ICON_SIZES)
    assert [int(image.height()) for image in images] == list(icons.ICON_SIZES)


def test_apply_window_icon_sets_the_toplevel_default(root, monkeypatch):
    calls: list[tuple] = []
    monkeypatch.setattr(icons, "window_icon_images", lambda *_a, **_k: ["small", "big"])
    monkeypatch.setattr(root, "iconphoto", lambda *args: calls.append(args))

    assert icons.apply_window_icon(root) == ["small", "big"]
    # default=True is what makes the icon apply to dialogs and toplevels too.
    assert calls == [(True, "small", "big")]


def test_rendered_icon_stays_alive_for_tk(root):
    before = len(root.tk.splitlist(root.tk.call("image", "names")))
    images = icons.apply_window_icon(root)

    assert len(root.tk.splitlist(root.tk.call("image", "names"))) == before + len(icons.ICON_SIZES)
    assert all(str(image) for image in images)


def test_missing_tk_support_never_breaks_startup(root, monkeypatch):
    def boom(*_args, **_kwargs):
        raise ImportError("no Tk support in Pillow")

    monkeypatch.setattr(icons, "window_icon_images", boom)

    assert icons.apply_window_icon(root) == []
