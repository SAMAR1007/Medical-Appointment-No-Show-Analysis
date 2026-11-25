-- ============================================================
-- 04_patient_risk_view.sql
-- Step 4 of 4: the v_appointment_risk view and a preview of it.
--
-- The view is a saved query - it stores no data, so every SELECT against
-- it re-computes the windows from the current medicalappointment table.
--
-- Re-runnable: yes (CREATE OR REPLACE VIEW).
-- ============================================================

USE healthcare_analytics;

-- ------------------------------------------------------------
-- Risk tier per appointment, based on the patient's own history
-- BEFORE that visit:
--   New Patient - Monitor : no prior appointments
--   High Risk             : prior no-show rate >= 0.5 OR lead time >= 8 days
--   Medium Risk           : prior no-show rate >= 0.2 OR lead time 4-7 days
--   Low Risk              : everything else
--
-- ORDER BY includes AppointmentID so patients with two appointments on the
-- same day get a deterministic history rather than an arbitrary one.
-- ------------------------------------------------------------
CREATE OR REPLACE VIEW v_appointment_risk AS
WITH patient_history AS (
    SELECT
        PatientId,
        AppointmentID,
        AppointmentDay,
        Neighbourhood,
        lead_time_days,
        SMS_received,
        Scholarship,
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
)
SELECT
    PatientId,
    AppointmentID,
    AppointmentDay,
    Neighbourhood,
    lead_time_days,
    prior_appointments,
    prior_no_shows,
    ROUND(prior_no_shows / NULLIF(prior_appointments, 0), 2) AS prior_no_show_rate,
    CASE
        WHEN prior_appointments = 0 THEN 'New Patient - Monitor'
        WHEN (prior_no_shows / NULLIF(prior_appointments, 0)) >= 0.5
             OR lead_time_days >= 8 THEN 'High Risk'
        WHEN (prior_no_shows / NULLIF(prior_appointments, 0)) >= 0.2
             OR lead_time_days BETWEEN 4 AND 7 THEN 'Medium Risk'
        ELSE 'Low Risk'
    END AS risk_tier
FROM patient_history;

-- ------------------------------------------------------------
-- Preview: the 50 highest-risk upcoming appointments.
-- Ranked by prior no-show rate first, then lead time, so the worst
-- offenders surface instead of whichever dates happen to come first.
-- ------------------------------------------------------------
SELECT *
FROM v_appointment_risk
WHERE risk_tier = 'High Risk'
ORDER BY prior_no_show_rate DESC, lead_time_days DESC, AppointmentDay
LIMIT 50;

-- Tier distribution across the whole dataset.
SELECT risk_tier, COUNT(*) AS appointments
FROM v_appointment_risk
GROUP BY risk_tier
ORDER BY appointments DESC;
