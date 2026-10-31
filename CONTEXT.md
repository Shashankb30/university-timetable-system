# CONTEXT.md: University Timetable Generation System (handoff file)

> **How to use:** start a new chat, attach this file (plus the `sql/` files if you
> have them), and say: *"Read CONTEXT.md. The project is complete; ask me what to improve (see 5f 'Possible next work')."*
> You can also copy it to `CLAUDE.md` if you use Claude Code in this folder.
> Last updated after **Step 6 complete** (all six steps done).

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
| 4 | Neo4j layer (ETL, constraints, 12 Cypher queries, Python wrappers) | **Done, verified on the user's live Neo4j 5.26.0** (see 5c) |
| 5 | Streamlit app (`app/streamlit_app.py`) | **Done**, tested headlessly (see 5d); not yet seen in the user's browser |
| 6 | Tooling: docker-compose, requirements, .env.example, tests, README, final checklist | **Done** (see 5e, 5f). Docker compose and the live-Neo4j tests are written but never run |

Files delivered so far: `sql/01_schema.sql`, `sql/02_migration_improvements.sql`,
`sql/03_seed_data.sql`, `sql/04_views_procedures.sql`, `tools/make_seed.py`, `src/db.py`, `src/utils.py`, `src/generator.py`, `cypher/constraints.cypher`, `cypher/queries.cypher`,
`src/neo4j_etl.py`, `src/neo4j_queries.py`, `.gitignore`, `app/streamlit_app.py`, `README.md`, `requirements.txt`, `.env.example`, `docker-compose.yml`, `docker/mysql/00_app_user.sh`, `tools/probe_pandas.sh`.

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
- User switches between **fish** and plain **`sh`** (VS Code terminal), so shell syntax for env vars is unreliable. **Put settings in the project-root `.env`**
  (read by `utils.load_env()` in every shell); avoid `export` / `set -x` in instructions. `uv` is at `~/.local/bin/uv` and may not be on PATH in `sh`.
- Python runs from a **`uv` venv (Python 3.12) at `.venv`**; the system Python is broken, so install packages with `uv pip install`.
- DB user **`timetable`@`localhost`** already exists. Step 6 should provide a `.env` so env vars needn't be set by hand.
- Local DB server is **MariaDB** (load SQL with `sudo mariadb`), likely on port 3306. Step 6's Docker MySQL must use a different host port
  (e.g. 3307) to avoid a clash; keep SQL/Python compatible with both MariaDB and MySQL 8.
- Docker/Neo4j availability is **unconfirmed**; if no Docker, give a non-Docker Neo4j install route.
- The user's parent folder path contains a space (`Uni timetable`), so quote paths in commands.
- **VS Code is the Flatpak build (`com.visualstudio.code`); its integrated terminal runs inside a Flatpak sandbox** (Freedesktop runtime, no `pacman`, glibc 2.42).
  In that sandbox `import pandas` with pyarrow installed **segfaults** (pandas alone is fine; verified with pandas 3.1.0 / 3.0.2 / 2.3.3 and pyarrow 26 / 21),
  so **Streamlit and anything importing pandas must be run from a normal host terminal** (outside VS Code). The generator, ETL and Neo4j queries do not
  use pandas and work in either terminal. Diagnostic script: `bash tools/probe_pandas.sh` (`--env-only` for a quick check).
  Step 6 consequence: keep the core tests pandas-free; gate UI/AppTest tests behind an opt-in (`RUN_UI_TESTS=1`) because a segfault cannot be caught by pytest.

