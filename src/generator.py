"""
generator.py -- clash-free timetable generator (reads MySQL, writes Timetable).

ALGORITHM (how it works)
------------------------
1. LOAD everything from MySQL: slots, sections, Section_Course plan, Teaches,
   rooms and the course CONFLICT GRAPH (utils.build_conflict_graph: courses
   that share qualified faculty or enrolled students, weighted by how many).

2. PRE-FLIGHT. Cheap necessary-condition checks that explain impossible
   inputs immediately (course nobody teaches, no room big enough, a faculty
   pool that cannot cover a course's hours, more hours than slots ...).

3. TASKS. Every (section, course) needs `weekly_hours` periods per week.
   THEORY -> one task per period. LAB -> blocks of up to 3 consecutive
   periods (one task per block).

4. ORDER BY DIFFICULTY ("most constrained first", as in graph colouring):
   lab blocks first, then fewer qualified faculty, fewer eligible rooms,
   higher weighted conflict-graph degree, more weekly hours. Hard tasks
   placed early have the most free slots to choose from.

5. BACKTRACKING SEARCH (iterative DFS, explicit stack). For each task we
   enumerate every placement (faculty, room, slot-window) that satisfies the
   HARD constraints:
       - section / faculty / room not already used in any of those slots
       - faculty in Teaches for the course
       - room type matches (LAB<->LAB, THEORY<->LECTURE), capacity >= strength
   and sort them by a SOFT-constraint penalty (lower first):
       - same course twice on one day (section)           +100 per repeat
       - faculty teaching more than 3 consecutive periods +80 per extra
       - lab block split into non-consecutive periods     +1000 (fallback)
       - uneven daily load of section / faculty, Saturday  small weights
   The first (cheapest) candidate is placed; if a later task has no
   candidate we UNDO and try the next one at the previous level
   (chronological backtracking). Because the soft penalties steer the
   search, the first solution found is usually also a good one.
   Budget: node limit + time limit, with a few randomised restarts
   (shuffled tie-breaks) so one unlucky early choice cannot trap the search.

6. FAILURE HANDLING. If no complete timetable is found, a no-backtracking
   greedy pass places what it can and diagnoses every task it cannot place
   (e.g. "31 of 48 slots already used by the section, 12 slots have every
   qualified faculty busy ..."). Nothing is written unless allow_partial.

7. SAVE in ONE TRANSACTION (delete the year's old rows, insert the new).
   MySQL errors (UNIQUE clash 1062, FK 1452, trigger rules 5001-5003) are
   caught, translated to plain English, and the transaction is rolled back.

Run:  python src/generator.py --year 2026-2027 --replace
"""
from __future__ import annotations

import argparse
import random
import sys
import time
from collections import Counter, defaultdict
from dataclasses import dataclass, field

import mysql.connector

import db
import utils

# ----------------------------------------------------------------- tunables
MAX_LAB_BLOCK = 3          # longest run of consecutive lab periods in one block
MAX_FAC_CONSEC = 3         # soft: faculty should not teach more than this in a row
LONG_BREAK_MIN = 30        # a gap >= this (lunch) breaks "consecutive"
K_ROOMS = 2                # rooms tried per (faculty, window): best-fit first

W_SAME_DAY = 100           # same course again on a day the section already has it
W_FAC_RUN = 80             # per period beyond MAX_FAC_CONSEC in a row
W_SCATTER = 1000           # lab hours not in one consecutive block
W_SEC_BALANCE = 4          # per class the section already has that day
W_FAC_BALANCE = 3          # per class the faculty already has that day
W_SATURDAY = 5             # mild preference for Mon-Fri


# --------------------------------------------------------------- data types
@dataclass(frozen=True)
class Task:
    """One schedulable unit: `block_size` periods of a course for a section."""
    section_id: int
    course_id: str
    block_size: int
    seq: int                # index among the (section, course) tasks
    course_type: str


@dataclass(frozen=True)
class Placement:
    task: Task
    faculty_id: int
    room_id: int
    slots: tuple            # slot_ids covered (contiguous unless scattered fallback)
    penalty: int


