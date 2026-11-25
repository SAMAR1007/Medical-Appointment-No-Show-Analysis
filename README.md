# Medical Appointment No-Show Analysis

An end-to-end analytics project on **110,521 medical appointments** (62,298 patients, April–June 2016) that measures why patients miss appointments and turns the findings into a reusable **patient risk tier** and an **executive Tableau dashboard**.

The core of the project is a reproducible **MySQL pipeline** that loads, cleans, explores and scores the data, plus a **Tableau executive dashboard** built on the cleaned output. A Python statistical companion is included to validate the SQL rules and quantify confounding, but the shipped decision tools are SQL and Tableau.

**Headline result:** 20.2% of appointments are missed. Lead time is the dominant driver — same-day appointments are missed 4.7% of the time, appointments booked 8+ days ahead 32.1%.

---

## Tableau dashboard — `Noshow final.twbx`

**Dashboard: "No-Show Risk | Executive Overview"**

Open `Noshow final.twbx` in Tableau Desktop (packaged workbook — the extract travels with the file, so no database connection is needed).

| Zone | Contents |
|---|---|
| KPI cards | **No-Show Rate**, **Total Appointments**, **Average Lead Time (Days)** |
| Chart | **No-Show Rate by Lead Time** — the primary risk curve |
| Chart | **No-Show Rate by Weekday** — day-of-week variation |
| Chart | **Appointment Share by Risk Tier** — how the SQL tiers split the population |
| Chart | **Top 15 Highest-Risk Neighbourhoods** — geographic hotspots |

The workbook reads an extract of the cleaned dataset (`Data/cleaned_data.csv` and `Data/v appointment-.csv`). To refresh it, replace those CSVs with fresh output from the SQL pipeline and update the extract's data source.

---

## SQL pipeline

All scripts live in `SQL/` and run against the `healthcare_analytics` schema in MySQL. They are numbered in execution order and are safe to re-run.

| Step | File | What it does |
|---|---|---|
| 1 | `load_healthcare_analytics.sql` | Creates the database and a text **staging table**, bulk-loads `KaggleV2-May-2016.csv` with `LOAD DATA LOCAL INFILE`, then converts and verifies the load. Includes an alternative MySQL Workbench Table Data Import Wizard path. |
| 2 | `02_data_cleaning.sql` | Builds the clean, typed `medicalappointment` table from the raw load — one re-runnable definition of every cleaning rule. |
| 3 | `03_data_exploration.sql` | Read-only analysis: overall rate, weekday, lead-time buckets, age groups, SMS effect (crude and controlled), neighbourhood ranking, and patient history windows. |
| 4 | `04_patient_risk_view.sql` | Creates `v_appointment_risk`, a view that scores every appointment into a risk tier, plus preview queries. |

### Schema

The cleaned table `medicalappointment` (110,521 rows) contains:

`PatientId`, `PatientId_raw`, `AppointmentID` (primary key), `Gender`, `ScheduledDay`, `AppointmentDay`, `Age`, `Neighbourhood`, `Scholarship`, `hypertension`, `Diabetes`, `Alcoholism`, `disability_count`, `SMS_received`, `no_show`, `lead_time_days`.

Indexes are defined on `PatientId` and `AppointmentDay`.

### Cleaning rules (`02_data_cleaning.sql`)

1. **Rename** source columns to readable names (`Hipertension` → `hypertension`, `Handcap` → `disability_count`, `No-show` → `no_show`).
2. **Type casting** — IDs to integers, flags to `TINYINT`, `Age` to `INT`.
3. **Timestamp parsing** — ISO strings such as `2016-04-29T18:38:08Z` are parsed with `STR_TO_DATE`; `AppointmentDay` is stored as `DATE` because its time part is always midnight.
4. **Derived field** — `lead_time_days = DATEDIFF(AppointmentDay, ScheduledDay)`, so lead time is a whole number of days.
5. **Excluded** the single impossible `Age = -1` row.
6. **Excluded** 5 rows where the appointment precedes the booking (negative lead time).
7. **Preserved** 5 rows whose `PatientId` arrived in scientific notation (`9377952927E-5`, an Excel export artefact). These cannot be recovered numerically, so `PatientId` is `NULL` and the raw token is kept in `PatientId_raw`.

