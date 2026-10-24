"""Построение дневных рядов потоков из операций."""
from __future__ import annotations

import pandas as pd

from ..calendar_ru import business_days
from ..categories import FINANCING_CODES, FIXED_CODES, VARIABLE_CODES

DIR_IN = "Поступление"


def daily_flows(ops: pd.DataFrame, as_of: pd.Timestamp, start: pd.Timestamp | None = None) -> pd.DataFrame:
    """Дневной ряд по рабочим дням до as_of включительно.

    Колонки: inflow_var/outflow_var (операционные потоки для статистической модели),
    inflow_fix/outflow_fix (регулярные категории), inflow_fin/outflow_fin (финансирование),
    inflow/outflow (всё), balance (остаток на конец дня, forward-fill).
    """
    df = ops[ops.operation_date <= as_of]
    if df.empty:
        return pd.DataFrame()
    start = pd.Timestamp(start or df.operation_date.min()).normalize()
    idx = business_days(start.date(), pd.Timestamp(as_of).date())
    # операции в нерабочий день (бывает в реальных выписках) относим к ближайшему рабочему дню
    out = pd.DataFrame(index=idx)
    is_in = df.direction == DIR_IN
    groups = {
        "inflow_var": is_in & df.category.isin(VARIABLE_CODES),
        "outflow_var": ~is_in & df.category.isin(VARIABLE_CODES),
        "inflow_fix": is_in & df.category.isin(FIXED_CODES),
        "outflow_fix": ~is_in & df.category.isin(FIXED_CODES),
        "inflow_fin": is_in & df.category.isin(FINANCING_CODES),
        "outflow_fin": ~is_in & df.category.isin(FINANCING_CODES),
        "inflow": is_in,
        "outflow": ~is_in,
    }
    for col, mask in groups.items():
        s = df[mask].groupby("operation_date").amount.sum()
        s.index = _snap_to_index(s.index, idx)
        out[col] = s.groupby(level=0).sum().reindex(idx).fillna(0.0)
    bal = df.groupby("operation_date").balance_eod.first()
    bal.index = _snap_to_index(bal.index, idx)
    out["balance"] = bal.groupby(level=0).last().reindex(idx).ffill().bfill()
    out["has_ops"] = df.groupby("operation_date").size().reindex(idx).fillna(0).astype(int)
    out.index.name = "date"
    return out


def _snap_to_index(dates: pd.Index, idx: pd.DatetimeIndex) -> pd.Index:
    if len(idx) == 0:
        return dates
    pos = idx.searchsorted(pd.DatetimeIndex(dates), side="left").clip(0, len(idx) - 1)
    return idx[pos]


def balance_as_of(ops: pd.DataFrame, as_of: pd.Timestamp) -> float:
    df = ops[ops.operation_date <= as_of]
    if df.empty:
        return 0.0
    last = df.operation_date.max()
    return float(df[df.operation_date == last].balance_eod.iloc[0])


def actual_daily(ops: pd.DataFrame, as_of: pd.Timestamp) -> pd.DataFrame:
    """Calendar-day facts: never move weekend operations to a different date."""
    df = ops[ops.operation_date <= as_of]
    if df.empty:
        return pd.DataFrame(columns=['inflow', 'outflow', 'balance'], index=pd.DatetimeIndex([], name='date'))
    idx = pd.date_range(df.operation_date.min(), as_of, name='date')
    out = pd.DataFrame(index=idx)
    for col, direction in [('inflow', 'Поступление'), ('outflow', 'Списание')]:
        out[col] = df[df.direction == direction].groupby('operation_date').amount.sum().reindex(idx).fillna(0.)
    out['balance'] = df.groupby('operation_date').balance_eod.first().reindex(idx).ffill()
    return out


def seasonal_month_factors(daily: pd.DataFrame, col: str) -> dict[int, float]:
    """Отношение среднего дневного потока месяца к среднему за год (если истории ≥ 12 месяцев)."""
    if daily.empty or (daily.index.max() - daily.index.min()).days < 360:
        return {}
    m = daily[col].groupby(daily.index.month).mean()
    overall = daily[col].mean()
    if not overall:
        return {}
    # нижняя граница 0.05: месяц без операций (закрытие точки, сезон) не должен обнулять знаменатель
    return {int(k): max(float(v / overall), 0.05) for k, v in m.items()}
