"""
utils.py -- helpers shared by the generator, ETL, queries, tests and UI.

  * load_env / env / academic_year   configuration from environment / .env
  * describe_mysql_error             turn MySQL errors into plain English
  * build_slot_links                 "which period follows which" (breaks aware)
  * build_conflict_graph             course-vs-course conflict weights
"""
from __future__ import annotations

import os
import re
from collections import defaultdict
from datetime import time, timedelta
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
DAY_ORDER = ["MON", "TUE", "WED", "THU", "FRI", "SAT"]
DEFAULT_ACADEMIC_YEAR = "2026-2027"


# --------------------------------------------------------------------------
# Configuration
# --------------------------------------------------------------------------
def read_env_file(path: str | os.PathLike | None = None) -> dict[str, str]:
    """Parse a .env file (KEY=VALUE per line, '#' comments) into a dict. Missing file -> {}."""
    env_file = Path(path) if path else PROJECT_ROOT / ".env"
    values: dict[str, str] = {}
    if not env_file.is_file():
        return values
    for raw in env_file.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        values[key.strip()] = value.strip().strip('"').strip("'")
    return values


def load_env(path: str | os.PathLike | None = None) -> None:
    """
    Minimal .env loader. Values already present in the real environment win,
    so `export` and docker-compose can override the file. Avoids an extra
    dependency (python-dotenv).
    """
    for key, value in read_env_file(path).items():
        os.environ.setdefault(key, value)


def env(name: str, default: str | None = None) -> str | None:
    load_env()
    return os.environ.get(name, default)


def academic_year() -> str:
    """Academic year used by default (env ACADEMIC_YEAR, else 2026-2027)."""
    return env("ACADEMIC_YEAR", DEFAULT_ACADEMIC_YEAR)


# --------------------------------------------------------------------------
# MySQL error translation
# --------------------------------------------------------------------------
_UNIQUE_MEANING = {
    "uq_room_slot": "that room is already booked in that slot",
    "uq_faculty_slot": "that faculty member already teaches in that slot",
    "uq_section_slot": "that section already has a class in that slot",
}
_FK_MEANING = {
    "fk_tt_teaches": "the faculty member is not listed in Teaches for that course",
    "fk_tt_section_course": "that course is not in the section's Section_Course plan",
    "fk_tt_course": "unknown course",
    "fk_tt_faculty": "unknown faculty",
    "fk_tt_room": "unknown room",
    "fk_tt_slot": "unknown time slot",
    "fk_tt_section": "unknown section",
}
_TRIGGER_MEANING = {
    5001: "room capacity is smaller than the section strength",
    5002: "course type does not match the room type (LAB->LAB, THEORY->LECTURE)",
    5003: "Section_Course.semester does not match Section.semester",
}


def describe_mysql_error(exc: BaseException) -> str:
    """Human-readable explanation of a mysql.connector error."""
    errno = getattr(exc, "errno", None)
    msg = str(getattr(exc, "msg", None) or exc)
    if errno == 1062:                       # duplicate key -> a UNIQUE clash
        m = re.search(r"for key '([^']+)'", msg)
        key = m.group(1).split(".")[-1] if m else "?"
        return f"clash ({key}): {_UNIQUE_MEANING.get(key, 'duplicate row')}"
    if errno == 1452:                       # FK failure
        m = re.search(r"CONSTRAINT `([^`]+)`", msg)
        name = m.group(1) if m else "?"
        return f"foreign key {name}: {_FK_MEANING.get(name, 'referenced row missing')}"
    if errno in _TRIGGER_MEANING:
        return f"rule {errno}: {_TRIGGER_MEANING[errno]}"
    return f"MySQL error {errno}: {msg}"


# --------------------------------------------------------------------------
# Time-slot geometry
# --------------------------------------------------------------------------
def to_minutes(t) -> float:
    """TIME columns arrive as timedelta (mysql-connector) or datetime.time."""
    if isinstance(t, timedelta):
        return t.total_seconds() / 60.0
    if isinstance(t, time):
        return t.hour * 60 + t.minute + t.second / 60.0
    if isinstance(t, str):                        # 'HH:MM' or 'HH:MM:SS'
        parts = [float(x) for x in t.split(":")]
        parts += [0.0] * (3 - len(parts))
        return parts[0] * 60 + parts[1] + parts[2] / 60.0
    raise TypeError(f"unsupported time value: {t!r}")


