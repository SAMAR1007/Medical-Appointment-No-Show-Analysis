#!/usr/bin/env python3
"""
Healthcare appointment no-show analysis (statistical companion to SQL_PORTION.sql)

Usage:
    python analysis.py --data cleaned_data.csv --out outputs

Sections
    1. Data audit
    2. Overall rate + univariate tests (chi-square, Cramer's V, odds ratios, BH-FDR)
    3. Continuous variables (Mann-Whitney U, probability of superiority)
    4. SMS "paradox": confounding by lead time (CMH test, Breslow-Day)
    5. Neighbourhood risk with Wilson CIs + beta-binomial shrinkage
    6. Patient history features (leakage-safe) + SQL risk-tier validation
    7. Adjusted logistic regression (cluster-robust SEs by patient)
    8. Predictive evaluation on a patient-grouped hold-out set
"""
import argparse
import json
import warnings
from pathlib import Path

import numpy as np
import pandas as pd
import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt
import statsmodels.formula.api as smf
from scipy import stats
from sklearn.calibration import calibration_curve
from sklearn.ensemble import HistGradientBoostingClassifier
from sklearn.inspection import permutation_importance
from sklearn.metrics import (average_precision_score, brier_score_loss,
                             roc_auc_score, roc_curve)
from sklearn.model_selection import GroupShuffleSplit
from statsmodels.stats.contingency_tables import StratifiedTable
from statsmodels.stats.multitest import multipletests
from statsmodels.stats.proportion import proportion_confint

warnings.filterwarnings("ignore")
RNG = np.random.default_rng(42)

# ---------- plot style (validated reference palette, light surface) ----------
BLUE, ORANGE = "#2a78d6", "#eb6834"
INK, INK2, MUTED = "#0b0b0b", "#52514e", "#898781"
GRID, AXIS, SURFACE = "#e1e0d9", "#c3c2b7", "#fcfcfb"
plt.rcParams.update({
    "figure.facecolor": SURFACE, "axes.facecolor": SURFACE, "savefig.facecolor": SURFACE,
    "axes.edgecolor": AXIS, "axes.labelcolor": INK2, "xtick.color": MUTED, "ytick.color": MUTED,
    "text.color": INK, "axes.titlecolor": INK, "axes.titlesize": 12, "axes.titleweight": "bold",
    "axes.titlelocation": "left", "axes.spines.top": False, "axes.spines.right": False,
    "axes.grid": True, "grid.color": GRID, "grid.linewidth": 0.7, "axes.axisbelow": True,
    "font.family": "sans-serif", "font.size": 10, "legend.frameon": False,
})

AGE_ORDER = ["Child", "Teen", "Young Adult", "Adult", "Senior"]
LEAD_ORDER = ["Same Day", "Short (1-3 Days)", "Within a week", "Long Lead (8+ days)"]
DAY_ORDER = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday"]
TIER_ORDER = ["Low Risk", "Medium Risk", "High Risk", "New Patient - Monitor"]


# =============================== helpers ===============================
def wilson(k, n):
    lo, hi = proportion_confint(k, n, alpha=0.05, method="wilson")
    return lo, hi


def rate_table(df, col, order=None):
    g = df.groupby(col, observed=True)["y"].agg(n="size", no_shows="sum").reset_index()
    g["rate"] = g["no_shows"] / g["n"]
    g["ci_low"], g["ci_high"] = wilson(g["no_shows"].values, g["n"].values)
    if order is not None:
        g[col] = pd.Categorical(g[col], order, ordered=True)
        g = g.sort_values(col)
    g.insert(0, "factor", col)
    return g.rename(columns={col: "level"})


def chi2_test(df, col):
    ct = pd.crosstab(df[col], df["y"])
    chi2, p, dof, _ = stats.chi2_contingency(ct, correction=False)
    v = np.sqrt(chi2 / (ct.values.sum() * (min(ct.shape) - 1)))
    return chi2, p, dof, v


def odds_ratio(df, col, exposed_value=1):
    e = df[col] == exposed_value
    a, b = ((df["y"] == 1) & e).sum(), ((df["y"] == 0) & e).sum()
    c, d = ((df["y"] == 1) & ~e).sum(), ((df["y"] == 0) & ~e).sum()
    orr = (a * d) / (b * c)
    se = np.sqrt(1 / a + 1 / b + 1 / c + 1 / d)  # Woolf
    return orr, np.exp(np.log(orr) - 1.96 * se), np.exp(np.log(orr) + 1.96 * se)


def mannwhitney(df, col):
    x1 = df.loc[df.y == 1, col].values
    x0 = df.loc[df.y == 0, col].values
    u, p = stats.mannwhitneyu(x1, x0, alternative="two-sided")
    return {"variable": col, "median_no_show": float(np.median(x1)), "median_show": float(np.median(x0)),
            "mean_no_show": float(x1.mean()), "mean_show": float(x0.mean()),
            "U": float(u), "p_value": float(p), "prob_superiority": float(u / (len(x1) * len(x0)))}


