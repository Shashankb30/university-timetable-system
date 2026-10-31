"""
Streamlit UI smoke tests (headless AppTest). OPT-IN: RUN_UI_TESTS=1 pytest -m ui
They import pandas, so run them from a NORMAL terminal (not the Flatpak VS Code one).
They only read data (the generator page is exercised as a dry run).
"""
from pathlib import Path

import pytest

pytestmark = pytest.mark.ui
APP = str(Path(__file__).resolve().parent.parent / "app" / "streamlit_app.py")


def start(page=None):
    from streamlit.testing.v1 import AppTest
    at = AppTest.from_file(APP, default_timeout=90)
    at.run()
    if page:
        at.sidebar.radio[0].set_value(page).run()
    return at


def no_exceptions(at):
    assert [e.value for e in at.exception] == []


def test_timetable_page_shows_the_weekly_grid():
    at = start()
    no_exceptions(at)
    html = " ".join(m.value for m in at.markdown)
    assert "CS201" in html and "Lunch" in html and "<table" in html
    assert {m.label for m in at.metric} >= {"Periods per week", "Distinct courses", "Days with classes"}


@pytest.mark.parametrize("view", ["Faculty", "Room"])
def test_other_grid_views_load(view):
    at = start()
    at.radio[0].set_value(view).run()
    no_exceptions(at)


@pytest.mark.parametrize("page", ["Statistics", "Clash checker", "Faculty absent", "Run generator"])
def test_every_page_loads_even_without_neo4j(page):
    no_exceptions(start(page))


def test_generator_page_dry_run():
    at = start("Run generator")
    next(c for c in at.checkbox if "Dry run" in c.label).set_value(True)
    next(b for b in at.button if "Generate" in b.label).click().run()
    no_exceptions(at)
    assert any("COMPLETE" in s.value for s in at.success)
