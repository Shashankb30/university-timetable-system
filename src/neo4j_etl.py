"""
neo4j_etl.py -- export MySQL (system of record) into Neo4j (graph analytics).

IDEMPOTENT: every node and relationship is written with MERGE on its key, so
running the ETL twice produces the same graph. Order of work:

  1. constraints + indexes   (cypher/constraints.cypher, created FIRST)
  2. nodes                   Department, Faculty, Course, Section, Student, Classroom, TimeSlot
  3. static relationships    WORKS_IN, OFFERED_BY, BELONGS_TO, STUDIES_IN, TEACHES, ENROLLED_IN, TAKES
  4. timetable               SCHEDULED_IN (one per Timetable row) + derived TEACHES_SECTION.
                             The affected year's old SCHEDULED_IN / TEACHES_SECTION are deleted
                             first, so a regenerated timetable leaves no ghost classes.
  5. derived CONFLICTS_WITH  rebuilt inside Neo4j from TEACHES / ENROLLED_IN
                             (shared_count = shared faculty + shared students,
                              identical to utils.build_conflict_graph)
  6. verification            graph counts compared with expected counts from MySQL

Run:   python src/neo4j_etl.py                 # all academic years in Timetable
       python src/neo4j_etl.py --year 2026-2027
       python src/neo4j_etl.py --reset         # wipe the graph first
Env:   MYSQL_* (see db.py) and NEO4J_URI / NEO4J_USER / NEO4J_PASSWORD / NEO4J_DATABASE
"""
from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

import mysql.connector

import db
import neo4j_queries
import utils

BATCH_SIZE = 500
CONSTRAINTS_FILE = utils.PROJECT_ROOT / "cypher" / "constraints.cypher"


# ------------------------------------------------------------------ helpers
def read_statements(path: str | Path) -> list[str]:
    """Split a .cypher file into statements: drop '//' comment lines, split on ';'."""
    lines = [l for l in Path(path).read_text(encoding="utf-8").splitlines()
             if not l.strip().startswith("//")]
    return [s.strip() for s in "\n".join(lines).split(";") if s.strip()]


def dept_code(name: str) -> str:
    """'Computer Science and Engineering' -> 'CSE' (shared helper in utils)."""
    return utils.dept_code(name)


def _write(session, cypher: str, **params) -> None:
    """Run one write statement in its own transaction (retried by the driver)."""
    session.execute_write(lambda tx: tx.run(cypher, params).consume())


def _batched(session, cypher: str, rows: list[dict], what: str, log) -> int:
    """UNWIND `rows` into `cypher` in chunks of BATCH_SIZE; returns rows sent."""
    for i in range(0, len(rows), BATCH_SIZE):
        _write(session, cypher, rows=rows[i:i + BATCH_SIZE])
    log(f"  {what:<28s} {len(rows):>6d} rows")
    return len(rows)


# ----------------------------------------------------------- Cypher (writes)
Q_NODES = {
    "Department": """
UNWIND $rows AS row
MERGE (n:Department {dept_id: row.dept_id})
SET n.dept_name = row.dept_name, n.building = row.building, n.dept_code = row.dept_code""",
    "Faculty": """
UNWIND $rows AS row
MERGE (n:Faculty {faculty_id: row.faculty_id})
SET n.name = row.name, n.email = row.email, n.dept_id = row.dept_id""",
    "Course": """
UNWIND $rows AS row
MERGE (n:Course {course_id: row.course_id})
SET n.course_name = row.course_name, n.credits = row.credits,
    n.weekly_hours = row.weekly_hours, n.course_type = row.course_type, n.dept_id = row.dept_id""",
    "Section": """
UNWIND $rows AS row
MERGE (n:Section {section_id: row.section_id})
SET n.section_name = row.section_name, n.semester = row.semester,
    n.strength = row.strength, n.dept_id = row.dept_id, n.label = row.label""",
    "Student": """
UNWIND $rows AS row
MERGE (n:Student {student_id: row.student_id})
SET n.name = row.name, n.email = row.email, n.section_id = row.section_id""",
    "Classroom": """
UNWIND $rows AS row
MERGE (n:Classroom {room_id: row.room_id})
SET n.room_no = row.room_no, n.building = row.building,
    n.capacity = row.capacity, n.room_type = row.room_type""",
    "TimeSlot": """
UNWIND $rows AS row
MERGE (n:TimeSlot {slot_id: row.slot_id})
SET n.day_of_week = row.day_of_week, n.day_no = row.day_no, n.period_no = row.period_no,
    n.start_time = row.start_time, n.end_time = row.end_time""",
}

