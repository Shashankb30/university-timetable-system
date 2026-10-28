"""
streamlit_app.py -- web UI for the University Timetable Generation System.

Run from the project root:   streamlit run app/streamlit_app.py

Pages
  Timetable          weekly grid per section / faculty / room (+ CSV download)
  Run generator      build a clash-free timetable (and refresh the Neo4j graph)
  Faculty absent     Neo4j: affected classes + suggested substitutes
  Clash checker      saved-timetable audit, "is this class allowed?" what-if,
                     and the ripple effect of moving a class (Neo4j)
  Statistics         workload, room utilisation and load charts

MySQL is the source of truth for everything except the "Faculty absent" tool
and the ripple what-if, which need Neo4j. If Neo4j is down those parts show a
clear message and the rest of the app keeps working.
"""
from __future__ import annotations

import sys
from datetime import time, timedelta
from decimal import Decimal
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

import pandas as pd
import streamlit as st

import db
import generator
import neo4j_etl
import neo4j_queries
import utils

st.set_page_config(page_title="University Timetable", page_icon="🗓️", layout="wide")

DAYS = utils.DAY_ORDER
STATUS_ICON = {"COVERABLE": "✅ Coverable", "NEEDS_TEACHES": "⚠️ Needs Teaches row",
               "UNCOVERED": "❌ Uncovered"}


# ============================================================ data access
def _clean(rows: list[dict]) -> list[dict]:
    """Make rows display-friendly: TIME -> 'HH:MM', Decimal -> float."""
    out = []
    for r in rows:
        c = {}
        for k, v in r.items():
            if isinstance(v, (timedelta, time)):
                v = utils.fmt_time(v)
            elif isinstance(v, Decimal):
                v = float(v)
            c[k] = v
        out.append(c)
    return out


@st.cache_data(ttl=20, show_spinner=False)
def sql(query: str, params: tuple = ()) -> list[dict]:
    """Cached SELECT (20 s). Cache is cleared whenever the generator saves a timetable."""
    with db.connection() as conn:
        return db.query(conn, query, params)


@st.cache_data(ttl=20, show_spinner=False)
def proc(name: str, args: tuple = ()) -> list[dict]:
    """Cached stored-procedure call (first result set)."""
    with db.connection() as conn:
        return db.call_procedure(conn, name, args)


@st.cache_data(ttl=10, show_spinner=False)
def neo4j_status() -> tuple[bool, str]:
    try:
        neo4j_queries.get_driver()
        return True, "connected"
    except Exception as exc:                       # noqa: BLE001 -- show any reason in the sidebar
        return False, type(exc).__name__


def run_neo4j(fn, *args, **kwargs):
    """Call a Neo4j function; returns (result, None) or (None, friendly error text)."""
    try:
        return fn(*args, **kwargs), None
    except Exception as exc:                       # noqa: BLE001
        name = type(exc).__name__
        if name in ("ServiceUnavailable", "AuthError", "SessionExpired") or (
                name == "RuntimeError" and "NEO4J" in str(exc)):
            return None, (f"Neo4j is not reachable ({name}). Start it (`bin/neo4j console`), check "
                          "`python src/neo4j_queries.py check`, then reload this page.")
        return None, f"{name}: {exc}"


def available_years() -> list[str]:
    years = [r["y"] for r in sql("SELECT DISTINCT academic_year AS y FROM Timetable ORDER BY y DESC")]
    default = utils.academic_year()
    if default not in years:
        years.append(default)
    return years


def timetable_rows(year: str) -> int:
    return sql("SELECT COUNT(*) AS n FROM Timetable WHERE academic_year = %s", (year,))[0]["n"]


def graph_stale_message(year: str) -> str | None:
    """Compare the Neo4j graph with MySQL for `year`; None when equal or Neo4j unreachable."""
    rows, err = run_neo4j(neo4j_queries.faculty_workload_ranking, year)
    if err:
        return None
    in_graph, in_mysql = sum(r["periods"] for r in rows), timetable_rows(year)
    if in_graph != in_mysql:
        return (f"The Neo4j graph has {in_graph} classes for {year} but MySQL has {in_mysql}. "
                "Results below may be out of date.")
    return None