def fmt_time(t) -> str:
    m = int(round(to_minutes(t)))
    return f"{m // 60:02d}:{m % 60:02d}"


def build_slot_links(slots: list[dict], long_break_min: float = 30.0):
    """
    Return (next_slot, prev_slot): slot_id -> slot_id of the period that
    directly follows/precedes it ON THE SAME DAY WITHOUT A LONG BREAK.

    Two periods are "consecutive" when period_no differs by 1 and the gap
    between end and next start is < long_break_min. With the seed data the
    15-minute tea breaks keep periods linked, the 55-minute lunch (P4->P5)
    does not. Lab blocks and the "max 3 consecutive periods" rule use this.
    """
    by_day: dict[str, list[dict]] = defaultdict(list)
    for s in slots:
        by_day[s["day_of_week"]].append(s)
    nxt: dict[int, int] = {}
    prv: dict[int, int] = {}
    for day_slots in by_day.values():
        day_slots.sort(key=lambda s: s["period_no"])
        for a, b in zip(day_slots, day_slots[1:]):
            gap = to_minutes(b["start_time"]) - to_minutes(a["end_time"])
            if b["period_no"] == a["period_no"] + 1 and gap < long_break_min:
                nxt[a["slot_id"]] = b["slot_id"]
                prv[b["slot_id"]] = a["slot_id"]
    return nxt, prv


# --------------------------------------------------------------------------
# Conflict graph
# --------------------------------------------------------------------------
def build_conflict_graph(conn) -> dict[str, dict[str, int]]:
    """
    Course conflict graph: course -> {other_course: shared_count}.

    Two courses CONFLICT when they cannot sensibly share a time slot because
    the same people are involved:
        shared_count = (# faculty qualified for both, from Teaches)
                     + (# students enrolled in both, from Enrollment)
    Undirected: stored both ways. The Neo4j layer (Step 4) materialises the
    same definition as CONFLICTS_WITH {shared_count}.

    The generator uses it to order tasks (courses with many heavy conflicts
    are placed first, i.e. DSATUR-style "most constrained first").
    """
    graph: dict[str, dict[str, int]] = defaultdict(lambda: defaultdict(int))
    sql_pairs = (
        "SELECT a.course_id AS c1, b.course_id AS c2, COUNT(*) AS n "
        "FROM {t} a JOIN {t} b ON a.{k} = b.{k} AND a.course_id < b.course_id "
        "GROUP BY a.course_id, b.course_id"
    )
    cur = conn.cursor()
    try:
        for table, key in (("Teaches", "faculty_id"), ("Enrollment", "student_id")):
            cur.execute(sql_pairs.format(t=table, k=key))
            for c1, c2, n in cur.fetchall():
                graph[c1][c2] += int(n)
                graph[c2][c1] += int(n)
    finally:
        cur.close()
    return {c: dict(nb) for c, nb in graph.items()}


# --------------------------------------------------------------------------
# Neo4j connection settings (shared by neo4j_etl.py / neo4j_queries.py / UI)
# --------------------------------------------------------------------------
DAY_NO = {d: i + 1 for i, d in enumerate(DAY_ORDER)}     # MON=1 ... SAT=6


def neo4j_settings() -> dict:
    """
    Neo4j settings from the environment (or .env):
        NEO4J_URI       default bolt://localhost:7687
        NEO4J_USER      default neo4j
        NEO4J_PASSWORD  required
        NEO4J_DATABASE  default neo4j  (Community Edition has only this one)
    """
    load_env()
    return {
        "uri": os.environ.get("NEO4J_URI", "bolt://localhost:7687"),
        "user": os.environ.get("NEO4J_USER", "neo4j"),
        "password": os.environ.get("NEO4J_PASSWORD", ""),
        "database": os.environ.get("NEO4J_DATABASE", "neo4j"),
    }


_notification_filter_installed = False


