"""Движок прогноза остатка: детерминированный слой + стохастический слой + Монте-Карло.

Остаток(t) = остаток(as_of) + Σ детерминированных событий + Σ стохастических потоков.

* детерминированный слой — регулярные платежи, налоговый календарь, выставленные
  счета, ручные события (все с источником и уверенностью);
* стохастический слой — операционные поступления и списания, которые даёт
  ``FlowModel``; разброс берётся из эмпирического распределения отношения
  факт/база по истории компании, плюс «уровневый» шок на всю траекторию;
* результат — квантили остатка по дням и вероятность пробить порог.

Финансирование (кредитные транши, займы учредителя) в прогноз не включается —
это «спасательные» деньги, отсутствие которых и есть кассовый разрыв.
"""
from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np
import pandas as pd

from ..calendar_ru import is_business_day
from ..categories import FIXED_CODES
from ..recurring import RecurringPattern, project
from ..taxes import tax_calendar
from .models import FlowModel, SeasonalNaiveModel
from .scenarios import Scenario, apply_to_events
from .series import balance_as_of, daily_flows

EVENT_COLS = ["date", "amount", "direction", "category", "label", "counterparty_name", "source", "confidence", "pattern_key"]
MIN_HISTORY_FOR_MODEL = 60


@dataclass
class ForecastInputs:
    ops: pd.DataFrame  # категоризированные операции компании
    as_of: pd.Timestamp
    horizon_days: int = 30
    patterns: list[RecurringPattern] = field(default_factory=list)
    planned_events: pd.DataFrame | None = None  # счета, договоры, ручной ввод, банк
    meta: dict = field(default_factory=dict)  # industry, legal_form
    min_balance: float = 0.0
    scenario: Scenario = field(default_factory=Scenario)
    include_taxes: bool = True
    industry_profile: dict | None = None  # для компаний с короткой историей


@dataclass
class ForecastResult:
    as_of: pd.Timestamp
    opening_balance: float
    daily: pd.DataFrame  # date, p10, p50, p90, mean, prob_below, det_in, det_out, stoch_in, stoch_out
    events: pd.DataFrame
    paths: np.ndarray  # n_paths × H балансов
    model_name: str
    scenario: Scenario
    min_balance: float
    data_quality: dict
    components: dict

    def to_dict(self, with_paths: bool = False) -> dict:
        d = self.daily.reset_index().rename(columns={"index": "date"})
        d["date"] = d["date"].dt.strftime("%Y-%m-%d")
        ev = self.events.copy()
        if not ev.empty:
            ev["date"] = pd.to_datetime(ev["date"]).dt.strftime("%Y-%m-%d")
        out = {
            "as_of": self.as_of.strftime("%Y-%m-%d"),
            "opening_balance": round(self.opening_balance, 2),
            "model": self.model_name,
            "scenario": self.scenario.as_dict(),
            "min_balance": self.min_balance,
            "daily": d.round(2).to_dict(orient="records"),
            "events": ev.round(2).to_dict(orient="records"),
            "data_quality": self.data_quality,
            "components": self.components,
        }
        if with_paths:
            out["paths"] = np.round(self.paths, 2).tolist()
        return out


