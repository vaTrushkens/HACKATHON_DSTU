"""Rolling-origin бэктест прогноза остатка и детекции кассовых разрывов.

Для каждой компании и даты среза прогноз сравнивается с реальным остатком.
Опциональный actual_mode=without_financing вычитает финансирование после
среза, чтобы отдельно проверить риск при отсутствии экстренных вливаний.
Модель и признаки не должны использовать данные после даты среза.
"""
from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np
import pandas as pd

from .categories import FINANCING_CODES
from .forecast.engine import ForecastInputs, run_forecast
from .forecast.models import FlowModel
from .recurring import detect_recurring
from .categorizer.pipeline import categorize_frame, apply_rules

DIR_IN = "Поступление"


@dataclass
class BacktestConfig:
    horizon: int = 30
    cutoffs_every_days: int = 14
    test_start: str = "2026-03-01"
    test_end: str = "2026-08-31"
    min_history_days: int = 120
    n_paths: int = 300
    gap_prob_threshold: float = 0.5
    min_balance: float = 0.0
    actual_mode: str = "actual"

    def __post_init__(self):
        if not 1 <= self.horizon <= 180 or not 1 <= self.cutoffs_every_days <= 365 or not 1 <= self.n_paths <= 10000:
            raise ValueError("Некорректные параметры бэктеста")
        if pd.Timestamp(self.test_start) > pd.Timestamp(self.test_end):
            raise ValueError("Начало периода позже окончания")
        if self.actual_mode not in ("actual", "without_financing"):
            raise ValueError("Неизвестный способ расчёта факта")


@dataclass
class BacktestRow:
    company_id: str
    industry: str
    model: str
    cutoff: str
    mae_balance: float
    mae_norm: float  # MAE / среднемесячный оборот
    mase: float | None  # MAE / среднее абсолютное дневное изменение в истории
    coverage80: float
    end_err_norm: float
    actual_gap: bool
    predicted_gap: bool
    gap_prob: float
    actual_gap_date: str | None
    predicted_gap_date: str | None
    lead_days: int | None
    mse_balance: float = 0.0
    naive_mae_ratio: float | None = None
    extras: dict = field(default_factory=dict)


def counterfactual_actual(ops: pd.DataFrame, cutoff: pd.Timestamp, horizon_idx: pd.DatetimeIndex) -> pd.Series:
    """Фактический остаток по календарным дням горизонта минус финансирование после среза."""
    after = ops[(ops.operation_date > cutoff) & (ops.operation_date <= horizon_idx[-1])]
    bal = ops[ops.operation_date <= horizon_idx[-1]].groupby("operation_date").balance_eod.first()
    bal = bal.reindex(pd.date_range(ops.operation_date.min(), horizon_idx[-1])).ffill()
    fin = after[after.category.isin(FINANCING_CODES)]
    fin_signed = pd.Series(np.where(fin.direction == DIR_IN, fin.amount, -fin.amount), index=fin.operation_date).groupby(level=0).sum()
    fin_cum = fin_signed.reindex(horizon_idx).fillna(0.0).cumsum()
    return bal.reindex(horizon_idx) - fin_cum