def etl_button(year: str, key: str) -> None:
    """Button that refreshes the Neo4j graph from MySQL for `year`."""
    if st.button(f"Update Neo4j graph for {year}", key=key):
        lines: list[str] = []
        with st.spinner("Exporting MySQL -> Neo4j ..."):
            try:
                result = neo4j_etl.run_etl(year, log=lines.append)
            except Exception as exc:               # noqa: BLE001
                st.error(f"ETL failed: {type(exc).__name__}: {exc}")
                return
            finally:
                neo4j_queries.close_driver()
                neo4j_status.clear()
        (st.success if result["ok"] else st.error)(
            "Neo4j graph matches MySQL." if result["ok"] else "Graph does not match MySQL:\n"
            + "\n".join(result["problems"]))
        with st.expander("ETL log"):
            st.code("\n".join(lines))


# =============================================================== sidebar
def sidebar() -> tuple[str, str]:
    st.sidebar.title("🗓️ Timetable System")
    year = st.sidebar.selectbox("Academic year", available_years())
    page = st.sidebar.radio("Page", ["Timetable", "Run generator", "Faculty absent",
                                     "Clash checker", "Statistics"])
    st.sidebar.divider()
    st.sidebar.caption("MySQL: connected")
    ok, why = neo4j_status()
    st.sidebar.caption("Neo4j: connected" if ok else f"Neo4j: unavailable ({why})")
    st.sidebar.caption(f"{timetable_rows(year)} classes saved for {year}")
    return year, page


# ============================================================ page: timetable
def page_timetable(year: str) -> None:
    st.header("Weekly timetable")
    view = st.radio("View by", ["Section", "Faculty", "Room"], horizontal=True)
    slots = sql("SELECT slot_id, day_of_week, period_no, start_time, end_time FROM TimeSlot ORDER BY slot_id")

    if view == "Section":
        opts = sql("SELECT s.section_id AS id, CONCAT(d.dept_name, ' / Sem ', s.semester, ' / ', "
                   "s.section_name) AS label FROM Section s JOIN Department d ON d.dept_id = s.dept_id "
                   "ORDER BY d.dept_name, s.semester, s.section_name")
        col, line_fn = "section_id", lambda r: [r["course_id"], r["faculty_name"], f"{r['building']} {r['room_no']}"]
    elif view == "Faculty":
        opts = sql("SELECT f.faculty_id AS id, CONCAT(f.name, ' (', d.dept_name, ')') AS label "
                   "FROM Faculty f JOIN Department d ON d.dept_id = f.dept_id ORDER BY d.dept_name, f.name")
        col, line_fn = "faculty_id", lambda r: [r["course_id"], _sec(r), f"{r['building']} {r['room_no']}"]
    else:
        opts = sql("SELECT room_id AS id, CONCAT(building, ' ', room_no, ' (', room_type, ', ', capacity, "
                   "' seats)') AS label FROM Classroom ORDER BY building, room_no")
        col, line_fn = "room_id", lambda r: [r["course_id"], _sec(r), r["faculty_name"]]

    labels = {o["id"]: o["label"] for o in opts}
    chosen = st.selectbox(view, list(labels), format_func=labels.get)

    rows = _clean(sql(f"SELECT * FROM v_section_timetable WHERE academic_year = %s AND {col} = %s "
                      "ORDER BY slot_id", (year, chosen)))
    classes = [{**r, "lines": line_fn(r)} for r in rows]
    grid = utils.build_week_grid(slots, classes)
    st.markdown(utils.grid_to_html(grid), unsafe_allow_html=True)

    st.caption("🟦 theory  🟧 lab   |   lunch is shown as its own row")
    c1, c2, c3 = st.columns(3)
    c1.metric("Periods per week", len(rows))
    c2.metric("Distinct courses", len({r["course_id"] for r in rows}))
    c3.metric("Days with classes", len({r["day_of_week"] for r in rows}))
    if not rows:
        st.info(f"No classes saved for {year}. Use **Run generator** to create a timetable.")
    else:
        flat = pd.DataFrame(rows)[["day_of_week", "period_no", "start_time", "end_time", "course_id",
                                   "course_name", "faculty_name", "dept_name", "semester",
                                   "section_name", "building", "room_no"]]
        flat["_d"] = flat["day_of_week"].map(DAYS.index)
        flat = flat.sort_values(["_d", "period_no"]).drop(columns="_d")
        st.download_button("Download as CSV", flat.to_csv(index=False).encode("utf-8"),
                           file_name=f"timetable_{view.lower()}_{chosen}_{year}.csv", mime="text/csv")