Q_RELS = {
    "WORKS_IN": """
UNWIND $rows AS row
MATCH (f:Faculty {faculty_id: row.faculty_id}), (d:Department {dept_id: row.dept_id})
MERGE (f)-[:WORKS_IN]->(d)""",
    "OFFERED_BY": """
UNWIND $rows AS row
MATCH (c:Course {course_id: row.course_id}), (d:Department {dept_id: row.dept_id})
MERGE (c)-[:OFFERED_BY]->(d)""",
    "BELONGS_TO": """
UNWIND $rows AS row
MATCH (s:Section {section_id: row.section_id}), (d:Department {dept_id: row.dept_id})
MERGE (s)-[:BELONGS_TO]->(d)""",
    "STUDIES_IN": """
UNWIND $rows AS row
MATCH (st:Student {student_id: row.student_id}), (s:Section {section_id: row.section_id})
MERGE (st)-[:STUDIES_IN]->(s)""",
    "TEACHES": """
UNWIND $rows AS row
MATCH (f:Faculty {faculty_id: row.faculty_id}), (c:Course {course_id: row.course_id})
MERGE (f)-[:TEACHES]->(c)""",
    "ENROLLED_IN": """
UNWIND $rows AS row
MATCH (st:Student {student_id: row.student_id}), (c:Course {course_id: row.course_id})
MERGE (st)-[:ENROLLED_IN]->(c)""",
    "TAKES": """
UNWIND $rows AS row
MATCH (s:Section {section_id: row.section_id}), (c:Course {course_id: row.course_id})
MERGE (s)-[t:TAKES]->(c)
SET t.semester = row.semester""",
}

# one SCHEDULED_IN per Timetable row; MERGE key = timetable_id (unique constraint)
Q_SCHEDULED = """
UNWIND $rows AS row
MATCH (c:Course {course_id: row.course_id}), (sec:Section {section_id: row.section_id})
MERGE (c)-[s:SCHEDULED_IN {timetable_id: row.timetable_id}]->(sec)
SET s.academic_year = row.academic_year, s.course_id = row.course_id,
    s.section_id = row.section_id, s.faculty_id = row.faculty_id,
    s.faculty_name = row.faculty_name, s.room_id = row.room_id,
    s.room_label = row.room_label, s.slot_id = row.slot_id,
    s.day_of_week = row.day_of_week, s.day_no = row.day_no, s.period_no = row.period_no,
    s.start_time = row.start_time, s.end_time = row.end_time"""

# derived: which faculty actually teaches which section (and how many periods)
Q_TEACHES_SECTION = """
MATCH (c:Course)-[s:SCHEDULED_IN {academic_year: $year}]->(sec:Section)
WITH s.faculty_id AS fid, c, sec, count(s) AS periods
MATCH (f:Faculty {faculty_id: fid})
MERGE (f)-[t:TEACHES_SECTION {academic_year: $year, course_id: c.course_id}]->(sec)
SET t.periods = periods"""

# derived CONFLICTS_WITH: same definition as utils.build_conflict_graph
Q_CONFLICTS = [
    "MATCH ()-[r:CONFLICTS_WITH]->() DELETE r",
    """MATCH (a:Course)<-[:TEACHES]-(f:Faculty)-[:TEACHES]->(b:Course)
WHERE a.course_id < b.course_id
WITH a, b, count(DISTINCT f) AS shared_faculty
MERGE (a)-[r:CONFLICTS_WITH]->(b)
SET r.shared_faculty = shared_faculty""",
    """MATCH (a:Course)<-[:ENROLLED_IN]-(st:Student)-[:ENROLLED_IN]->(b:Course)
WHERE a.course_id < b.course_id
WITH a, b, count(DISTINCT st) AS shared_students
MERGE (a)-[r:CONFLICTS_WITH]->(b)
SET r.shared_students = shared_students""",
    """MATCH ()-[r:CONFLICTS_WITH]->()
SET r.shared_faculty = coalesce(r.shared_faculty, 0),
    r.shared_students = coalesce(r.shared_students, 0),
    r.shared_count = coalesce(r.shared_faculty, 0) + coalesce(r.shared_students, 0)""",
]

Q_TIMETABLE_SQL = """
SELECT t.timetable_id, t.course_id, t.faculty_id, f.name AS faculty_name,
       t.room_id, CONCAT(r.building, '-', r.room_no) AS room_label,
       t.slot_id, ts.day_of_week, ts.period_no, ts.start_time, ts.end_time,
       t.section_id, t.academic_year
FROM Timetable t
JOIN Faculty   f  ON f.faculty_id = t.faculty_id
JOIN Classroom r  ON r.room_id    = t.room_id
JOIN TimeSlot  ts ON ts.slot_id   = t.slot_id
WHERE t.academic_year = %s
ORDER BY t.timetable_id"""