def evaluate_company(ops: pd.DataFrame, meta: dict, models: dict[str, FlowModel], cfg: BacktestConfig, cutoffs: list[pd.Timestamp] | None = None) -> list[BacktestRow]:
    rows: list[BacktestRow] = []
    if ops.empty:
        return rows
    first, last = ops.operation_date.min(), ops.operation_date.max()
    if cutoffs is None:
        start = max(pd.Timestamp(cfg.test_start), first + pd.Timedelta(days=cfg.min_history_days))
        end = min(pd.Timestamp(cfg.test_end), last - pd.Timedelta(days=cfg.horizon))
        if start > end:
            return rows
        cutoffs = list(pd.date_range(start, end, freq=f"{cfg.cutoffs_every_days}D"))
    if not cutoffs:
        return rows
    for model in models.values():
        if model.name == "hgb":
            info = getattr(model, "train_info", {})
            until = info.get("train_until")
            if info.get("feature_version") != 2 or not until or pd.Timestamp(until) > min(cutoffs):
                raise ValueError("HGB должна быть переобучена с feature_version=2 и train_until не позже первого среза")
    # Future operations are classified by rules only; no propagation from the future
    # to historical features and no categorizer trained on the evaluation period.
    truth = ops.copy()
    truth["category"] = apply_rules(truth).category
    for cutoff in cutoffs:
        hist = categorize_frame(ops[ops.operation_date <= cutoff])
        if len(hist) < 30 or (cutoff - hist.operation_date.min()).days < cfg.min_history_days:
            continue
        horizon_idx = pd.date_range(cutoff + pd.Timedelta(days=1), periods=cfg.horizon)
        if last < horizon_idx[-1]:  # нужен факт на весь горизонт
            continue
        if cfg.actual_mode == "without_financing":
            actual = counterfactual_actual(truth, cutoff, horizon_idx)
        else:
            actual = truth.groupby("operation_date").balance_eod.first().reindex(pd.date_range(first, horizon_idx[-1])).ffill().reindex(horizon_idx)
        if actual.isna().all():
            continue
        actual = actual.ffill().bfill()
        recent = hist[hist.operation_date > cutoff - pd.Timedelta(days=91)]
        monthly_turnover = float(recent.amount.sum() / 3) if len(recent) else 1.0
        monthly_turnover = max(monthly_turnover, 1.0)
        opening = float(hist[hist.operation_date == hist.operation_date.max()].balance_eod.iloc[0])
        naive_mae = float(np.abs(actual.values - opening).mean())
        historical_balance = hist.groupby("operation_date").balance_eod.first().reindex(pd.date_range(first, cutoff)).ffill()
        scale = float(historical_balance.diff().abs().mean())
        actual_gap_mask = actual < cfg.min_balance
        actual_gap = bool(actual_gap_mask.any())
        actual_gap_date = actual.index[actual_gap_mask.values][0] if actual_gap else None
        patterns = detect_recurring(hist, cutoff)
        for name, model in models.items():
            inp = ForecastInputs(ops=hist, as_of=cutoff, horizon_days=cfg.horizon, patterns=patterns, meta=meta, min_balance=cfg.min_balance)
            res = run_forecast(inp, model, n_paths=cfg.n_paths)
            p50 = res.daily.p50.values
            err = np.abs(actual.values - p50)
            mae = float(err.mean())
            cover = float(((actual.values >= res.daily.p10.values) & (actual.values <= res.daily.p90.values)).mean())
            gap_prob = float(res.components["prob_gap_any_day"])
            predicted_gap = gap_prob >= cfg.gap_prob_threshold
            pred_dates = res.daily.index[res.daily.prob_below >= 0.5]
            pred_date = pred_dates[0] if len(pred_dates) else None
            lead = int((actual_gap_date - cutoff).days) if (actual_gap and predicted_gap) else None
            rows.append(
                BacktestRow(
                    company_id=meta.get("company_id", ""),
                    industry=meta.get("industry", ""),
                    model=name,
                    cutoff=str(cutoff.date()),
                    mae_balance=round(mae, 2),
                    mae_norm=round(mae / monthly_turnover, 4),
                    mase=round(mae / scale, 4) if scale > 0 else None,
                    coverage80=round(cover, 3),
                    end_err_norm=round(float(abs(actual.values[-1] - p50[-1]) / monthly_turnover), 4),
                    actual_gap=actual_gap,
                    predicted_gap=bool(predicted_gap),
                    gap_prob=round(gap_prob, 3),
                    actual_gap_date=str(actual_gap_date.date()) if actual_gap_date is not None else None,
                    predicted_gap_date=str(pred_date.date()) if pred_date is not None else None,
                    lead_days=lead,
                    mse_balance=round(float(np.square(actual.values - p50).mean()), 2),
                    naive_mae_ratio=round(mae / naive_mae, 4) if naive_mae > 0 else None,
                    extras={"date_err_days": int((pred_date - actual_gap_date).days) if (pred_date is not None and actual_gap_date is not None) else None},
                )
            )
    return rows