def _sec(r: dict) -> str:
    return f"{utils.dept_code(r['dept_name'])} Sem {r['semester']} {r['section_name']}"


# ============================================================ page: generator
def page_generator(year_default: str) -> None:
    st.header("Run the timetable generator")
    st.caption("Greedy most-constrained-first search with backtracking. All hard constraints are "
               "enforced; soft ones are best effort. Everything is saved in one transaction.")
    year = st.text_input("Academic year", value=year_default)
    existing = timetable_rows(year)
    if existing:
        st.warning(f"{year} already has {existing} classes. Tick **Replace** to overwrite them.")
    left, right = st.columns(2)
    replace = left.checkbox("Replace existing timetable", value=False)
    partial = left.checkbox("Save a partial timetable if a complete one is impossible", value=False)
    dry = right.checkbox("Dry run (compute and report only)", value=False)
    limit = right.slider("Search time limit (seconds)", 5, 120, 30)
    seed = right.number_input("Random seed", value=42, step=1)

    if st.button("Generate timetable", type="primary"):
        with st.spinner("Searching for a clash-free timetable ..."):
            res = generator.generate_timetable(year, replace=replace, allow_partial=partial, dry_run=dry,
                                               time_limit=float(limit), seed=int(seed))
        st.session_state["gen_result"] = res
        if res.committed:
            st.cache_data.clear()

    res = st.session_state.get("gen_result")
    if res is None:
        return
    status = "COMPLETE" if res.complete else ("PARTIAL" if res.placed else "FAILED")
    (st.success if res.complete and not res.error else st.error if res.error else st.warning)(
        f"{status}: {res.placed} of {res.required} periods placed"
        + (" and saved to MySQL." if res.committed else f". Not saved: {res.note}" if res.note else "."))
    m = st.columns(4)
    m[0].metric("Periods placed", f"{res.placed}/{res.required}")
    m[1].metric("Search nodes", res.nodes)
    m[2].metric("Time (s)", f"{res.elapsed:.2f}")
    m[3].metric("Restarts used", res.attempts)
    if res.soft:
        s = st.columns(3)
        s[0].metric("Same course twice a day", res.soft["same_day_repeats"])
        s[1].metric(f"Faculty runs > {generator.MAX_FAC_CONSEC} periods", res.soft["faculty_long_runs"])
        s[2].metric("Lab blocks split", res.soft["lab_blocks_split"])
    if res.preflight:
        st.error("The input data makes a timetable impossible:")
        for p in res.preflight:
            st.write(f"- {p}")
    if res.unplaced:
        st.subheader("Could not be placed")
        st.dataframe(pd.DataFrame(res.unplaced), hide_index=True)
    with st.expander("Full report"):
        st.code(res.report())
    if res.committed:
        st.info("The Neo4j graph is now out of date. Refresh it so the *Faculty absent* tool sees this timetable.")
        etl_button(res.academic_year, key="etl_after_generate")


