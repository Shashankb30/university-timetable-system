# CONTEXT.md: University Timetable Generation System (handoff file)

> **How to use:** start a new chat, attach this file (plus the `sql/` files if you
> have them), and say: *"Read CONTEXT.md. Continue from Step 3."*
> You can also copy it to `CLAUDE.md` if you use Claude Code in this folder.
> Last updated after **Step 2 complete**.

---

## 1. Role and working protocol
- Act as a **senior full-stack database engineer**.
- Deliver **one step at a time**. After each step, stop and say:
  `Step N complete, say 'continue' for the next step.`
- Output **complete, working code**: no placeholders, no `...`, no "implement this yourself".
- Give **every file with full contents**; keep concise comments on non-obvious logic.
- **Do not rename tables or columns** from the original PDF schema except in the migration file, and flag every change.
- All credentials come from **environment variables** (never hard-coded).
- Finish the whole project with a **verification checklist** (how to confirm it works end to end).
- Test code against a real database before delivering it where possible.

## 2. Goal
A runnable DBMS mini-project around a "University Timetable Generation System":
**MySQL = system of record**, **Neo4j = graph analytics**. It must auto-generate a
clash-free timetable and let the user query and visualise it.

**Tech stack (do not substitute):** MySQL 8.x, Neo4j 5.x (Cypher), Python 3.11+ with
`mysql-connector-python`, `neo4j` (official driver), `pandas`, `streamlit`; `docker-compose`
for MySQL and Neo4j. No other databases.

## 3. Target project layout
```
project/
  docker-compose.yml
  requirements.txt
  .env.example
  CONTEXT.md
  sql/    01_schema.sql  02_migration_improvements.sql  03_seed_data.sql  04_views_procedures.sql
  src/    db.py  generator.py  neo4j_etl.py  neo4j_queries.py  utils.py
  cypher/ constraints.cypher  queries.cypher
  app/    streamlit_app.py
  tests/  (no clashes, constraints enforced, ETL counts match MySQL)
  tools/  make_seed.py            <- extra helper, optional (generates 03_seed_data.sql)
  README.md
```

## 4. Progress

| Step | Content | Status |
|---|---|---|
| 0 | Understanding summary | Done |
| 1 | MySQL schema + migration + views + procedures | **Done, tested on MySQL 8.0** |
| 2 | Seed data (`03_seed_data.sql`) | **Done, tested** |
| 3 | Timetable generator (`src/generator.py`, plus `db.py`, `utils.py`) | **Next** |
| 4 | Neo4j layer (ETL, constraints, 8+ Cypher queries, Python wrappers) | Todo |
| 5 | Streamlit app | Todo |
| 6 | Tooling: docker-compose, requirements, .env.example, tests, README, final checklist | Todo |

Files delivered so far: `sql/01_schema.sql`, `sql/02_migration_improvements.sql`,
`sql/03_seed_data.sql`, `sql/04_views_procedures.sql`, `tools/make_seed.py`.

## 5. User environment (important)
- Load files with the **CLI client**, not the Python connector: `02` and `04` use `DELIMITER`.
- Python needs a non-root DB user. The user was told to create one (`timetable`@`localhost`, ALL on `university_timetable.*`).
  Credentials go in `.env` (variables like `MYSQL_HOST`, `MYSQL_PORT`, `MYSQL_USER`, `MYSQL_PASSWORD`, `MYSQL_DATABASE`,
  `NEO4J_URI`, `NEO4J_USER`, `NEO4J_PASSWORD`, `ACADEMIC_YEAR`). **Never store the password in this file.**
- Step 6 will give Docker (real MySQL 8 + Neo4j 5). Docker availability on the user's machine is unconfirmed.

