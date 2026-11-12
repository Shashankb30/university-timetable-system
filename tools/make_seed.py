#!/usr/bin/env python3
"""
make_seed.py -- writes sql/03_seed_data.sql (plain INSERT statements).

Optional helper: you do NOT need to run this, the generated SQL file is
already provided. Re-run it only if you want to tweak the data:
    python tools/make_seed.py            # rewrites sql/03_seed_data.sql
Output is deterministic (fixed random seed), so reruns give identical SQL.

Sizing (all >= the brief's minimums):
    3 departments, 20 faculty, 30 courses (24 THEORY + 6 LAB), 10 sections,
    120 students, 25 rooms (18 LECTURE + 7 LAB), 48 time slots (6 days x 8).
"""
import random
import sys
from pathlib import Path

rng = random.Random(42)


def q(v):
    """SQL literal: numbers raw, strings quoted with '' escaping."""
    if v is None:
        return "NULL"
    if isinstance(v, (int, float)):
        return str(v)
    return "'" + str(v).replace("'", "''") + "'"


def insert(table, cols, rows):
    body = ",\n".join("  (" + ", ".join(q(x) for x in r) + ")" for r in rows)
    return f"INSERT INTO {table} ({', '.join(cols)}) VALUES\n{body};\n"


# ---------------------------------------------------------------- departments
DEPTS = [
    (1, "Computer Science and Engineering", "Block A"),
    (2, "Electronics and Communication Engineering", "Block B"),
    (3, "Mechanical Engineering", "Block C"),
]
DEPT_CODE = {1: "CS", 2: "EC", 3: "ME"}

# ------------------------------------------------------------------- faculty
# ids are explicit so Teaches below can reference them deterministically
FACULTY_BY_DEPT = {
    1: ["Dr. Meenakshi Sundaram", "Dr. Arvind Krishnan", "Prof. Lakshmi Narayanan",
        "Dr. Priya Venkatesh", "Prof. Rajesh Iyer", "Dr. Kavitha Subramanian",
        "Prof. Suresh Babu", "Dr. Anjali Menon", "Prof. Karthik Raman",
        "Dr. Divya Chandran"],                                   # ids 1-10
    2: ["Dr. Ramesh Pillai", "Prof. Sangeetha Gopal", "Dr. Vikram Nair",
        "Prof. Deepa Murali", "Dr. Harish Kumar", "Prof. Nirmala Devi"],  # 11-16
    3: ["Dr. Balaji Srinivasan", "Prof. Gayathri Ramesh", "Dr. Mohan Rajan",
        "Prof. Sunitha Das"],                                    # 17-20
}
FACULTY = []
fid = 1
for dept_id, names in FACULTY_BY_DEPT.items():
    for n in names:
        plain = n.replace("Dr. ", "").replace("Prof. ", "")
        email = plain.lower().replace(" ", ".") + "@university.edu"
        FACULTY.append((fid, n, email, dept_id))
        fid += 1

# ------------------------------------------------------------------- courses
# (id, name, credits, weekly_hours, type, dept_id)
T, L = "THEORY", "LAB"
COURSES = [
    # CSE semester 3
    ("CS201", "Data Structures", 4, 4, T, 1),
    ("CS202", "Digital Logic Design", 3, 3, T, 1),
    ("CS203", "Discrete Mathematics", 3, 3, T, 1),
    ("CS204", "Object Oriented Programming", 3, 3, T, 1),
    ("CS205L", "Data Structures Lab", 1, 2, L, 1),
    # CSE semester 5
    ("CS301", "Database Management Systems", 4, 4, T, 1),
    ("CS302", "Operating Systems", 4, 4, T, 1),
    ("CS303", "Computer Networks", 3, 3, T, 1),
    ("CS304", "Theory of Computation", 3, 3, T, 1),
    ("CS305L", "DBMS Lab", 1, 2, L, 1),
    # ECE semester 3
    ("EC201", "Network Analysis", 4, 4, T, 2),
    ("EC202", "Electronic Devices", 3, 3, T, 2),
    ("EC203", "Signals and Systems", 3, 3, T, 2),
    ("EC204", "Digital Electronics", 3, 3, T, 2),
    ("EC205L", "Electronics Lab", 1, 2, L, 2),
    # ECE semester 5
    ("EC301", "Analog Circuits", 4, 4, T, 2),
    ("EC302", "Digital Signal Processing", 4, 4, T, 2),
    ("EC303", "Microprocessors", 3, 3, T, 2),
    ("EC304", "Communication Systems", 3, 3, T, 2),
    ("EC305L", "DSP Lab", 1, 2, L, 2),
    # MECH semester 3
    ("ME201", "Thermodynamics", 4, 4, T, 3),
    ("ME202", "Fluid Mechanics", 3, 3, T, 3),
    ("ME203", "Strength of Materials", 3, 3, T, 3),
    ("ME204", "Manufacturing Processes", 3, 3, T, 3),
    ("ME205L", "Workshop Lab", 1, 2, L, 3),
    # MECH semester 5
    ("ME301", "Heat Transfer", 4, 4, T, 3),
    ("ME302", "Machine Design", 4, 4, T, 3),
    ("ME303", "Dynamics of Machinery", 3, 3, T, 3),
    ("ME304", "Metrology and Measurements", 3, 3, T, 3),
    ("ME305L", "Thermal Engineering Lab", 1, 2, L, 3),
]
# Courses of each (dept, semester) = the plan every section of it follows
PLAN = {}
for c in COURSES:
    sem = 3 if c[0][2] == "2" else 5          # '2xx' -> semester 3, '3xx' -> 5
    PLAN.setdefault((c[5], sem), []).append(c[0])

