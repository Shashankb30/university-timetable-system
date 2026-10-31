"""
Database rules: the schema, constraints, composite foreign keys and triggers must
REJECT invalid data. Every insert happens inside a transaction that is rolled back
(see the `conn` fixture) and uses TEST_YEAR, so real data is never touched.
"""
import mysql.connector
import pytest

import db
import utils
from conftest import TEST_YEAR

BASE = dict(course_id="CS201", faculty_id=1, room_id=1, slot_id=1, section_id=1)
SQL = ("INSERT INTO Timetable (course_id, faculty_id, room_id, slot_id, section_id, academic_year) "
       "VALUES (%(course_id)s, %(faculty_id)s, %(room_id)s, %(slot_id)s, %(section_id)s, %(year)s)")


def insert(conn, **override):
    cur = conn.cursor()
    try:
        cur.execute(SQL, {**BASE, **override, "year": TEST_YEAR})
    finally:
        cur.close()


def clear_year(conn):
    cur = conn.cursor()
    cur.execute("DELETE FROM Timetable WHERE academic_year = %s", (TEST_YEAR,))
    cur.close()


@pytest.fixture
def base_row(conn):
    """One valid class (section 1, CS201, faculty 1, room 1, slot 1) in TEST_YEAR."""
    clear_year(conn)
    insert(conn)                       # must succeed: proves BASE is valid
    return BASE


def scalar(conn, sql, params=()):
    return db.query(conn, sql, params)[0]["v"]


# ----------------------------------------------------------------- structure
def test_schema_objects_exist(conn):
    q = lambda sql: scalar(conn, sql)
    assert q("SELECT COUNT(*) v FROM information_schema.tables WHERE table_schema = DATABASE() "
             "AND table_type = 'BASE TABLE'") == 11
    assert q("SELECT COUNT(*) v FROM information_schema.views WHERE table_schema = DATABASE()") == 4
    assert q("SELECT COUNT(*) v FROM information_schema.triggers WHERE trigger_schema = DATABASE()") == 4
    names = {r["n"] for r in db.query(conn, "SELECT routine_name n FROM information_schema.routines "
                                            "WHERE routine_schema = DATABASE()")}
    assert {"sp_validate_placement", "sp_faculty_workload", "sp_room_utilization",
            "sp_list_clashes", "sp_hours_coverage"} <= names


def test_seed_data_sizes(conn):
    want = {"Department": 3, "Faculty": 20, "Course": 30, "Section": 10, "Student": 120,
            "Classroom": 25, "TimeSlot": 48, "Teaches": 62, "Section_Course": 50, "Enrollment": 600}
    got = {t: scalar(conn, f"SELECT COUNT(*) v FROM {t}") for t in want}
    assert got == want


def test_check_constraint_rejects_non_positive_credits(conn):
    """CHECK constraints are enforced from MySQL 8.0.16 / MariaDB 10.2 (errors 3819 / 4025)."""
    cur = conn.cursor()
    with pytest.raises(mysql.connector.Error) as exc:
        cur.execute("INSERT INTO Course VALUES ('ZZ999', 'Bad', 0, 3, 'THEORY', 1)")
    assert exc.value.errno in (3819, 4025)


# --------------------------------------------- hard constraints: no double booking
@pytest.mark.parametrize("label, override, expected_word", [
    ("room clash", dict(section_id=2, faculty_id=2, room_id=1, slot_id=1), "room"),
    ("faculty clash", dict(section_id=2, faculty_id=1, room_id=2, slot_id=1), "faculty"),
    ("section clash", dict(course_id="CS202", faculty_id=3, room_id=2, slot_id=1, section_id=1), "section"),
])
def test_unique_keys_reject_double_booking(conn, base_row, label, override, expected_word):
    with pytest.raises(mysql.connector.Error) as exc:
        insert(conn, **override)
    assert exc.value.errno == 1062, label
    assert expected_word in utils.describe_mysql_error(exc.value)


