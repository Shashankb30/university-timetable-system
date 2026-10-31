"""
The scheduler. The key idea: results are re-validated by an INDEPENDENT checker
written here from the raw tables (not by generator internals), and by the
database's own audit procedures.
"""
from collections import Counter, defaultdict

import pytest

import db
import generator
import utils
from conftest import TEST_YEAR


# ------------------------------------------------------------ independent checker
def hard_violations(conn, rows):
    """Every hard rule, recomputed from MySQL tables. rows = (course, faculty, room, slot, section, year)."""
    rooms = {r["room_id"]: r for r in db.query(conn, "SELECT * FROM Classroom")}
    courses = {r["course_id"]: r for r in db.query(conn, "SELECT * FROM Course")}
    sections = {r["section_id"]: r for r in db.query(conn, "SELECT * FROM Section")}
    teaches = {(r["faculty_id"], r["course_id"]) for r in db.query(conn, "SELECT * FROM Teaches")}
    plan = {(r["section_id"], r["course_id"]) for r in db.query(conn, "SELECT * FROM Section_Course")}
    bad = []
    for key_name, idx in (("room", 2), ("faculty", 1), ("section", 4)):
        for (k, slot), n in Counter((r[idx], r[3]) for r in rows).items():
            if n > 1:
                bad.append(f"{key_name} {k} double-booked in slot {slot}")
    for course, fac, room, slot, sec, _ in rows:
        c, r, s = courses[course], rooms[room], sections[sec]
        need = "LAB" if c["course_type"] == "LAB" else "LECTURE"
        if r["room_type"] != need:
            bad.append(f"{course} ({c['course_type']}) in {r['room_type']} room {room}")
        if r["capacity"] < s["strength"]:
            bad.append(f"room {room} too small for section {sec}")
        if (fac, course) not in teaches:
            bad.append(f"faculty {fac} does not teach {course}")
        if (sec, course) not in plan:
            bad.append(f"section {sec} does not take {course}")
    per = Counter((r[4], r[0]) for r in rows)
    for sec, course in plan:
        if per[(sec, course)] != courses[course]["weekly_hours"]:
            bad.append(f"section {sec} / {course}: {per[(sec, course)]} periods, needs {courses[course]['weekly_hours']}")
    return bad


# ----------------------------------------------------------------- pure helpers
@pytest.mark.parametrize("hours, kind, expected", [
    (3, "THEORY", [1, 1, 1]), (2, "LAB", [2]), (3, "LAB", [3]),
    (4, "LAB", [2, 2]), (5, "LAB", [3, 2]), (6, "LAB", [3, 3]),
])
def test_split_hours(hours, kind, expected):
    assert generator.split_hours(hours, kind) == expected


# -------------------------------------------------------------- generation (dry)
def test_dry_run_finds_a_complete_valid_timetable(conn):
    res = generator.generate_timetable(TEST_YEAR, dry_run=True, conn=conn)
    assert res.complete and res.placed == res.required == 154, res.report()
    assert not res.preflight and not res.unplaced and not res.committed
    assert hard_violations(conn, res.rows) == []


def test_soft_constraints_are_met_on_the_seed_data(conn):
    res = generator.generate_timetable(TEST_YEAR, dry_run=True, conn=conn)
    assert res.soft == {"same_day_repeats": 0, "faculty_long_runs": 0, "lab_blocks_split": 0}


def test_labs_are_consecutive_and_never_straddle_lunch(conn):
    res = generator.generate_timetable(TEST_YEAR, dry_run=True, conn=conn)
    slots = {s["slot_id"]: s for s in db.query(conn, "SELECT * FROM TimeSlot")}
    labs = {r["course_id"] for r in db.query(conn, "SELECT course_id FROM Course WHERE course_type='LAB'")}
    groups = defaultdict(list)
    for course, fac, room, slot, sec, _ in res.rows:
        if course in labs:
            groups[(course, sec, slots[slot]["day_of_week"])].append(slots[slot]["period_no"])
    assert groups
    for key, periods in groups.items():
        periods.sort()
        assert periods == list(range(periods[0], periods[0] + len(periods))), key        # consecutive
        assert not (periods[0] <= 4 < periods[-1]), f"{key} spans lunch (period 4 -> 5)"


def test_same_seed_gives_the_same_timetable(conn):
    a = generator.generate_timetable(TEST_YEAR, dry_run=True, conn=conn, seed=7)
    b = generator.generate_timetable(TEST_YEAR, dry_run=True, conn=conn, seed=7)
    assert a.rows == b.rows