## 6. Schema (from the PDF, source of truth)
Tables and columns (names must not change):
- `Department(dept_id PK, dept_name UNIQUE, building)`
- `Faculty(faculty_id PK, name, email UNIQUE, dept_id FK)`
- `Course(course_id VARCHAR(10) PK, course_name, credits>0, weekly_hours DEFAULT 3, course_type ENUM('THEORY','LAB'), dept_id FK)`
- `Classroom(room_id PK, room_no, building, capacity>0, room_type ENUM('LECTURE','LAB'), UNIQUE(building,room_no))`
- `Section(section_id PK, section_name, semester 1-8, strength DEFAULT 60, dept_id FK, UNIQUE(dept_id,semester,section_name))`
- `Student(student_id VARCHAR(15) PK, name, email UNIQUE, section_id FK)`
- `TimeSlot(slot_id PK, day_of_week ENUM('MON'..'SAT'), period_no 1-8, start_time, end_time, UNIQUE(day_of_week,period_no))`
- `Teaches(faculty_id, course_id)` PK both; M:N Faculty-Course
- `Enrollment(student_id, course_id)` PK both; M:N Student-Course
- `Timetable(timetable_id PK, course_id, faculty_id, room_id, slot_id, section_id, academic_year VARCHAR(9))`
  with **hard constraints** `UNIQUE(room_id,slot_id,academic_year)`, `UNIQUE(faculty_id,slot_id,academic_year)`,
  `UNIQUE(section_id,slot_id,academic_year)`.

Relationships: Department 1:N Faculty / Course / Section; Section 1:N Student; Faculty M:N Course;
Student M:N Course; Timetable is the n-ary "Scheduled_In" relationship.
(The ER drawing seems to attach Offered_By and Belongs_To to Faculty; the relational schema uses `dept_id`, which is treated as correct.)

