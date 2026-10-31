"""Pure-Python helpers and the Cypher query file. No database needed."""
import re
import shutil
import subprocess
from datetime import timedelta

import pytest

import neo4j_queries as NQ
import utils


# ----------------------------------------------------------------------- times
def test_time_helpers_accept_every_representation():
    assert utils.to_minutes(timedelta(hours=8, minutes=30)) == 510
    assert utils.to_minutes("08:30") == utils.to_minutes("08:30:00") == 510
    assert utils.fmt_time("15:45:00") == "15:45" and utils.fmt_time(timedelta(hours=9)) == "09:00"
    with pytest.raises(TypeError):
        utils.to_minutes(12)


def slots():
    periods = [("08:30", "09:20"), ("09:20", "10:10"), ("10:25", "11:15"), ("11:15", "12:05"),
               ("13:00", "13:50"), ("13:50", "14:40"), ("14:55", "15:45"), ("15:45", "16:35")]
    return [{"slot_id": d * 8 + p, "day_of_week": day, "period_no": p, "start_time": s, "end_time": e}
            for d, day in enumerate(utils.DAY_ORDER) for p, (s, e) in enumerate(periods, 1)]


def test_slot_links_break_at_lunch_but_not_at_tea():
    nxt, prv = utils.build_slot_links(slots())
    assert nxt[2] == 3 and nxt[6] == 7           # tea breaks (15 min) keep periods linked
    assert 4 not in nxt and 5 not in prv         # lunch (P4 -> P5) breaks the chain
    assert prv[3] == 2


def test_week_grid_shape_and_lunch_row():
    classes = [{"day_of_week": "MON", "period_no": 1, "course_id": "CS201", "course_type": "THEORY", "lines": ["CS201", "A"]}]
    grid = utils.build_week_grid(slots(), classes)
    assert grid["days"] == utils.DAY_ORDER
    assert [r["kind"] for r in grid["rows"]].count("period") == 8
    assert [r["label"] for r in grid["rows"] if r["kind"] == "break"] == ["Lunch"]
    assert grid["rows"][0]["cells"]["MON"][0]["course_id"] == "CS201"


def test_grid_html_escapes_untrusted_text():
    classes = [{"day_of_week": "MON", "period_no": 1, "course_id": "X", "course_type": "LAB",
                "lines": ["<script>alert(1)</script>", "O'Neil & Co"]}]
    html = utils.grid_to_html(utils.build_week_grid(slots(), classes))
    assert "<script>" not in html and "&lt;script&gt;" in html and "&amp;" in html
    assert "#f59e0b" in html                      # lab colour


# ------------------------------------------------------------ MySQL error wording
class FakeErr(Exception):
    def __init__(self, errno, msg):
        super().__init__(msg)
        self.errno, self.msg = errno, msg


@pytest.mark.parametrize("errno, msg, expected", [
    (1062, "Duplicate entry '1-1-2026' for key 'Timetable.uq_room_slot'", "room"),
    (1062, "Duplicate entry for key 'uq_faculty_slot'", "faculty"),
    (1062, "Duplicate entry for key 'uq_section_slot'", "section"),
    (1452, "foreign key constraint fails (CONSTRAINT `fk_tt_teaches` FOREIGN KEY ...)", "Teaches"),
    (1451, "Cannot delete or update a parent row: a foreign key constraint fails (CONSTRAINT `fk_tt_teaches` ...)",
     "still used"),
    (5001, "Room capacity is less than section strength", "capacity"),
    (5002, "Course type does not match room type", "room type"),
    (9999, "something else", "9999"),
])
def test_describe_mysql_error(errno, msg, expected):
    assert expected in utils.describe_mysql_error(FakeErr(errno, msg))


# --------------------------------------------------------------- configuration
def test_env_file_parsing(tmp_path):
    f = tmp_path / ".env"
    f.write_text('# comment\nA=1\nB = "two"\n\nC=\nnot a setting\nD=x=y\n')
    assert utils.read_env_file(f) == {"A": "1", "B": "two", "C": "", "D": "x=y"}
    assert utils.read_env_file(tmp_path / "missing.env") == {}


def test_neo4j_settings_report_masks_the_password_and_flags_overrides(tmp_path, monkeypatch):
    f = tmp_path / ".env"
    f.write_text("NEO4J_PASSWORD=file_password\n")
    monkeypatch.setattr(utils, "load_env", lambda *_a, **_k: None)       # keep os.environ clean
    monkeypatch.setenv("NEO4J_PASSWORD", "file_password")
    text = "\n".join(utils.describe_neo4j_settings(f))
    assert "file_password" not in text and "13 chars" in text and "source: .env file" in text
    monkeypatch.setenv("NEO4J_PASSWORD", "other_password")
    assert "OVERRIDING" in "\n".join(utils.describe_neo4j_settings(f))
    monkeypatch.delenv("NEO4J_PASSWORD")
    assert "NOT SET" in "\n".join(utils.describe_neo4j_settings(tmp_path / "none.env"))


def test_format_table_and_dept_code():
    out = utils.format_table([{"a": 1, "b": ["x", "y"], "c": None}, {"a": 22, "b": [], "c": "z"}])
    assert "x, y" in out and out.splitlines()[0].split() == ["a", "b", "c"]
    assert utils.format_table([]) == "(no rows)"
    assert utils.dept_code("Computer Science and Engineering") == "CSE"
    assert utils.dept_code("Mechanical Engineering") == "ME"