# --------------------------------------------------------------- saving (TEST_YEAR)
def test_saved_timetable_passes_the_database_audits(saved_test_year, conn):
    res = saved_test_year
    saved = db.query(conn, "SELECT COUNT(*) v FROM Timetable WHERE academic_year = %s", (TEST_YEAR,))[0]["v"]
    assert saved == res.required == 154
    assert db.call_procedure(conn, "sp_list_clashes", [TEST_YEAR]) == []        # zero clashes
    assert db.call_procedure(conn, "sp_hours_coverage", [TEST_YEAR]) == []      # every course has its hours
    util = db.call_procedure(conn, "sp_room_utilization", [TEST_YEAR])
    assert len(util) == 25 and sum(u["used_slots"] for u in util) == 154


def test_refuses_to_overwrite_without_replace(saved_test_year):
    again = generator.generate_timetable(TEST_YEAR)                 # replace=False
    assert not again.committed and "already exist" in again.note
    assert generator.generate_timetable(TEST_YEAR, replace=True).committed


def test_save_is_all_or_nothing(saved_test_year, conn):
    """A bad row in the batch must roll back everything, keeping the previous timetable."""
    rows = list(saved_test_year.rows)
    rows[-1] = (rows[-1][0], 20, *rows[-1][2:])                      # faculty 20 does not teach that course
    with pytest.raises(generator.GenerationError) as exc:
        generator.save_timetable(conn, TEST_YEAR, rows)
    assert "fk_tt_teaches" in str(exc.value)
    count = db.query(conn, "SELECT COUNT(*) v FROM Timetable WHERE academic_year = %s", (TEST_YEAR,))[0]["v"]
    assert count == 154                                              # old timetable survived


# --------------------------------------------------------------- failure reporting
def test_preflight_names_a_course_nobody_teaches(conn):
    P = generator.Problem(conn)
    P.teach["CS201"] = []                                            # in-memory only, DB untouched
    issues = generator.preflight(P, generator.make_tasks(P))
    assert any("CS201" in i and "no faculty" in i for i in issues)


def test_preflight_catches_a_faculty_pool_that_is_too_small(conn):
    P = generator.Problem(conn)
    P.courses["CS201"]["weekly_hours"] = 200
    issues = generator.preflight(P, generator.make_tasks(P))
    assert issues, "expected an impossibility to be detected"


def test_diagnosis_explains_why_a_class_cannot_be_placed(conn):
    P = generator.Problem(conn)
    sched = generator.Scheduler(P, __import__("random").Random(1))
    task = generator.make_tasks(P)[0]
    for slot in P.slot_ids:                                          # section fully busy
        sched.state.busy_sec.add((task.section_id, slot))
    assert sched.candidates(task) == []
    assert "already used by the section" in sched.diagnose(task)


def test_impossible_input_reports_instead_of_crashing(conn):
    """No lecture room is large enough: pre-flight explains it, nothing is written."""
    cur = conn.cursor()
    cur.execute("UPDATE Classroom SET capacity = 5 WHERE room_type = 'LECTURE'")   # rolled back by fixture
    res = generator.generate_timetable(TEST_YEAR, conn=conn, replace=True)
    assert not res.complete and not res.committed and res.preflight
    assert any("LECTURE room" in p for p in res.preflight)


# ------------------------------------------------------------ "is this class allowed?"
def test_check_proposed_class_rules(saved_test_year, conn):
    course, fac, room, slot, sec, _ = saved_test_year.rows[0]
    kinds = lambda **kw: {r["rule"]: r for r in generator.check_proposed_class(
        conn, TEST_YEAR, kw.get("sec", sec), kw.get("course", course), kw.get("fac", fac),
        kw.get("room", room), kw.get("slot", slot))}

    same = kinds()                                   # exactly an existing class: everything clashes
    assert not same["Section is free in this slot"]["ok"]
    assert not same["Faculty is free in this slot"]["ok"]
    assert not same["Room is free in this slot"]["ok"]

    lab_room = db.query(conn, "SELECT room_id FROM Classroom WHERE room_type='LAB' LIMIT 1")[0]["room_id"]
    wrong_type = kinds(room=lab_room)
    assert not wrong_type["Room type fits the course"]["ok"]

    not_taught = kinds(fac=20)
    assert not not_taught["Faculty is qualified (Teaches)"]["ok"]
    assert all(r["hard"] for r in same.values() if r["rule"] != "Section still needs hours for this course")