## 7. Migration changes already made (`02_migration_improvements.sql`)
- **M1** new table `Section_Course(section_id, course_id, semester)`, PK (section_id, course_id).
- **M2** FK `fk_tt_teaches`: Timetable(faculty_id, course_id) -> Teaches. *(requested)*
- **M3** FK `fk_tt_section_course`: Timetable(section_id, course_id) -> Section_Course. *(extra, flagged; easy to remove)*
- **M4** procedure `sp_validate_placement(room_id, section_id, course_id)`.
- **M5** triggers `trg_tt_validate_bi/_bu` (Timetable) and `trg_sc_semester_bi/_bu` (Section_Course).
  Custom error numbers (SQLSTATE 45000): **5001** room capacity < section strength;
  **5002** LAB needs LAB room / THEORY needs LECTURE room; **5003** Section_Course.semester <> Section.semester.
  Other violations come as MySQL **1062** (duplicate/unique) and **1452** (FK fail, e.g. faculty doesn't teach the course).
- **M6** indexes: `idx_tt_year_slot`, `idx_tt_faculty_course`, `idx_tt_section_course`, `idx_room_type_capacity`, `idx_course_type`.
- Known limit: triggers fire on Timetable writes only; lowering a room's capacity later is caught by `sp_list_clashes`.

## 8. Views and procedures (`04_views_procedures.sql`)
- Views (filter by `academic_year` yourself): `v_section_timetable`, `v_faculty_timetable`, `v_room_utilization`,
  `v_free_slots` (rooms and faculty via `resource_type`). Year-dependent views are empty until Timetable has rows.
- Procedures (take `p_year VARCHAR(9)`): `sp_faculty_workload`, `sp_room_utilization`,
  `sp_list_clashes` (**must return zero rows** on a valid timetable), `sp_hours_coverage` (empty = every section got
  exactly `weekly_hours` per course).

## 9. Seed data facts (`03_seed_data.sql`, deterministic, re-runnable; it TRUNCATEs everything incl. Timetable)
- 3 departments: 1 CSE, 2 ECE, 3 MECH. 20 faculty: ids 1-10 CSE, 11-16 ECE, 17-20 MECH.
- 30 courses = 24 THEORY + 6 LAB. Theory 3 or 4 weekly hours; **labs = 2 weekly hours** (one 2-period block).
  CSE `CS201-205L` (sem 3), `CS301-305L` (sem 5); same pattern `EC...`, `ME...`. Lab ids end in `L`.
- 10 sections, odd semesters (3 and 5): ids 1-5 CSE (3A,3B,3C,5A,5B), 6-8 ECE (3A,3B,5A), 9-10 MECH (3A,5A).
  Strengths 45-60. Each section needs 15-16 periods/week. **154 section-periods in total.**
- 120 students (12 per section), ids like `UG25CS3A01`; each takes their section's 5 courses -> 600 Enrollment rows.
  `Section.strength` (declared class size) is what the capacity check uses, not the student count.
- 25 rooms: 18 LECTURE (Main Block 12 with capacity 60-70; Annex 6 with 40/40/50/50/100/100), 7 LAB (Lab Complex, 60-70).
- 48 slots, ids 1-48 = day*8 + period (MON=0 ... SAT=5). Times: P1 08:30-09:20, P2 09:20-10:10, P3 10:25-11:15,
  P4 11:15-12:05, **lunch**, P5 13:00-13:50, P6 13:50-14:40, P7 14:55-15:45, P8 15:45-16:35.
- 62 Teaches rows: most courses have 2 qualified faculty; `CS202` has 3 and `EC303` has 3, incl. 2 cross-department
  rows (faculty 13 teaches `CS202`; faculty 7 teaches `EC303`).
- **Timetable is NOT seeded**; `src/generator.py` fills it.
- Feasibility was verified: a throwaway greedy placed all 154 sessions through every trigger/FK/UNIQUE with 0 clashes.
  (That solver is not part of the project.)

## 10. Decisions made so far
- Academic year default: **`2026-2027`** (odd-semester term, current date Oct 2026); make it configurable (`ACADEMIC_YEAR`).
- Lab "consecutive periods" must **not straddle lunch** (P4 -> P5). P2->P3 and P6->P7 are acceptable.
- Triggers/validation live in the DB; the generator must still pre-check constraints in Python and catch errors 1062, 1452, 5001-5003.

## 11. Remaining steps: full requirements

### Step 3: Timetable generator (`src/generator.py`; also `src/db.py`, `src/utils.py`)
Reads from MySQL, inserts into `Timetable`.
- **Hard:** no room/faculty/section double-booked in a slot; room type and capacity match; faculty only teaches courses in
  `Teaches`; each course gets exactly `weekly_hours` slots per week **per section** (use `Section_Course`).
- **Soft (best effort):** spread a course's sessions across different days; avoid more than 3 consecutive periods for a
  faculty member; keep lab sessions in consecutive periods; avoid the same course twice on one day.
- **Algorithm:** greedy ordering by difficulty (most constrained first) **with backtracking**, and use the **conflict graph**
  from Step 4 (courses sharing faculty or students). Explain the algorithm in comments.
- **Failure handling:** report which course/section could not be placed and why.
- Wrap inserts in **transactions**; catch MySQL unique-constraint violations.

### Step 4: Neo4j layer (`src/neo4j_etl.py`, `src/neo4j_queries.py`, `cypher/constraints.cypher`, `cypher/queries.cypher`)
- ETL MySQL -> Neo4j: **idempotent (MERGE)**; create uniqueness constraints and indexes **first**.
- Nodes: Department, Faculty, Course, Section, Student, Classroom, TimeSlot.
- Relationships: `WORKS_IN`, `TEACHES`, `STUDIES_IN`, `ENROLLED_IN`, `OFFERED_BY`,
  `SCHEDULED_IN` (carries room, slot, academic_year), and derived `CONFLICTS_WITH` between courses that share faculty or
  students (property `shared_count`).
- **At least 8 documented Cypher queries**, including: impact analysis if a faculty member is absent; substitute faculty
  finder (same department, related course, free in the slot); courses with the most conflicts; free rooms for a slot by
  capacity and type; students with the heaviest day; shortest path between two faculty through shared courses or sections;
  faculty workload ranking; ripple-effect query for moving one class.
- Expose all of them as Python functions.

### Step 5: Streamlit UI (`app/streamlit_app.py`)
Pages: weekly-grid timetable per section / faculty / room; run the generator; **"faculty absent" tool** (Neo4j: affected
classes + suggested substitutes); clash checker; basic statistics charts.

### Step 6: Tooling and docs
`docker-compose.yml` (MySQL 8 + Neo4j 5), `requirements.txt`, `.env.example`, `tests/` (no clashes; constraints enforced;
ETL counts match MySQL), and `README.md` containing: exact setup/run commands (start containers, load SQL, run ETL, run
generator, launch UI), the ER-to-schema explanation, and a short "why MySQL + Neo4j" section. End with the **verification checklist**.