def test_same_slot_is_fine_in_another_year(conn, base_row):
    """The uniqueness is per academic year: the identical row in another year is accepted."""
    cur = conn.cursor()
    cur.execute(SQL, {**BASE, "year": "2098-2099"})
    cur.execute("DELETE FROM Timetable WHERE academic_year = '2098-2099'")


# ------------------------------------------------------- triggers (rules 5001-5003)
def test_trigger_rejects_room_smaller_than_section(conn):
    small = scalar(conn, "SELECT room_id v FROM Classroom WHERE room_type = 'LECTURE' AND capacity < "
                         "(SELECT strength FROM Section WHERE section_id = 1) LIMIT 1")
    with pytest.raises(mysql.connector.Error) as exc:
        insert(conn, room_id=small)
    assert exc.value.errno == 5001
    assert "capacity" in utils.describe_mysql_error(exc.value)


def test_trigger_rejects_theory_in_lab_room(conn):
    lab = scalar(conn, "SELECT room_id v FROM Classroom WHERE room_type = 'LAB' LIMIT 1")
    with pytest.raises(mysql.connector.Error) as exc:
        insert(conn, room_id=lab)                       # CS201 is THEORY
    assert exc.value.errno == 5002


def test_trigger_rejects_lab_in_lecture_room(conn):
    with pytest.raises(mysql.connector.Error) as exc:
        insert(conn, course_id="CS205L", room_id=1)     # CS205L is LAB, room 1 is LECTURE
    assert exc.value.errno == 5002


def test_trigger_keeps_section_course_semester_consistent(conn):
    cur = conn.cursor()
    with pytest.raises(mysql.connector.Error) as exc:   # section 1 is semester 3
        cur.execute("INSERT INTO Section_Course (section_id, course_id, semester) VALUES (1, 'CS301', 5)")
    assert exc.value.errno == 5003


# ------------------------------------------------- composite foreign keys (M2 / M3)
def test_faculty_must_teach_the_course(conn):
    with pytest.raises(mysql.connector.Error) as exc:   # faculty 20 (MECH) does not teach CS201
        insert(conn, faculty_id=20)
    assert exc.value.errno == 1452
    assert "fk_tt_teaches" in str(exc.value)
    assert "Teaches" in utils.describe_mysql_error(exc.value)


def test_course_must_be_in_the_section_plan(conn):
    with pytest.raises(mysql.connector.Error) as exc:   # EC201 is taught by faculty 11 but not in section 1's plan
        insert(conn, course_id="EC201", faculty_id=11)
    assert exc.value.errno == 1452
    assert "fk_tt_section_course" in str(exc.value)


def test_teaches_row_in_use_cannot_be_deleted(conn, base_row):
    cur = conn.cursor()
    with pytest.raises(mysql.connector.Error) as exc:
        cur.execute("DELETE FROM Teaches WHERE faculty_id = 1 AND course_id = 'CS201'")
    assert exc.value.errno == 1451                      # RESTRICT: still used by Timetable


# ------------------------------------------------------------- stored procedures
def test_clash_audit_flags_a_rule_violation_that_triggers_cannot_see(conn):
    """sp_list_clashes must catch drift: shrink a room AFTER a class was placed in it."""
    clear_year(conn)
    insert(conn)                                         # section 1 (60) in room 1 (60)
    cur = conn.cursor()
    cur.execute("UPDATE Classroom SET capacity = 10 WHERE room_id = 1")   # triggers do not watch this
    rows = db.call_procedure(conn, "sp_list_clashes", [TEST_YEAR])
    assert any(r["clash_type"] == "ROOM_CAPACITY_EXCEEDED" for r in rows)
    conn.rollback()                                      # restores the capacity too


def test_hours_coverage_reports_unscheduled_sections(conn):
    clear_year(conn)
    gaps = db.call_procedure(conn, "sp_hours_coverage", [TEST_YEAR])
    assert len(gaps) == 50                               # empty year: all 50 (section, course) pairs short
    assert {g["status"] for g in gaps} == {"UNDER_SCHEDULED"}