# ============================================================ page: faculty absent
def page_absent(year: str) -> None:
    st.header("Faculty absent: impact and substitutes")
    st.caption("Uses the Neo4j graph. QUALIFIED = listed in Teaches for that course (can be assigned "
               "directly). RELATED = teaches a course sharing students/faculty with it (needs a "
               "Teaches row first). Candidates are same-department colleagues who are free in the slot.")
    fac = sql("SELECT f.faculty_id AS id, CONCAT(f.name, ' (', d.dept_name, ')') AS label FROM Faculty f "
              "JOIN Department d ON d.dept_id = f.dept_id ORDER BY d.dept_name, f.name")
    labels = {f["id"]: f["label"] for f in fac}
    a, b = st.columns(2)
    fid = a.selectbox("Absent faculty member", list(labels), format_func=labels.get)
    day = b.selectbox("Absent on", ["Whole week"] + DAYS)
    day_arg = None if day == "Whole week" else day

    stale = graph_stale_message(year)
    if stale:
        st.warning(stale)
        etl_button(year, key="etl_stale")

    plan, err = run_neo4j(neo4j_queries.faculty_absence_plan, int(fid), year, day_arg)
    if err:
        st.error(err)
        return
    s = plan["summary"]
    if s["classes_affected"] == 0:
        st.success("This faculty member has no classes in that period; nothing is affected.")
        return
    m = st.columns(6)
    m[0].metric("Classes affected", s["classes_affected"])
    m[1].metric("Sections", s["sections_affected"])
    m[2].metric("Students", s["students_affected"])
    m[3].metric("Coverable", s["coverable"])
    m[4].metric("Needs Teaches row", s["needs_teaches_row"])
    m[5].metric("Uncovered", s["uncovered"])

    table = []
    for c in plan["classes"]:
        best = c["substitutes"][0]["candidate"] if c["substitutes"] else "-"
        table.append({"Day": c["day"], "Period": c["period"], "Time": c["start_time"],
                      "Course": f"{c['course_id']} {c['course_name']}", "Section": c["section"],
                      "Room": c["room"], "Students": c["students"],
                      "Status": STATUS_ICON[c["status"]], "Suggested": best})
    st.dataframe(pd.DataFrame(table), hide_index=True)
    st.download_button("Download plan as CSV", pd.DataFrame(table).to_csv(index=False).encode("utf-8"),
                       file_name=f"absence_plan_faculty{fid}_{year}.csv", mime="text/csv")

    st.subheader("Substitute candidates per class")
    for c in plan["classes"]:
        title = f"{c['day']} P{c['period']}  {c['course_id']}  {c['section']}  -  {STATUS_ICON[c['status']]}"
        with st.expander(title):
            if not c["substitutes"]:
                st.write("Nobody in the department is free and suitable in this slot.")
                continue
            st.dataframe(pd.DataFrame([{"Candidate": x["candidate"], "Fit": x["fit"],
                                        "Related courses taught": x["related_courses"]}
                                       for x in c["substitutes"]]), hide_index=True)


# ============================================================ page: clash checker
def page_clashes(year: str) -> None:
    st.header("Clash checker")
    t1, t2, t3 = st.tabs(["Saved timetable", "Check a proposed class", "Move a class (ripple)"])
    with t1:
        _tab_saved(year)
    with t2:
        _tab_proposal(year)
    with t3:
        _tab_ripple(year)


def _tab_saved(year: str) -> None:
    n = timetable_rows(year)
    if n == 0:
        st.info(f"No timetable saved for {year}.")
        return
    clashes = _clean(proc("sp_list_clashes", (year,)))
    gaps = _clean(proc("sp_hours_coverage", (year,)))
    a, b, c = st.columns(3)
    a.metric("Classes saved", n)
    b.metric("Clashes / rule violations", len(clashes))
    c.metric("Sections short of hours", len(gaps))
    if clashes:
        st.error("Rule violations found:")
        st.dataframe(pd.DataFrame(clashes), hide_index=True)
    else:
        st.success("No clashes: no room, faculty or section is double-booked, every room fits its "
                   "course type and size, and every faculty member is qualified.")
    if gaps:
        st.warning("Some courses do not have exactly their weekly hours scheduled:")
        st.dataframe(pd.DataFrame(gaps), hide_index=True)
    else:
        st.success("Every (section, course) has exactly its required weekly hours.")
    st.caption("Runs the stored procedures sp_list_clashes and sp_hours_coverage.")


