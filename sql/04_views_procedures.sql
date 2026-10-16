-- =====================================================================
-- 04_views_procedures.sql
-- Run after 02_migration_improvements.sql (order vs. 03 seed is free).
--     mysql -u root -p < sql/04_views_procedures.sql
-- Views cannot take parameters, so filter them yourself, e.g.
--     SELECT * FROM v_section_timetable WHERE academic_year = '2025-2026';
-- Views that "fill in the blanks" (room_utilization, free_slots) take the
-- list of years from the Timetable table, so they are empty until at
-- least one timetable row exists. The procedures take the year as a
-- parameter and work on an empty timetable too.
-- =====================================================================

USE university_timetable;

-- ---------------------------------------------------------------------
-- VIEW 1: v_section_timetable  (one row per scheduled class, section view)
-- ---------------------------------------------------------------------
CREATE OR REPLACE VIEW v_section_timetable AS
SELECT t.academic_year,
       s.section_id,
       d.dept_name,
       s.semester,
       s.section_name,
       ts.slot_id,
       ts.day_of_week,
       ts.period_no,
       ts.start_time,
       ts.end_time,
       c.course_id,
       c.course_name,
       c.course_type,
       f.faculty_id,
       f.name AS faculty_name,
       r.room_id,
       r.building,
       r.room_no
FROM Timetable t
JOIN Section    s  ON s.section_id = t.section_id
JOIN Department d  ON d.dept_id    = s.dept_id
JOIN TimeSlot   ts ON ts.slot_id   = t.slot_id
JOIN Course     c  ON c.course_id  = t.course_id
JOIN Faculty    f  ON f.faculty_id = t.faculty_id
JOIN Classroom  r  ON r.room_id    = t.room_id;

-- ---------------------------------------------------------------------
-- VIEW 2: v_faculty_timetable  (same facts, faculty perspective)
-- ---------------------------------------------------------------------
CREATE OR REPLACE VIEW v_faculty_timetable AS
SELECT t.academic_year,
       f.faculty_id,
       f.name AS faculty_name,
       fd.dept_name AS faculty_dept,
       ts.slot_id,
       ts.day_of_week,
       ts.period_no,
       ts.start_time,
       ts.end_time,
       c.course_id,
       c.course_name,
       c.course_type,
       s.section_id,
       CONCAT(sd.dept_name, ' Sem ', s.semester, ' Sec ', s.section_name) AS section_label,
       r.room_id,
       r.building,
       r.room_no
FROM Timetable t
JOIN Faculty    f  ON f.faculty_id = t.faculty_id
JOIN Department fd ON fd.dept_id   = f.dept_id
JOIN TimeSlot   ts ON ts.slot_id   = t.slot_id
JOIN Course     c  ON c.course_id  = t.course_id
JOIN Section    s  ON s.section_id = t.section_id
JOIN Department sd ON sd.dept_id   = s.dept_id
JOIN Classroom  r  ON r.room_id    = t.room_id;

-- ---------------------------------------------------------------------
-- VIEW 3: v_room_utilization
-- Every room x every academic year present in Timetable (LEFT JOIN so
-- unused rooms show 0%). total_slots = all rows of TimeSlot (48 in seed).
-- ---------------------------------------------------------------------
CREATE OR REPLACE VIEW v_room_utilization AS
SELECT y.academic_year,
       r.room_id,
       r.building,
       r.room_no,
       r.room_type,
       r.capacity,
       COUNT(t.timetable_id) AS used_slots,
       st.total_slots,
       ROUND(100 * COUNT(t.timetable_id) / st.total_slots, 2) AS utilization_pct
FROM (SELECT DISTINCT academic_year FROM Timetable) y
CROSS JOIN Classroom r
CROSS JOIN (SELECT COUNT(*) AS total_slots FROM TimeSlot) st
LEFT JOIN Timetable t
       ON t.room_id = r.room_id AND t.academic_year = y.academic_year
GROUP BY y.academic_year, r.room_id, r.building, r.room_no,
         r.room_type, r.capacity, st.total_slots;

-- ---------------------------------------------------------------------
-- VIEW 4: v_free_slots
-- Free (resource, slot) pairs for rooms AND faculty in one view, told
-- apart by resource_type. A free slot = no Timetable row for that
-- resource in that slot and year (anti-join via LEFT JOIN ... IS NULL).
-- ---------------------------------------------------------------------
CREATE OR REPLACE VIEW v_free_slots AS
SELECT y.academic_year,
       'ROOM' AS resource_type,
       r.room_id AS resource_id,
       CONCAT(r.building, '-', r.room_no) AS resource_name,
       ts.slot_id, ts.day_of_week, ts.period_no, ts.start_time, ts.end_time
