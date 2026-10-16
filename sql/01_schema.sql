-- =====================================================================
-- 01_schema.sql  --  University Timetable Generation System
-- Reproduced EXACTLY from the PDF (section 2, "DDL Statements (MySQL)").
-- Only whitespace / line-break glitches from PDF text extraction were
-- repaired; no table, column or constraint was changed.
-- All improvements live in 02_migration_improvements.sql.
-- =====================================================================

CREATE DATABASE university_timetable;
USE university_timetable;

-- 1. Department
CREATE TABLE Department (
    dept_id   INT AUTO_INCREMENT PRIMARY KEY,
    dept_name VARCHAR(100) NOT NULL UNIQUE,
    building  VARCHAR(50)
);

-- 2. Faculty (1:N with Department)
CREATE TABLE Faculty (
    faculty_id INT AUTO_INCREMENT PRIMARY KEY,
    name       VARCHAR(100) NOT NULL,
    email      VARCHAR(100) UNIQUE,
    dept_id    INT NOT NULL,
    CONSTRAINT fk_faculty_dept
        FOREIGN KEY (dept_id) REFERENCES Department(dept_id)
        ON UPDATE CASCADE ON DELETE RESTRICT
);

-- 3. Course (1:N with Department)
CREATE TABLE Course (
    course_id    VARCHAR(10) PRIMARY KEY,
    course_name  VARCHAR(100) NOT NULL,
    credits      INT NOT NULL CHECK (credits > 0),
    weekly_hours INT NOT NULL DEFAULT 3,
    course_type  ENUM('THEORY', 'LAB') NOT NULL DEFAULT 'THEORY',
    dept_id      INT NOT NULL,
    CONSTRAINT fk_course_dept
        FOREIGN KEY (dept_id) REFERENCES Department(dept_id)
        ON UPDATE CASCADE ON DELETE RESTRICT
);

-- 4. Classroom
CREATE TABLE Classroom (
    room_id   INT AUTO_INCREMENT PRIMARY KEY,
    room_no   VARCHAR(10) NOT NULL,
    building  VARCHAR(50) NOT NULL,
    capacity  INT NOT NULL CHECK (capacity > 0),
    room_type ENUM('LECTURE', 'LAB') NOT NULL DEFAULT 'LECTURE',
    CONSTRAINT uq_room UNIQUE (building, room_no)
);

-- 5. Section (1:N with Department)
CREATE TABLE Section (
    section_id   INT AUTO_INCREMENT PRIMARY KEY,
    section_name VARCHAR(10) NOT NULL,            -- e.g. 'A'
    semester     INT NOT NULL CHECK (semester BETWEEN 1 AND 8),
    strength     INT NOT NULL DEFAULT 60,
    dept_id      INT NOT NULL,
    CONSTRAINT fk_section_dept
        FOREIGN KEY (dept_id) REFERENCES Department(dept_id)
        ON UPDATE CASCADE ON DELETE RESTRICT,
    CONSTRAINT uq_section UNIQUE (dept_id, semester, section_name)
);

-- 6. Student (1:N with Section)
CREATE TABLE Student (
    student_id VARCHAR(15) PRIMARY KEY,
    name       VARCHAR(100) NOT NULL,
    email      VARCHAR(100) UNIQUE,
    section_id INT NOT NULL,
    CONSTRAINT fk_student_section
        FOREIGN KEY (section_id) REFERENCES Section(section_id)
        ON UPDATE CASCADE ON DELETE RESTRICT
);

-- 7. TimeSlot (one row per day-period combination)
CREATE TABLE TimeSlot (
    slot_id     INT AUTO_INCREMENT PRIMARY KEY,
    day_of_week ENUM('MON','TUE','WED','THU','FRI','SAT') NOT NULL,
    period_no   INT NOT NULL CHECK (period_no BETWEEN 1 AND 8),
    start_time  TIME NOT NULL,
    end_time    TIME NOT NULL,
    CONSTRAINT uq_slot UNIQUE (day_of_week, period_no)
);

-- 8. Teaches (M:N Faculty-Course)
CREATE TABLE Teaches (
    faculty_id INT NOT NULL,
    course_id  VARCHAR(10) NOT NULL,
    PRIMARY KEY (faculty_id, course_id),
    CONSTRAINT fk_teaches_faculty
        FOREIGN KEY (faculty_id) REFERENCES Faculty(faculty_id)
        ON UPDATE CASCADE ON DELETE CASCADE,
    CONSTRAINT fk_teaches_course
        FOREIGN KEY (course_id) REFERENCES Course(course_id)
        ON UPDATE CASCADE ON DELETE CASCADE
);

-- 9. Enrollment (M:N Student-Course)
CREATE TABLE Enrollment (
    student_id VARCHAR(15) NOT NULL,
    course_id  VARCHAR(10) NOT NULL,
    PRIMARY KEY (student_id, course_id),
    CONSTRAINT fk_enroll_student
        FOREIGN KEY (student_id) REFERENCES Student(student_id)
        ON UPDATE CASCADE ON DELETE CASCADE,
    CONSTRAINT fk_enroll_course
        FOREIGN KEY (course_id) REFERENCES Course(course_id)
        ON UPDATE CASCADE ON DELETE CASCADE
);

-- 10. Timetable (N-ary scheduling relationship)
CREATE TABLE Timetable (
    timetable_id  INT AUTO_INCREMENT PRIMARY KEY,
    course_id     VARCHAR(10) NOT NULL,
    faculty_id    INT NOT NULL,
    room_id       INT NOT NULL,
    slot_id       INT NOT NULL,
    section_id    INT NOT NULL,
    academic_year VARCHAR(9) NOT NULL,
    -- Hard constraints: no clashes
    CONSTRAINT uq_room_slot    UNIQUE (room_id,    slot_id, academic_year),
    CONSTRAINT uq_faculty_slot UNIQUE (faculty_id, slot_id, academic_year),
    CONSTRAINT uq_section_slot UNIQUE (section_id, slot_id, academic_year),
    CONSTRAINT fk_tt_course
        FOREIGN KEY (course_id)  REFERENCES Course(course_id),
    CONSTRAINT fk_tt_faculty
        FOREIGN KEY (faculty_id) REFERENCES Faculty(faculty_id),
    CONSTRAINT fk_tt_room
        FOREIGN KEY (room_id)    REFERENCES Classroom(room_id),
    CONSTRAINT fk_tt_slot
        FOREIGN KEY (slot_id)    REFERENCES TimeSlot(slot_id),
    CONSTRAINT fk_tt_section
        FOREIGN KEY (section_id) REFERENCES Section(section_id)
);
