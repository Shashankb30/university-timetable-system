"""
Shared pytest fixtures for the timetable project.

SAFETY RULE: tests never modify your real data. Everything that writes uses the
throw-away academic year TEST_YEAR (cleaned before and after), or runs inside a
transaction that is rolled back. The Neo4j tests only READ.

Requirements: MySQL/MariaDB reachable with the settings in .env (the same ones the
app uses). Neo4j tests skip themselves when Neo4j is not running / not loaded.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

import db  # noqa: E402  (also loads .env)
import generator  # noqa: E402
import utils  # noqa: E402

TEST_YEAR = "2099-2100"          # VARCHAR(9); never used for real data


def pytest_collection_modifyitems(config, items):
    """UI tests (Streamlit AppTest imports pandas) are opt-in."""
    if os.environ.get("RUN_UI_TESTS") == "1":
        return
    skip = pytest.mark.skip(reason="UI tests are opt-in: set RUN_UI_TESTS=1 "
                                   "(from a normal terminal, not the Flatpak VS Code one)")
    for item in items:
        if "ui" in item.keywords:
            item.add_marker(skip)


@pytest.fixture
def year() -> str:
    """The real academic year (read-only use)."""
    assert utils.academic_year() != TEST_YEAR
    return utils.academic_year()


@pytest.fixture
def conn():
    """A MySQL connection with autocommit OFF; always rolled back at the end."""
    try:
        c = db.connect()
    except Exception as exc:                      # noqa: BLE001
        pytest.fail(f"Cannot connect to MySQL ({type(exc).__name__}: {exc}). Check .env and that "
                    "the database server is running and loaded (see README).", pytrace=False)
    try:
        yield c
    finally:
        try:
            c.rollback()
        except Exception:                         # noqa: BLE001
            pass
        c.close()


def _wipe_test_year() -> None:
    with db.connection(autocommit=True) as c:
        cur = c.cursor()
        cur.execute("DELETE FROM Timetable WHERE academic_year = %s", (TEST_YEAR,))
        cur.close()


@pytest.fixture
def clean_test_year():
    """Guarantees TEST_YEAR is empty before and after the test."""
    _wipe_test_year()
    yield TEST_YEAR
    _wipe_test_year()


@pytest.fixture
def saved_test_year(clean_test_year):
    """Generate a full timetable and SAVE it for TEST_YEAR; returns the GenerationResult."""
    res = generator.generate_timetable(TEST_YEAR, replace=True)
    assert res.committed and res.complete, res.report()
    return res


@pytest.fixture(scope="module")
def neo4j_session():
    """A Neo4j session, or skip when Neo4j is not reachable."""
    import neo4j_queries
    try:
        driver = neo4j_queries.get_driver()
    except Exception as exc:                      # noqa: BLE001
        pytest.skip(f"Neo4j not reachable ({type(exc).__name__}). Start it, then run "
                    "`python src/neo4j_etl.py`.")
    with driver.session(database=utils.neo4j_settings()["database"]) as session:
        yield session


@pytest.fixture(scope="module")
def graph_loaded(neo4j_session):
    """Skip unless the graph has been loaded by the ETL."""
    import neo4j_queries
    if not neo4j_queries.node_counts(neo4j_session).get("Course"):
        pytest.skip("The Neo4j graph is empty. Run `python src/neo4j_etl.py` first.")
    return neo4j_session