## 5c. Step 4 deliverables: Neo4j layer
**Status: VERIFIED on the user's live Neo4j 5.26.0 (Java 21, tarball install, no Docker).** The ETL ran clean (15 constraint/index statements,
all nodes and relationships, `Verification: OK, graph matches MySQL`), a second run gave identical counts (idempotent), and
`python src/neo4j_queries.py demo` executed all queries; results were spot-checked against the data (e.g. CS301 vs CS302 shared_count 25 =
24 students + 1 faculty; Q6 path faculty 1 -> 11 is 6 hops via TEACHES_SECTION and TEACHES). Offline checks before that: `cypher-lint` on
all queries/ETL statements, fake-session ETL test, wrapper-parameter check. Needs Neo4j >= 5.7 (5.26 LTS recommended).
Diagnostics: `python src/neo4j_queries.py check` prints which NEO4J_* settings are used (password masked; says if a real env var overrides `.env`) and tests login.
A harmless server notice 01G11 ("null value eliminated in set function", from count()/collect() over OPTIONAL MATCH) is filtered in `utils.get_neo4j_driver()`.
Lessons from setup: a fresh Neo4j has password `neo4j` (browser forces a change); `set-initial-password` only works while Neo4j is STOPPED and before
first start (reset: `bin/neo4j stop`, `rm -rf data/databases data/transactions`, set-initial-password, start). `.env` must match.

- **Graph model.** Nodes: Department (+`dept_code` e.g. CSE), Faculty, Course, Section (+`label` e.g. "CSE Sem 3 A"), Student, Classroom,
  TimeSlot (+`day_no` 1-6). Relationships: WORKS_IN, TEACHES (qualified), STUDIES_IN, ENROLLED_IN, OFFERED_BY,
  **SCHEDULED_IN `(Course)->(Section)`** (one per Timetable row; props timetable_id, academic_year, course_id, section_id, faculty_id,
  faculty_name, room_id, room_label, slot_id, day_of_week, day_no, period_no, start_time, end_time), **CONFLICTS_WITH** (Course-Course, stored once
  per pair a<b; props shared_faculty, shared_students, shared_count). **Flagged extras:** BELONGS_TO (Section->Department),
  TAKES (Section->Course, from Section_Course), TEACHES_SECTION (Faculty->Section, derived from the timetable; used so Q6 can path "through shared sections").
- **Design choices.** Room/slot/faculty are PROPERTIES of SCHEDULED_IN (brief: "carrying room, slot, academic_year"), so free-room /
  busy-faculty checks are anti-joins on relationship properties. CONFLICTS_WITH is derived inside Neo4j from TEACHES/ENROLLED_IN with the same
  definition as `utils.build_conflict_graph` (so a test can compare them). Classic Cypher (OPTIONAL MATCH ... WHERE x IS NULL) is used instead of newer EXISTS{}.
- **`cypher/queries.cypher`** is the single source of truth: `// @query <name>` markers are parsed by `neo4j_queries.load_queries()`. Queries:
  impact_analysis, substitute_finder, most_conflicted_courses, free_rooms, heaviest_day_students, faculty_shortest_path, faculty_workload,
  move_class_ripple, cross_department_teaching, course_conflict_neighbours, node_counts, relationship_counts.
- **`src/neo4j_queries.py`** wrappers: `impact_analysis`, `substitute_finder`, `faculty_absence_plan` (Q1+Q2; per-class status COVERABLE /
  NEEDS_TEACHES / UNCOVERED + summary; for the UI), `most_conflicted_courses`, `free_rooms(day, period, min_capacity, room_type, year)`,
  `heaviest_day_students`, `faculty_shortest_path` (-> {connected, hops, path}), `faculty_workload_ranking` (adds dense `rank`),
  `move_class_ripple` (-> {moved, clashes, free_move, students_displaced}), `cross_department_teaching`, `course_conflict_neighbours`,
  `node_counts`, `relationship_counts`, plus `get_driver()/close_driver()`. CLI: `python src/neo4j_queries.py list|demo`.
  Substitutes: QUALIFIED = in Teaches for that course (assignable; FK allows); RELATED = teaches a CONFLICTS_WITH course (needs a Teaches row first).
- **`src/neo4j_etl.py`**: `run_etl(year=None, reset=False)` (None = all years in Timetable), `expected_counts(conn, years)`,
  `actual_counts(session)`, `compare_counts(expected, actual)` -> list of mismatches (for Step 6 tests). CLI: `python src/neo4j_etl.py [--year Y] [--reset]`.
  Rebuilds SCHEDULED_IN / TEACHES_SECTION for the affected year(s) so regenerated timetables leave no ghosts; everything else is MERGE.