def run_forecast(inp: ForecastInputs, model: FlowModel | None = None, n_paths: int = 1000, seed: int = 42) -> ForecastResult:
    model = model or SeasonalNaiveModel()
    rng = np.random.default_rng(seed)
    as_of = pd.Timestamp(inp.as_of).normalize()
    horizon = pd.date_range(as_of + pd.Timedelta(days=1), periods=inp.horizon_days, freq="D")
    end = horizon[-1]
    H = len(horizon)

    ops = inp.ops[inp.ops.operation_date <= as_of]
    opening = balance_as_of(ops, as_of)
    daily = daily_flows(ops, as_of)
    history_days = int((daily.index.max() - daily.index.min()).days) if len(daily) else 0
    quality = _data_quality(ops, daily, as_of, history_days)

    # ---- 1. детерминированный слой -------------------------------------------------
    events = project(inp.patterns, as_of + pd.Timedelta(days=1), end)
    if inp.include_taxes:
        taxes = tax_calendar(ops, as_of, end, inp.meta.get("legal_form"))
        events = pd.concat([events, taxes], ignore_index=True)
    if inp.planned_events is not None and not inp.planned_events.empty:
        pe = inp.planned_events.copy()
        pe["date"] = pd.to_datetime(pe["date"]).dt.normalize()
        for c in EVENT_COLS:
            if c not in pe.columns:
                pe[c] = None
        events = pd.concat([events, pe[EVENT_COLS + [c for c in ("probability", "timing_std_days") if c in pe.columns]]], ignore_index=True)
    events = apply_to_events(events, inp.scenario, as_of, end)
    if "probability" not in events.columns:
        events["probability"] = 1.0
    if "timing_std_days" not in events.columns:
        events["timing_std_days"] = 0.0
    events["probability"] = events["probability"].fillna(1.0).astype(float)
    events["timing_std_days"] = events["timing_std_days"].fillna(0.0).astype(float)
    events["amount"] = events["amount"].astype(float)
    events = events.sort_values("date").reset_index(drop=True)

    # «прочие регулярные расходы»: fixed-категории, не покрытые паттернами/налогами
    fixed_residual_daily = _fixed_residual(ops, daily, events, as_of)
    if inp.scenario.fixed_cost_change_pct:
        fixed_residual_daily *= 1 + inp.scenario.fixed_cost_change_pct / 100

    # ---- 2. стохастический слой ----------------------------------------------------
    if len(daily) >= MIN_HISTORY_FOR_MODEL:
        means = model.predict(daily, horizon, inp.meta)
        model_name = model.name
    else:
        means = _industry_fallback(daily, horizon, inp.industry_profile)
        model_name = "industry_profile"
    rev_mult = 1 + inp.scenario.revenue_change_pct / 100
    mean_in = means.inflow_mean.values * rev_mult
    mean_out = means.outflow_mean.values
    bd = means.is_business_day.values

    ratios_in, ratios_out, level_sigma = _dispersion(daily)

    # ---- 3. Монте-Карло ------------------------------------------------------------
    level_in = np.exp(rng.normal(0, level_sigma, size=(n_paths, 1)))
    level_out = np.exp(rng.normal(0, level_sigma * 0.5, size=(n_paths, 1)))
    r_in = rng.choice(ratios_in, size=(n_paths, H)) if len(ratios_in) else np.ones((n_paths, H))
    r_out = rng.choice(ratios_out, size=(n_paths, H)) if len(ratios_out) else np.ones((n_paths, H))
    stoch_in = mean_in[None, :] * r_in * level_in
    stoch_out = (mean_out[None, :] * r_out * level_out) + fixed_residual_daily * bd[None, :]

    det = np.zeros((n_paths, H))
    det_in_mean = np.zeros(H)
    det_out_mean = np.zeros(H)
    for ev in events.itertuples():
        sign = 1.0 if ev.direction == "Поступление" else -1.0
        base_idx = (pd.Timestamp(ev.date) - horizon[0]).days
        if ev.probability < 1.0 or ev.timing_std_days > 0:
            occurs = rng.random(n_paths) < ev.probability
            jitter = rng.normal(0, ev.timing_std_days, n_paths).round().astype(int) if ev.timing_std_days > 0 else np.zeros(n_paths, dtype=int)
            idx = np.clip(base_idx + np.maximum(jitter, -base_idx), 0, H)  # сдвиг за горизонт = не пришло
            for p in np.where(occurs & (idx < H))[0]:
                det[p, idx[p]] += sign * ev.amount
            exp_idx = min(base_idx, H - 1)
            (det_in_mean if sign > 0 else det_out_mean)[exp_idx] += ev.amount * ev.probability
        else:
            if 0 <= base_idx < H:
                det[:, base_idx] += sign * ev.amount
                (det_in_mean if sign > 0 else det_out_mean)[base_idx] += ev.amount

    net = det + stoch_in - stoch_out
    paths = opening + np.cumsum(net, axis=1)

    q = np.percentile(paths, [10, 50, 90], axis=0)
    out = pd.DataFrame(
        {
            "p10": q[0],
            "p50": q[1],
            "p90": q[2],
            "mean": paths.mean(axis=0),
            "prob_below": (paths < inp.min_balance).mean(axis=0),
            "det_in": det_in_mean,
            "det_out": det_out_mean,
            "stoch_in": stoch_in.mean(axis=0),
            "stoch_out": stoch_out.mean(axis=0),
            "is_business_day": bd,
        },
        index=horizon,
    )
    out.index.name = "date"
    components = {
        "opening_balance": round(opening, 2),
        "det_in_total": round(float(det_in_mean.sum()), 2),
        "det_out_total": round(float(det_out_mean.sum()), 2),
        "stoch_in_total": round(float(stoch_in.mean(axis=0).sum()), 2),
        "stoch_out_total": round(float(stoch_out.mean(axis=0).sum()), 2),
        "fixed_residual_daily": round(float(fixed_residual_daily), 2),
        "level_sigma": round(float(level_sigma), 4),
        "end_balance_p50": round(float(q[1][-1]), 2),
        "min_balance_p50": round(float(q[1].min()), 2),
        "prob_gap_any_day": round(float((paths.min(axis=1) < inp.min_balance).mean()), 4),
    }
    return ForecastResult(as_of, opening, out, events, paths, model_name, inp.scenario, inp.min_balance, quality, components)