Result: **110,527 raw rows → 110,521 cleaned rows**, with 0 nulls in analysis columns and 0 duplicate appointment IDs. Verification queries are included at the end of the script.

### Exploration highlights (`03_data_exploration.sql`)

- **Lead time** — bucketed into same day, 1–3 days, 4–7 days and 8+ days; no-show risk rises steeply with each bucket.
- **Weekday** — comparison across appointment days.
- **Age group** — child, teen, young adult, adult, senior; teens are the highest-risk group and seniors the lowest.
- **SMS reminders** — reported twice: crudely, and stratified **within lead-time buckets**. The stratified view exposes a Simpson's-paradox-style confound: SMS is only sent on longer-lead bookings, and once lead time is held constant, SMS recipients miss *fewer* appointments.
- **Neighbourhoods** — ranked with `RANK()` and filtered to areas with at least 100 appointments, so small-sample noise does not top the list.
- **Patient history** — window functions (`COUNT`, `SUM ... ROWS BETWEEN UNBOUNDED PRECEDING AND 1 PRECEDING`) compute each patient's prior appointments and prior no-shows *before* the current visit.

### Risk scoring (`04_patient_risk_view.sql`)

`v_appointment_risk` adds `prior_appointments`, `prior_no_shows`, `prior_no_show_rate` and a `risk_tier`:

| Tier | Rule |
|---|---|
| New Patient – Monitor | No prior appointments |
| High Risk | Prior no-show rate ≥ 0.5 **or** lead time ≥ 8 days |
| Medium Risk | Prior no-show rate ≥ 0.2 **or** lead time 4–7 days |
| Low Risk | Everything else |

Ordering includes `AppointmentID` so a patient with two appointments on the same day gets a deterministic history. The tier distribution and the 50 highest-risk upcoming appointments are included as preview queries.

---

## Repository layout

```
├── Noshow final.twbx            # Packaged Tableau executive dashboard
├── SQL/
│   ├── load_healthcare_analytics.sql
│   ├── 02_data_cleaning.sql
│   ├── 03_data_exploration.sql
│   └── 04_patient_risk_view.sql
├── Data/
│   ├── KaggleV2-May-2016.csv    # Raw source data
│   ├── cleaned_data.csv         # Cleaned output used by Tableau/Python
│   └── v appointment-.csv       # Risk-scored appointment view
├── Python anlaysis/             # Statistical validation (brief below)
└── Advance Python analysis chart/
```

## Python companion (brief)

`Python anlaysis/analysis.py` is a reproducible statistical validation of the SQL findings: chi-square effect sizes, Mantel–Haenszel adjustment for the SMS confound, cluster-robust logistic regression, a patient-grouped train/test split, and comparison of the SQL risk tiers against logistic regression and gradient boosting (AUC 0.594 for the rule-based tiers vs 0.715 and 0.729 for the models). It also quantifies the main limitation of the tier rules: new patients are checked first, so 60% of appointments never receive a lead-time rule. Full methodology and tables are documented in `Python anlaysis/README.md`.

---

## Reproducing the analysis

1. **SQL** — run the four scripts in `SQL/` in order in MySQL. Enable `local_infile` for the bulk load, or use the Workbench import alternative at the bottom of step 1.
2. **Tableau** — open `Noshow final.twbx`; refresh the extract if you want it to reflect a new run.
3. **Python (optional)** — `pip install pandas numpy scipy statsmodels scikit-learn matplotlib`, then:
   ```
   python analysis.py --data cleaned_data.csv --out outputs
   ```

## Data source

[Medical Appointment No Shows](https://www.kaggle.com/datasets/joniarroba/noshowappointments) (Kaggle) — 110,527 appointment records from Brazilian public clinics, May–June 2016.

## Limitations

- Observational data: all results are associations, not causal effects. Neighbourhood, scholarship and SMS may stand in for unmeasured factors such as income, distance and transport.
- About six weeks of appointments, so seasonality cannot be assessed; Saturday has only 39 rows and is not reliable.
- 14.7% of rows share a patient and appointment date, which matters for same-day tie ordering in the history windows.
- The SMS comparison has no observations for lead times under 3 days (a positivity violation), so nothing can be concluded there.
- The risk tiers are rule-based and deliberately simple; a trained model performs better (Python companion).
