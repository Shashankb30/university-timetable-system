-- =====================================================================
-- 02_migration_improvements.sql
-- Run ONCE, after 01_schema.sql and BEFORE the seed data.
-- Load with the mysql CLI (it uses DELIMITER, which the Python
-- connector does not understand):
--     mysql -u root -p < sql/02_migration_improvements.sql
--
-- CHANGE LOG (every change to the PDF schema is flagged here)
--   [M1] NEW TABLE  Section_Course (section_id, course_id, semester)
--   [M2] NEW FK     Timetable(faculty_id, course_id) -> Teaches
--   [M3] NEW FK     Timetable(section_id, course_id) -> Section_Course
--                   (extra: stops a section being given a course outside
--                    its plan; not requested, but follows from M1)
--   [M4] NEW PROC   sp_validate_placement  (capacity + room-type rules)
--   [M5] NEW TRIGGERS  trg_tt_validate_bi / _bu on Timetable,
--                      trg_sc_semester_bi / _bu on Section_Course
--   [M6] NEW INDEXES   on Timetable, Classroom, Course
-- No existing table or column was renamed, dropped or altered.
-- =====================================================================

USE university_timetable;

-- ---------------------------------------------------------------------
-- [M1] Section_Course: the courses each section must take.
-- semester is kept (as requested) and is validated against
-- Section.semester by trigger [M5] so the two can never disagree.
-- ---------------------------------------------------------------------
CREATE TABLE Section_Course (
    section_id INT NOT NULL,
    course_id  VARCHAR(10) NOT NULL,
    semester   INT NOT NULL CHECK (semester BETWEEN 1 AND 8),
    PRIMARY KEY (section_id, course_id),
    CONSTRAINT fk_sc_section
        FOREIGN KEY (section_id) REFERENCES Section(section_id)
        ON UPDATE CASCADE ON DELETE CASCADE,
    CONSTRAINT fk_sc_course
        FOREIGN KEY (course_id) REFERENCES Course(course_id)
        ON UPDATE CASCADE ON DELETE RESTRICT
);

-- ---------------------------------------------------------------------
-- [M6] Indexes.
-- Created BEFORE the composite FKs so the FKs reuse these named indexes
-- (a composite FK needs an index whose leading columns match it).
--   idx_tt_year_slot       : "what is happening in slot X this year"
--   idx_tt_faculty_course  : supports fk_tt_teaches
--   idx_tt_section_course  : supports fk_tt_section_course and
--                            "sessions of course C for section S"
--   idx_room_type_capacity : free-room search by type + capacity
--   idx_course_type        : scheduler orders/filters by LAB vs THEORY
-- ---------------------------------------------------------------------
ALTER TABLE Timetable
    ADD INDEX idx_tt_year_slot      (academic_year, slot_id),
    ADD INDEX idx_tt_faculty_course (faculty_id, course_id),
    ADD INDEX idx_tt_section_course (section_id, course_id, academic_year);

ALTER TABLE Classroom ADD INDEX idx_room_type_capacity (room_type, capacity);
ALTER TABLE Course    ADD INDEX idx_course_type (course_type);

-- ---------------------------------------------------------------------
-- [M2] + [M3] Composite foreign keys on Timetable.
-- M2: only a faculty member who teaches the course can be scheduled
--     for it.
-- M3: only courses in the section's plan can be scheduled for it.
-- The original single-column FKs (fk_tt_course, fk_tt_faculty, ...)
-- are kept untouched.
-- ---------------------------------------------------------------------
ALTER TABLE Timetable
    ADD CONSTRAINT fk_tt_teaches
        FOREIGN KEY (faculty_id, course_id)
        REFERENCES Teaches (faculty_id, course_id)
        ON UPDATE CASCADE ON DELETE RESTRICT,
    ADD CONSTRAINT fk_tt_section_course
        FOREIGN KEY (section_id, course_id)
        REFERENCES Section_Course (section_id, course_id)
        ON UPDATE CASCADE ON DELETE RESTRICT;

