"""План-факт: сопоставление ожидаемых событий с фактическими операциями
и сравнение сохранённого прогноза с реальностью по неделям."""
from __future__ import annotations

import numpy as np
import pandas as pd

from .categorizer.pipeline import counterparty_key

AMOUNT_TOL = 0.10  # ±10% по сумме
AMOUNT_TOL_BY_CATEGORY = {"SALARY": 0.4, "TAXES": 0.5, "CONTRACTORS": 0.4}  # суммы этих платежей плавают от месяца к месяцу
DATE_TOL_DAYS = 3


def match_events(expected: pd.DataFrame, actual: pd.DataFrame, today: pd.Timestamp) -> pd.DataFrame:
    """Статус каждого ожидаемого события: matched | overdue | pending.

    ``expected``: id, date, amount, direction, category, counterparty_inn, counterparty_name
    ``actual``: операции с category и counterparty_*.
    """
    exp = expected.copy()
    exp["status"] = "pending"
    exp["matched_operation_id"] = None
    exp["matched_amount"] = np.nan
    exp["matched_date"] = pd.NaT
    if exp.empty:
        return exp
    act = actual[actual.operation_date <= pd.Timestamp(today)].copy()
    if act.empty:
        exp.loc[exp.date < today - pd.Timedelta(days=DATE_TOL_DAYS), "status"] = "overdue"
        return exp
    act["cp_key"] = counterparty_key(act)
    act["op_key"] = list(zip(act.operation_date, act.operation_id))
    exp["cp_key"] = counterparty_key(exp.rename(columns={"counterparty_inn": "counterparty_inn", "counterparty_name": "counterparty_name"})) if "counterparty_inn" in exp.columns else ""
    used: set = set()
    for i, e in exp.sort_values("date").iterrows():
        lo, hi = e.date - pd.Timedelta(days=DATE_TOL_DAYS), e.date + pd.Timedelta(days=DATE_TOL_DAYS + 4)
        cand = act[(act.direction == e.direction) & (act.operation_date >= lo) & (act.operation_date <= hi) & ~act.op_key.isin(used)]
        if e.cp_key and not str(e.cp_key).startswith("name:"):
            cand = cand[cand.cp_key == e.cp_key]
        elif e.cp_key:
            cand = cand[cand.cp_key == e.cp_key]
        else:
            cand = cand[cand.category == e.category]
        tol = AMOUNT_TOL_BY_CATEGORY.get(e.category, AMOUNT_TOL)
        cand = cand[(cand.amount - e.amount).abs() <= tol * e.amount]
        if not cand.empty:
            m = cand.iloc[(cand.operation_date - e.date).abs().argsort().iloc[0]]
            used.add(m.op_key)
            exp.at[i, "status"] = "matched"
            exp.at[i, "matched_operation_id"] = m.operation_id
            exp.at[i, "matched_amount"] = float(m.amount)
            exp.at[i, "matched_date"] = m.operation_date
        elif e.date < today - pd.Timedelta(days=DATE_TOL_DAYS):
            exp.at[i, "status"] = "overdue"
    return exp


def weekly_plan_fact(plan_daily: pd.DataFrame, actual_daily: pd.DataFrame) -> pd.DataFrame:
    """План (сохранённый прогноз: p50, det_in+stoch_in, det_out+stoch_out) против факта по неделям."""
    if plan_daily.empty:
        return pd.DataFrame()
    plan = plan_daily.copy()
    plan["plan_in"] = plan.det_in + plan.stoch_in
    plan["plan_out"] = plan.det_out + plan.stoch_out
    plan = plan[["plan_in", "plan_out", "p50", "p10", "p90"]]
    act = actual_daily[["inflow", "outflow", "balance"]].reindex(plan.index) if not actual_daily.empty else pd.DataFrame(index=plan.index, columns=["inflow", "outflow", "balance"])
    merged = plan.join(act)
    merged["has_fact"] = merged.inflow.notna()
    merged[["inflow", "outflow"]] = merged[["inflow", "outflow"]].fillna(0.0)
    merged["balance"] = merged.balance.ffill()
    wk = merged.resample("W-SUN").agg(plan_in=("plan_in", "sum"), plan_out=("plan_out", "sum"), fact_in=("inflow", "sum"), fact_out=("outflow", "sum"), plan_balance=("p50", "last"), fact_balance=("balance", "last"), has_fact=("has_fact", "any"))
    wk["dev_in"] = wk.fact_in - wk.plan_in
    wk["dev_out"] = wk.fact_out - wk.plan_out
    wk["dev_balance"] = wk.fact_balance - wk.plan_balance
    wk.loc[~wk.has_fact, ["fact_in", "fact_out", "fact_balance", "dev_in", "dev_out", "dev_balance"]] = np.nan
    wk.index.name = "week_end"
    return wk.reset_index()
