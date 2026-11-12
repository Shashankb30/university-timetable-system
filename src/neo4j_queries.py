"""
neo4j_queries.py -- Python access to the documented Cypher queries.

The Cypher text lives ONLY in cypher/queries.cypher (documented there);
`load_queries()` parses its "// @query <name>" markers, so the functions
below run exactly the documented statements.

    Q1  impact_analysis            Q7  faculty_workload_ranking
    Q2  substitute_finder          Q8  move_class_ripple
    Q3  most_conflicted_courses    Q9  cross_department_teaching
    Q4  free_rooms                 Q10 course_conflict_neighbours
    Q5  heaviest_day_students      Q11/12 node_counts / relationship_counts
    Q6  faculty_shortest_path
    + faculty_absence_plan(): Q1 + Q2 combined for the UI's "faculty absent" tool

All functions return plain Python lists/dicts (no Neo4j objects) so they
drop straight into pandas / Streamlit.

CLI:   python src/neo4j_queries.py check    # test Neo4j connection and show which settings are used
       python src/neo4j_queries.py list     # query names
       python src/neo4j_queries.py demo     # run every query with sample inputs
"""
from __future__ import annotations

import re
import sys
from collections import defaultdict
from pathlib import Path

import utils

QUERY_FILE = utils.PROJECT_ROOT / "cypher" / "queries.cypher"
_MARKER = re.compile(r"^\s*//\s*@query\s+(\w+)\s*$")

_queries_cache: dict[str, str] | None = None
_driver = None


# --------------------------------------------------------------- plumbing
def load_queries(path: str | Path | None = None) -> dict[str, str]:
    """Parse queries.cypher into {name: cypher text}. Comments are kept (Cypher ignores them)."""
    text = Path(path or QUERY_FILE).read_text(encoding="utf-8")
    blocks: dict[str, list[str]] = {}
    name = None
    for line in text.splitlines():
        m = _MARKER.match(line)
        if m:
            name = m.group(1)
            blocks[name] = []
        elif name is not None:
            blocks[name].append(line)
    out = {}
    for n, lines in blocks.items():
        body = "\n".join(lines).strip()
        out[n] = body[:-1].rstrip() if body.endswith(";") else body   # drop the trailing ';'
    return out


def queries() -> dict[str, str]:
    global _queries_cache
    if _queries_cache is None:
        _queries_cache = load_queries()
    return _queries_cache


def get_driver():
    """One shared driver per process (Neo4j drivers are thread-safe and pooled)."""
    global _driver
    if _driver is None:
        drv = utils.get_neo4j_driver()
        try:
            drv.verify_connectivity()          # fail fast instead of retrying for ~30 s
        except Exception:
            drv.close()
            raise
        _driver = drv
    return _driver


def close_driver() -> None:
    global _driver
    if _driver is not None:
        _driver.close()
        _driver = None


def run_query(name: str, params: dict | None = None, session=None) -> list[dict]:
    """
    Run a named query from queries.cypher (read transaction) and return rows
    as dicts. Pass `session` to reuse one (the ETL does); otherwise a
    short-lived session on the shared driver is used.
    """
    qs = queries()
    if name not in qs:
        raise KeyError(f"unknown query '{name}'. Available: {', '.join(qs)}")
    cypher, params = qs[name], dict(params or {})

    def work(tx):
        return tx.run(cypher, params).data()

    if session is not None:
        return session.execute_read(work)
    with get_driver().session(database=utils.neo4j_settings()["database"]) as s:
        return s.execute_read(work)


def _day(day: str | None) -> str | None:
    """Normalise a day argument to 'MON'..'SAT' (or None for the whole week)."""
    if day is None or str(day).strip() == "":
        return None
    d = str(day).strip().upper()[:3]
    if d not in utils.DAY_ORDER:
        raise ValueError(f"day must be one of {utils.DAY_ORDER}, got {day!r}")
    return d


def _year(year: str | None) -> str:
    return year or utils.academic_year()


# --------------------------------------------------------------- Q1 / Q2
def impact_analysis(faculty_id: int, academic_year: str | None = None,
                    day: str | None = None) -> list[dict]:
    """Q1: classes lost if `faculty_id` is absent (whole week or one day)."""
    return run_query("impact_analysis", {"faculty_id": int(faculty_id),
                                         "academic_year": _year(academic_year),
                                         "day": _day(day)})


def substitute_finder(faculty_id: int, academic_year: str | None = None,
                      day: str | None = None) -> list[dict]:
    """Q2 (raw): one row per (affected class, free same-department candidate)."""
    return run_query("substitute_finder", {"faculty_id": int(faculty_id),
                                           "academic_year": _year(academic_year),
                                           "day": _day(day)})