# ---------------------------------------------------------------------------
def _dispersion(daily: pd.DataFrame) -> tuple[np.ndarray, np.ndarray, float]:
    """Эмпирические отношения факт/база для бутстрапа и сигма уровневого шока."""
    if daily.empty or len(daily) < 20:
        return np.array([1.0]), np.array([1.0]), 0.25
    tail = daily.tail(182)
    base_in = tail.inflow_var.rolling(20, min_periods=5).mean().shift(1)
    base_out = tail.outflow_var.rolling(20, min_periods=5).mean().shift(1)
    r_in = (tail.inflow_var / base_in.replace(0, np.nan)).dropna().clip(0, 8).values
    r_out = (tail.outflow_var / base_out.replace(0, np.nan)).dropna().clip(0, 8).values
    # нормируем, чтобы среднее отношение было 1 (иначе уровень модели сдвигается)
    r_in = r_in / r_in.mean() if len(r_in) and r_in.mean() > 0 else np.array([1.0])
    r_out = r_out / r_out.mean() if len(r_out) and r_out.mean() > 0 else np.array([1.0])
    # уровневый шок: насколько месяц к месяцу «прыгает» выручка (сезонный тренд
    # отчасти уже учтён моделью, поэтому берём половину разброса приростов)
    monthly = daily.inflow_var.resample("ME").sum()
    monthly = monthly[monthly > 0].tail(12)
    if len(monthly) >= 4:
        mom = np.diff(np.log(monthly.values))
        level_sigma = float(np.clip(np.std(mom) / np.sqrt(2) * 0.5, 0.05, 0.35))
    else:
        level_sigma = 0.25
    return r_in, r_out, level_sigma


def _fixed_residual(ops: pd.DataFrame, daily: pd.DataFrame, events: pd.DataFrame, as_of: pd.Timestamp) -> float:
    """Среднедневной объём регулярных категорий, который не объяснён ни паттернами, ни налогами."""
    if daily.empty:
        return 0.0
    tail = daily.tail(63)  # ~3 месяца рабочих дней
    if len(tail) < 20:
        return 0.0
    fixed_actual_daily = float(tail.outflow_fix.mean())
    cov_days = max(len(tail), 1)
    # проекция паттернов на тот же период в прошлом ≈ сумма событий горизонта, нормированная на день
    ev = events[(events.direction == "Списание") & events.category.isin(FIXED_CODES)]
    horizon_bd = max(int(events.date.nunique()) if not events.empty else 0, 1)
    span_days = (events.date.max() - events.date.min()).days + 1 if not ev.empty else 30
    bd_in_span = max(int(sum(is_business_day(d) for d in pd.date_range(as_of + pd.Timedelta(days=1), periods=max(span_days, 1)))), 1)
    explained_daily = float(ev.amount.sum() / bd_in_span) if not ev.empty else 0.0
    del cov_days, horizon_bd
    return max(fixed_actual_daily - explained_daily, 0.0)


def _industry_fallback(daily: pd.DataFrame, horizon: pd.DatetimeIndex, profile: dict | None) -> pd.DataFrame:
    """Короткая история: масштабируем отраслевой профиль на фактический оборот компании."""
    bd = np.array([is_business_day(d) for d in horizon])
    if daily.empty:
        return pd.DataFrame({"inflow_mean": np.zeros(len(horizon)), "outflow_mean": np.zeros(len(horizon)), "is_business_day": bd}, index=horizon)
    lv_in = float(daily.inflow_var.mean())
    lv_out = float(daily.outflow_var.mean())
    dow_in = (profile or {}).get("dow_in", {})
    dow_out = (profile or {}).get("dow_out", {})
    inflow = np.array([lv_in * dow_in.get(d.weekday(), 1.0) for d in horizon])
    outflow = np.array([lv_out * dow_out.get(d.weekday(), 1.0) for d in horizon])
    return pd.DataFrame({"inflow_mean": np.where(bd, inflow, 0), "outflow_mean": np.where(bd, outflow, 0), "is_business_day": bd}, index=horizon)


def _data_quality(ops: pd.DataFrame, daily: pd.DataFrame, as_of: pd.Timestamp, history_days: int) -> dict:
    last_op = ops.operation_date.max() if len(ops) else None
    stale_days = int((as_of - last_op).days) if last_op is not None else None
    level = "ok"
    notes = []
    if history_days < 90:
        level = "low"
        notes.append("Истории меньше 3 месяцев — прогноз опирается на отраслевой профиль, коридор широкий")
    elif history_days < 365:
        level = "medium"
        notes.append("Истории меньше года — сезонность оценена приближённо")
    if stale_days is not None and stale_days > 14:
        notes.append(f"Последняя операция {stale_days} дн. назад — выписка может быть неполной")
        level = "low" if stale_days > 45 else level
    if len(daily) and (daily.has_ops == 0).tail(60).mean() > 0.5:
        notes.append("Много дней без операций — возможно, счёт используется не как основной")
    return {"level": level, "history_days": history_days, "stale_days": stale_days, "notes": notes}
