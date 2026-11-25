-- ============================================================
-- 03_data_exploration.sql
-- Step 3 of 4: read-only analysis. Nothing here modifies data.
--
-- Requires 02_data_cleaning.sql to have run (medicalappointment).
-- Re-runnable: yes.
-- ============================================================

USE healthcare_analytics;

-- ------------------------------------------------------------
-- 1: What's our overall no-show rate?
-- ------------------------------------------------------------
SELECT
  no_show,
  COUNT(*) AS total_appointments,
  ROUND(COUNT(*) * 100.0 / (SELECT COUNT(*) FROM medicalappointment), 2) AS pct_of_total
FROM medicalappointment
GROUP BY no_show;

-- ------------------------------------------------------------
-- 2: Does the day of the week matter?
-- ------------------------------------------------------------
SELECT
  DAYNAME(AppointmentDay) AS appointment_day,
  COUNT(*) AS total_appointments,
  SUM(CASE WHEN no_show = 'Yes' THEN 1 ELSE 0 END) AS no_shows,
  ROUND(SUM(CASE WHEN no_show = 'Yes' THEN 1 ELSE 0 END) * 100 / COUNT(*), 2) AS rate_of_no_shows
FROM medicalappointment
GROUP BY DAYNAME(AppointmentDay)
ORDER BY rate_of_no_shows DESC;

-- ------------------------------------------------------------
-- 3: Does lead time matter?
-- same day / 1-3 days / within a week (4-7) / long lead (8+)
-- ------------------------------------------------------------
SELECT
  CASE
    WHEN lead_time_days = 0 THEN 'Same Day'
    WHEN lead_time_days BETWEEN 1 AND 3 THEN 'Short (1-3 Days)'
    WHEN lead_time_days BETWEEN 4 AND 7 THEN 'Within a week'
    ELSE 'Long Lead (8+ days)'
  END AS lead_time_bucket,
  COUNT(*) AS total_appointments,
  ROUND(SUM(CASE WHEN no_show = 'Yes' THEN 1 ELSE 0 END) * 100 / COUNT(*), 2) AS no_show_rate
FROM medicalappointment
GROUP BY lead_time_bucket
ORDER BY no_show_rate DESC;

-- ------------------------------------------------------------
-- 4: Age groups
-- child 0-12 / teen 13-19 / young adult 20-39 / adult 40-59 / senior 60+
-- ------------------------------------------------------------
SELECT
  CASE
    WHEN Age BETWEEN 0 AND 12 THEN 'Child'
    WHEN Age BETWEEN 13 AND 19 THEN 'Teen'
    WHEN Age BETWEEN 20 AND 39 THEN 'Young Adult'
    WHEN Age BETWEEN 40 AND 59 THEN 'Adult'
    ELSE 'Senior'
  END AS age_group,
  COUNT(*) AS total_appointments,
  ROUND(SUM(CASE WHEN no_show = 'Yes' THEN 1 ELSE 0 END) * 100 / COUNT(*), 2) AS no_show_rate
FROM medicalappointment
GROUP BY age_group
ORDER BY no_show_rate DESC;

-- ------------------------------------------------------------
-- 5: Do SMS reminders help?
-- NB: this is a raw correlation - reminders are sent to longer-lead
-- bookings, so the comparison is confounded by lead time. See
-- 03b in this file (SMS within lead-time buckets) for the controlled view.
-- ------------------------------------------------------------
SELECT
  CASE WHEN SMS_received = 1 THEN 'Received SMS' ELSE 'No SMS' END AS sms_status,
  COUNT(*) AS total_appointments,
  ROUND(SUM(CASE WHEN no_show = 'Yes' THEN 1 ELSE 0 END) * 100 / COUNT(*), 2) AS no_show_rate
FROM medicalappointment
GROUP BY sms_status;

-- ------------------------------------------------------------
-- 5b: SMS effect within each lead-time bucket (controls for the confound)
-- ------------------------------------------------------------
SELECT
  CASE
    WHEN lead_time_days = 0 THEN 'Same Day'
    WHEN lead_time_days BETWEEN 1 AND 3 THEN 'Short (1-3 Days)'
    WHEN lead_time_days BETWEEN 4 AND 7 THEN 'Within a week'
    ELSE 'Long Lead (8+ days)'
  END AS lead_time_bucket,
  CASE WHEN SMS_received = 1 THEN 'Received SMS' ELSE 'No SMS' END AS sms_status,
  COUNT(*) AS total_appointments,
  ROUND(SUM(CASE WHEN no_show = 'Yes' THEN 1 ELSE 0 END) * 100 / COUNT(*), 2) AS no_show_rate
FROM medicalappointment
GROUP BY lead_time_bucket, sms_status
ORDER BY lead_time_bucket, sms_status;

-- ------------------------------------------------------------
-- 6: Which neighbourhoods have the highest risk? (min 100 appointments)
-- ------------------------------------------------------------
SELECT
  Neighbourhood,
  COUNT(*) AS total_appointments,
  ROUND(SUM(CASE WHEN no_show = 'Yes' THEN 1 ELSE 0 END) * 100 / COUNT(*), 2) AS no_show_rate,
  RANK() OVER (ORDER BY ROUND(SUM(CASE WHEN no_show = 'Yes' THEN 1 ELSE 0 END) * 100 / COUNT(*), 2) DESC) AS risk_rank
FROM medicalappointment
GROUP BY Neighbourhood
HAVING COUNT(*) >= 100
ORDER BY no_show_rate DESC
LIMIT 15;

-- ------------------------------------------------------------
-- 7: Patient-level view - each visit against that patient's prior history
-- Limited to 100 rows for on-screen use; the full version is the
-- v_appointment_risk view in 04_patient_risk_view.sql.
-- ------------------------------------------------------------
SELECT
  PatientId,
  AppointmentID,
  AppointmentDay,
  no_show,
  COUNT(*) OVER (
    PARTITION BY PatientId
    ORDER BY AppointmentDay, AppointmentID
    ROWS BETWEEN UNBOUNDED PRECEDING AND 1 PRECEDING
  ) AS prior_appointments,
  SUM(CASE WHEN no_show = 'Yes' THEN 1 ELSE 0 END) OVER (
    PARTITION BY PatientId
    ORDER BY AppointmentDay, AppointmentID
    ROWS BETWEEN UNBOUNDED PRECEDING AND 1 PRECEDING
  ) AS prior_no_shows
FROM medicalappointment
ORDER BY PatientId, AppointmentDay
LIMIT 100;
