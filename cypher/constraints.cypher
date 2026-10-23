// =====================================================================
// constraints.cypher  --  run BEFORE loading data (neo4j_etl.py does this
// automatically). Every statement is idempotent (IF NOT EXISTS).
//
// Manual run with the tarball's shell:
//     bin/cypher-shell -u neo4j -p <password> -f cypher/constraints.cypher
//
// Requires Neo4j 5.7+ (relationship-property uniqueness); 5.26 LTS is the
// recommended 5.x release.
// =====================================================================

// ---- Uniqueness constraints (each also creates a backing index) -----
CREATE CONSTRAINT department_id_unique IF NOT EXISTS FOR (n:Department) REQUIRE n.dept_id IS UNIQUE;
CREATE CONSTRAINT faculty_id_unique    IF NOT EXISTS FOR (n:Faculty)    REQUIRE n.faculty_id IS UNIQUE;
CREATE CONSTRAINT course_id_unique     IF NOT EXISTS FOR (n:Course)     REQUIRE n.course_id IS UNIQUE;
CREATE CONSTRAINT section_id_unique    IF NOT EXISTS FOR (n:Section)    REQUIRE n.section_id IS UNIQUE;
CREATE CONSTRAINT student_id_unique    IF NOT EXISTS FOR (n:Student)    REQUIRE n.student_id IS UNIQUE;
CREATE CONSTRAINT classroom_id_unique  IF NOT EXISTS FOR (n:Classroom)  REQUIRE n.room_id IS UNIQUE;
CREATE CONSTRAINT timeslot_id_unique   IF NOT EXISTS FOR (n:TimeSlot)   REQUIRE n.slot_id IS UNIQUE;

// One SCHEDULED_IN relationship per Timetable row (timetable_id is the MySQL PK)
CREATE CONSTRAINT scheduled_timetable_unique IF NOT EXISTS FOR ()-[r:SCHEDULED_IN]-() REQUIRE r.timetable_id IS UNIQUE;

// ---- Indexes used by the analytic queries -----------------------------
CREATE INDEX faculty_dept_idx    IF NOT EXISTS FOR (n:Faculty)   ON (n.dept_id);
CREATE INDEX course_type_idx     IF NOT EXISTS FOR (n:Course)    ON (n.course_type);
CREATE INDEX classroom_type_cap  IF NOT EXISTS FOR (n:Classroom) ON (n.room_type, n.capacity);
CREATE INDEX timeslot_day_period IF NOT EXISTS FOR (n:TimeSlot)  ON (n.day_of_week, n.period_no);

CREATE INDEX scheduled_slot_idx    IF NOT EXISTS FOR ()-[r:SCHEDULED_IN]-() ON (r.slot_id);
CREATE INDEX scheduled_faculty_idx IF NOT EXISTS FOR ()-[r:SCHEDULED_IN]-() ON (r.faculty_id);
CREATE INDEX scheduled_year_idx    IF NOT EXISTS FOR ()-[r:SCHEDULED_IN]-() ON (r.academic_year);