def _tab_proposal(year: str) -> None:
    st.caption("Test a class without saving it: every rule MySQL enforces is checked, plus the "
               "weekly-hours rule.")
    secs = sql("SELECT s.section_id AS id, CONCAT(d.dept_name, ' / Sem ', s.semester, ' / ', s.section_name) "
               "AS label FROM Section s JOIN Department d ON d.dept_id = s.dept_id "
               "ORDER BY d.dept_name, s.semester, s.section_name")
    crs = sql("SELECT course_id AS id, CONCAT(course_id, ' ', course_name, ' (', course_type, ')') AS label "
              "FROM Course ORDER BY course_id")
    fac = sql("SELECT faculty_id AS id, name AS label FROM Faculty ORDER BY name")
    rooms = sql("SELECT room_id AS id, CONCAT(building, ' ', room_no, ' (', room_type, ', ', capacity, ')') "
                "AS label FROM Classroom ORDER BY building, room_no")
    slots = _clean(sql("SELECT slot_id AS id, CONCAT(day_of_week, ' P', period_no, ' ', "
                       "TIME_FORMAT(start_time, '%H:%i')) AS label FROM TimeSlot ORDER BY slot_id"))
    c1, c2, c3 = st.columns(3)
    section = c1.selectbox("Section", [r["id"] for r in secs],
                           format_func={r["id"]: r["label"] for r in secs}.get)
    course = c2.selectbox("Course", [r["id"] for r in crs],
                          format_func={r["id"]: r["label"] for r in crs}.get)
    faculty = c3.selectbox("Faculty", [r["id"] for r in fac],
                           format_func={r["id"]: r["label"] for r in fac}.get)
    d1, d2 = st.columns(2)
    room = d1.selectbox("Room", [r["id"] for r in rooms], format_func={r["id"]: r["label"] for r in rooms}.get)
    slot = d2.selectbox("Slot", [r["id"] for r in slots], format_func={r["id"]: r["label"] for r in slots}.get)

    if st.button("Check this class"):
        with db.connection() as conn:
            results = generator.check_proposed_class(conn, year, int(section), course, int(faculty),
                                                     int(room), int(slot))
        bad_hard = [r for r in results if not r["ok"] and r["hard"]]
        (st.error if bad_hard else st.success)(
            f"MySQL would REJECT this class ({len(bad_hard)} hard rule(s) broken)." if bad_hard
            else "MySQL would accept this class.")
        for r in results:
            icon = "✅" if r["ok"] else ("❌" if r["hard"] else "⚠️")
            st.write(f"{icon} **{r['rule']}**: {r['detail']}")


def _tab_ripple(year: str) -> None:
    st.caption("Uses Neo4j: if you move a class to another slot (keeping faculty, section and room), "
               "which existing classes collide and how many students are displaced?")
    secs = sql("SELECT s.section_id AS id, CONCAT(d.dept_name, ' / Sem ', s.semester, ' / ', s.section_name) "
               "AS label FROM Section s JOIN Department d ON d.dept_id = s.dept_id "
               "ORDER BY d.dept_name, s.semester, s.section_name")
    labels = {r["id"]: r["label"] for r in secs}
    section = st.selectbox("Section whose class you want to move", list(labels), format_func=labels.get,
                           key="ripple_section")
    classes = _clean(sql("SELECT t.timetable_id, ts.day_of_week, ts.period_no, t.course_id, f.name AS faculty "
                         "FROM Timetable t JOIN TimeSlot ts ON ts.slot_id = t.slot_id "
                         "JOIN Faculty f ON f.faculty_id = t.faculty_id "
                         "WHERE t.academic_year = %s AND t.section_id = %s ORDER BY ts.slot_id",
                         (year, section)))
    if not classes:
        st.info("This section has no classes saved for the selected year.")
        return
    cl = {c["timetable_id"]: f"{c['day_of_week']} P{c['period_no']}  {c['course_id']}  ({c['faculty']})"
          for c in classes}
    tid = st.selectbox("Class to move", list(cl), format_func=cl.get)
    a, b = st.columns(2)
    to_day = a.selectbox("Move to day", DAYS)
    to_period = b.selectbox("Move to period", list(range(1, 9)))
    if st.button("Show the ripple effect"):
        r, err = run_neo4j(neo4j_queries.move_class_ripple, int(tid), to_day, int(to_period))
        if err:
            st.error(err)
            return
        mv = r["moved"]
        st.write(f"Moving **{mv['moved_course']}** ({mv['moved_section']}) from "
                 f"{mv['from_day']} P{mv['from_period']} to {mv['to_day']} P{mv['to_period']}:")
        if r["free_move"]:
            st.success("Free move: nothing in the target slot collides with it.")
        else:
            st.error(f"{len(r['clashes'])} existing class(es) collide; {r['students_displaced']} "
                     "students would be displaced. Each of these classes would have to move too.")
            st.dataframe(pd.DataFrame([{"Course": c["clash_course"], "Section": c["clash_section"],
                                        "Faculty": c["clash_faculty"], "Room": c["clash_room"],
                                        "Clash on": c["clash_types"], "Students": c["students_displaced"]}
                                       for c in r["clashes"]]), hide_index=True)