FROM (SELECT DISTINCT academic_year FROM Timetable) y
CROSS JOIN Classroom r
CROSS JOIN TimeSlot ts
LEFT JOIN Timetable t
       ON t.room_id = r.room_id AND t.slot_id = ts.slot_id
      AND t.academic_year = y.academic_year
WHERE t.timetable_id IS NULL
UNION ALL
SELECT y.academic_year,
       'FACULTY' AS resource_type,
       f.faculty_id AS resource_id,
       f.name AS resource_name,
       ts.slot_id, ts.day_of_week, ts.period_no, ts.start_time, ts.end_time
FROM (SELECT DISTINCT academic_year FROM Timetable) y
CROSS JOIN Faculty f
CROSS JOIN TimeSlot ts
LEFT JOIN Timetable t
       ON t.faculty_id = f.faculty_id AND t.slot_id = ts.slot_id
      AND t.academic_year = y.academic_year
WHERE t.timetable_id IS NULL;

-- ---------------------------------------------------------------------
-- PROCEDURES
-- ---------------------------------------------------------------------
DELIMITER $$

-- Faculty workload: periods/week, teaching days, heaviest day, variety.
DROP PROCEDURE IF EXISTS sp_faculty_workload$$
CREATE PROCEDURE sp_faculty_workload(IN p_year VARCHAR(9))
BEGIN
    SELECT f.faculty_id,
           f.name AS faculty_name,
           d.dept_name,
           COALESCE(w.periods_per_week, 0)    AS periods_per_week,
           COALESCE(w.teaching_days, 0)       AS teaching_days,
           COALESCE(w.max_periods_in_a_day, 0) AS max_periods_in_a_day,
           (SELECT COUNT(DISTINCT t2.course_id)
              FROM Timetable t2
             WHERE t2.faculty_id = f.faculty_id
               AND t2.academic_year = p_year) AS distinct_courses
    FROM Faculty f
    JOIN Department d ON d.dept_id = f.dept_id
    LEFT JOIN (
        -- inner query: sessions per faculty per day; outer: roll up
        SELECT faculty_id,
               CAST(SUM(cnt) AS UNSIGNED) AS periods_per_week,
               COUNT(*)                   AS teaching_days,
               MAX(cnt)                   AS max_periods_in_a_day
        FROM (
            SELECT t.faculty_id, ts.day_of_week, COUNT(*) AS cnt
            FROM Timetable t
            JOIN TimeSlot ts ON ts.slot_id = t.slot_id
            WHERE t.academic_year = p_year
            GROUP BY t.faculty_id, ts.day_of_week
        ) per_day
        GROUP BY faculty_id
    ) w ON w.faculty_id = f.faculty_id
    ORDER BY periods_per_week DESC, f.name;
END$$

-- Room utilization % (used slots / all slots in the week).
DROP PROCEDURE IF EXISTS sp_room_utilization$$
CREATE PROCEDURE sp_room_utilization(IN p_year VARCHAR(9))
BEGIN
    SELECT r.room_id,
           r.building,
           r.room_no,
           r.room_type,
           r.capacity,
           COUNT(t.timetable_id) AS used_slots,
           (SELECT COUNT(*) FROM TimeSlot) AS total_slots,
           ROUND(100 * COUNT(t.timetable_id) / (SELECT COUNT(*) FROM TimeSlot), 2)
               AS utilization_pct
    FROM Classroom r
    LEFT JOIN Timetable t
           ON t.room_id = r.room_id AND t.academic_year = p_year
    GROUP BY r.room_id, r.building, r.room_no, r.room_type, r.capacity
    ORDER BY utilization_pct DESC, r.building, r.room_no;
END$$