def _silence_null_elimination_notice() -> None:
    """
    Neo4j sends warning 01G11 "null value eliminated in set function" whenever
    count()/collect() skip NULLs coming from an OPTIONAL MATCH. Several queries
    do that ON PURPOSE (e.g. substitute_finder), so this one notice is dropped.
    Every other server notification is still shown.
    """
    global _notification_filter_installed
    if _notification_filter_installed:
        return
    import logging

    class _Drop01G11(logging.Filter):
        def filter(self, record: logging.LogRecord) -> bool:
            return "01G11" not in record.getMessage()

    logging.getLogger("neo4j.notifications").addFilter(_Drop01G11())
    _notification_filter_installed = True


def _import_neo4j():
    """
    Import the neo4j driver WITHOUT letting it import pandas or pyarrow.

    The driver optionally imports numpy, pandas and pyarrow (each inside a "skip if
    missing" block) only for Result.to_df() / to_arrow(), which this project never uses.
    On a machine where importing pandas/pyarrow crashes (seen inside the Flatpak build of
    VS Code once Streamlit had installed them) that made every `import neo4j` crash too.
    A temporary `sys.modules[name] = None` makes those optional imports fail harmlessly;
    the entries are removed again afterwards so a later real `import pandas` (the web app)
    still works. Faster import as a bonus. numpy is left alone: it is harmless.
    """
    import sys
    blocked = [m for m in ("pandas", "pyarrow") if m not in sys.modules]
    for m in blocked:
        sys.modules[m] = None
    try:
        import neo4j
    finally:
        for m in blocked:
            sys.modules.pop(m, None)
    return neo4j


def get_neo4j_driver():
    """Create a Neo4j driver from the settings above (imported lazily)."""
    GraphDatabase = _import_neo4j().GraphDatabase
    _silence_null_elimination_notice()
    s = neo4j_settings()
    if not s["password"]:
        raise RuntimeError("NEO4J_PASSWORD is not set. Put it in .env or export it.")
    return GraphDatabase.driver(s["uri"], auth=(s["user"], s["password"]))


# --------------------------------------------------------------------------
# Small console table printer (keeps the CLI demos free of extra dependencies)
# --------------------------------------------------------------------------
def format_table(rows: list[dict], columns: list[str] | None = None,
                 max_rows: int | None = None) -> str:
    """Render a list of dicts as an aligned text table."""
    if not rows:
        return "(no rows)"
    cols = columns or list(rows[0].keys())

    def cell(v) -> str:
        if v is None:
            return "-"
        if isinstance(v, (list, tuple)):
            return ", ".join(cell(x) for x in v) if v else "-"
        if isinstance(v, dict):
            return ":".join(str(x) for x in v.values())
        return str(v)

    shown = rows if max_rows is None else rows[:max_rows]
    body = [[cell(r.get(c)) for c in cols] for r in shown]
    widths = [max(len(c), *(len(b[i]) for b in body)) for i, c in enumerate(cols)]
    line = "  ".join(c.ljust(w) for c, w in zip(cols, widths))
    out = [line, "  ".join("-" * w for w in widths)]
    out += ["  ".join(v.ljust(w) for v, w in zip(b, widths)) for b in body]
    if max_rows is not None and len(rows) > max_rows:
        out.append(f"... {len(rows) - max_rows} more row(s)")
    return "\n".join(out)


def describe_neo4j_settings(env_path: str | os.PathLike | None = None) -> list[str]:
    """
    Human-readable view of the Neo4j settings the code will use, WITHOUT
    printing the password: its length, first/last character and where it
    came from (.env file, or a real environment variable overriding .env).
    """
    load_env(env_path)
    s = neo4j_settings()
    file_vals = read_env_file(env_path)
    pw = s["password"]
    if not pw:
        pw_text = "NOT SET"
    else:
        hint = f"{len(pw)} chars, starts '{pw[0]}', ends '{pw[-1]}'"
        in_file = file_vals.get("NEO4J_PASSWORD")
        if in_file is None:
            source = "real environment variable (not in .env)"
        elif in_file != pw:
            source = "real environment variable, OVERRIDING a different value in .env"
        else:
            source = ".env file"
        pw_text = f"{hint}  [source: {source}]"
    return [f"URI       : {s['uri']}", f"User      : {s['user']}",
            f"Password  : {pw_text}", f"Database  : {s['database']}"]