def style_axes(ax, pct=True):
    ax.tick_params(length=0)
    if pct:
        ax.yaxis.set_major_formatter(matplotlib.ticker.PercentFormatter(1.0, decimals=0))


def bar_with_ci(ax, tbl, label_col, overall, color=BLUE, rotate=0):
    x = np.arange(len(tbl))
    ax.bar(x, tbl["rate"], color=color, width=0.62)
    ax.errorbar(x, tbl["rate"], yerr=[tbl["rate"] - tbl["ci_low"], tbl["ci_high"] - tbl["rate"]],
                fmt="none", ecolor=INK2, elinewidth=1.2, capsize=3)
    ax.axhline(overall, color=ORANGE, lw=1.4, ls="--")
    ax.set_xticks(x)
    ax.set_xticklabels([f"{v}\nn={n_:,}" for v, n_ in zip(tbl[label_col], tbl["n"])], rotation=rotate,
                       ha="right" if rotate else "center", fontsize=8.5)
    style_axes(ax)
    ax.grid(axis="x", visible=False)


# =============================== load / features ===============================
def load(path):
    df = pd.read_csv(path, dtype={"PatientId": "string"}, parse_dates=["ScheduledDay", "AppointmentDay"])
    # 5 IDs lost precision on export; keep each as its own patient via the raw value
    df["PatientKey"] = df["PatientId"].fillna("raw_" + df["PatientId_raw"].astype(str))
    df["y"] = (df["no_show"] == "Yes").astype(int)
    df["disability_any"] = (df["disability_count"] > 0).astype(int)
    df["weekday"] = pd.Categorical(df["AppointmentDay"].dt.day_name(), DAY_ORDER, ordered=True)
    df["age_group"] = pd.cut(df["Age"], [-1, 12, 19, 39, 59, 200], labels=AGE_ORDER).astype(str)
    df["lead_bucket"] = pd.cut(df["lead_time_days"], [-1, 0, 3, 7, 10_000], labels=LEAD_ORDER).astype(str)
    df["lead_fine"] = pd.cut(df["lead_time_days"], [-1, 0, 2, 6, 13, 29, 10_000],
                             labels=["0", "1-2", "3-6", "7-13", "14-29", "30+"]).astype(str)
    return df


def add_history(df):
    """Prior appointments strictly BEFORE the current appointment date (deterministic, no
    same-day tie ambiguity). Mirrors the SQL window frame but is leakage-safe."""
    daily = (df.groupby(["PatientKey", "AppointmentDay"])["y"].agg(n="size", ns="sum").reset_index()
             .sort_values(["PatientKey", "AppointmentDay"]))
    daily["prior_appointments"] = daily.groupby("PatientKey")["n"].cumsum() - daily["n"]
    daily["prior_no_shows"] = daily.groupby("PatientKey")["ns"].cumsum() - daily["ns"]
    df = df.merge(daily[["PatientKey", "AppointmentDay", "prior_appointments", "prior_no_shows"]],
                  on=["PatientKey", "AppointmentDay"], how="left")
    df["prior_no_show_rate"] = df["prior_no_shows"] / df["prior_appointments"].replace(0, np.nan)
    pr, lead = df["prior_no_show_rate"], df["lead_time_days"]
    df["risk_tier"] = np.select(
        [df["prior_appointments"] == 0, (pr >= 0.5) | (lead >= 8), (pr >= 0.2) | lead.between(4, 7)],
        ["New Patient - Monitor", "High Risk", "Medium Risk"], default="Low Risk")
    df["history_group"] = np.select(
        [df["prior_appointments"] == 0, df["prior_no_shows"] >= 1],
        ["New patient", "Prior no-show"], default="Prior visits, all attended")
    return df


def booking_time_history(df):
    """Stricter history: only appointments whose date is before THIS appointment was booked,
    i.e. what a scheduler could actually know at booking time."""
    n_prior = np.zeros(len(df), dtype=int)
    ns_prior = np.zeros(len(df), dtype=int)
    appt = df["AppointmentDay"].values
    booked = df["ScheduledDay"].dt.normalize().values
    yv_all = df["y"].values
    for _, idx in df.groupby("PatientKey").indices.items():
        if len(idx) == 1:
            continue
        order = np.argsort(appt[idx], kind="stable")
        d = appt[idx][order]
        cy = np.concatenate([[0], np.cumsum(yv_all[idx][order])])
        pos = np.searchsorted(d, booked[idx], side="left")  # appts dated before booking day
        n_prior[idx] = pos
        ns_prior[idx] = cy[pos]
    df["bk_prior_appointments"], df["bk_prior_no_shows"] = n_prior, ns_prior
    df["history_group_bk"] = np.select(
        [n_prior == 0, ns_prior >= 1], ["New patient", "Prior no-show"], default="Prior visits, all attended")
    return df