# ============================================================ page: statistics
def page_stats(year: str) -> None:
    st.header("Statistics")
    n = timetable_rows(year)
    if n == 0:
        st.info(f"No timetable saved for {year}.")
        return
    wl = pd.DataFrame(_clean(proc("sp_faculty_workload", (year,))))
    ru = pd.DataFrame(_clean(proc("sp_room_utilization", (year,))))
    used_rooms = int((ru["used_slots"] > 0).sum())

    m = st.columns(5)
    m[0].metric("Classes per week", n)
    m[1].metric("Faculty teaching", int((wl["periods_per_week"] > 0).sum()))
    m[2].metric("Avg faculty load", f"{wl['periods_per_week'].mean():.1f} periods")
    m[3].metric("Rooms in use", f"{used_rooms}/{len(ru)}")
    m[4].metric("Avg room utilisation", f"{ru['utilization_pct'].mean():.1f}%")

    left, right = st.columns(2)
    with left:
        st.subheader("Faculty workload (periods per week)")
        st.bar_chart(wl.set_index("faculty_name")["periods_per_week"])
    with right:
        st.subheader("Room utilisation (% of 48 slots)")
        ru["room"] = ru["building"] + " " + ru["room_no"]
        st.bar_chart(ru.sort_values("utilization_pct", ascending=False).head(15)
                     .set_index("room")["utilization_pct"])

    by_day = pd.DataFrame(sql(
        "SELECT ts.day_of_week AS day, COUNT(*) AS classes FROM Timetable t "
        "JOIN TimeSlot ts ON ts.slot_id = t.slot_id WHERE t.academic_year = %s GROUP BY ts.day_of_week",
        (year,)))
    by_day["_o"] = by_day["day"].map(DAYS.index)
    by_period = pd.DataFrame(sql(
        "SELECT ts.period_no AS period, COUNT(*) AS classes FROM Timetable t "
        "JOIN TimeSlot ts ON ts.slot_id = t.slot_id WHERE t.academic_year = %s GROUP BY ts.period_no "
        "ORDER BY ts.period_no", (year,)))
    l2, r2 = st.columns(2)
    with l2:
        st.subheader("Classes per day")
        st.bar_chart(by_day.sort_values("_o").set_index("day")["classes"])
    with r2:
        st.subheader("Classes per period")
        st.bar_chart(by_period.assign(period=by_period["period"].map(lambda p: f"P{p}"))
                     .set_index("period")["classes"])

    kinds = pd.DataFrame(sql(
        "SELECT c.course_type AS type, COUNT(*) AS classes FROM Timetable t "
        "JOIN Course c ON c.course_id = t.course_id WHERE t.academic_year = %s GROUP BY c.course_type", (year,)))
    l3, r3 = st.columns(2)
    with l3:
        st.subheader("Theory vs lab periods")
        st.bar_chart(kinds.set_index("type")["classes"])
    with r3:
        st.subheader("Section load: periods per day")
        load = pd.DataFrame(_clean(sql(
            "SELECT CONCAT(dept_name, ' S', semester, section_name) AS section, day_of_week AS day, "
            "COUNT(*) AS n FROM v_section_timetable WHERE academic_year = %s "
            "GROUP BY dept_name, semester, section_name, day_of_week", (year,))))
        pivot = load.pivot(index="section", columns="day", values="n").fillna(0).astype(int)
        st.dataframe(pivot[[d for d in DAYS if d in pivot.columns]])

    with st.expander("Graph insights (Neo4j)"):
        courses, err = run_neo4j(neo4j_queries.most_conflicted_courses, 10)
        if err:
            st.info(err)
        else:
            st.write("Courses colliding with the most others (shared faculty + shared students):")
            st.bar_chart(pd.DataFrame(courses).set_index("course_id")["total_shared_count"])
            bridges, _ = run_neo4j(neo4j_queries.cross_department_teaching)
            if bridges:
                st.write("Cross-department teaching links:")
                st.dataframe(pd.DataFrame(bridges), hide_index=True)


# ====================================================================== main
def main() -> None:
    try:
        sql("SELECT 1 AS ok")
    except Exception as exc:                       # noqa: BLE001
        st.error(f"Cannot connect to MySQL: {exc}")
        st.info("Check MYSQL_* in `.env` and that the database server is running "
                "(see README). Then reload this page.")
        st.stop()

    year, page = sidebar()
    {"Timetable": page_timetable, "Run generator": page_generator, "Faculty absent": page_absent,
     "Clash checker": page_clashes, "Statistics": page_stats}[page](year)


main()