class GenerationError(Exception):
    """Raised when saving fails (carries a plain-English reason)."""


@dataclass
class GenerationResult:
    academic_year: str
    required: int = 0                       # periods that must be placed
    placed: int = 0                         # periods actually placed
    complete: bool = False
    committed: bool = False
    rows: list = field(default_factory=list)       # (course, fac, room, slot, section, year)
    unplaced: list = field(default_factory=list)   # dicts: section, course, periods, reason
    preflight: list = field(default_factory=list)  # blocking input problems
    soft: dict = field(default_factory=dict)
    nodes: int = 0
    attempts: int = 0
    elapsed: float = 0.0
    error: str | None = None
    note: str | None = None

    @property
    def success(self) -> bool:
        return self.complete and self.error is None

    def report(self) -> str:
        status = ("COMPLETE" if self.complete
                  else "PARTIAL" if self.placed else "FAILED")
        lines = [f"Academic year : {self.academic_year}",
                 f"Result        : {status}",
                 f"Periods placed: {self.placed} / {self.required}"
                 f"   (search: {self.attempts} attempt(s), {self.nodes} nodes, "
                 f"{self.elapsed:.2f}s)"]
        if self.soft:
            lines.append("Soft constraints (best effort):")
            lines.append(f"  - same course twice on one day : {self.soft['same_day_repeats']}")
            lines.append(f"  - faculty runs > {MAX_FAC_CONSEC} consecutive   : {self.soft['faculty_long_runs']}")
            lines.append(f"  - lab blocks not consecutive   : {self.soft['lab_blocks_split']}")
        if self.preflight:
            lines.append("Input problems (nothing was searched):")
            lines += [f"  ! {p}" for p in self.preflight]
        if self.unplaced:
            lines.append("Could NOT be placed:")
            for u in self.unplaced:
                lines.append(f"  ! section {u['section']} / {u['course']} "
                             f"({u['periods']} period(s)): {u['reason']}")
        lines.append("Saved to MySQL: " + (f"yes ({len(self.rows)} rows)" if self.committed
                                           else "no" + (f" -- {self.note}" if self.note else "")))
        if self.error:
            lines.append(f"Error         : {self.error}")
        return "\n".join(lines)


# --------------------------------------------------------------- the problem
class Problem:
    """Everything the scheduler needs, loaded once from MySQL."""

    def __init__(self, conn):
        self.slots = db.query(conn, "SELECT slot_id, day_of_week, period_no, start_time, "
                                    "end_time FROM TimeSlot ORDER BY slot_id")
        self.slot_ids = [s["slot_id"] for s in self.slots]
        self.slot_day = {s["slot_id"]: s["day_of_week"] for s in self.slots}
        self.next_slot, self.prev_slot = utils.build_slot_links(self.slots, LONG_BREAK_MIN)

        self.sections = {r["section_id"]: r for r in db.query(
            conn, "SELECT section_id, section_name, semester, strength, dept_id FROM Section")}
        self.courses = {r["course_id"]: r for r in db.query(
            conn, "SELECT course_id, course_name, weekly_hours, course_type FROM Course")}
        self.plan = db.query(
            conn, "SELECT section_id, course_id FROM Section_Course ORDER BY section_id, course_id")
        self.teach: dict[str, list[int]] = defaultdict(list)
        for r in db.query(conn, "SELECT faculty_id, course_id FROM Teaches ORDER BY faculty_id"):
            self.teach[r["course_id"]].append(r["faculty_id"])
        self.faculty_name = {r["faculty_id"]: r["name"] for r in db.query(
            conn, "SELECT faculty_id, name FROM Faculty")}
        self.rooms = db.query(
            conn, "SELECT room_id, room_no, building, capacity, room_type FROM Classroom")
        self.conflict = utils.build_conflict_graph(conn)
        self._windows: dict[int, list[tuple]] = {}

    # rooms that satisfy hard room constraints for (section, course), best fit first
    def eligible_rooms(self, section_id: int, course_id: str) -> list[int]:
        need = self.room_need(course_id)
        strength = self.sections[section_id]["strength"]
        ok = [r for r in self.rooms if r["room_type"] == need and r["capacity"] >= strength]
        ok.sort(key=lambda r: (r["capacity"], r["room_id"]))
        return [r["room_id"] for r in ok]

    def room_need(self, course_id: str) -> str:
        return "LAB" if self.courses[course_id]["course_type"] == "LAB" else "LECTURE"

    def windows(self, size: int) -> list[tuple]:
        """All tuples of `size` consecutive slot ids (no lunch break inside)."""
        if size not in self._windows:
            out = []
            for sid in self.slot_ids:
                win, cur = [sid], sid
                while len(win) < size and cur in self.next_slot:
                    cur = self.next_slot[cur]
                    win.append(cur)
                if len(win) == size:
                    out.append(tuple(win))
            self._windows[size] = out
        return self._windows[size]

    def conflict_weight(self, course_id: str) -> int:
        return sum(self.conflict.get(course_id, {}).values())