def faculty_absence_plan(faculty_id: int, academic_year: str | None = None,
                         day: str | None = None) -> dict:
    """
    Combine Q1 and Q2 for the UI's "faculty absent" tool.

    Returns {"classes": [...], "summary": {...}} where every class carries a
    `substitutes` list (QUALIFIED first, then RELATED) and a `status`:
        COVERABLE      >= 1 QUALIFIED substitute is free (assignable directly)
        NEEDS_TEACHES  only RELATED candidates (a Teaches row must be added first)
        UNCOVERED      nobody in the department is free and suitable
    """
    classes = impact_analysis(faculty_id, academic_year, day)
    by_class: dict[int, list[dict]] = defaultdict(list)
    for row in substitute_finder(faculty_id, academic_year, day):
        by_class[row["timetable_id"]].append(row)

    counts = {"COVERABLE": 0, "NEEDS_TEACHES": 0, "UNCOVERED": 0}
    for c in classes:
        subs = by_class.get(c["timetable_id"], [])
        c["substitutes"] = subs
        if any(s["fit"] == "QUALIFIED" for s in subs):
            c["status"] = "COVERABLE"
        elif subs:
            c["status"] = "NEEDS_TEACHES"
        else:
            c["status"] = "UNCOVERED"
        counts[c["status"]] += 1

    sections = {c["section_id"]: c["students"] for c in classes}   # students per distinct section
    summary = {"classes_affected": len(classes),
               "sections_affected": len(sections),
               "students_affected": sum(sections.values()),
               "coverable": counts["COVERABLE"],
               "needs_teaches_row": counts["NEEDS_TEACHES"],
               "uncovered": counts["UNCOVERED"]}
    return {"classes": classes, "summary": summary}


# --------------------------------------------------------------- Q3 - Q10
def most_conflicted_courses(limit: int = 10) -> list[dict]:
    """Q3: courses that collide with the most (and most heavily shared) others."""
    return run_query("most_conflicted_courses", {"limit": int(limit)})


def free_rooms(day: str, period: int, min_capacity: int = 0, room_type: str | None = None,
               academic_year: str | None = None) -> list[dict]:
    """Q4: rooms free in (day, period), optionally by minimum capacity and type."""
    d = _day(day)
    if d is None:
        raise ValueError("day is required")
    if room_type is not None:
        room_type = room_type.upper()
        if room_type not in ("LECTURE", "LAB"):
            raise ValueError("room_type must be 'LECTURE', 'LAB' or None")
    return run_query("free_rooms", {"day": d, "period": int(period),
                                    "min_capacity": int(min_capacity),
                                    "room_type": room_type,
                                    "academic_year": _year(academic_year)})


def heaviest_day_students(limit: int = 10, academic_year: str | None = None) -> list[dict]:
    """Q5: students whose busiest day has the most periods."""
    rows = run_query("heaviest_day_students", {"limit": int(limit),
                                               "academic_year": _year(academic_year)})
    for r in rows:                                   # Cypher returns tied days in arbitrary order
        r["days"] = sorted(r["days"], key=utils.DAY_ORDER.index)
    return rows


def faculty_shortest_path(from_id: int, to_id: int) -> dict:
    """
    Q6: shortest chain linking two faculty through shared courses/sections.
    Returns {"connected": bool, "hops": int | None, "path": [{kind, name}, ...]}.
    """
    if int(from_id) == int(to_id):
        raise ValueError("from_id and to_id must be different faculty")
    rows = run_query("faculty_shortest_path", {"from_id": int(from_id), "to_id": int(to_id)})
    if not rows:
        return {"connected": False, "hops": None, "path": []}
    return {"connected": True, "hops": rows[0]["hops"], "path": rows[0]["path"]}


def faculty_workload_ranking(academic_year: str | None = None) -> list[dict]:
    """Q7: faculty ranked by periods per week (adds a dense `rank`)."""
    rows = run_query("faculty_workload", {"academic_year": _year(academic_year)})
    rank, prev = 0, None
    for r in rows:
        if r["periods"] != prev:
            rank, prev = rank + 1, r["periods"]
        r["rank"] = rank
    return rows


def move_class_ripple(timetable_id: int, day: str, period: int) -> dict:
    """
    Q8: what happens if class `timetable_id` moves to (day, period).
    Returns {"moved": {...}, "clashes": [...], "free_move": bool, "students_displaced": int}.
    """
    d = _day(day)
    if d is None:
        raise ValueError("day is required")
    rows = run_query("move_class_ripple", {"timetable_id": int(timetable_id),
                                           "day": d, "period": int(period)})
    if not rows:
        raise LookupError(f"no class with timetable_id={timetable_id}, or no slot {d} P{period}")
    first = rows[0]
    moved = {k: first[k] for k in ("moved_timetable_id", "moved_course", "moved_section",
                                   "from_day", "from_period", "to_day", "to_period")}
    clashes = [r for r in rows if r["clash_timetable_id"] is not None]
    return {"moved": moved, "clashes": clashes, "free_move": not clashes,
            "students_displaced": sum(r["students_displaced"] for r in clashes)}