def summarize(rows: list[BacktestRow]) -> dict:
    if not rows:
        return {"models": [], "by_industry": [], "n_forecasts": 0, "n_companies": 0, "cutoffs": [], "horizon": None, "actual_gap_rate": None}
    df = pd.DataFrame([r.__dict__ for r in rows])
    df["date_err_days"] = df.extras.map(lambda e: e.get("date_err_days"))
    out_models = []
    for m, g in df.groupby("model"):
        tp = int((g.actual_gap & g.predicted_gap).sum())
        fp = int((~g.actual_gap & g.predicted_gap).sum())
        fn = int((g.actual_gap & ~g.predicted_gap).sum())
        tn = int((~g.actual_gap & ~g.predicted_gap).sum())
        precision = tp / (tp + fp) if tp + fp else None
        recall = tp / (tp + fn) if tp + fn else None
        # калибровка вероятности разрыва: бины
        bins = pd.cut(g.gap_prob, [-0.01, 0.2, 0.4, 0.6, 0.8, 1.01], labels=["0–20%", "20–40%", "40–60%", "60–80%", "80–100%"])
        calib = g.groupby(bins, observed=True).actual_gap.agg(["mean", "size"]).reset_index()
        out_models.append(
            {
                "model": m,
                "n": int(len(g)),
                "mae_balance": round(float(g.mae_balance.mean()), 2),
                "mse_balance": round(float(g.mse_balance.mean()), 2),
                "rmse_balance": round(float(np.sqrt(g.mse_balance.mean())), 2),
                "gap_accuracy": round((tp + tn) / len(g), 4),
                "brier_score": round(float(np.square(g.gap_prob - g.actual_gap.astype(float)).mean()), 4),
                "mae_norm_median": round(float(g.mae_norm.median()), 4),
                "mae_norm_mean": round(float(g.mae_norm.mean()), 4),
                "mase_median": round(float(g.mase.dropna().median()), 4) if g.mase.notna().any() else None,
                "naive_mae_ratio_median": round(float(g.naive_mae_ratio.dropna().median()), 4) if g.naive_mae_ratio.notna().any() else None,
                "coverage80": round(float(g.coverage80.mean()), 3),
                "end_err_norm_median": round(float(g.end_err_norm.median()), 4),
                "gap_precision": round(precision, 3) if precision is not None else None,
                "gap_recall": round(recall, 3) if recall is not None else None,
                "gap_f1": round(2 * tp / (2 * tp + fp + fn), 3) if 2 * tp + fp + fn else None,
                "confusion": {"tp": tp, "fp": fp, "fn": fn, "tn": tn},
                "lead_days_mean": round(float(g.lead_days.dropna().mean()), 1) if g.lead_days.notna().any() else None,
                "date_err_days_mae": round(float(g.date_err_days.dropna().abs().mean()), 1) if g.date_err_days.notna().any() else None,
                "calibration": [{"bin": str(r.gap_prob), "actual_rate": round(float(r["mean"]), 3), "n": int(r["size"])} for _, r in calib.iterrows()],
            }
        )
    by_ind = (
        df.groupby(["industry", "model"]).agg(n=("mae_norm", "size"), mae_norm=("mae_norm", "median"), mase=("mase", "median"), coverage80=("coverage80", "mean"), gap_rate=("actual_gap", "mean")).reset_index().round(4)
    )
    return {
        "n_forecasts": int(len(df)),
        "n_companies": int(df.company_id.nunique()),
        "cutoffs": sorted(df.cutoff.unique().tolist()),
        "horizon": None,
        "models": out_models,
        "by_industry": by_ind.astype(object).where(pd.notna(by_ind), None).to_dict(orient="records"),
        "actual_gap_rate": round(float(df.drop_duplicates(["company_id", "cutoff"]).actual_gap.mean()), 3),
    }