-- Clash / rule-violation report. MUST return ZERO ROWS on a valid
-- timetable. The first three checks are normally impossible because of
-- the UNIQUE constraints; they are kept as a safety net in case those are
-- ever dropped. The rest catch drift that triggers cannot see (e.g. room
-- capacity lowered after scheduling).
-- Columns: clash_type, slot_id, entity_id, timetable_ids, details
DROP PROCEDURE IF EXISTS sp_list_clashes$$
CREATE PROCEDURE sp_list_clashes(IN p_year VARCHAR(9))
BEGIN
    SELECT 'ROOM_DOUBLE_BOOKED' AS clash_type,
           t.slot_id,
           CAST(t.room_id AS CHAR) AS entity_id,
           GROUP_CONCAT(t.timetable_id ORDER BY t.timetable_id) AS timetable_ids,
           CONCAT('room ', t.room_id, ' booked ', COUNT(*), ' times') AS details
    FROM Timetable t
    WHERE t.academic_year = p_year
    GROUP BY t.room_id, t.slot_id
    HAVING COUNT(*) > 1

    UNION ALL
    SELECT 'FACULTY_DOUBLE_BOOKED',
           t.slot_id,
           CAST(t.faculty_id AS CHAR),
           GROUP_CONCAT(t.timetable_id ORDER BY t.timetable_id),
           CONCAT('faculty ', t.faculty_id, ' booked ', COUNT(*), ' times')
    FROM Timetable t
    WHERE t.academic_year = p_year
    GROUP BY t.faculty_id, t.slot_id
    HAVING COUNT(*) > 1

    UNION ALL
    SELECT 'SECTION_DOUBLE_BOOKED',
           t.slot_id,
           CAST(t.section_id AS CHAR),
           GROUP_CONCAT(t.timetable_id ORDER BY t.timetable_id),
           CONCAT('section ', t.section_id, ' booked ', COUNT(*), ' times')
    FROM Timetable t
    WHERE t.academic_year = p_year
    GROUP BY t.section_id, t.slot_id
    HAVING COUNT(*) > 1

    UNION ALL
    SELECT 'ROOM_TYPE_MISMATCH',
           t.slot_id,
           CAST(t.timetable_id AS CHAR),
           CAST(t.timetable_id AS CHAR),
           CONCAT(c.course_type, ' course ', c.course_id, ' in ',
                  r.room_type, ' room ', r.building, '-', r.room_no)
    FROM Timetable t
    JOIN Course    c ON c.course_id = t.course_id
    JOIN Classroom r ON r.room_id   = t.room_id
    WHERE t.academic_year = p_year
      AND ((c.course_type = 'LAB'    AND r.room_type <> 'LAB')
        OR (c.course_type = 'THEORY' AND r.room_type <> 'LECTURE'))

    UNION ALL
    SELECT 'ROOM_CAPACITY_EXCEEDED',
           t.slot_id,
           CAST(t.timetable_id AS CHAR),
           CAST(t.timetable_id AS CHAR),
           CONCAT('section strength ', s.strength, ' > capacity ', r.capacity,
                  ' of ', r.building, '-', r.room_no)
    FROM Timetable t
    JOIN Section   s ON s.section_id = t.section_id
    JOIN Classroom r ON r.room_id    = t.room_id
    WHERE t.academic_year = p_year AND r.capacity < s.strength

    UNION ALL
    SELECT 'FACULTY_NOT_QUALIFIED',
           t.slot_id,
           CAST(t.timetable_id AS CHAR),
           CAST(t.timetable_id AS CHAR),
           CONCAT('faculty ', t.faculty_id, ' does not teach ', t.course_id)
    FROM Timetable t
    LEFT JOIN Teaches th
           ON th.faculty_id = t.faculty_id AND th.course_id = t.course_id
    WHERE t.academic_year = p_year AND th.faculty_id IS NULL

    UNION ALL
    SELECT 'COURSE_NOT_IN_SECTION_PLAN',
           t.slot_id,
           CAST(t.timetable_id AS CHAR),
           CAST(t.timetable_id AS CHAR),
           CONCAT('section ', t.section_id, ' does not take ', t.course_id)
    FROM Timetable t
    LEFT JOIN Section_Course sc
           ON sc.section_id = t.section_id AND sc.course_id = t.course_id
    WHERE t.academic_year = p_year AND sc.section_id IS NULL

    ORDER BY clash_type, slot_id;
END$$

-- Weekly-hours coverage: every (section, course) in Section_Course whose
-- scheduled session count differs from Course.weekly_hours. Empty result
-- = every section got exactly the hours it needs. (Not a "clash", so it
-- is separate from sp_list_clashes; used by the tests and generator.)
DROP PROCEDURE IF EXISTS sp_hours_coverage$$
CREATE PROCEDURE sp_hours_coverage(IN p_year VARCHAR(9))
BEGIN
    SELECT sc.section_id,
           s.section_name,
           s.semester,
           sc.course_id,
           c.course_name,
           c.weekly_hours AS required_hours,
           COUNT(t.timetable_id) AS scheduled_hours,
           CASE WHEN COUNT(t.timetable_id) < c.weekly_hours
                THEN 'UNDER_SCHEDULED' ELSE 'OVER_SCHEDULED' END AS status
    FROM Section_Course sc
    JOIN Section s ON s.section_id = sc.section_id
    JOIN Course  c ON c.course_id  = sc.course_id
    LEFT JOIN Timetable t
           ON t.section_id = sc.section_id
          AND t.course_id  = sc.course_id
          AND t.academic_year = p_year
    GROUP BY sc.section_id, sc.course_id, s.section_name, s.semester,
             c.course_name, c.weekly_hours
    HAVING COUNT(t.timetable_id) <> c.weekly_hours
    ORDER BY sc.section_id, sc.course_id;
END$$

DELIMITER ;