# ---------------------------------------------------------------- the ETL
def apply_schema(session, log=print) -> None:
    """Step 1: constraints and indexes (all IF NOT EXISTS)."""
    stmts = read_statements(CONSTRAINTS_FILE)
    for s in stmts:
        _write(session, s)
    log(f"  constraints/indexes          {len(stmts):>6d} statements")


def load_nodes(session, conn, log=print) -> None:
    depts = db.query(conn, "SELECT dept_id, dept_name, building FROM Department")
    for d in depts:
        d["dept_code"] = dept_code(d["dept_name"])
    _batched(session, Q_NODES["Department"], depts, "Department", log)

    _batched(session, Q_NODES["Faculty"],
             db.query(conn, "SELECT faculty_id, name, email, dept_id FROM Faculty"), "Faculty", log)
    _batched(session, Q_NODES["Course"],
             db.query(conn, "SELECT course_id, course_name, credits, weekly_hours, course_type, "
                            "dept_id FROM Course"), "Course", log)

    sections = db.query(conn, "SELECT s.section_id, s.section_name, s.semester, s.strength, "
                              "s.dept_id, d.dept_name FROM Section s JOIN Department d "
                              "ON d.dept_id = s.dept_id")
    for s in sections:
        s["label"] = f"{dept_code(s.pop('dept_name'))} Sem {s['semester']} {s['section_name']}"
    _batched(session, Q_NODES["Section"], sections, "Section", log)

    _batched(session, Q_NODES["Student"],
             db.query(conn, "SELECT student_id, name, email, section_id FROM Student"), "Student", log)
    _batched(session, Q_NODES["Classroom"],
             db.query(conn, "SELECT room_id, room_no, building, capacity, room_type FROM Classroom"),
             "Classroom", log)

    slots = db.query(conn, "SELECT slot_id, day_of_week, period_no, start_time, end_time FROM TimeSlot")
    for s in slots:
        s["day_no"] = utils.DAY_NO[s["day_of_week"]]
        s["start_time"], s["end_time"] = utils.fmt_time(s["start_time"]), utils.fmt_time(s["end_time"])
    _batched(session, Q_NODES["TimeSlot"], slots, "TimeSlot", log)


def load_static_relationships(session, conn, log=print) -> None:
    src = {
        "WORKS_IN": "SELECT faculty_id, dept_id FROM Faculty",
        "OFFERED_BY": "SELECT course_id, dept_id FROM Course",
        "BELONGS_TO": "SELECT section_id, dept_id FROM Section",
        "STUDIES_IN": "SELECT student_id, section_id FROM Student",
        "TEACHES": "SELECT faculty_id, course_id FROM Teaches",
        "ENROLLED_IN": "SELECT student_id, course_id FROM Enrollment",
        "TAKES": "SELECT section_id, course_id, semester FROM Section_Course",
    }
    for rel, sql in src.items():
        _batched(session, Q_RELS[rel], db.query(conn, sql), rel, log)


def load_timetable(session, conn, years: list[str], all_years: bool, log=print) -> None:
    """Rebuild SCHEDULED_IN and TEACHES_SECTION for the given year(s)."""
    if all_years:                       # full sync: drop every year, then reload all
        _write(session, "MATCH ()-[s:SCHEDULED_IN]->() DELETE s")
        _write(session, "MATCH ()-[t:TEACHES_SECTION]->() DELETE t")
    for year in years:
        if not all_years:
            _write(session, "MATCH ()-[s:SCHEDULED_IN {academic_year: $year}]->() DELETE s", year=year)
            _write(session, "MATCH ()-[t:TEACHES_SECTION {academic_year: $year}]->() DELETE t", year=year)
        rows = db.query(conn, Q_TIMETABLE_SQL, (year,))
        for r in rows:
            r["day_no"] = utils.DAY_NO[r["day_of_week"]]
            r["start_time"], r["end_time"] = utils.fmt_time(r["start_time"]), utils.fmt_time(r["end_time"])
        _batched(session, Q_SCHEDULED, rows, f"SCHEDULED_IN [{year}]", log)
        _write(session, Q_TEACHES_SECTION, year=year)
        log(f"  TEACHES_SECTION [{year}]      derived")


def derive_conflicts(session, log=print) -> None:
    for stmt in Q_CONFLICTS:
        _write(session, stmt)
    log("  CONFLICTS_WITH               derived")


