"""paper.py event-day gating now delegates to cherrypit.calendar (single source of truth).

Locks in the fix for the old hardcoded-config drift where triple_witching_dates_2026 listed
2026-06-18 (a Thursday) instead of the real 3rd Friday 2026-06-19.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))
import paper  # noqa: E402  (import also bootstraps the src/_core cherrypit submodule)


def test_event_day_helper_handles_none_and_bad_input():
    assert paper._is_event_day(None, paper._cal.is_triple_witching) is False
    assert paper._is_event_day("", paper._cal.is_triple_witching) is False
    assert paper._is_event_day("not-a-date", paper._cal.is_triple_witching) is False


def test_triple_witching_uses_the_correct_june_friday():
    assert paper._is_event_day("2026-06-19", paper._cal.is_triple_witching) is True   # correct 3rd Friday
    assert paper._is_event_day("2026-06-18", paper._cal.is_triple_witching) is False  # the old config bug


def test_fomc_and_quarterly_delegation():
    assert paper._is_event_day("2026-01-28", paper._cal.is_fomc_day) is True
    assert paper._is_event_day("2026-03-31", paper._cal.is_quarterly_expiry) is True
    assert paper._is_event_day("2026-07-06", paper._cal.is_fomc_day) is False  # ordinary Monday
