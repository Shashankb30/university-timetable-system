# CONTEXT.md: University Timetable Generation System (handoff file)

> **How to use:** start a new chat, attach this file (plus the `sql/` files if you
> have them), and say: *"Read CONTEXT.md. Continue from Step 4."*
> You can also copy it to `CLAUDE.md` if you use Claude Code in this folder.
> Last updated after **Step 3 complete**.

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
| 3 | Timetable generator (`src/generator.py`, `db.py`, `utils.py`) | **Done, tested on MySQL 8.0** |
| 4 | Neo4j layer (ETL, constraints, 8+ Cypher queries, Python wrappers) | **Next** |
| 5 | Streamlit app | Todo |
| 6 | Tooling: docker-compose, requirements, .env.example, tests, README, final checklist | Todo |

Files delivered so far: `sql/01_schema.sql`, `sql/02_migration_improvements.sql`,
`sql/03_seed_data.sql`, `sql/04_views_procedures.sql`, `tools/make_seed.py`, `src/db.py`, `src/utils.py`, `src/generator.py`.

## 5. User environment (important)
- Linux; the local `mysql` command is actually **MariaDB's** client (prints "Deprecated program name").
- Root login uses socket auth: run `sudo mariadb < file.sql` (**no password** works for root).
- SQL files for Steps 1 and 2 are **already loaded** in the user's local MariaDB (database `university_timetable`).
  Other unrelated databases exist there; never touch them.
- Steps 1-3 were developed/tested on MySQL 8.0 AND **confirmed working on the user's MariaDB**: all SQL files load, the seed loads
  (20/120/600 counts), and the generator placed 154/154 periods and saved 154 rows; `sp_list_clashes` and `sp_hours_coverage` return nothing.
  Stay compatible with both where cheap.
- Load files with the **CLI client**, not the Python connector: `02` and `04` use `DELIMITER`.
- Python uses the non-root DB user **`timetable`@`localhost`** (ALL on `university_timetable.*`), **already created** on the user's MariaDB.
  Credentials go in `.env` (variables like `MYSQL_HOST`, `MYSQL_PORT`, `MYSQL_USER`, `MYSQL_PASSWORD`, `MYSQL_DATABASE`,
  `NEO4J_URI`, `NEO4J_USER`, `NEO4J_PASSWORD`, `ACADEMIC_YEAR`). **Never store the password in this file.**
- Step 6 will give Docker (real MySQL 8 + Neo4j 5). Docker availability on the user's machine is unconfirmed.

## 5b. Step 3 deliverables: what exists and how it behaves
- `src/db.py`: `connect()`, `connection()` (context manager), `query(conn, sql, params)` -> list of dicts,
  `call_procedure(conn, name, args)`, `transaction(conn)`. Env: `MYSQL_HOST` (127.0.0.1), `MYSQL_PORT` (3306), `MYSQL_USER`,
  `MYSQL_PASSWORD`, `MYSQL_DATABASE` (university_timetable), optional `MYSQL_UNIX_SOCKET`. Loads `.env` via `utils.load_env()`
  (own tiny loader; no python-dotenv).
- `src/utils.py`: `load_env`, `env`, `academic_year()` (env `ACADEMIC_YEAR`, default 2026-2027), `describe_mysql_error`,
  `build_slot_links` (next/prev period; a gap >= 30 min such as lunch breaks "consecutive"),
  `build_conflict_graph(conn)` -> `{course: {other: shared_count}}` where
  **shared_count = #shared qualified faculty (Teaches) + #shared enrolled students (Enrollment)**.
  **Step 4's Neo4j `CONFLICTS_WITH {shared_count}` must use this same definition.**
- `src/generator.py`: `generate_timetable(year, replace, allow_partial, dry_run, time_limit, max_nodes, restarts, seed, conn)`
  -> `GenerationResult` (`.report()`, `.success`, `.rows`, `.unplaced`, `.preflight`, `.soft`). Also reusable: `Problem`,
  `make_tasks`, `Scheduler`, `evaluate_soft`, `save_timetable`.
  CLI: `python src/generator.py [--year Y] [--replace] [--allow-partial] [--dry-run] [--time-limit S] [--max-nodes N] [--restarts N] [--seed N]`
  (exit code 0 = complete and saved/dry-run OK, 1 otherwise).
- Behaviour: refuses to overwrite an existing year unless `--replace`; saves in one transaction (rollback on any MySQL error,
  with plain-English reason); incomplete result is NOT saved unless `--allow-partial`; pre-flight explains impossible input;
  search = greedy most-constrained-first + backtracking + randomised restarts; lab blocks (<=3 periods) never straddle lunch,
  with a flagged non-consecutive fallback.
- Verified on seed: 154/154 periods placed, 144 search nodes, ~0.2 s, 0 same-day repeats, 0 faculty runs > 3, 0 split labs;
  `sp_list_clashes` and `sp_hours_coverage` both return zero rows; average room utilisation 12.8%.
  Failure paths verified: no teacher (pre-flight), faculty pool too small (pre-flight), rooms too small (search fails ->
  partial 116/154 with per-course diagnosis), save errors (1062 / trigger 5002 / FK fk_tt_teaches) roll back cleanly.
- Generator also confirmed on the user's MariaDB (see Confirmed local setup).
- DB constraint reminder: the composite FK `fk_tt_teaches` blocks deleting a `Teaches` row that a saved timetable uses; clear
  `Timetable` first (or re-run `03_seed_data.sql`, which truncates everything).

### Local setup: shawshank's machine only (teammate uses plain MySQL 8.0.16+, so `mysql -u root -p < file.sql` and none of this applies to him)
- User's shell is **fish**: give fish syntax for shell commands (`set -x VAR value`, not `export`).
- Python runs from a **`uv` venv (Python 3.12) at `.venv`**; the system Python is broken, so install packages with `uv pip install`.
- DB user **`timetable`@`localhost`** already exists. Step 6 should provide a `.env` so env vars needn't be set by hand.
- Local DB server is **MariaDB** (load SQL with `sudo mariadb`), likely on port 3306. Step 6's Docker MySQL must use a different host port
  (e.g. 3307) to avoid a clash; keep SQL/Python compatible with both MariaDB and MySQL 8.
- Docker/Neo4j availability is **unconfirmed**; if no Docker, give a non-Docker Neo4j install route.
- The user's parent folder path contains a space (`Uni timetable`), so quote paths in commands.

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

### Step 3: Timetable generator: DONE (requirements kept for reference)
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

## 12. Notes for the next session
- The user is on Linux with MariaDB locally. Step 6's README should show both the Docker path (preferred) and a
  local-MariaDB path (`sudo mariadb < file.sql`), and mention the `DELIMITER` caveat.
- When loading SQL from Python (tests/setup), shell out to the `mysql`/`mariadb` client or split on `DELIMITER` yourself.