# -------------------------------------------------------------- verification
def expected_counts(conn, years: list[str]) -> dict:
    """What the graph should contain, computed from MySQL."""
    one = lambda sql, params=(): db.query(conn, sql, params)[0]["n"]
    nodes = {label: one(f"SELECT COUNT(*) AS n FROM {table}")
             for label, table in (("Department", "Department"), ("Faculty", "Faculty"),
                                  ("Course", "Course"), ("Section", "Section"),
                                  ("Student", "Student"), ("Classroom", "Classroom"),
                                  ("TimeSlot", "TimeSlot"))}
    rels = {"WORKS_IN": nodes["Faculty"], "OFFERED_BY": nodes["Course"],
            "BELONGS_TO": nodes["Section"], "STUDIES_IN": nodes["Student"],
            "TEACHES": one("SELECT COUNT(*) AS n FROM Teaches"),
            "ENROLLED_IN": one("SELECT COUNT(*) AS n FROM Enrollment"),
            "TAKES": one("SELECT COUNT(*) AS n FROM Section_Course")}
    sched = teaches_section = 0
    for y in years:
        sched += one("SELECT COUNT(*) AS n FROM Timetable WHERE academic_year = %s", (y,))
        teaches_section += one("SELECT COUNT(*) AS n FROM (SELECT DISTINCT faculty_id, section_id, "
                               "course_id FROM Timetable WHERE academic_year = %s) x", (y,))
    graph = utils.build_conflict_graph(conn)
    rels["SCHEDULED_IN"] = sched
    rels["TEACHES_SECTION"] = teaches_section
    rels["CONFLICTS_WITH"] = sum(len(nb) for nb in graph.values()) // 2    # each pair counted twice
    return {"nodes": nodes, "relationships": rels}


def actual_counts(session) -> dict:
    return {"nodes": neo4j_queries.node_counts(session),
            "relationships": neo4j_queries.relationship_counts(session)}


def compare_counts(expected: dict, actual: dict) -> list[str]:
    """Return a list of mismatch descriptions (empty = graph matches MySQL)."""
    problems = []
    for kind in ("nodes", "relationships"):
        for name, want in expected[kind].items():
            got = actual[kind].get(name, 0)
            if got != want:
                problems.append(f"{kind[:-1]} {name}: MySQL expects {want}, Neo4j has {got}")
    return problems


def run_etl(academic_year: str | None = None, reset: bool = False, log=print) -> dict:
    """
    Full ETL. `academic_year=None` syncs every year present in Timetable.
    Returns {"expected": ..., "actual": ..., "problems": [...], "ok": bool}.
    """
    t0 = time.monotonic()
    with db.connection() as conn:
        years = ([academic_year] if academic_year else
                 [r["academic_year"] for r in db.query(
                     conn, "SELECT DISTINCT academic_year FROM Timetable ORDER BY academic_year")])
        log(f"MySQL -> Neo4j ETL (years: {', '.join(years) or 'none; no timetable yet'})")
        driver = neo4j_queries.get_driver()
        driver.verify_connectivity()
        with driver.session(database=utils.neo4j_settings()["database"]) as session:
            if reset:
                _write(session, "MATCH (n) DETACH DELETE n")
                log("  graph wiped (--reset)")
            apply_schema(session, log)
            load_nodes(session, conn, log)
            load_static_relationships(session, conn, log)
            load_timetable(session, conn, years, all_years=academic_year is None, log=log)
            derive_conflicts(session, log)
            expected = expected_counts(conn, years)
            actual = actual_counts(session)
    problems = compare_counts(expected, actual)
    log(f"\nVerification: {'OK, graph matches MySQL' if not problems else 'MISMATCH'} "
        f"({time.monotonic() - t0:.1f}s)")
    for p in problems:
        log(f"  ! {p}")
    return {"expected": expected, "actual": actual, "problems": problems, "ok": not problems}


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Export MySQL timetable data into Neo4j.")
    ap.add_argument("--year", default=None, help="only (re)load this academic year's timetable")
    ap.add_argument("--reset", action="store_true", help="delete everything in the graph first")
    args = ap.parse_args(argv)
    try:
        result = run_etl(args.year, args.reset)
    except mysql.connector.Error as exc:
        print(f"\nCould not use MySQL: {exc}\n"
              "Check that the database server is running and MYSQL_* in .env are correct.",
              file=sys.stderr)
        return 2
    except Exception as exc:                      # friendly hint for the usual first-run problems
        name = type(exc).__name__
        if name == "RuntimeError" and "MYSQL" in str(exc):
            print(f"\nCould not use MySQL: {exc}", file=sys.stderr)
            return 2
        if name in ("ServiceUnavailable", "AuthError") or (
                name == "RuntimeError" and "NEO4J" in str(exc)):
            print(f"\nCould not use Neo4j ({name}): {exc}\n"
                  "Check that Neo4j is running (bin/neo4j console), and that NEO4J_URI / "
                  "NEO4J_USER / NEO4J_PASSWORD are set correctly.\n"
                  "Diagnose with: python src/neo4j_queries.py check", file=sys.stderr)
            return 2
        raise
    finally:
        neo4j_queries.close_driver()
    return 0 if result["ok"] else 1


if __name__ == "__main__":
    sys.exit(main())