# ------------------------------------------------------------------ teaches
# Each course has exactly 2 qualified faculty; two cross-department rows
# (F7 -> EC303, F13 -> CS202) give the graph some inter-department links.
TEACHES = {
    "CS201": [1, 2], "CS202": [3, 9, 13], "CS203": [3, 4], "CS204": [2, 4],
    "CS205L": [1, 10], "CS301": [5, 6], "CS302": [6, 7], "CS303": [7, 8],
    "CS304": [8, 9], "CS305L": [5, 10],
    "EC201": [11, 12], "EC202": [11, 13], "EC203": [12, 13], "EC204": [12, 13],
    "EC205L": [11, 16], "EC301": [14, 15], "EC302": [14, 16],
    "EC303": [15, 16, 7], "EC304": [15, 16], "EC305L": [14, 15],
    "ME201": [17, 18], "ME202": [17, 19], "ME203": [18, 20], "ME204": [19, 20],
    "ME205L": [17, 20], "ME301": [18, 19], "ME302": [17, 18],
    "ME303": [19, 20], "ME304": [18, 19], "ME305L": [17, 20],
}

# ------------------------------------------------------------------ sections
# (id, name, semester, strength, dept_id). Odd semesters, matching an
# Aug-Dec academic term. strength = declared class size (<= room capacity).
SECTIONS = [
    (1, "A", 3, 60, 1), (2, "B", 3, 60, 1), (3, "C", 3, 55, 1),
    (4, "A", 5, 60, 1), (5, "B", 5, 50, 1),
    (6, "A", 3, 60, 2), (7, "B", 3, 58, 2), (8, "A", 5, 50, 2),
    (9, "A", 3, 60, 3), (10, "A", 5, 45, 3),
]

# ------------------------------------------------------------------ classrooms
ROOMS = []
for room_no, cap in zip(["101", "102", "103", "104", "105", "106",
                         "201", "202", "203", "204", "205", "206"],
                        [60, 60, 60, 70, 70, 70, 60, 60, 65, 65, 70, 70]):
    ROOMS.append((room_no, "Main Block", cap, "LECTURE"))
for room_no, cap in zip(["N101", "N102", "N103", "N104", "N201", "N202"],
                        [40, 40, 50, 50, 100, 100]):
    ROOMS.append((room_no, "Annex", cap, "LECTURE"))
for room_no, cap in zip(["L101", "L102", "L103", "L104", "L201", "L202", "L203"],
                        [60, 60, 64, 64, 70, 70, 60]):
    ROOMS.append((room_no, "Lab Complex", cap, "LAB"))
ROOMS = [(i + 1,) + r for i, r in enumerate(ROOMS)]

# ------------------------------------------------------------------ time slots
# 8 periods with a tea break after P2, lunch after P4, break after P6.
# NOTE for the scheduler: "consecutive" lab periods must not straddle lunch
# (P4 -> P5); P2->P3 and P6->P7 are short breaks and acceptable.
PERIODS = [("08:30", "09:20"), ("09:20", "10:10"), ("10:25", "11:15"),
           ("11:15", "12:05"), ("13:00", "13:50"), ("13:50", "14:40"),
           ("14:55", "15:45"), ("15:45", "16:35")]
DAYS = ["MON", "TUE", "WED", "THU", "FRI", "SAT"]
SLOTS = []
for d, day in enumerate(DAYS):
    for p, (s, e) in enumerate(PERIODS, start=1):
        SLOTS.append((d * 8 + p, day, p, s + ":00", e + ":00"))

# ------------------------------------------------------------------ students
FIRST = ["Aarav", "Aditi", "Akash", "Ananya", "Arjun", "Bhavana", "Chaitra",
         "Deepak", "Divya", "Eshwar", "Gokul", "Harini", "Ishaan", "Janani",
         "Karan", "Keerthana", "Lavanya", "Madhan", "Nandini", "Naveen",
         "Pooja", "Pranav", "Rahul", "Revathi", "Rohan", "Sanjana", "Shreya",
         "Siddharth", "Sneha", "Tarun", "Uma", "Varun", "Vidya", "Vishal",
         "Yamini", "Yuvan", "Zoya", "Ashwin", "Meera", "Nikhil"]
LAST = ["Agarwal", "Balasubramanian", "Chopra", "Dutta", "Gupta", "Hegde",
        "Iyengar", "Joshi", "Krishnamurthy", "Kulkarni", "Mehta", "Mukherjee",
        "Nambiar", "Patel", "Prasad", "Rao", "Reddy", "Sharma", "Shetty",
        "Singh", "Thomas", "Varma", "Venkatesan", "Yadav"]