def split_hours(hours: int, course_type: str) -> list[int]:
    """THEORY: one period per task. LAB: as few blocks as possible, each <= MAX_LAB_BLOCK."""
    if course_type != "LAB":
        return [1] * hours
    n_blocks = -(-hours // MAX_LAB_BLOCK)           # ceil
    base, extra = divmod(hours, n_blocks)
    return [base + (1 if i < extra else 0) for i in range(n_blocks)]


def make_tasks(P: Problem) -> list[Task]:
    tasks = []
    for row in P.plan:
        c = P.courses[row["course_id"]]
        for seq, size in enumerate(split_hours(c["weekly_hours"], c["course_type"])):
            tasks.append(Task(row["section_id"], row["course_id"], size, seq, c["course_type"]))
    return tasks


# --------------------------------------------------------------- pre-flight
def preflight(P: Problem, tasks: list[Task]) -> list[str]:
    """Necessary conditions only: if one fails, no timetable can exist."""
    issues = []
    n_slots = len(P.slots)
    if not P.slots:
        return ["TimeSlot is empty."]
    if not P.plan:
        return ["Section_Course is empty: no section has any course to schedule."]

    hours_by_section = Counter()
    hours_by_course = Counter()
    for row in P.plan:
        s, c = row["section_id"], row["course_id"]
        h = P.courses[c]["weekly_hours"]
        hours_by_section[s] += h
        hours_by_course[c] += h
        if not P.teach.get(c):
            issues.append(f"course {c}: no faculty in Teaches (section {s}).")
        if not P.eligible_rooms(s, c):
            sec = P.sections[s]
            issues.append(f"course {c} / section {s}: no {P.room_need(c)} room with "
                          f"capacity >= {sec['strength']}.")
    for s, h in hours_by_section.items():
        if h > n_slots:
            issues.append(f"section {s} needs {h} periods/week but only {n_slots} slots exist.")
    for c, h in hours_by_course.items():
        cap = len(P.teach.get(c, [])) * n_slots
        if P.teach.get(c) and h > cap:
            issues.append(f"course {c} needs {h} periods/week across sections but its "
                          f"{len(P.teach[c])} qualified faculty offer at most {cap}.")
    for need in ("LAB", "LECTURE"):
        demand = sum(t.block_size for t in tasks if P.room_need(t.course_id) == need)
        n_rooms = sum(1 for r in P.rooms if r["room_type"] == need)
        if demand > n_rooms * n_slots:
            issues.append(f"{need} rooms: {demand} periods needed but {n_rooms} rooms x "
                          f"{n_slots} slots = {n_rooms * n_slots}.")
    return issues


# ----------------------------------------------------------------- state
class State:
    """Mutable schedule under construction, with O(1) place/unplace."""

    def __init__(self, P: Problem):
        self.P = P
        self.busy_room: set = set()      # (room_id, slot_id)
        self.busy_fac: set = set()       # (faculty_id, slot_id)
        self.busy_sec: set = set()       # (section_id, slot_id)
        self.sec_day = Counter()         # (section_id, day) -> periods
        self.fac_day = Counter()         # (faculty_id, day) -> periods
        self.course_day = defaultdict(Counter)   # (section, course) -> day -> sessions

    def _days(self, slots):
        return {self.P.slot_day[x] for x in slots}

    def place(self, p: Placement):
        t = p.task
        for sl in p.slots:
            day = self.P.slot_day[sl]
            self.busy_room.add((p.room_id, sl))
            self.busy_fac.add((p.faculty_id, sl))
            self.busy_sec.add((t.section_id, sl))
            self.sec_day[(t.section_id, day)] += 1
            self.fac_day[(p.faculty_id, day)] += 1
        for d in self._days(p.slots):
            self.course_day[(t.section_id, t.course_id)][d] += 1

    def unplace(self, p: Placement):
        t = p.task
        for sl in p.slots:
            day = self.P.slot_day[sl]
            self.busy_room.discard((p.room_id, sl))
            self.busy_fac.discard((p.faculty_id, sl))
            self.busy_sec.discard((t.section_id, sl))
            self.sec_day[(t.section_id, day)] -= 1
            self.fac_day[(p.faculty_id, day)] -= 1
        for d in self._days(p.slots):
            self.course_day[(t.section_id, t.course_id)][d] -= 1


# -------------------------------------------------------------- scheduler
class Scheduler:
    def __init__(self, P: Problem, rng: random.Random):
        self.P = P
        self.rng = rng
        self.state = State(P)
        self._rooms_cache: dict = {}

    def rooms_for(self, t: Task) -> list[int]:
        key = (t.section_id, t.course_id)
        if key not in self._rooms_cache:
            self._rooms_cache[key] = self.P.eligible_rooms(*key)
        return self._rooms_cache[key]

    # ---- ordering ---------------------------------------------------
    def order_tasks(self, tasks: list[Task], jitter: float) -> list[Task]:
        """Most constrained first. jitter > 0 perturbs scores (used on restarts)."""
        noise: dict = {}

        def key(t: Task):
            k = (t.section_id, t.course_id)
            if k not in noise:
                noise[k] = 1.0 + jitter * self.rng.random()
            n = noise[k]
            return (1 if t.block_size > 1 else 0,
                    n / max(1, len(self.P.teach[t.course_id])),        # few faculty = hard
                    n / max(1, len(self.rooms_for(t))),                # few rooms = hard
                    n * self.P.conflict_weight(t.course_id),           # conflict-graph degree
                    self.P.courses[t.course_id]["weekly_hours"])
        # sort by difficulty (desc); ties keep (section, course, seq) order so a
        # course's tasks stay adjacent, which helps the same-day soft penalty
        return sorted(tasks, key=lambda t: (tuple(-x for x in key(t)),
                                            t.section_id, t.course_id, t.seq))

    # ---- soft constraints --------------------------------------------
    def soft_penalty(self, t: Task, fac: int, slots: tuple) -> int:
        P, st = self.P, self.state
        pen = 0
        days = {P.slot_day[x] for x in slots}
        for d in days:
            pen += W_SAME_DAY * st.course_day[(t.section_id, t.course_id)][d]
            pen += W_SEC_BALANCE * st.sec_day[(t.section_id, d)]
            pen += W_FAC_BALANCE * st.fac_day[(fac, d)]
            if d == "SAT":
                pen += W_SATURDAY
        # longest run of consecutive periods this faculty would then have
        newset, worst = set(slots), 0
        for sl in slots:
            run, x = 1, P.prev_slot.get(sl)
            while x is not None and (x in newset or (fac, x) in st.busy_fac):
                run += 1
                x = P.prev_slot.get(x)
            x = P.next_slot.get(sl)
            while x is not None and (x in newset or (fac, x) in st.busy_fac):
                run += 1
                x = P.next_slot.get(x)
            worst = max(worst, run)
        if worst > MAX_FAC_CONSEC:
            pen += W_FAC_RUN * (worst - MAX_FAC_CONSEC)
        return pen

    # ---- candidate generation ----------------------------------------
    def candidates(self, t: Task) -> list[Placement]:
        """All hard-constraint-valid placements of `t`, cheapest soft penalty first."""
        P, st = self.P, self.state
        rooms, facs = self.rooms_for(t), P.teach[t.course_id]
        out: list = []
        for win in P.windows(t.block_size):
            if any((t.section_id, x) in st.busy_sec for x in win):
                continue
            for f in facs:
                if any((f, x) in st.busy_fac for x in win):
                    continue
                pen = None
                taken = 0
                for r in rooms:                       # best-fit rooms first
                    if any((r, x) in st.busy_room for x in win):
                        continue
                    if pen is None:
                        pen = self.soft_penalty(t, f, win)
                    out.append((pen, self.rng.random(), Placement(t, f, r, win, pen)))
                    taken += 1
                    if taken >= K_ROOMS:
                        break
        out.sort(key=lambda x: (x[0], x[1]))
        result = [p for _, _, p in out]
        if t.block_size > 1:                          # soft fallback: split the lab hours
            result += self._scattered(t, rooms, facs)
        return result

    def _scattered(self, t: Task, rooms: list[int], facs: list[int], limit: int = 6):
        """Fallback when no consecutive window works: pick free periods on distinct days."""
        P, st = self.P, self.state
        found = []
        for f in facs:
            for r in rooms[:K_ROOMS]:
                free = [x for x in P.slot_ids
                        if (t.section_id, x) not in st.busy_sec
                        and (f, x) not in st.busy_fac and (r, x) not in st.busy_room]
                free.sort(key=lambda x: self.soft_penalty(t, f, (x,)))
                chosen, used_days = [], set()
                for x in free:                        # prefer distinct days first
                    if P.slot_day[x] not in used_days:
                        chosen.append(x)
                        used_days.add(P.slot_day[x])
                    if len(chosen) == t.block_size:
                        break
                for x in free:                        # then top up from any day
                    if len(chosen) == t.block_size:
                        break
                    if x not in chosen:
                        chosen.append(x)
                if len(chosen) == t.block_size:
                    slots = tuple(sorted(chosen))
                    found.append(Placement(t, f, r, slots,
                                           W_SCATTER + self.soft_penalty(t, f, slots)))
        found.sort(key=lambda p: p.penalty)
        return found[:limit]

    # ---- backtracking search ------------------------------------------
    def solve(self, tasks: list[Task], max_nodes: int, deadline: float):
        """
        Iterative DFS with an explicit stack (no recursion limit).
        stack[i] = [candidate list, next index to try, placement currently applied].
        Returns (placements | None, nodes_used).
        The candidate list of level i stays valid while we backtrack through
        deeper levels, because levels < i are unchanged and level i's own
        placement is undone before the next candidate is applied.
        """
        self.state = State(self.P)
        stack: list = []
        i, nodes = 0, 0
        while 0 <= i < len(tasks):
            if nodes >= max_nodes or time.monotonic() > deadline:
                return None, nodes
            if i == len(stack):                       # first visit of this level
                stack.append([self.candidates(tasks[i]), 0, None])
            frame = stack[i]
            if frame[2] is not None:                  # undo the previous try here
                self.state.unplace(frame[2])
                frame[2] = None
            if frame[1] < len(frame[0]):
                p = frame[0][frame[1]]
                frame[1] += 1
                nodes += 1
                self.state.place(p)
                frame[2] = p
                i += 1
            else:                                     # dead end: backtrack
                stack.pop()
                i -= 1
        if i < 0:                                     # whole tree exhausted
            return None, nodes
        return [f[2] for f in stack], nodes

    # ---- best-effort pass + diagnosis ----------------------------------
    def greedy_partial(self, tasks: list[Task]):
        """No backtracking: place what fits, explain what does not."""
        self.state = State(self.P)
        placed, unplaced = [], []
        for t in tasks:
            cands = self.candidates(t)
            if cands:
                self.state.place(cands[0])
                placed.append(cands[0])
            else:
                unplaced.append((t, self.diagnose(t)))
        return placed, unplaced

    def diagnose(self, t: Task) -> str:
        P, st = self.P, self.state
        facs, rooms = P.teach[t.course_id], self.rooms_for(t)
        need = P.room_need(t.course_id)
        strength = P.sections[t.section_id]["strength"]
        if not facs:
            return "no faculty listed in Teaches for this course"
        if not rooms:
            return f"no {need} room with capacity >= {strength}"
        sec_busy = no_fac = no_room = usable = 0
        for sl in P.slot_ids:
            if (t.section_id, sl) in st.busy_sec:
                sec_busy += 1
            elif all((f, sl) in st.busy_fac for f in facs):
                no_fac += 1
            elif all((r, sl) in st.busy_room for r in rooms):
                no_room += 1
            else:
                usable += 1
        names = ", ".join(P.faculty_name.get(f, str(f)) for f in facs)
        msg = (f"of {len(P.slot_ids)} slots: {sec_busy} already used by the section, "
               f"{no_fac} have every qualified faculty ({names}) busy, "
               f"{no_room} have no free {need} room with >= {strength} seats, "
               f"{usable} usable")
        if t.block_size > 1 and usable:
            msg += f" (but no {t.block_size} consecutive free periods together)"
        return msg


# ------------------------------------------------------- soft-constraint audit
def evaluate_soft(P: Problem, placements: list[Placement]) -> dict:
    """Independent audit of the soft constraints (used in the report and tests)."""
    per_course_day = defaultdict(Counter)
    fac_slots = defaultdict(set)
    lab_split = 0
    for p in placements:
        t = p.task
        for d in {P.slot_day[x] for x in p.slots}:
            per_course_day[(t.section_id, t.course_id)][d] += 1
        fac_slots[p.faculty_id].update(p.slots)
        if t.block_size > 1 and any(P.next_slot.get(a) != b
                                    for a, b in zip(p.slots, p.slots[1:])):
            lab_split += 1
    repeats = sum(max(0, n - 1) for days in per_course_day.values() for n in days.values())
    long_runs = 0
    for slots in fac_slots.values():
        for sl in slots:
            if P.prev_slot.get(sl) in slots:
                continue                              # not the start of a run
            run, x = 1, sl
            while P.next_slot.get(x) in slots:
                run += 1
                x = P.next_slot[x]
            if run > MAX_FAC_CONSEC:
                long_runs += 1
    return {"same_day_repeats": repeats, "faculty_long_runs": long_runs,
            "lab_blocks_split": lab_split}


# ----------------------------------------------------------------- saving
_INSERT = ("INSERT INTO Timetable (course_id, faculty_id, room_id, slot_id, section_id, "
           "academic_year) VALUES (%s, %s, %s, %s, %s, %s)")


def save_timetable(conn, academic_year: str, rows: list[tuple]) -> None:
    """
    Replace the year's timetable atomically. Any MySQL error rolls everything
    back (the old timetable survives) and is raised as a GenerationError with
    a plain-English reason and the offending row.
    """
    cur = conn.cursor()
    try:
        cur.execute("DELETE FROM Timetable WHERE academic_year = %s", (academic_year,))
        for row in rows:
            try:
                cur.execute(_INSERT, row)
            except mysql.connector.Error as exc:
                raise GenerationError(
                    f"{utils.describe_mysql_error(exc)} "
                    f"[course={row[0]} faculty={row[1]} room={row[2]} "
                    f"slot={row[3]} section={row[4]}]") from exc
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        cur.close()


# ------------------------------------------------------ what-if validation
def check_proposed_class(conn, academic_year: str, section_id: int, course_id: str,
                         faculty_id: int, room_id: int, slot_id: int) -> list[dict]:
    """
    Would this class be accepted? Checks every rule WITHOUT writing anything.
    Returns one dict per rule: {"rule", "ok", "hard", "detail"}. hard=True rules
    are enforced by MySQL (unique keys, foreign keys, triggers); the weekly-hours
    rule is enforced by the generator only.
    """
    def one(sql, params):
        rows = db.query(conn, sql, params)
        return rows[0] if rows else None

    sec = one("SELECT strength FROM Section WHERE section_id = %s", (section_id,))
    crs = one("SELECT course_type, weekly_hours FROM Course WHERE course_id = %s", (course_id,))
    room = one("SELECT building, room_no, capacity, room_type FROM Classroom WHERE room_id = %s", (room_id,))
    taught = one("SELECT 1 AS x FROM Teaches WHERE faculty_id = %s AND course_id = %s",
                 (faculty_id, course_id))
    planned = one("SELECT 1 AS x FROM Section_Course WHERE section_id = %s AND course_id = %s",
                  (section_id, course_id))
    busy = db.query(conn, "SELECT t.section_id, t.faculty_id, t.room_id, t.course_id "
                          "FROM Timetable t WHERE t.academic_year = %s AND t.slot_id = %s "
                          "AND (t.section_id = %s OR t.faculty_id = %s OR t.room_id = %s)",
                    (academic_year, slot_id, section_id, faculty_id, room_id))
    done = one("SELECT COUNT(*) AS n FROM Timetable WHERE academic_year = %s AND section_id = %s "
               "AND course_id = %s", (academic_year, section_id, course_id))["n"]

    def clash(col, value):
        hit = [b for b in busy if b[col] == value]
        return (not hit, "free" if not hit else f"already used by a {hit[0]['course_id']} class in this slot")

    ok_s, d_s = clash("section_id", section_id)
    ok_f, d_f = clash("faculty_id", faculty_id)
    ok_r, d_r = clash("room_id", room_id)
    need = "LAB" if crs and crs["course_type"] == "LAB" else "LECTURE"
    out = [
        {"rule": "Section is free in this slot", "ok": ok_s, "hard": True, "detail": d_s},
        {"rule": "Faculty is free in this slot", "ok": ok_f, "hard": True, "detail": d_f},
        {"rule": "Room is free in this slot", "ok": ok_r, "hard": True, "detail": d_r},
        {"rule": "Faculty is qualified (Teaches)", "ok": taught is not None, "hard": True,
         "detail": "listed in Teaches" if taught else "not listed in Teaches for this course"},
        {"rule": "Course is in the section's plan (Section_Course)", "ok": planned is not None, "hard": True,
         "detail": "in the plan" if planned else "section does not take this course"},
    ]
    if crs and room:
        out.append({"rule": "Room type fits the course", "ok": room["room_type"] == need, "hard": True,
                    "detail": f"{crs['course_type']} course needs a {need} room; "
                              f"{room['building']}-{room['room_no']} is {room['room_type']}"})
    if sec and room:
        out.append({"rule": "Room capacity >= section strength", "ok": room["capacity"] >= sec["strength"],
                    "hard": True, "detail": f"capacity {room['capacity']} vs strength {sec['strength']}"})
    if crs:
        out.append({"rule": "Section still needs hours for this course", "ok": done < crs["weekly_hours"],
                    "hard": False, "detail": f"{done} of {crs['weekly_hours']} weekly periods already scheduled"})
    return out


# ------------------------------------------------------------- main entry
def generate_timetable(academic_year: str | None = None, *, replace: bool = False,
                       allow_partial: bool = False, dry_run: bool = False,
                       time_limit: float = 30.0, max_nodes: int = 300_000,
                       restarts: int = 4, seed: int = 42, conn=None) -> GenerationResult:
    """
    Build a timetable for `academic_year` and (unless dry_run) save it.
      replace       overwrite existing rows of that year (else refuse if any exist)
      allow_partial save a clash-free partial timetable if a complete one is impossible
      dry_run       compute and report only; never touch MySQL data
    """
    year = academic_year or utils.academic_year()
    res = GenerationResult(academic_year=year)
    t0 = time.monotonic()
    own = conn is None
    if own:
        conn = db.connect()
    try:
        existing = db.query(conn, "SELECT COUNT(*) AS n FROM Timetable WHERE academic_year = %s",
                            (year,))[0]["n"]
        P = Problem(conn)
        tasks = make_tasks(P)
        res.required = sum(t.block_size for t in tasks)

        res.preflight = preflight(P, tasks)
        if res.preflight:
            res.note = "input data makes a timetable impossible"
            return res

        solution = None
        deadline = t0 + time_limit
        attempts = max(1, restarts)                   # restarts=0 still means one attempt
        for attempt in range(attempts):
            now = time.monotonic()
            share = (deadline - now) / (attempts - attempt)
            sched = Scheduler(P, random.Random(seed + attempt))
            ordered = sched.order_tasks(tasks, jitter=0.0 if attempt == 0 else 0.4)
            solution, nodes = sched.solve(ordered, max_nodes // attempts, now + share)
            res.nodes += nodes
            res.attempts = attempt + 1
            if solution is not None:
                break

        if solution is not None:
            placements, res.complete = solution, True
        else:
            # no complete timetable within budget: best effort + diagnosis
            sched = Scheduler(P, random.Random(seed))
            placements, unplaced = sched.greedy_partial(sched.order_tasks(tasks, 0.0))
            grouped: dict = {}                    # one entry per (section, course)
            for t, why in unplaced:
                g = grouped.setdefault((t.section_id, t.course_id),
                                       {"section": t.section_id, "course": t.course_id,
                                        "periods": 0, "reason": why})
                g["periods"] += t.block_size
            res.unplaced = list(grouped.values())

        res.placed = sum(len(p.slots) for p in placements)
        res.soft = evaluate_soft(P, placements)
        res.rows = sorted(
            ((p.task.course_id, p.faculty_id, p.room_id, sl, p.task.section_id, year)
             for p in placements for sl in p.slots), key=lambda r: (r[4], r[3]))

        if dry_run:
            res.note = "dry run"
        elif not res.complete and not allow_partial:
            res.note = "incomplete timetable not saved (use allow_partial to keep a partial one)"
        elif existing and not replace:
            res.note = (f"{existing} rows already exist for {year}; "
                        "pass replace=True / --replace to overwrite")
        else:
            try:
                save_timetable(conn, year, res.rows)
                res.committed = True
            except GenerationError as exc:
                res.error = str(exc)
                res.note = "save failed, transaction rolled back"
        return res
    finally:
        res.elapsed = time.monotonic() - t0
        if own:
            conn.close()


# --------------------------------------------------------------------- CLI
def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Generate a clash-free university timetable.")
    ap.add_argument("--year", default=None, help="academic year (default env ACADEMIC_YEAR)")
    ap.add_argument("--replace", action="store_true", help="overwrite an existing timetable for the year")
    ap.add_argument("--allow-partial", action="store_true", help="save a partial timetable if complete is impossible")
    ap.add_argument("--dry-run", action="store_true", help="compute and report, write nothing")
    ap.add_argument("--time-limit", type=float, default=30.0, help="search time budget in seconds")
    ap.add_argument("--max-nodes", type=int, default=300_000, help="search node budget")
    ap.add_argument("--restarts", type=int, default=4, help="randomised restarts")
    ap.add_argument("--seed", type=int, default=42)
    args = ap.parse_args(argv)

    try:
        res = generate_timetable(args.year, replace=args.replace, allow_partial=args.allow_partial,
                                 dry_run=args.dry_run, time_limit=args.time_limit,
                                 max_nodes=args.max_nodes, restarts=args.restarts, seed=args.seed)
    except (mysql.connector.Error, RuntimeError) as exc:      # connection / configuration problems
        print(f"Could not use MySQL: {exc}\n"
              "Check that the database server is running and that MYSQL_HOST / MYSQL_PORT / "
              "MYSQL_USER / MYSQL_PASSWORD in .env are correct (see README, Troubleshooting).",
              file=sys.stderr)
        return 2
    print(res.report())
    return 0 if res.success else 1


if __name__ == "__main__":
    sys.exit(main())