- **Expected counts on the seed + generated timetable:** nodes 3/20/30/10/120/25/48 (Dept/Faculty/Course/Section/Student/Classroom/TimeSlot);
  WORKS_IN 20, OFFERED_BY 30, BELONGS_TO 10, STUDIES_IN 120, TEACHES 62, ENROLLED_IN 600, TAKES 50, SCHEDULED_IN 154,
  TEACHES_SECTION 80, CONFLICTS_WITH 91.
- **Neo4j env vars:** `NEO4J_URI` (default bolt://localhost:7687), `NEO4J_USER` (neo4j), `NEO4J_PASSWORD` (required), `NEO4J_DATABASE` (neo4j).
  `utils.get_neo4j_driver()`, `utils.neo4j_settings()`, `utils.format_table()`, `utils.DAY_NO` were added.
- **No Docker on the user's machine.** Neo4j runs from the official tarball (`neo4j-community-5.26.0-unix.tar.gz` from dist.neo4j.org) with
  **Java 21** (`jre21-openjdk-headless`; Neo4j 5 accepts only Java 17 or 21): `bin/neo4j-admin dbms set-initial-password <pw>`, then `bin/neo4j console`
  (Browser http://localhost:7474, Bolt 7687). The partial-upgrade glibc problem must be fixed (`pacman -Syu`) before Java will run.
  Step 6 must therefore make Docker OPTIONAL (README: Docker path + tarball path).

## 5d. Step 5 deliverables: Streamlit app
Run from the project root: `streamlit run app/streamlit_app.py` (needs `streamlit` + `pandas` in the venv: `~/.local/bin/uv pip install streamlit pandas`;
tested with Streamlit 1.66.1 / pandas 3.0.2). Reads MySQL via `db.py`; Neo4j is optional per page. Sidebar: academic year, page, MySQL/Neo4j status.
- **Timetable**: weekly grid per Section / Faculty / Room (HTML table from `utils.build_week_grid` + `utils.grid_to_html`; theory blue, lab orange,
  lunch row, all text HTML-escaped), metrics, CSV download. Uses view `v_section_timetable` (has faculty_id, room_id, section_id) for all three views.
- **Run generator**: calls `generator.generate_timetable` with replace / allow-partial / dry-run / time limit / seed; shows verdict, soft-constraint
  metrics, unplaced classes, pre-flight problems, full report; after a saved run offers "Update Neo4j graph" (`neo4j_etl.run_etl(year)`).
- **Faculty absent** (Neo4j): `neo4j_queries.faculty_absence_plan`; metrics, per-class status (Coverable / Needs Teaches row / Uncovered),
  substitutes per class, CSV; warns when the graph is stale vs MySQL (compares sum of Q7 periods with Timetable rows) with an update button.
- **Clash checker**, 3 tabs: saved timetable (`sp_list_clashes`, `sp_hours_coverage`); "Check a proposed class" via new
  `generator.check_proposed_class(conn, year, section, course, faculty, room, slot)` (8 rules, hard vs soft, writes nothing); "Move a class (ripple)" via Neo4j Q8.
- **Statistics**: metrics + charts (faculty workload, room utilisation, classes per day/period, theory vs lab, section-load table) from
  `sp_faculty_workload` / `sp_room_utilization`; Neo4j "graph insights" expander (most conflicted courses, cross-department links).
- **Neo4j down** => friendly message on the Neo4j parts; the rest keeps working. MySQL down => error + `st.stop()`.
- Added helpers: `utils.dept_code`, `utils.build_week_grid`, `utils.grid_to_html`, `utils.to_minutes` now accepts 'HH:MM' strings;
  `neo4j_etl.dept_code` delegates to utils. Layout args that changed across Streamlit versions (`use_container_width`/`width`) are deliberately avoided.
- **Tested** with Streamlit's `AppTest` against real MySQL: every page loads without exceptions, numbers match (section 1 = 15 periods, 154 classes, 12.8% avg
  room use), generator page (dry run / refuses without Replace / saves with Replace / pre-flight error), proposal check (accept + 3-rule reject),
  Neo4j pages with a stand-in server (absent plan, stale warning, ripple, insights) and with Neo4j down. **Not verified:** visual appearance in a real browser.
- Step 6 should ship `requirements.txt` with streamlit + pandas, and tests can reuse `AppTest`, `build_week_grid`, `check_proposed_class`.

## 5e. Step 6 progress: documentation and configuration files
- **`README.md`** (complete project guide): features, architecture, structure, quick start, per-OS setup (Ubuntu/Debian, Fedora, Arch, macOS, Windows/WSL; Docker and native paths),
  Neo4j install (tarball/zip + Java 17/21), usage of every CLI, web-app tour, ER-to-schema explanation, migration table M1-M6, scheduler algorithm, graph model + the 12 queries,
  why MySQL + Neo4j, seed data facts, configuration table, 12-point verification checklist, troubleshooting (MySQL, Neo4j, Python/Streamlit incl. Flatpak segfault), limitations, team workflow.
  Mechanically checked: every named file exists, every CLI flag exists, internal links resolve, all `.env.example` variables documented, quoted numbers match the live database.
  Platform honesty in README: verified on Linux (Ubuntu/MySQL 8.0 sandbox; user's Arch + MariaDB + Neo4j 5.26.0); macOS / Windows / Docker instructions follow official docs but are UNTESTED.
- **`requirements.txt`**: `mysql-connector-python>=8.4`, `neo4j>=5.20,<7`, `pandas>=2.2`, `streamlit>=1.40` (tested: 26.7.0 / 6.4.1 / 3.0.2 and 3.1.0 / 1.66.1; Python 3.12).
- **`.env.example`**: all MYSQL_*, NEO4J_*, ACADEMIC_YEAR (+ commented Docker vars). The loader is `utils.load_env` (real env vars win over the file).
- **`docker-compose.yml`** (MySQL 8.0 + Neo4j 5.26-community): MySQL published on host port **3307**; init scripts mounted in `/docker-entrypoint-initdb.d` run once in order
  (`docker/mysql/00_app_user.sh` creates the app user from `MYSQL_USER`/`MYSQL_PASSWORD`, then `sql/01`..`04`). `MYSQL_DATABASE` is deliberately NOT set because `01_schema.sql` runs `CREATE DATABASE`.
  Volumes `mysql_data`, `neo4j_data`; `docker compose down -v` re-runs the init. **YAML/shell syntax validated only; never run (no Docker in sandbox or on the user's machine).**
- **Portable SQL loading** verified: `mysql -e "source sql/0X_....sql"` works on every OS/shell (PowerShell has no `<`) and handles `DELIMITER` files; result 11 tables, 4 views, 4 triggers, 5 routines.
- **Friendly errors:** `generator.py` and `neo4j_etl.py` now print "Could not use MySQL: ..." with a hint (exit code 2) instead of a traceback when MySQL is down / wrong password / `MYSQL_USER` missing.
- User started Neo4j and reported done after the "how to connect Neo4j" instructions; the Streamlit app was seen working in their browser (Timetable page; sidebar then showed Neo4j unavailable until it was started).
- **Still to do for Step 6:** `tests/` (pytest; core tests must stay pandas-free; UI tests opt-in via `RUN_UI_TESTS=1`; tests = no clashes, constraints enforced incl. error codes 5001/5002/5003/1062/1452,
  ETL counts match MySQL, `expected_counts`/`compare_counts` and `utils.build_conflict_graph` vs Neo4j CONFLICTS_WITH, grid builder, `check_proposed_class`), add pytest to a dev requirements section,
  update the README "Tests" section and status table, then a final end-to-end verification checklist message.

## 5f. Step 6 completion: tests
- `pytest.ini`, `requirements-dev.txt` (adds `pytest>=8.0`), `tests/` with `conftest.py` + 7 test files (see README "Tests" for the table).
- **Safety rules:** tests never touch real data: writes use `TEST_YEAR = "2099-2100"` (wiped before/after by a fixture) or run in a rolled-back transaction (`conn` fixture); Neo4j tests only read.
  `db.connect()` failure => tests FAIL with a clear message (not skip). Neo4j tests skip when Neo4j is down or the graph is empty. UI tests (`-m ui`) are skipped unless `RUN_UI_TESTS=1`.
- **Results in the sandbox (MySQL 8.0):** `71 passed, 15 skipped` (7 live-Neo4j + 8 UI); with `RUN_UI_TESTS=1 pytest -m ui`: 8 passed. Expected with Neo4j loaded: 78 passed / 8 skipped; everything: 86.
  **User's first real run (Arch + MariaDB, VS Code Flatpak terminal):** test_etl_offline (7) and test_generator (18) PASSED on MariaDB; then `pytest` segfaulted in `test_neo4j_live` because `import neo4j` imports pandas when installed
  (pandas was installed with Streamlit) and pandas+pyarrow crashes in that terminal. **First fix** blocked only pandas: the crash moved to the driver's next optional import (pyarrow, `neo4j/_optional_deps.py` line 45).
  **Fix:** `utils._import_neo4j()` blocks the driver's optional pandas AND pyarrow imports (temporary `sys.modules[...]=None`, removed afterwards);
  regression test `test_neo4j_driver_import_does_not_load_pandas_or_pyarrow`.
  **VERIFIED ON THE USER'S MACHINE (Arch + MariaDB + Neo4j 5.26.0, Flatpak VS Code terminal):** `python src/neo4j_etl.py` -> `Verification: OK`; `pytest` -> **77 passed, 9 skipped**
  (the 7 live-Neo4j tests PASS; skips = 8 opt-in UI tests + 1 optional `cypher-lint` test). `.env.example` now exists (user pasted it). Counts without cypher-lint: MySQL only 70 passed/16 skipped; with Neo4j 77/9; with RUN_UI_TESTS=1 85 passed/1 skipped.
- **Mutation-checked:** dropping `fk_tt_teaches`, the trigger `trg_tt_validate_bi`, the unique key `uq_faculty_slot`, or altering seed data made the matching tests fail; the pristine DB passes.
- **Never run by anyone:** Docker compose; the macOS/Windows instructions; `RUN_UI_TESTS=1 pytest -m ui` on the user's machine (needs a normal, non-Flatpak terminal). Live-Neo4j tests: PASSED on the user's machine.
- `db.call_procedure` now works with mysql-connector's deprecated `stored_results()` method AND the announced property form (26.7.0 warns).
- **User's folder as of the last screenshot:** present = app, cypher, docker/mysql/00_app_user.sh, sql (4), src (5), tests (empty), tools/probe_pandas.sh, .env, .gitignore, CONTEXT.md, docker-compose.yml, README.md, requirements.txt, probe_output.txt (can delete).
  **Missing:** `.env.example`, `tools/make_seed.py` (and now `pytest.ini`, `requirements-dev.txt`, `tests/*`). `tests/test_project_files.py` fails until these exist.
- **Project state: COMPLETE and verified end to end on the user's machine, apart from the items above.**
- **Possible next work:** run the live Neo4j tests and Docker compose once; apply a chosen substitute from the UI (UPDATE Timetable.faculty_id + ETL); student electives/backlogs; split lab batches; LICENSE; CI (GitHub Actions running `pytest` against a MySQL service container).

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

### Step 4: Neo4j layer: DONE (requirements kept for reference)
- ETL MySQL -> Neo4j: **idempotent (MERGE)**; create uniqueness constraints and indexes **first**.
- Nodes: Department, Faculty, Course, Section, Student, Classroom, TimeSlot.
- Relationships: `WORKS_IN`, `TEACHES`, `STUDIES_IN`, `ENROLLED_IN`, `OFFERED_BY`,
  `SCHEDULED_IN` (carries room, slot, academic_year), and derived `CONFLICTS_WITH` between courses that share faculty or
  students (property `shared_count`).
- **At least 8 documented Cypher queries** (12 delivered), including: impact analysis if a faculty member is absent; substitute faculty
  finder (same department, related course, free in the slot); courses with the most conflicts; free rooms for a slot by
  capacity and type; students with the heaviest day; shortest path between two faculty through shared courses or sections;
  faculty workload ranking; ripple-effect query for moving one class.
- Expose all of them as Python functions.

### Step 5: Streamlit UI: DONE (requirements kept for reference)
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