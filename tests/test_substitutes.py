"""
Substitute assignment (generator.assign_substitute / revert_substitution).

These tests COMMIT (the function is transactional by design), so they only touch
TEST_YEAR classes; any Teaches row they add is removed afterwards.
"""
import pytest

import db
import generator as G
from conftest import TEST_YEAR


@pytest.fixture
def saved(saved_test_year):
    """A saved TEST_YEAR timetable; Teaches is restored to its original rows afterwards."""
    with db.connection() as c:
        before = {(r["faculty_id"], r["course_id"]) for r in db.query(c, "SELECT * FROM Teaches")}
    yield saved_test_year
    with db.connection(autocommit=True) as c:
        cur = c.cursor()
        cur.execute("DELETE FROM Timetable WHERE academic_year = %s", (TEST_YEAR,))   # frees the FK first
        for fac, course in {(r["faculty_id"], r["course_id"]) for r in db.query(c, "SELECT * FROM Teaches")} - before:
            cur.execute("DELETE FROM Teaches WHERE faculty_id = %s AND course_id = %s", (fac, course))
        cur.close()


def classes(conn):
    return db.query(conn, "SELECT * FROM Timetable WHERE academic_year = %s ORDER BY timetable_id", (TEST_YEAR,))


def free_faculty(conn, cls, qualified: bool):
    """A faculty member (not the current one) free in the class's slot, qualified for the course or not."""
    rows = db.query(conn, "SELECT f.faculty_id FROM Faculty f WHERE f.faculty_id <> %s "
                          "AND f.faculty_id NOT IN (SELECT faculty_id FROM Timetable WHERE academic_year = %s "
                          "AND slot_id = %s) AND " + ("" if qualified else "NOT ")
                    + "EXISTS (SELECT 1 FROM Teaches t WHERE t.faculty_id = f.faculty_id AND t.course_id = %s) "
                    "ORDER BY f.faculty_id",
                    (cls["faculty_id"], TEST_YEAR, cls["slot_id"], cls["course_id"]))
    return rows[0]["faculty_id"] if rows else None


def first_with(conn, qualified: bool):
    for cls in classes(conn):
        fac = free_faculty(conn, cls, qualified)
        if fac is not None:
            return cls, fac
    pytest.skip("no suitable class/faculty combination in the generated timetable")


def faculty_of(conn, timetable_id):
    conn.rollback()                                   # fresh snapshot
    return db.query(conn, "SELECT faculty_id FROM Timetable WHERE timetable_id = %s", (timetable_id,))[0]["faculty_id"]


def teaches(conn, fac, course):
    conn.rollback()
    return bool(db.query(conn, "SELECT 1 x FROM Teaches WHERE faculty_id = %s AND course_id = %s", (fac, course)))


def test_qualified_substitute_is_assigned_and_can_be_undone(saved, conn):
    cls, sub = first_with(conn, qualified=True)
    change = G.assign_substitute(conn, cls["timetable_id"], sub)
    assert change["old_faculty_id"] == cls["faculty_id"] and change["new_faculty_id"] == sub
    assert change["teaches_added"] is False and change["academic_year"] == TEST_YEAR
    assert faculty_of(conn, cls["timetable_id"]) == sub
    assert db.call_procedure(conn, "sp_list_clashes", [TEST_YEAR]) == []          # still clash-free
    assert db.call_procedure(conn, "sp_hours_coverage", [TEST_YEAR]) == []

    G.revert_substitution(conn, change)
    assert faculty_of(conn, cls["timetable_id"]) == cls["faculty_id"]


def test_busy_faculty_is_refused_and_nothing_changes(saved, conn):
    rows = classes(conn)
    a = rows[0]
    b = next(r for r in rows if r["slot_id"] == a["slot_id"] and r["faculty_id"] != a["faculty_id"])
    with pytest.raises(G.SubstitutionError, match="already teaches"):
        G.assign_substitute(conn, a["timetable_id"], b["faculty_id"])
    assert faculty_of(conn, a["timetable_id"]) == a["faculty_id"]


def test_unqualified_substitute_needs_a_teaches_row(saved, conn):
    cls, sub = first_with(conn, qualified=False)
    with pytest.raises(G.SubstitutionError, match="not listed in Teaches"):
        G.assign_substitute(conn, cls["timetable_id"], sub)
    assert faculty_of(conn, cls["timetable_id"]) == cls["faculty_id"]
    assert not teaches(conn, sub, cls["course_id"])

    change = G.assign_substitute(conn, cls["timetable_id"], sub, add_teaches=True)
    assert change["teaches_added"] is True
    assert teaches(conn, sub, cls["course_id"]) and faculty_of(conn, cls["timetable_id"]) == sub

    G.revert_substitution(conn, change)                  # gives the class back AND removes the Teaches row
    assert faculty_of(conn, cls["timetable_id"]) == cls["faculty_id"]
    assert not teaches(conn, sub, cls["course_id"])


def test_undo_is_all_or_nothing_when_the_added_teaches_row_is_in_use(saved, conn):
    """If the substitute was meanwhile given a second class of that course, the Teaches row
    cannot be dropped (FK RESTRICT): the undo must fail as a whole, changing nothing."""
    cls, sub = first_with(conn, qualified=False)
    change = G.assign_substitute(conn, cls["timetable_id"], sub, add_teaches=True)
    other = next((r for r in classes(conn) if r["course_id"] == cls["course_id"]
                  and r["timetable_id"] != cls["timetable_id"] and r["faculty_id"] != sub
                  and free_faculty(conn, r, qualified=True) is not None
                  and not db.query(conn, "SELECT 1 x FROM Timetable WHERE academic_year = %s AND slot_id = %s "
                                         "AND faculty_id = %s", (TEST_YEAR, r["slot_id"], sub))), None)
    if other is None:
        pytest.skip("no second class of that course where the substitute is free")
    G.assign_substitute(conn, other["timetable_id"], sub)       # now qualified, no Teaches row added
    with pytest.raises(G.SubstitutionError, match="still used"):
        G.revert_substitution(conn, change)
    assert faculty_of(conn, cls["timetable_id"]) == sub          # rolled back: nothing changed
    assert teaches(conn, sub, cls["course_id"])


@pytest.mark.parametrize("case", ["unknown class", "unknown faculty", "same faculty"])
def test_bad_requests_are_explained(saved, conn, case):
    cls = classes(conn)[0]
    tid, fac, words = {"unknown class": (999_999_999, 1, "no saved class"),
                       "unknown faculty": (cls["timetable_id"], 999_999, "unknown faculty"),
                       "same faculty": (cls["timetable_id"], cls["faculty_id"], "already teaches this class")}[case]
    with pytest.raises(G.SubstitutionError, match=words):
        G.assign_substitute(conn, tid, fac)