# ----------------------------------------------------------- the Cypher query file
EXPECTED_QUERIES = {"impact_analysis", "substitute_finder", "most_conflicted_courses", "free_rooms",
                    "heaviest_day_students", "faculty_shortest_path", "faculty_workload",
                    "move_class_ripple", "cross_department_teaching", "course_conflict_neighbours",
                    "node_counts", "relationship_counts"}


def test_all_documented_queries_are_loaded():
    qs = NQ.queries()
    assert set(qs) == EXPECTED_QUERIES
    assert all(q.strip() and not q.rstrip().endswith(";") for q in qs.values())


def test_every_wrapper_passes_exactly_the_parameters_its_query_uses(monkeypatch):
    calls = []
    monkeypatch.setattr(NQ, "run_query", lambda name, params=None, session=None: calls.append((name, dict(params or {}))) or [])
    NQ.impact_analysis(1, "2026-2027", "mon")
    NQ.substitute_finder(1)
    NQ.most_conflicted_courses()
    NQ.free_rooms("MON", 1, 60, "lecture")
    NQ.heaviest_day_students()
    NQ.faculty_shortest_path(1, 11)
    NQ.faculty_workload_ranking()
    NQ.cross_department_teaching()
    NQ.course_conflict_neighbours("CS301")
    NQ.node_counts()
    NQ.relationship_counts()
    with pytest.raises(LookupError):
        NQ.move_class_ripple(1, "FRI", 8)
    assert {n for n, _ in calls} == EXPECTED_QUERIES
    for name, params in calls:
        used = set(re.findall(r"\$(\w+)", NQ.queries()[name]))
        assert used == set(params), name


@pytest.mark.skipif(shutil.which("cypher-lint") is None, reason="optional: install 'cypher-lint' to syntax-check Cypher")
def test_cypher_files_have_valid_syntax():
    from pathlib import Path
    root = Path(__file__).resolve().parent.parent
    r = subprocess.run(["cypher-lint", str(root / "cypher" / "queries.cypher")], capture_output=True, text=True)
    assert r.returncode == 0 and not (r.stdout + r.stderr).strip(), r.stdout + r.stderr


# ----------------------------------------------------- Python-side query logic
def test_absence_plan_statuses_and_student_counts(monkeypatch):
    monkeypatch.setattr(NQ, "impact_analysis", lambda *a: [
        {"timetable_id": 1, "section_id": 1, "students": 12}, {"timetable_id": 2, "section_id": 2, "students": 12},
        {"timetable_id": 3, "section_id": 1, "students": 12}, {"timetable_id": 4, "section_id": 3, "students": 12}])
    monkeypatch.setattr(NQ, "substitute_finder", lambda *a: [
        {"timetable_id": 1, "fit": "QUALIFIED"}, {"timetable_id": 1, "fit": "RELATED"},
        {"timetable_id": 2, "fit": "RELATED"}])
    plan = NQ.faculty_absence_plan(1)
    assert [c["status"] for c in plan["classes"]] == ["COVERABLE", "NEEDS_TEACHES", "UNCOVERED", "UNCOVERED"]
    assert plan["summary"]["sections_affected"] == 3 and plan["summary"]["students_affected"] == 36  # section 1 counted once


def test_ranking_shortest_path_and_ripple_mapping(monkeypatch):
    monkeypatch.setattr(NQ, "run_query", lambda name, params=None, session=None: {
        "faculty_workload": [{"periods": 10}, {"periods": 10}, {"periods": 8}, {"periods": 0}],
        "faculty_shortest_path": [],
        "move_class_ripple": [dict(moved_timetable_id=7, moved_course="C", moved_section="S", from_day="MON",
                                   from_period=1, to_day="FRI", to_period=8, clash_timetable_id=None, students_displaced=0)],
    }[name])
    assert [r["rank"] for r in NQ.faculty_workload_ranking()] == [1, 1, 2, 3]       # ties share a rank
    assert NQ.faculty_shortest_path(1, 17) == {"connected": False, "hops": None, "path": []}
    assert NQ.move_class_ripple(7, "FRI", 8)["free_move"] is True


@pytest.mark.parametrize("call", [lambda: NQ.impact_analysis(1, day="XYZ"), lambda: NQ.free_rooms("MON", 1, room_type="gym"),
                                  lambda: NQ.faculty_shortest_path(3, 3), lambda: NQ.free_rooms(None, 1)])
def test_bad_arguments_are_rejected_early(call):
    with pytest.raises(ValueError):
        call()


def test_neo4j_driver_import_does_not_load_pandas_or_pyarrow():
    """
    Regression: the driver imports pandas and pyarrow if present, which segfaults on some
    machines (Flatpak VS Code terminal). utils._import_neo4j must keep both out, yet leave a
    later real import possible. Run in a fresh interpreter so other tests cannot interfere.
    """
    pytest.importorskip("neo4j")
    code = (
        "import sys; sys.path.insert(0, 'src'); import utils\n"
        "utils._import_neo4j()\n"
        "assert 'neo4j' in sys.modules\n"
        "assert 'pandas' not in sys.modules, 'pandas was imported by the neo4j driver'\n"
        "assert 'pyarrow' not in sys.modules, 'pyarrow was imported by the neo4j driver'\n"
        "print('ok')"
    )
    from pathlib import Path
    root = Path(__file__).resolve().parent.parent
    r = subprocess.run([__import__("sys").executable, "-c", code], cwd=root, capture_output=True, text=True)
    assert r.returncode == 0 and r.stdout.strip() == "ok", r.stdout + r.stderr