# --------------------------------------------------------------------------
# Department code and weekly-grid helpers (used by the Streamlit UI)
# --------------------------------------------------------------------------
def dept_code(name: str) -> str:
    """'Computer Science and Engineering' -> 'CSE' (initials of capitalised words)."""
    return "".join(w[0] for w in name.split() if w[0].isupper())


def build_week_grid(slots: list[dict], classes: list[dict], break_min: float = 30.0) -> dict:
    """
    Arrange classes into a weekly grid.

    slots   : TimeSlot rows (day_of_week, period_no, start_time, end_time)
    classes : dicts with day_of_week, period_no, course_id, course_type, lines (list[str])
    Returns {"days": [...], "rows": [row, ...]} where each row is
        {"kind": "period", "label": "P1", "sub": "08:30 - 09:20", "cells": {day: [class, ...]}}
    or  {"kind": "break",  "label": "Lunch", "sub": "12:05 - 13:00", "cells": {}}
    A break row is inserted wherever the gap between two periods is >= break_min
    minutes (the 15-minute tea breaks are not shown; lunch is).
    """
    days = [d for d in DAY_ORDER if any(s["day_of_week"] == d for s in slots)]
    periods: dict[int, dict] = {}
    for s in slots:
        periods.setdefault(s["period_no"], s)
    cells: dict[tuple, list] = defaultdict(list)
    for c in classes:
        cells[(c["day_of_week"], c["period_no"])].append(c)
    rows, prev = [], None
    for p in sorted(periods):
        s = periods[p]
        if prev is not None:
            gap = to_minutes(s["start_time"]) - to_minutes(prev["end_time"])
            if gap >= break_min:
                rows.append({"kind": "break", "label": "Lunch" if gap >= 45 else "Break",
                             "sub": f"{fmt_time(prev['end_time'])} - {fmt_time(s['start_time'])}",
                             "cells": {}})
        rows.append({"kind": "period", "label": f"P{p}",
                     "sub": f"{fmt_time(s['start_time'])} - {fmt_time(s['end_time'])}",
                     "cells": {d: cells.get((d, p), []) for d in days}})
        prev = s
    return {"days": days, "rows": rows}


_TYPE_STYLE = {   # translucent colours so it works in both light and dark themes
    "THEORY": ("rgba(66,135,245,0.20)", "#4287f5"),
    "LAB": ("rgba(245,158,11,0.24)", "#f59e0b"),
}


def grid_to_html(grid: dict) -> str:
    """Render build_week_grid() output as an HTML table (all text HTML-escaped)."""
    from html import escape
    cell_css = "border:1px solid rgba(128,128,128,.35);padding:6px;vertical-align:top;"
    out = ['<table style="border-collapse:collapse;width:100%;table-layout:fixed;font-size:0.8rem;">',
           "<tr>" + f'<th style="{cell_css}width:92px;">Period</th>'
           + "".join(f'<th style="{cell_css}">{escape(d)}</th>' for d in grid["days"]) + "</tr>"]
    for row in grid["rows"]:
        head = (f'<td style="{cell_css}"><b>{escape(row["label"])}</b><br>'
                f'<span style="opacity:.7">{escape(row["sub"])}</span></td>')
        if row["kind"] == "break":
            out.append(f'<tr>{head}<td colspan="{len(grid["days"])}" style="{cell_css}'
                       f'text-align:center;opacity:.55;">{escape(row["label"])}</td></tr>')
            continue
        tds = []
        for d in grid["days"]:
            blocks = []
            for c in row["cells"][d]:
                bg, bar = _TYPE_STYLE.get(c.get("course_type"), _TYPE_STYLE["THEORY"])
                lines = [escape(str(x)) for x in c["lines"]]
                body = f"<b>{lines[0]}</b>" + "".join(f"<br>{x}" for x in lines[1:])
                blocks.append(f'<div style="background:{bg};border-left:4px solid {bar};'
                              f'padding:4px 6px;border-radius:4px;margin:2px 0;">{body}</div>')
            tds.append(f'<td style="{cell_css}">{"".join(blocks) or "&nbsp;"}</td>')
        out.append(f"<tr>{head}{''.join(tds)}</tr>")
    out.append("</table>")
    return "".join(out)