# =============================== main ===============================
def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--data", default="cleaned_data.csv")
    ap.add_argument("--out", default="outputs")
    args = ap.parse_args()
    out = Path(args.out)
    (out / "figures").mkdir(parents=True, exist_ok=True)
    (out / "tables").mkdir(parents=True, exist_ok=True)

    df0 = load(args.data)
    raw_nulls = int(df0.drop(columns=["PatientId", "PatientKey"]).isna().sum().sum())
    df = booking_time_history(add_history(df0))
    S = {}  # summary dict -> summary.json

    # ---------------- 1. audit ----------------
    n = len(df)
    same_day_ties = df.duplicated(["PatientKey", "AppointmentDay"], keep=False).mean()
    S["audit"] = {
        "rows": int(n), "patients": int(df["PatientKey"].nunique()),
        "appointment_date_min": str(df["AppointmentDay"].min().date()),
        "appointment_date_max": str(df["AppointmentDay"].max().date()),
        "age_min": int(df["Age"].min()), "age_max": int(df["Age"].max()),
        "age_over_100": int((df["Age"] > 100).sum()),
        "lead_min": int(df["lead_time_days"].min()), "lead_max": int(df["lead_time_days"].max()),
        "null_cells": raw_nulls,
        "patient_id_precision_nulls": int(df["PatientId"].isna().sum()),
        "duplicate_appointment_ids": int(df["AppointmentID"].duplicated().sum()),
        "share_rows_same_patient_same_day": float(same_day_ties),
        "share_patients_with_repeat_visits": float((df.groupby("PatientKey").size() > 1).mean()),
        "saturday_rows": int((df["weekday"] == "Saturday").sum()),
    }

    # ---------------- 2. overall + univariate ----------------
    k = int(df["y"].sum())
    lo, hi = wilson(k, n)
    p0 = k / n
    S["overall"] = {"no_show_rate": p0, "ci_low": float(lo), "ci_high": float(hi), "no_shows": k}

    cat_factors = ["Gender", "Scholarship", "hypertension", "Diabetes", "Alcoholism", "disability_any",
                   "SMS_received", "weekday", "age_group", "lead_bucket"]
    rates = pd.concat([rate_table(df, c, {"weekday": DAY_ORDER, "age_group": AGE_ORDER,
                                          "lead_bucket": LEAD_ORDER}.get(c)) for c in cat_factors])
    rates.to_csv(out / "tables/rates_by_factor.csv", index=False)

    rows = []
    for c in cat_factors:
        chi2, p, dof, v = chi2_test(df, c)
        r = {"factor": c, "test": "chi-square", "chi2": chi2, "dof": dof, "p_value": p, "cramers_v": v}
        if df[c].nunique() == 2:
            expo = "M" if c == "Gender" else 1
            r["odds_ratio"], r["or_ci_low"], r["or_ci_high"] = odds_ratio(df, c, expo)
        rows.append(r)
    uni = pd.DataFrame(rows)
    uni["p_adj_BH"] = multipletests(uni["p_value"], method="fdr_bh")[1]
    uni.sort_values("cramers_v", ascending=False).to_csv(out / "tables/univariate_tests.csv", index=False)

    # ---------------- 3. continuous ----------------
    cont = pd.DataFrame([mannwhitney(df, "Age"), mannwhitney(df, "lead_time_days")])
    cont.to_csv(out / "tables/continuous_tests.csv", index=False)
    rho_age = stats.spearmanr(df["Age"], df["y"])[0]
    rho_lead = stats.spearmanr(df["lead_time_days"], df["y"])[0]
    S["spearman"] = {"age": float(rho_age), "lead": float(rho_lead)}

    # ---------------- 4. SMS vs lead time ----------------
    sms_by_lead = (df.groupby("lead_fine")
                   .agg(n=("y", "size"), sms_share=("SMS_received", "mean"), no_show=("y", "mean"))
                   .reindex(["0", "1-2", "3-6", "7-13", "14-29", "30+"]))
    strata, strat_rows = [], []
    for lvl in sms_by_lead.index:
        sub = df[df["lead_fine"] == lvl]
        a = ((sub.SMS_received == 1) & (sub.y == 1)).sum(); b = ((sub.SMS_received == 1) & (sub.y == 0)).sum()
        c_ = ((sub.SMS_received == 0) & (sub.y == 1)).sum(); d = ((sub.SMS_received == 0) & (sub.y == 0)).sum()
        r_sms = a / (a + b) if a + b else np.nan
        r_no = c_ / (c_ + d) if c_ + d else np.nan
        usable = min(a + b, c_ + d) > 0 and (a + c_) > 0 and (b + d) > 0
        strat_rows.append({"lead_days": lvl, "n": len(sub), "sms_share": sub.SMS_received.mean(),
                           "rate_sms": r_sms, "rate_no_sms": r_no, "n_sms": a + b, "n_no_sms": c_ + d,
                           "usable_for_CMH": usable})
        if usable:
            strata.append(np.array([[a, b], [c_, d]]))
    sms_tbl = pd.DataFrame(strat_rows)
    sms_tbl.to_csv(out / "tables/sms_by_lead_time.csv", index=False)
    crude_or = odds_ratio(df, "SMS_received", 1)
    st = StratifiedTable(strata)
    cmh = st.test_null_odds(correction=True)
    bd = st.test_equal_odds(adjust=False)
    S["sms"] = {
        "crude_or": crude_or[0], "crude_ci": [crude_or[1], crude_or[2]],
        "rate_sms": float(df.loc[df.SMS_received == 1, "y"].mean()),
        "rate_no_sms": float(df.loc[df.SMS_received == 0, "y"].mean()),
        "share_sms_when_lead_le_2": float(df.loc[df.lead_time_days <= 2, "SMS_received"].mean()),
        "share_sms_when_lead_ge_3": float(df.loc[df.lead_time_days >= 3, "SMS_received"].mean()),
        "mh_pooled_or": float(st.oddsratio_pooled),
        "mh_ci": [float(x) for x in st.oddsratio_pooled_confint()],
        "cmh_p": float(cmh.pvalue), "breslow_day_p": float(bd.pvalue),
        "strata_used": len(strata),
    }

    # ---------------- 5. neighbourhoods ----------------
    nb = df.groupby("Neighbourhood")["y"].agg(n="size", no_shows="sum").reset_index()
    nb["rate"] = nb["no_shows"] / nb["n"]
    chi2_nb, p_nb, dof_nb, v_nb = chi2_test(df, "Neighbourhood")
    nb = nb[nb["n"] >= 100].copy()
    nb["ci_low"], nb["ci_high"] = wilson(nb["no_shows"].values, nb["n"].values)
    nb["p_vs_overall"] = [stats.binomtest(int(k_), int(n_), p0).pvalue for k_, n_ in zip(nb.no_shows, nb.n)]
    nb["p_adj_BH"] = multipletests(nb["p_vs_overall"], method="fdr_bh")[1]
    nb["significant_BH"] = nb["p_adj_BH"] < 0.05
    # beta-binomial shrinkage (method of moments)
    m = nb["no_shows"].sum() / nb["n"].sum()
    s2 = np.average((nb["rate"] - m) ** 2, weights=nb["n"])
    samp = m * (1 - m) * np.average(1 / nb["n"], weights=nb["n"])
    tau2 = max(s2 - samp, 1e-8)
    ab = max(m * (1 - m) / tau2 - 1, 2)
    a_, b_ = m * ab, (1 - m) * ab
    nb["shrunk_rate"] = (nb["no_shows"] + a_) / (nb["n"] + ab)
    nb = nb.sort_values("shrunk_rate", ascending=False)
    nb["risk_rank"] = np.arange(1, len(nb) + 1)
    nb.to_csv(out / "tables/neighbourhood_risk.csv", index=False)
    S["neighbourhood"] = {"n_neighbourhoods_ge100": int(len(nb)), "chi2": chi2_nb, "p": p_nb, "dof": int(dof_nb),
                          "cramers_v": v_nb, "n_significant_BH": int(nb["significant_BH"].sum()),
                          "prior_strength_alpha_plus_beta": float(ab),
                          "highest": nb.iloc[0][["Neighbourhood", "n", "rate", "shrunk_rate"]].to_dict(),
                          "lowest": nb.iloc[-1][["Neighbourhood", "n", "rate", "shrunk_rate"]].to_dict()}

    # ---------------- 6. SQL risk tiers ----------------
    tiers = rate_table(df, "risk_tier", TIER_ORDER)
    tiers["share_of_appointments"] = tiers["n"] / n
    tiers.to_csv(out / "tables/risk_tier_validation.csv", index=False)
    chi2_t, p_t, dof_t, v_t = chi2_test(df, "risk_tier")
    S["risk_tiers"] = {"chi2": chi2_t, "p": p_t, "cramers_v": v_t,
                       "history_effect": rate_table(df, "history_group").drop(columns="factor").to_dict("records")}
    rate_table(df, "history_group").to_csv(out / "tables/rates_by_history.csv", index=False)
    np_lead = rate_table(df[df["risk_tier"] == "New Patient - Monitor"], "lead_bucket", LEAD_ORDER)
    np_lead.to_csv(out / "tables/new_patient_tier_by_lead.csv", index=False)
    S["new_patient_tier_by_lead"] = [{"lead": str(r.level), "n": int(r.n), "rate": float(r.rate)}
                                     for r in np_lead.itertuples()]
    S["history_known_at_booking_differs_share"] = float((df["history_group"] != df["history_group_bk"]).mean())

    # ---------------- 7. adjusted logistic regression ----------------
    formula = ("y ~ C(age_group, Treatment('Adult')) + C(lead_bucket, Treatment('Same Day')) + "
               "SMS_received + Scholarship + hypertension + Diabetes + Alcoholism + disability_any + "
               "C(Gender, Treatment('F')) + C(weekday, Treatment('Monday')) + "
               "C(history_group, Treatment('New patient'))")
    groups = pd.factorize(df["PatientKey"])[0]
    fit = smf.logit(formula, data=df).fit(disp=0, cov_type="cluster", cov_kwds={"groups": groups})
    ci = fit.conf_int()
    coef = pd.DataFrame({"term": fit.params.index, "odds_ratio": np.exp(fit.params.values),
                         "ci_low": np.exp(ci[0].values), "ci_high": np.exp(ci[1].values),
                         "p_value": fit.pvalues.values}).query("term != 'Intercept'")
    coef["term"] = (coef["term"].str.replace(r"C\((\w+), Treatment\('([^']+)'\)\)\[T\.(.+)\]",
                                              lambda m_: f"{m_.group(1)}: {m_.group(3)} (vs {m_.group(2)})", regex=True))
    coef.to_csv(out / "tables/logit_adjusted_odds_ratios.csv", index=False)
    null = smf.logit("y ~ 1", data=df).fit(disp=0)
    S["logit"] = {"pseudo_r2_mcfadden": float(1 - fit.llf / null.llf), "n": int(fit.nobs),
                  "n_patients_clusters": int(groups.max() + 1)}

    # crude vs adjusted odds ratios (how confounding changes the story)
    cva = []
    for f_, term in [("SMS_received", "SMS_received"), ("Scholarship", "Scholarship"),
                     ("hypertension", "hypertension"), ("Diabetes", "Diabetes"), ("Alcoholism", "Alcoholism"),
                     ("disability_any", "disability_any"), ("Gender", "Gender: M (vs F)")]:
        u = uni.loc[uni.factor == f_].iloc[0]
        a2 = coef.loc[coef.term == term].iloc[0]
        cva.append({"factor": f_, "crude_or": u.odds_ratio, "crude_ci_low": u.or_ci_low, "crude_ci_high": u.or_ci_high,
                    "adjusted_or": a2.odds_ratio, "adj_ci_low": a2.ci_low, "adj_ci_high": a2.ci_high,
                    "adj_p": a2.p_value})
    cva = pd.DataFrame(cva)
    cva.to_csv(out / "tables/crude_vs_adjusted_or.csv", index=False)

    # does neighbourhood still matter after adjustment? (likelihood-ratio test, small groups pooled)
    nb_counts = df["Neighbourhood"].value_counts()
    df["nb_grp"] = np.where(df["Neighbourhood"].map(nb_counts) >= 100, df["Neighbourhood"], "Other")
    base_m = smf.logit(formula, data=df).fit(disp=0)
    full_m = smf.logit(formula + " + C(nb_grp)", data=df).fit(disp=0)
    lr = 2 * (full_m.llf - base_m.llf)
    ddf = int(full_m.df_model - base_m.df_model)
    S["neighbourhood_adjusted"] = {"lr_stat": float(lr), "df": ddf, "p": float(stats.chi2.sf(lr, ddf)),
                                   "pseudo_r2_without": float(1 - base_m.llf / null.llf),
                                   "pseudo_r2_with": float(1 - full_m.llf / null.llf)}

    # ---------------- 8. predictive evaluation ----------------
    gss = GroupShuffleSplit(n_splits=1, test_size=0.2, random_state=42)
    tr_idx, te_idx = next(gss.split(df, groups=df["PatientKey"]))
    tr, te = df.iloc[tr_idx].copy(), df.iloc[te_idx].copy()
    assert set(tr["PatientKey"]).isdisjoint(set(te["PatientKey"]))

    logit_tr = smf.logit(formula, data=tr).fit(disp=0)
    p_logit = logit_tr.predict(te).values

    feats = ["Age", "lead_time_days", "SMS_received", "Scholarship", "hypertension", "Diabetes", "Alcoholism",
             "disability_any", "gender_M", "weekday_num", "prior_appointments", "prior_no_shows",
             "prior_no_show_rate", "Neighbourhood"]
    def gbm_X(d):
        X = d.copy()
        X["gender_M"] = (X["Gender"] == "M").astype(int)
        X["weekday_num"] = X["AppointmentDay"].dt.dayofweek
        X["Neighbourhood"] = pd.Categorical(X["Neighbourhood"], categories=sorted(df["Neighbourhood"].unique()))
        return X[feats]
    gbm = HistGradientBoostingClassifier(max_iter=200, learning_rate=0.06, max_leaf_nodes=15,
                                         l2_regularization=1.0, categorical_features="from_dtype", random_state=42)
    gbm.fit(gbm_X(tr), tr["y"])
    p_gbm = gbm.predict_proba(gbm_X(te))[:, 1]

    # sensitivity: history restricted to what was known when the appointment was booked
    formula_bk = formula.replace("history_group", "history_group_bk")
    p_bk = smf.logit(formula_bk, data=tr).fit(disp=0).predict(te).values
    S["booking_time_sensitivity"] = {"auc_history_as_of_appointment_day": float(roc_auc_score(te["y"].values, p_logit)),
                                     "auc_history_known_at_booking": float(roc_auc_score(te["y"].values, p_bk))}

    tier_rate = tr.groupby("risk_tier")["y"].mean()
    p_tier = te["risk_tier"].map(tier_rate).values
    yte = te["y"].values

    def boot_auc(y, s, B=300):
        idx_all = np.arange(len(y)); out_ = []
        for _ in range(B):
            i = RNG.choice(idx_all, len(y))
            if y[i].min() == y[i].max():
                continue
            out_.append(roc_auc_score(y[i], s[i]))
        return np.percentile(out_, [2.5, 97.5])

    def top_k(y, s, frac):
        k_ = int(len(y) * frac); idx = np.argsort(-s)[:k_]
        return y[idx].mean(), y[idx].sum() / y.sum()

    metrics = []
    for name, s in [("SQL risk tiers (rule-based)", p_tier), ("Logistic regression", p_logit),
                    ("Gradient boosting", p_gbm)]:
        ci_ = boot_auc(yte, s)
        prec10, cap10 = top_k(yte, s, 0.10)
        prec20, cap20 = top_k(yte, s, 0.20)
        metrics.append({"model": name, "roc_auc": roc_auc_score(yte, s), "auc_ci_low": ci_[0], "auc_ci_high": ci_[1],
                        "pr_auc": average_precision_score(yte, s), "brier": brier_score_loss(yte, np.clip(s, 0, 1)),
                        "precision_top10pct": prec10, "lift_top10pct": prec10 / yte.mean(),
                        "recall_top20pct": cap20})
    metrics = pd.DataFrame(metrics)
    metrics.to_csv(out / "tables/model_metrics.csv", index=False)
    S["models"] = {"test_rows": int(len(te)), "test_prevalence": float(yte.mean()),
                   "train_rows": int(len(tr)), "records": metrics.to_dict("records")}

    # decile lift for logistic
    te["p_logit"] = p_logit
    te["decile"] = pd.qcut(te["p_logit"].rank(method="first"), 10, labels=False) + 1
    dec = te.groupby("decile").agg(n=("y", "size"), mean_predicted=("p_logit", "mean"), observed=("y", "mean")).reset_index()
    dec["lift"] = dec["observed"] / yte.mean()
    dec.to_csv(out / "tables/decile_lift_logistic.csv", index=False)

    perm = permutation_importance(gbm, gbm_X(te), yte, scoring="roc_auc", n_repeats=5, random_state=42)
    imp = pd.DataFrame({"feature": feats, "auc_drop": perm.importances_mean,
                        "sd": perm.importances_std}).sort_values("auc_drop", ascending=False)
    imp.to_csv(out / "tables/gbm_permutation_importance.csv", index=False)

    # =============================== figures ===============================
    # Fig 1: rates by key factors
    fig, axes = plt.subplots(2, 2, figsize=(11, 7.5))
    for ax, (col, title, order) in zip(axes.ravel(), [
            ("weekday", "No-show rate by appointment weekday", DAY_ORDER),
            ("age_group", "By age group", AGE_ORDER),
            ("lead_bucket", "By lead time (booking to appointment)", LEAD_ORDER),
            ("history_group", "By patient history", ["New patient", "Prior visits, all attended", "Prior no-show"])]):
        t = rate_table(df, col, order)
        bar_with_ci(ax, t, "level", p0)
        ax.set_title(title)
        ax.set_ylim(0, max(0.45, t["ci_high"].max() * 1.12))
    axes[0, 0].text(0.99, 0.97, "dashed = overall rate; bars = 95% Wilson CI", transform=axes[0, 0].transAxes,
                    ha="right", va="top", fontsize=8, color=INK2)
    fig.suptitle(f"No-show rate varies most with lead time and patient history (overall {p0:.1%})",
                 x=0.01, y=0.985, ha="left", fontsize=13, fontweight="bold")
    fig.tight_layout(rect=[0, 0, 1, 0.955]); fig.savefig(out / "figures/fig1_rates_by_factor.png", dpi=170); plt.close(fig)

    # Fig 2: continuous curves
    fig, axes = plt.subplots(1, 2, figsize=(11, 4.2))
    ab_ = df.assign(b=(df["Age"] // 5 * 5).clip(upper=90)).groupby("b")["y"].agg(["size", "sum"]).reset_index()
    ab_["rate"] = ab_["sum"] / ab_["size"]; ab_["lo"], ab_["hi"] = wilson(ab_["sum"].values, ab_["size"].values)
    axes[0].plot(ab_["b"], ab_["rate"], color=BLUE, lw=2, marker="o", ms=5)
    axes[0].fill_between(ab_["b"], ab_["lo"], ab_["hi"], color=BLUE, alpha=0.15, lw=0)
    axes[0].axhline(p0, color=ORANGE, ls="--", lw=1.3)
    axes[0].set_title("No-show rate by age (5-year bands, 90 = 90+)"); axes[0].set_xlabel("Age"); style_axes(axes[0])
    lb = df.assign(b=pd.cut(df["lead_time_days"], [-1, 0, 2, 5, 9, 14, 21, 30, 45, 60, 180]))
    lb = lb.groupby("b", observed=True)["y"].agg(["size", "sum"]).reset_index()
    lb["rate"] = lb["sum"] / lb["size"]; lb["lo"], lb["hi"] = wilson(lb["sum"].values, lb["size"].values)
    xs = np.arange(len(lb))
    axes[1].plot(xs, lb["rate"], color=BLUE, lw=2, marker="o", ms=5)
    axes[1].fill_between(xs, lb["lo"], lb["hi"], color=BLUE, alpha=0.15, lw=0)
    axes[1].axhline(p0, color=ORANGE, ls="--", lw=1.3)
    labels = ["0", "1-2", "3-5", "6-9", "10-14", "15-21", "22-30", "31-45", "46-60", "61+"]
    axes[1].set_xticks(xs); axes[1].set_xticklabels(labels)
    axes[1].set_title("No-show rate by lead time (days)"); axes[1].set_xlabel("Days between booking and appointment")
    style_axes(axes[1])
    fig.tight_layout(); fig.savefig(out / "figures/fig2_age_and_lead_time_curves.png", dpi=170); plt.close(fig)

    # Fig 3: SMS paradox
    fig, axes = plt.subplots(1, 2, figsize=(11, 4.3))
    x = np.arange(len(sms_tbl)); w = 0.38
    axes[0].bar(x, sms_tbl["sms_share"], color=BLUE, width=0.6)
    axes[0].set_xticks(x); axes[0].set_xticklabels(sms_tbl["lead_days"])
    axes[0].set_title("SMS is only sent when booked well ahead"); axes[0].set_xlabel("Lead time (days)")
    axes[0].set_ylabel("Share of appointments with SMS"); style_axes(axes[0])
    axes[1].bar(x - w / 2, sms_tbl["rate_no_sms"], w, color=BLUE, label="No SMS")
    axes[1].bar(x + w / 2, sms_tbl["rate_sms"], w, color=ORANGE, label="SMS received")
    axes[1].set_xticks(x); axes[1].set_xticklabels(sms_tbl["lead_days"])
    axes[1].set_title("Within the same lead time, SMS vs no SMS"); axes[1].set_xlabel("Lead time (days)")
    axes[1].legend(loc="upper left"); style_axes(axes[1])
    fig.suptitle(f"The SMS 'paradox': crude {S['sms']['rate_sms']:.1%} vs {S['sms']['rate_no_sms']:.1%} is confounded by lead time",
                 x=0.01, ha="left", fontsize=12.5, fontweight="bold")
    fig.tight_layout(rect=[0, 0, 1, 0.93]); fig.savefig(out / "figures/fig3_sms_confounding.png", dpi=170); plt.close(fig)

    # Fig 4: neighbourhoods (top and bottom 10 by shrunk rate)
    sel = pd.concat([nb.head(10), nb.tail(10)]).iloc[::-1]
    fig, ax = plt.subplots(figsize=(8.5, 7))
    y_ = np.arange(len(sel))
    ax.hlines(y_, sel["ci_low"], sel["ci_high"], color=INK2, lw=1.4)
    ax.scatter(sel["rate"], y_, color=BLUE, s=36, zorder=3, label="Observed rate")
    ax.scatter(sel["shrunk_rate"], y_, color=ORANGE, s=36, zorder=3, marker="D", label="Shrunk rate")
    ax.axvline(p0, color=MUTED, ls="--", lw=1.2)
    ax.set_yticks(y_); ax.set_yticklabels([f"{a} (n={b:,})" for a, b in zip(sel["Neighbourhood"], sel["n"])], fontsize=8.5)
    ax.xaxis.set_major_locator(matplotlib.ticker.MultipleLocator(0.04))
    ax.xaxis.set_major_formatter(matplotlib.ticker.PercentFormatter(1.0, decimals=0))
    ax.set_title("Highest and lowest-risk neighbourhoods (n >= 100), 95% Wilson CI"); ax.grid(axis="y", visible=False)
    ax.legend(loc="lower right"); ax.tick_params(length=0)
    fig.tight_layout(); fig.savefig(out / "figures/fig4_neighbourhood_risk.png", dpi=170); plt.close(fig)

    # Fig 5: adjusted ORs
    cf = coef.copy().iloc[::-1]
    fig, ax = plt.subplots(figsize=(9, 7.5))
    y_ = np.arange(len(cf))
    colors = [ORANGE if (p < 0.05 and o > 1) else (BLUE if (p < 0.05 and o < 1) else MUTED)
              for p, o in zip(cf["p_value"], cf["odds_ratio"])]
    ax.hlines(y_, cf["ci_low"], cf["ci_high"], color=INK2, lw=1.3)
    ax.scatter(cf["odds_ratio"], y_, c=colors, s=38, zorder=3)
    ax.axvline(1, color=INK, lw=1)
    ax.set_xscale("log"); ax.set_xticks([0.5, 0.75, 1, 1.5, 2, 3, 5, 10])
    ax.get_xaxis().set_major_formatter(matplotlib.ticker.ScalarFormatter()); ax.minorticks_off()
    ax.set_yticks(y_); ax.set_yticklabels(cf["term"], fontsize=8); ax.grid(axis="y", visible=False)
    ax.set_title("Adjusted odds ratios for no-show")
    ax.text(0.99, 0.02, "orange = raises odds, blue = lowers odds, gray = not significant",
            transform=ax.transAxes, ha="right", va="bottom", fontsize=8.5, color=INK2)
    ax.set_xlabel("Odds ratio, log scale (95% CI, cluster-robust by patient)"); ax.tick_params(length=0)
    fig.tight_layout(); fig.savefig(out / "figures/fig5_adjusted_odds_ratios.png", dpi=170); plt.close(fig)

    # Fig 6: model evaluation
    fig, axes = plt.subplots(1, 3, figsize=(14, 4.4))
    for (name, s), c, ls in zip([("SQL tiers", p_tier), ("Logistic", p_logit), ("Gradient boosting", p_gbm)],
                                [MUTED, BLUE, ORANGE], ["-", "-", "-"]):
        fpr, tpr, _ = roc_curve(yte, s)
        axes[0].plot(fpr, tpr, color=c, lw=2, label=f"{name} (AUC {roc_auc_score(yte, s):.3f})")
    axes[0].plot([0, 1], [0, 1], color=AXIS, ls="--", lw=1)
    axes[0].set_title("ROC curve (held-out patients)"); axes[0].set_xlabel("False positive rate")
    axes[0].set_ylabel("True positive rate"); axes[0].legend(loc="lower right", fontsize=8.5)
    for name, s, c in [("Logistic", p_logit, BLUE), ("Gradient boosting", p_gbm, ORANGE)]:
        fp, mp = calibration_curve(yte, s, n_bins=10, strategy="quantile")
        axes[1].plot(mp, fp, marker="o", color=c, lw=2, ms=5, label=name)
    lim = 0.5
    axes[1].plot([0, lim], [0, lim], color=AXIS, ls="--", lw=1)
    axes[1].set_xlim(0, lim); axes[1].set_ylim(0, lim)
    axes[1].set_title("Calibration"); axes[1].set_xlabel("Mean predicted probability")
    axes[1].set_ylabel("Observed no-show rate"); axes[1].legend(loc="upper left", fontsize=8.5)
    axes[1].xaxis.set_major_formatter(matplotlib.ticker.PercentFormatter(1.0, decimals=0)); style_axes(axes[1])
    axes[2].bar(dec["decile"], dec["observed"], color=BLUE, width=0.65)
    axes[2].axhline(yte.mean(), color=ORANGE, ls="--", lw=1.3)
    axes[2].set_xticks(range(1, 11)); axes[2].set_title("No-show rate by predicted-risk decile")
    axes[2].set_xlabel("Decile of predicted risk (10 = highest)"); style_axes(axes[2]); axes[2].grid(axis="x", visible=False)
    fig.tight_layout(); fig.savefig(out / "figures/fig6_model_evaluation.png", dpi=170); plt.close(fig)

    # Fig 7: SQL risk tiers
    t = tiers.copy()
    fig, ax = plt.subplots(figsize=(7.5, 4.3))
    bar_with_ci(ax, t, "level", p0, rotate=0)
    ax.set_title("Do the SQL risk tiers separate real no-show risk?"); ax.set_ylim(0, max(0.45, t["ci_high"].max() * 1.12))
    fig.tight_layout(); fig.savefig(out / "figures/fig7_sql_risk_tiers.png", dpi=170); plt.close(fig)

    # ---------------- save summary ----------------
    def clean(o):
        if isinstance(o, dict):
            return {str(k_): clean(v) for k_, v in o.items()}
        if isinstance(o, (list, tuple)):
            return [clean(v) for v in o]
        if isinstance(o, (np.integer,)):
            return int(o)
        if isinstance(o, (np.floating, float)):
            return float(o)
        return o
    (out / "summary.json").write_text(json.dumps(clean(S), indent=2, default=str))

    pd.set_option("display.width", 200, "display.max_columns", 30)
    print("== audit =="); print(json.dumps(clean(S["audit"]), indent=1))
    print("== overall =="); print(S["overall"])
    print("== univariate =="); print(uni.sort_values("cramers_v", ascending=False).round(4).to_string(index=False))
    print("== continuous =="); print(cont.round(4).to_string(index=False)); print(S["spearman"])
    print("== sms by lead =="); print(sms_tbl.round(4).to_string(index=False)); print(clean(S["sms"]))
    print("== neighbourhood =="); print(clean(S["neighbourhood"]))
    print(nb.head(8).round(4).to_string(index=False)); print(nb.tail(5).round(4).to_string(index=False))
    print("== tiers =="); print(tiers.round(4).to_string(index=False)); print(clean(S["risk_tiers"]["history_effect"]))
    print("== logit =="); print(coef.round(4).to_string(index=False)); print(S["logit"])
    print("== new-patient tier by lead =="); print(np_lead.round(4).to_string(index=False))
    print("== crude vs adjusted =="); print(cva.round(3).to_string(index=False))
    print("== neighbourhood adjusted =="); print(S["neighbourhood_adjusted"])
    print("== booking-time sensitivity =="); print(S["booking_time_sensitivity"], S["history_known_at_booking_differs_share"])
    print("== models =="); print(metrics.round(4).to_string(index=False))
    print("== deciles =="); print(dec.round(4).to_string(index=False))
    print("== importance =="); print(imp.round(4).to_string(index=False))


if __name__ == "__main__":
    main()
