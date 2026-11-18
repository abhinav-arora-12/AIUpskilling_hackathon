"""Streamlit workbench smoke test (brief Gate 18). Runs app.py headless and checks for exceptions."""

from pathlib import Path

from streamlit.testing.v1 import AppTest

APP = Path(__file__).resolve().parent.parent / "app.py"


def test_workbench_renders_six_tabs_without_exceptions():
    at = AppTest.from_file(str(APP), default_timeout=120).run()
    assert not at.exception, [e.value for e in at.exception]
    assert len(at.tabs) == 6
    labels = [t.label for t in at.tabs]
    assert labels[0].startswith("1.") and labels[5].startswith("6.")


def test_tab6_reads_kpi_scorecard_when_present():
    at = AppTest.from_file(str(APP), default_timeout=120).run()
    assert not at.exception
    text = " ".join(m.value for m in at.markdown) + " ".join(str(c.value) for c in at.caption)
    assert "scorecard" in text.lower() or "PASS" in text or "Run scripts" in text
