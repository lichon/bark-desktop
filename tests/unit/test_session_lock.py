"""Workstation lock probe: the two Windows answers and how they combine.

The live probe needs a real session, so the composition rules are exercised with
injected probes; a single smoke test asks the real APIs for an answer.
"""

from __future__ import annotations

import sys

import pytest

from bark_pc.platform.windows import session


def test_the_session_flags_answer_wins(monkeypatch):
    monkeypatch.setattr(session, "session_flags", lambda: session.WTS_SESSIONSTATE_LOCK)
    monkeypatch.setattr(session, "input_desktop_locked", lambda: False)

    assert session.is_workstation_locked() is True

    monkeypatch.setattr(session, "session_flags", lambda: session.WTS_SESSIONSTATE_UNLOCK)
    monkeypatch.setattr(session, "input_desktop_locked", lambda: True)

    assert session.is_workstation_locked() is False


def test_the_input_desktop_is_the_fallback(monkeypatch):
    monkeypatch.setattr(session, "session_flags", lambda: None)
    monkeypatch.setattr(session, "input_desktop_locked", lambda: True)

    assert session.is_workstation_locked() is True

    monkeypatch.setattr(session, "input_desktop_locked", lambda: False)

    assert session.is_workstation_locked() is False


def test_an_unanswerable_probe_is_reported_as_unlocked(monkeypatch):
    monkeypatch.setattr(session, "session_flags", lambda: None)
    monkeypatch.setattr(session, "input_desktop_locked", lambda: None)

    assert session.is_workstation_locked() is False


@pytest.mark.windows_only
@pytest.mark.skipif(not sys.platform.startswith("win"), reason="Windows session APIs")
def test_the_live_probe_answers_without_raising():
    flags = session.session_flags()
    assert flags in (None, session.WTS_SESSIONSTATE_LOCK, session.WTS_SESSIONSTATE_UNLOCK)

    locked = session.is_workstation_locked()

    assert isinstance(locked, bool)
    if flags is not None:
        assert locked is (flags == session.WTS_SESSIONSTATE_LOCK)


@pytest.mark.windows_only
@pytest.mark.skipif(not sys.platform.startswith("win"), reason="Windows session APIs")
def test_the_windows_implementation_matches_the_platform_protocol():
    from bark_pc.platform.base import SessionState

    assert isinstance(session.WindowsSessionState(), SessionState)