def cross_department_teaching() -> list[dict]:
    """Q9: faculty qualified for another department's courses."""
    return run_query("cross_department_teaching")


def course_conflict_neighbours(course_id: str) -> list[dict]:
    """Q10: courses sharing faculty/students with `course_id`."""
    return run_query("course_conflict_neighbours", {"course_id": course_id})


def node_counts(session=None) -> dict[str, int]:
    """Q11: {label: count}."""
    return {r["label"]: r["nodes"] for r in run_query("node_counts", session=session)}


def relationship_counts(session=None) -> dict[str, int]:
    """Q12: {relationship type: count}."""
    return {r["rel_type"]: r["relationships"] for r in run_query("relationship_counts", session=session)}


# --------------------------------------------------------------- CLI demo
def _demo() -> None:
    year = utils.academic_year()
    show = lambda title, rows, n=8: print(f"\n=== {title}\n{utils.format_table(rows, max_rows=n)}")

    print(f"Academic year: {year}")
    show("Graph size: nodes", [{"label": k, "count": v} for k, v in node_counts().items()], 20)
    show("Graph size: relationships", [{"type": k, "count": v} for k, v in relationship_counts().items()], 20)

    plan = faculty_absence_plan(1, year, "MON")
    print(f"\n=== Q1/Q2 Faculty 1 absent on MON: {plan['summary']}")
    for c in plan["classes"][:4]:
        best = ", ".join(f"{s['candidate']} ({s['fit']})" for s in c["substitutes"][:3]) or "none"
        print(f"  {c['day']} P{c['period']} {c['course_id']} {c['section']}: {c['status']} -> {best}")

    show("Q3 Most conflicted courses", most_conflicted_courses(5))
    show("Q4 Free LECTURE rooms (>=60 seats) MON period 1", free_rooms("MON", 1, 60, "LECTURE", year), 6)
    show("Q5 Students with the heaviest day", heaviest_day_students(5, year))
    sp = faculty_shortest_path(1, 11)
    print("\n=== Q6 Shortest path faculty 1 -> 11:",
          f"{sp['hops']} hops" if sp["connected"] else "not connected")
    if sp["connected"]:
        print("   " + "  ->  ".join(f"{n['kind']}:{n['name']}" for n in sp["path"]))
    show("Q7 Faculty workload ranking", faculty_workload_ranking(year), 6)
    mine = impact_analysis(1, year)
    if mine:
        r = move_class_ripple(mine[0]["timetable_id"], "FRI", 8)
        print(f"\n=== Q8 Move class {r['moved']['moved_timetable_id']} "
              f"({r['moved']['moved_course']}) to FRI P8: "
              f"{'free move' if r['free_move'] else str(len(r['clashes'])) + ' clash(es)'}")
        print(utils.format_table(r["clashes"], ["clash_course", "clash_section", "clash_faculty", "clash_types"]))
    show("Q9 Cross-department teaching", cross_department_teaching())
    show("Q10 Conflict neighbours of CS301", course_conflict_neighbours("CS301"), 6)


def main(argv=None) -> int:
    args = (argv if argv is not None else sys.argv[1:]) or ["list"]
    cmd = args[0]
    if cmd == "list":
        for name, text in queries().items():
            first = next((line.lstrip("/ ").strip() for line in text.splitlines()
                          if line.strip().startswith("//")), "")
            print(f"{name:28s} {first}")
        return 0
    if cmd == "check":                      # diagnose connection / password problems
        print("\n".join(utils.describe_neo4j_settings()))
        try:
            get_driver()
            print("Connection: OK (Neo4j reachable and login accepted)")
            return 0
        except Exception as exc:
            print(f"Connection: FAILED ({type(exc).__name__}): {str(exc)[:160]}")
            return 2
        finally:
            close_driver()
    if cmd == "demo":
        try:
            _demo()
        except Exception as exc:
            name = type(exc).__name__
            if name in ("ServiceUnavailable", "AuthError") or (
                    name == "RuntimeError" and "NEO4J" in str(exc)):
                print(f"\nCould not use Neo4j ({name}): {exc}\n"
                      "Check that Neo4j is running (bin/neo4j console) and that NEO4J_URI / "
                      "NEO4J_USER / NEO4J_PASSWORD in .env are correct.\n"
                      "Diagnose with: python src/neo4j_queries.py check", file=sys.stderr)
                return 2
            raise
        finally:
            close_driver()
        return 0
    print("usage: python src/neo4j_queries.py [list|demo|check]")
    return 2


if __name__ == "__main__":
    sys.exit(main())
