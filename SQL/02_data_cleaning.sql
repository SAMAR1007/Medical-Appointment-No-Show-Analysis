-- ============================================================
-- 02_data_cleaning.sql
-- Step 2 of 4: turn medicalappointment_raw into the clean, typed
-- table medicalappointment.
--
-- Re-runnable: yes. The clean table is dropped and rebuilt from raw,
-- so this is the single source of truth for every cleaning rule.
--
-- Cleaning rules applied:
--   1. Rename: Hipertension -> hypertension, Handcap -> disability_count,
--      `No-show` -> no_show.
--   2. Types: ids to integers, flags to TINYINT, Age to INT.
--   3. Timestamps '2016-04-29T18:38:08Z' parsed to DATETIME (ScheduledDay)
--      and DATE (AppointmentDay - the time part is always 00:00:00).
--   4. AppointmentDay is stored as DATE (its time part is always 00:00:00),
--      which makes lead_time_days a whole number of days.
--   5. Exclude Age = -1 (1 impossible value in the source).
--   6. Exclude rows where the appointment precedes the booking
--      (5 rows, lead_time_days would be negative).
--   7. 5 rows have PatientId in scientific notation ('9377952927E-5'), an
--      Excel export artefact. Those cannot be recovered as numbers: they stay
--      in the table with PatientId NULL and the raw token kept in PatientId_raw.
--
-- 110527 raw rows - 1 (Age = -1) - 5 (negative lead time) = 110521.
-- ============================================================

USE healthcare_analytics;

DROP TABLE IF EXISTS medicalappointment;

CREATE TABLE medicalappointment (
  PatientId       BIGINT       NULL,      -- NULL for the 5 corrupt source ids
  PatientId_raw   VARCHAR(32)  NOT NULL,  -- token exactly as it appears in the CSV
  AppointmentID   INT          NOT NULL,
  Gender          CHAR(1)      NOT NULL,
  ScheduledDay    DATETIME     NOT NULL,
  AppointmentDay  DATE         NOT NULL,
  Age             INT          NOT NULL,
  Neighbourhood   VARCHAR(128) NOT NULL,
  Scholarship     TINYINT      NOT NULL DEFAULT 0,
  hypertension    TINYINT      NOT NULL DEFAULT 0,
  Diabetes        TINYINT      NOT NULL DEFAULT 0,
  Alcoholism      TINYINT      NOT NULL DEFAULT 0,
  disability_count TINYINT     NOT NULL DEFAULT 0,
  SMS_received    TINYINT      NOT NULL DEFAULT 0,
  no_show         VARCHAR(3)   NOT NULL,
  lead_time_days  INT          NOT NULL,
  PRIMARY KEY (AppointmentID),
  KEY idx_patient (PatientId),
  KEY idx_appointment_day (AppointmentDay)
) ENGINE = InnoDB;

INSERT INTO medicalappointment (
  PatientId, PatientId_raw, AppointmentID, Gender, ScheduledDay, AppointmentDay,
  Age, Neighbourhood, Scholarship, hypertension, Diabetes, Alcoholism,
  disability_count, SMS_received, no_show, lead_time_days
)
SELECT
  CASE WHEN PatientId REGEXP '^[0-9]+$' THEN CAST(PatientId AS UNSIGNED) END,
  PatientId,
  CAST(AppointmentID AS UNSIGNED),
  Gender,
  STR_TO_DATE(ScheduledDay, '%Y-%m-%dT%H:%i:%sZ'),
  DATE(STR_TO_DATE(AppointmentDay, '%Y-%m-%dT%H:%i:%sZ')),
  CAST(Age AS SIGNED),
  TRIM(Neighbourhood),
  CAST(Scholarship AS UNSIGNED),
  CAST(Hipertension AS UNSIGNED),
  CAST(Diabetes AS UNSIGNED),
  CAST(Alcoholism AS UNSIGNED),
  CAST(Handcap AS UNSIGNED),
  CAST(SMS_received AS UNSIGNED),
  TRIM(`No-show`),
  DATEDIFF(
    DATE(STR_TO_DATE(AppointmentDay, '%Y-%m-%dT%H:%i:%sZ')),
    DATE(STR_TO_DATE(ScheduledDay,   '%Y-%m-%dT%H:%i:%sZ'))
  )
FROM medicalappointment_raw
WHERE Age <> '-1'                                                     -- rule 5
  AND DATE(STR_TO_DATE(AppointmentDay, '%Y-%m-%dT%H:%i:%sZ'))
      >= DATE(STR_TO_DATE(ScheduledDay, '%Y-%m-%dT%H:%i:%sZ'));       -- rule 6

-- ------------------------------------------------------------
-- Verification
-- ------------------------------------------------------------
SELECT COUNT(*) AS cleaned_rows FROM medicalappointment;                -- 110521
SELECT COUNT(*) AS raw_rows FROM medicalappointment_raw;                -- 110527

SELECT
  SUM(PatientId IS NULL) AS corrupt_ids,          -- 5
  SUM(ScheduledDay IS NULL) AS null_scheduled,    -- 0
  MIN(Age) AS min_age,                            -- 0
  MAX(Age) AS max_age,                            -- 115
  MIN(lead_time_days) AS min_lead,                -- 0
  MAX(lead_time_days) AS max_lead                 -- 179
FROM medicalappointment;

SELECT AppointmentID, PatientId_raw
FROM medicalappointment
WHERE PatientId IS NULL;
