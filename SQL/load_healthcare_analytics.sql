-- ============================================================
-- Load KaggleV2-May-2016.csv (medical appointment no-shows)
-- into the healthcare_analytics schema.
--
-- Run this in MySQL Workbench with the CSV at:
--   C:/Users/bists/OneDrive/Desktop/PROJECT-1/KaggleV2-May-2016.csv
--
-- Requirements:
--   * Server option local_infile must be ON
--       SHOW VARIABLES LIKE 'local_infile';        -- expect ON
--       SET GLOBAL local_infile = 1;               -- needs admin, or use the
--   * Workbench must allow local infile for this connection:
--       Database > Manage Connections > (your connection) > Advanced
--       > check "Allow LOAD DATA LOCAL INFILE"
--
-- If you cannot enable local infile, skip to the bottom of this file and
-- use the Table Data Import Wizard instructions instead.
-- ============================================================

CREATE DATABASE IF NOT EXISTS healthcare_analytics
  DEFAULT CHARACTER SET utf8mb4;

USE healthcare_analytics;

-- ------------------------------------------------------------
-- 1) Staging table: every column is text so nothing is rejected
--    while the file is being read.
-- ------------------------------------------------------------
DROP TABLE IF EXISTS medicalappointment_staging;

CREATE TABLE medicalappointment_staging (
  PatientId          VARCHAR(32),
  AppointmentID      VARCHAR(32),
  Gender             VARCHAR(8),
  ScheduledDay       VARCHAR(32),
  AppointmentDay     VARCHAR(32),
  Age                VARCHAR(8),
  Neighbourhood      VARCHAR(128),
  Scholarship        VARCHAR(8),
  Hipertension       VARCHAR(8),
  Diabetes           VARCHAR(8),
  Alcoholism         VARCHAR(8),
  Handcap            VARCHAR(8),
  SMS_received       VARCHAR(8),
  `No-show`          VARCHAR(8)
) ENGINE = InnoDB;

LOAD DATA LOCAL INFILE 'C:/Users/bists/OneDrive/Desktop/PROJECT-1/KaggleV2-May-2016.csv'
INTO TABLE medicalappointment_staging
CHARACTER SET utf8mb4
FIELDS TERMINATED BY ',' ENCLOSED BY '"'
LINES TERMINATED BY '\n'
IGNORE 1 LINES
(PatientId, AppointmentID, Gender, ScheduledDay, AppointmentDay, Age,
 Neighbourhood, Scholarship, Hipertension, Diabetes, Alcoholism, Handcap,
 SMS_received, `No-show`);

-- ------------------------------------------------------------
-- 2) Typed table, populated from the staging data.
--    Dates in the file look like 2016-04-29T18:38:08Z.
-- ------------------------------------------------------------
DROP TABLE IF EXISTS medicalappointment;

CREATE TABLE medicalappointment (
  PatientId      BIGINT       NULL,      -- 5 source rows are corrupt, see PatientId_raw
  PatientId_raw  VARCHAR(32)  NOT NULL,  -- original token exactly as it appears in the CSV
  AppointmentID  INT          NOT NULL,
  Gender         CHAR(1)      NOT NULL,
  ScheduledDay   DATETIME     NOT NULL,
  AppointmentDay DATETIME     NOT NULL,
  Age            INT          NOT NULL,
  Neighbourhood  VARCHAR(128) NOT NULL,
  Scholarship    TINYINT      NOT NULL DEFAULT 0,
  Hipertension   TINYINT      NOT NULL DEFAULT 0,
  Diabetes       TINYINT      NOT NULL DEFAULT 0,
  Alcoholism     TINYINT      NOT NULL DEFAULT 0,
  Handcap        TINYINT      NOT NULL DEFAULT 0,
  SMS_received   TINYINT      NOT NULL DEFAULT 0,
  `No-show`      VARCHAR(3)   NOT NULL,
  PRIMARY KEY (AppointmentID),
  KEY idx_patient (PatientId),
  KEY idx_appointment_day (AppointmentDay)
) ENGINE = InnoDB;

-- Five rows in the CSV hold PatientId in scientific notation (e.g. 9377952927E-5),
-- a spreadsheet export artefact. Those cannot be recovered as numbers, so they
-- become NULL and the raw token is kept in PatientId_raw.
INSERT INTO medicalappointment (
  PatientId, PatientId_raw, AppointmentID, Gender, ScheduledDay, AppointmentDay, Age,
  Neighbourhood, Scholarship, Hipertension, Diabetes, Alcoholism, Handcap,
  SMS_received, `No-show`
)
SELECT
  CASE WHEN PatientId REGEXP '^[0-9]+$' THEN CAST(PatientId AS UNSIGNED) END,
  PatientId,
  CAST(AppointmentID AS UNSIGNED),
  Gender,
  STR_TO_DATE(ScheduledDay,   '%Y-%m-%dT%H:%i:%sZ'),
  STR_TO_DATE(AppointmentDay, '%Y-%m-%dT%H:%i:%sZ'),
  CAST(Age AS SIGNED),
  TRIM(Neighbourhood),
  CAST(Scholarship  AS UNSIGNED),
  CAST(Hipertension AS UNSIGNED),
  CAST(Diabetes     AS UNSIGNED),
  CAST(Alcoholism   AS UNSIGNED),
  CAST(Handcap      AS UNSIGNED),
  CAST(SMS_received AS UNSIGNED),
  TRIM(`No-show`)
FROM medicalappointment_staging;

-- ------------------------------------------------------------
-- 3) Check the load (expect 110527 rows).
-- ------------------------------------------------------------
SELECT COUNT(*) AS rows_loaded FROM medicalappointment;
SELECT * FROM medicalappointment ORDER BY AppointmentID LIMIT 5;

-- Rows whose PatientId could not be read as a number (expected: 5).
SELECT AppointmentID, PatientId_raw
FROM medicalappointment
WHERE PatientId IS NULL;

-- Inspect anything that failed to convert, then clean up.
SELECT * FROM medicalappointment_staging
WHERE STR_TO_DATE(ScheduledDay,   '%Y-%m-%dT%H:%i:%sZ') IS NULL
   OR STR_TO_DATE(AppointmentDay, '%Y-%m-%dT%H:%i:%sZ') IS NULL;

DROP TABLE IF EXISTS medicalappointment_staging;

-- ============================================================
-- ALTERNATIVE: MySQL Workbench Table Data Import Wizard
-- (no local_infile needed)
--   1. Run only: CREATE DATABASE IF NOT EXISTS healthcare_analytics;
--   2. In the Navigator, right-click healthcare_analytics > Table Data
--      Import Wizard.
--   3. Browse to KaggleV2-May-2016.csv, choose "Create new table"
--      (name it medicalappointment_raw), encoding utf8mb4, first row = column names.
--   4. Import. Dates arrive as text; convert afterwards with:
--
--      ALTER TABLE medicalappointment_raw
--        MODIFY ScheduledDay   DATETIME,
--        MODIFY AppointmentDay DATETIME;
--
--      -- Workbench's wizard cannot parse the trailing "Z", so if the
--      -- ALTER fails, use the STR_TO_DATE approach from step 2 instead.
-- ============================================================