-- ---------------------------------------------------------------------
-- [M4] + [M5] Business-rule triggers.
-- MySQL has no combined INSERT/UPDATE trigger, so the logic lives in one
-- procedure that both triggers call (DRY). Each failure uses its own
-- MYSQL_ERRNO so Python can tell them apart:
--     5001 room capacity < section strength
--     5002 course type / room type mismatch
--     5003 Section_Course.semester <> Section.semester
-- ---------------------------------------------------------------------
DELIMITER $$

DROP PROCEDURE IF EXISTS sp_validate_placement$$
CREATE PROCEDURE sp_validate_placement(
    IN p_room_id    INT,
    IN p_section_id INT,
    IN p_course_id  VARCHAR(10)
)
BEGIN
    DECLARE v_capacity    INT;
    DECLARE v_room_type   VARCHAR(10);
    DECLARE v_strength    INT;
    DECLARE v_course_type VARCHAR(10);

    SELECT capacity, room_type INTO v_capacity, v_room_type
      FROM Classroom WHERE room_id = p_room_id;
    SELECT strength INTO v_strength
      FROM Section WHERE section_id = p_section_id;
    SELECT course_type INTO v_course_type
      FROM Course WHERE course_id = p_course_id;

    -- Unknown ids leave the variables NULL; the FKs then reject the row,
    -- so only non-NULL values are compared here.
    IF v_capacity IS NOT NULL AND v_strength IS NOT NULL
       AND v_capacity < v_strength THEN
        SIGNAL SQLSTATE '45000' SET MYSQL_ERRNO = 5001,
            MESSAGE_TEXT = 'Room capacity is less than section strength';
    END IF;

    IF v_course_type IS NOT NULL AND v_room_type IS NOT NULL
       AND ((v_course_type = 'LAB'    AND v_room_type <> 'LAB')
         OR (v_course_type = 'THEORY' AND v_room_type <> 'LECTURE')) THEN
        SIGNAL SQLSTATE '45000' SET MYSQL_ERRNO = 5002,
            MESSAGE_TEXT = 'Course type does not match room type (LAB->LAB, THEORY->LECTURE)';
    END IF;
END$$

DROP TRIGGER IF EXISTS trg_tt_validate_bi$$
CREATE TRIGGER trg_tt_validate_bi BEFORE INSERT ON Timetable
FOR EACH ROW
BEGIN
    CALL sp_validate_placement(NEW.room_id, NEW.section_id, NEW.course_id);
END$$

DROP TRIGGER IF EXISTS trg_tt_validate_bu$$
CREATE TRIGGER trg_tt_validate_bu BEFORE UPDATE ON Timetable
FOR EACH ROW
BEGIN
    CALL sp_validate_placement(NEW.room_id, NEW.section_id, NEW.course_id);
END$$

-- Section_Course.semester must match the section's own semester.
DROP TRIGGER IF EXISTS trg_sc_semester_bi$$
CREATE TRIGGER trg_sc_semester_bi BEFORE INSERT ON Section_Course
FOR EACH ROW
BEGIN
    DECLARE v_sem INT;
    SELECT semester INTO v_sem FROM Section WHERE section_id = NEW.section_id;
    IF v_sem IS NOT NULL AND v_sem <> NEW.semester THEN
        SIGNAL SQLSTATE '45000' SET MYSQL_ERRNO = 5003,
            MESSAGE_TEXT = 'Section_Course.semester does not match Section.semester';
    END IF;
END$$

DROP TRIGGER IF EXISTS trg_sc_semester_bu$$
CREATE TRIGGER trg_sc_semester_bu BEFORE UPDATE ON Section_Course
FOR EACH ROW
BEGIN
    DECLARE v_sem INT;
    SELECT semester INTO v_sem FROM Section WHERE section_id = NEW.section_id;
    IF v_sem IS NOT NULL AND v_sem <> NEW.semester THEN
        SIGNAL SQLSTATE '45000' SET MYSQL_ERRNO = 5003,
            MESSAGE_TEXT = 'Section_Course.semester does not match Section.semester';
    END IF;
END$$

DELIMITER ;

-- Known limitation: the triggers fire on Timetable writes only. Lowering a
-- room's capacity or raising a section's strength AFTER scheduling is not
-- blocked; sp_list_clashes (04_views_procedures.sql) detects that case.