combos = [(first, last) for first in FIRST for last in LAST]
rng.shuffle(combos)
STUDENTS_PER_SECTION = 12
STUDENTS = []
k = 0
for sid, sname, sem, _strength, dept in SECTIONS:
    adm_year = 25 if sem == 3 else 24            # admission year of the batch
    for n in range(1, STUDENTS_PER_SECTION + 1):
        first, last = combos[k]
        k += 1
        stu_id = f"UG{adm_year}{DEPT_CODE[dept]}{sem}{sname}{n:02d}"
        STUDENTS.append((stu_id, f"{first} {last}",
                         stu_id.lower() + "@student.university.edu", sid))

# ------------------------------------------------- section plans + enrollment
SECTION_COURSE = []
ENROLLMENT = []
for sid, _sname, sem, _s, dept in SECTIONS:
    for cid in PLAN[(dept, sem)]:
        SECTION_COURSE.append((sid, cid, sem))
for stu_id, _n, _e, sid in STUDENTS:
    for sc_sid, cid, _sem in SECTION_COURSE:
        if sc_sid == sid:                         # student takes the section plan
            ENROLLMENT.append((stu_id, cid))

TEACHES_ROWS = [(f, c) for c, fs in TEACHES.items() for f in fs]

# --------------------------------------------------------------------- output
out = []
out.append("""-- =====================================================================
-- 03_seed_data.sql  --  generated by tools/make_seed.py (deterministic)
-- Run after 02_migration_improvements.sql:
--     mysql -u root -p < sql/03_seed_data.sql
-- Re-runnable: it empties the tables first (including Timetable!) and
-- ids are explicit, so the Neo4j ETL and tests can rely on them.
-- Timetable is NOT seeded here; src/generator.py fills it.
--
-- Sizes: 3 departments | 20 faculty | 30 courses (24 THEORY, 6 LAB)
--        10 sections | 120 students | 25 rooms (18 LECTURE, 7 LAB)
--        48 time slots (MON-SAT x 8 periods) | 62 Teaches rows
--        50 Section_Course rows | 600 Enrollment rows
-- Feasibility: every section needs 15-16 periods/week out of 48 slots,
-- every course has 2 qualified faculty, and 154 section-periods compete
-- for 25 rooms x 48 slots. Section strengths (<= 60) fit the rooms.
-- Students (12 per section) are a sample; Section.strength is the
-- declared class size that the capacity check uses.
-- =====================================================================
USE university_timetable;

SET FOREIGN_KEY_CHECKS = 0;
TRUNCATE TABLE Timetable;
TRUNCATE TABLE Enrollment;
TRUNCATE TABLE Section_Course;
TRUNCATE TABLE Teaches;
TRUNCATE TABLE Student;
TRUNCATE TABLE Section;
TRUNCATE TABLE TimeSlot;
TRUNCATE TABLE Classroom;
TRUNCATE TABLE Course;
TRUNCATE TABLE Faculty;
TRUNCATE TABLE Department;
SET FOREIGN_KEY_CHECKS = 1;
""")
out.append("-- Departments\n" + insert("Department", ["dept_id", "dept_name", "building"], DEPTS))
out.append("-- Faculty\n" + insert("Faculty", ["faculty_id", "name", "email", "dept_id"], FACULTY))
out.append("-- Courses\n" + insert("Course", ["course_id", "course_name", "credits", "weekly_hours", "course_type", "dept_id"], COURSES))
out.append("-- Classrooms\n" + insert("Classroom", ["room_id", "room_no", "building", "capacity", "room_type"], ROOMS))
out.append("-- Sections\n" + insert("Section", ["section_id", "section_name", "semester", "strength", "dept_id"], SECTIONS))
out.append("-- Students\n" + insert("Student", ["student_id", "name", "email", "section_id"], STUDENTS))
out.append("-- Time slots\n" + insert("TimeSlot", ["slot_id", "day_of_week", "period_no", "start_time", "end_time"], SLOTS))
out.append("-- Teaches (faculty qualified for each course)\n" + insert("Teaches", ["faculty_id", "course_id"], TEACHES_ROWS))
out.append("-- Section_Course (what each section must take)\n" + insert("Section_Course", ["section_id", "course_id", "semester"], SECTION_COURSE))
out.append("-- Enrollment (each student takes their section's plan)\n" + insert("Enrollment", ["student_id", "course_id"], ENROLLMENT))

target = Path(sys.argv[1]) if len(sys.argv) > 1 else \
    Path(__file__).resolve().parent.parent / "sql" / "03_seed_data.sql"
target.parent.mkdir(parents=True, exist_ok=True)
target.write_text("\n".join(out), encoding="utf-8")
print(f"wrote {target}: {len(FACULTY)} faculty, {len(COURSES)} courses, "
      f"{len(SECTIONS)} sections, {len(STUDENTS)} students, {len(ROOMS)} rooms, "
      f"{len(SLOTS)} slots, {len(TEACHES_ROWS)} teaches, "
      f"{len(SECTION_COURSE)} section_course, {len(ENROLLMENT)} enrollment")
