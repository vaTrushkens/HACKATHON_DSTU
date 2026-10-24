"""Модели для стохастического слоя: прогноз средних дневных операционных потоков.

Единый интерфейс ``FlowModel.predict(daily, horizon, meta)`` → DataFrame с
колонками ``inflow_mean``/``outflow_mean`` по дням горизонта. Сравниваются в бэктесте:

* ``naive``          — среднее за последние 28 рабочих дней;
* ``seasonal_naive`` — профиль по дням недели (8 недель) × сезонный коэффициент месяца;
* ``hgb``            — глобальный градиентный бустинг на всех компаниях
                       (sklearn HistGradientBoostingRegressor), цель — поток,
                       нормированный на уровень компании.
"""
from __future__ import annotations

from dataclasses import dataclass, field

import joblib
import numpy as np
import pandas as pd
from sklearn.ensemble import HistGradientBoostingRegressor

from ..calendar_ru import is_business_day
from .series import seasonal_month_factors

EPS = 1.0


class FlowModel:
    name = "base"

    def fit(self, panel: pd.DataFrame) -> "FlowModel":  # noqa: D401 — глобальное обучение, по умолчанию не требуется
        return self

    def predict(self, daily: pd.DataFrame, horizon: pd.DatetimeIndex, meta: dict | None = None) -> pd.DataFrame:
        raise NotImplementedError


def _levels(daily: pd.DataFrame) -> dict:
    tail28 = daily.tail(28)
    tail91 = daily.tail(91)
    return {
        "in28": float(tail28.inflow_var.mean()) if len(tail28) else 0.0,
        "out28": float(tail28.outflow_var.mean()) if len(tail28) else 0.0,
        "in91": float(tail91.inflow_var.mean()) if len(tail91) else 0.0,
        "out91": float(tail91.outflow_var.mean()) if len(tail91) else 0.0,
    }


def _frame(horizon: pd.DatetimeIndex, inflow: np.ndarray, outflow: np.ndarray) -> pd.DataFrame:
    bd = np.array([is_business_day(d) for d in horizon])
    return pd.DataFrame({"inflow_mean": np.where(bd, np.maximum(inflow, 0), 0.0), "outflow_mean": np.where(bd, np.maximum(outflow, 0), 0.0), "is_business_day": bd}, index=horizon)


class NaiveMeanModel(FlowModel):
    name = "naive"

    def predict(self, daily, horizon, meta=None):
        lv = _levels(daily)
        n = len(horizon)
        return _frame(horizon, np.full(n, lv["in28"]), np.full(n, lv["out28"]))


class SeasonalNaiveModel(FlowModel):
    name = "seasonal_naive"

    def predict(self, daily, horizon, meta=None):
        recent = daily.tail(40)  # ~8 недель рабочих дней
        lv = _levels(daily)
        dow_in = recent.groupby(recent.index.weekday).inflow_var.mean()
        dow_out = recent.groupby(recent.index.weekday).outflow_var.mean()
        mf_in = seasonal_month_factors(daily, "inflow_var")
        mf_out = seasonal_month_factors(daily, "outflow_var")
        cur_m = daily.index.max().month if len(daily) else horizon[0].month
        inflow, outflow = [], []
        for d in horizon:
            # сезонность относительно текущего месяца, чтобы не задваивать уровень
            si = mf_in.get(d.month, 1.0) / mf_in.get(cur_m, 1.0) if mf_in else 1.0
            so = mf_out.get(d.month, 1.0) / mf_out.get(cur_m, 1.0) if mf_out else 1.0
            inflow.append(dow_in.get(d.weekday(), lv["in28"]) * np.clip(si, 0.3, 3.0))
            outflow.append(dow_out.get(d.weekday(), lv["out28"]) * np.clip(so, 0.3, 3.0))
        return _frame(horizon, np.array(inflow), np.array(outflow))


# ---------------------------------------------------------------------------
# Градиентный бустинг
# ---------------------------------------------------------------------------
FEATURES = [
    "h", "weekday", "dom", "month", "days_to_month_end", "is_q_month",
    "level_in28", "level_out28", "ratio_in", "ratio_out", "dow_factor_in", "dow_factor_out",
    "season_in", "season_out", "zero_share_in", "history_days", "industry", "legal_form",
]
CATEGORICAL = ["industry", "legal_form"]


def make_feature_rows(daily: pd.DataFrame, horizon: pd.DatetimeIndex, meta: dict | None) -> pd.DataFrame:
    meta = meta or {}
    lv = _levels(daily)
    recent = daily.tail(40)
    dow_in = (recent.groupby(recent.index.weekday).inflow_var.mean() / (lv["in28"] + EPS)).to_dict()
    dow_out = (recent.groupby(recent.index.weekday).outflow_var.mean() / (lv["out28"] + EPS)).to_dict()
    mf_in = seasonal_month_factors(daily, "inflow_var")
    mf_out = seasonal_month_factors(daily, "outflow_var")
    cur_m = daily.index.max().month if len(daily) else horizon[0].month
    hist_days = int((daily.index.max() - daily.index.min()).days) if len(daily) else 0
    zero_share = float((recent.inflow_var == 0).mean()) if len(recent) else 1.0
    rows = []
    for i, d in enumerate(horizon, start=1):
        rows.append(
            {
                "date": d,
                "h": int((d - pd.Timestamp(meta.get("as_of", daily.index.max()))).days),
                "weekday": d.weekday(),
                "dom": d.day,
                "month": d.month,
                "days_to_month_end": (d + pd.offsets.MonthEnd(0) - d).days,
                "is_q_month": int(d.month in (1, 4, 7, 10)),
                "level_in28": lv["in28"] / (lv["in91"] + EPS),
                "level_out28": lv["out28"] / (lv["out91"] + EPS),
                "ratio_in": lv["in91"] / (lv["out91"] + EPS),
                "ratio_out": lv["out91"] / (lv["in91"] + EPS),
                "dow_factor_in": dow_in.get(d.weekday(), 1.0),
                "dow_factor_out": dow_out.get(d.weekday(), 1.0),
                "season_in": (mf_in.get(d.month, 1.0) / mf_in.get(cur_m, 1.0)) if mf_in else 1.0,
                "season_out": (mf_out.get(d.month, 1.0) / mf_out.get(cur_m, 1.0)) if mf_out else 1.0,
                "zero_share_in": zero_share,
                "history_days": hist_days,
                "industry": meta.get("industry", "unknown"),
                "legal_form": meta.get("legal_form", "unknown"),
                "_lv_in": lv["in91"],
                "_lv_out": lv["out91"],
            }
        )
    return pd.DataFrame(rows)


@dataclass
class HGBFlowModel(FlowModel):
    name = "hgb"
    model_in: HistGradientBoostingRegressor | None = None
    model_out: HistGradientBoostingRegressor | None = None
    categories: dict[str, list] = field(default_factory=dict)
    fallback: FlowModel = field(default_factory=SeasonalNaiveModel)
    train_info: dict = field(default_factory=dict)

    def _encode(self, X: pd.DataFrame) -> pd.DataFrame:
        X = X[FEATURES].copy()
        for c in CATEGORICAL:
            cats = self.categories.get(c)
            X[c] = pd.Categorical(X[c], categories=cats).codes if cats else 0
        return X

    def fit(self, panel: pd.DataFrame) -> "HGBFlowModel":
        """``panel`` — строки от ``build_training_panel`` с колонками FEATURES + y_in, y_out."""
        self.categories = {c: sorted(panel[c].astype(str).unique().tolist()) for c in CATEGORICAL}
        X = self._encode(panel)
        cat_mask = [c in CATEGORICAL for c in FEATURES]
        params = dict(max_iter=300, learning_rate=0.05, max_leaf_nodes=31, min_samples_leaf=40, l2_regularization=1.0, categorical_features=cat_mask, random_state=42)
        self.model_in = HistGradientBoostingRegressor(**params).fit(X, panel["y_in"])
        self.model_out = HistGradientBoostingRegressor(**params).fit(X, panel["y_out"])
        self.train_info = {"rows": int(len(panel)), "companies": int(panel["company_id"].nunique()) if "company_id" in panel else None, "feature_version": 2}
        return self

    def predict(self, daily, horizon, meta=None):
        if self.model_in is None or len(daily) < 60:
            return self.fallback.predict(daily, horizon, meta)
        rows = make_feature_rows(daily, horizon, meta)
        X = self._encode(rows)
        y_in = np.clip(self.model_in.predict(X), 0, 20) * (rows["_lv_in"].values + EPS)
        y_out = np.clip(self.model_out.predict(X), 0, 20) * (rows["_lv_out"].values + EPS)
        return _frame(horizon, y_in, y_out)

    def save(self, path: str) -> None:
        joblib.dump(self, path)

    @classmethod
    def load(cls, path: str) -> "HGBFlowModel":
        return joblib.load(path)


def build_training_panel(company_daily: dict[str, pd.DataFrame], meta: dict[str, dict], cutoffs_every: int = 14, horizon: int = 45, min_history: int = 120, train_until: pd.Timestamp | None = None) -> pd.DataFrame:
    """Строки обучения: для каждой компании и даты среза — признаки на дату среза
    и фактический поток на каждый рабочий день горизонта (нормированный на уровень)."""
    parts = []
    for cid, daily in company_daily.items():
        if daily.empty:
            continue
        if train_until is not None:
            daily = daily[daily.index <= train_until]
        dates = daily.index
        if len(dates) < min_history + 20:
            continue
        start = dates[0] + pd.Timedelta(days=min_history)
        cutoffs = pd.date_range(start, dates[-1] - pd.Timedelta(days=horizon), freq=f"{cutoffs_every}D")
        for c in cutoffs:
            hist = daily[daily.index <= c]
            fut = daily[(daily.index > c) & (daily.index <= c + pd.Timedelta(days=horizon))]
            if len(fut) < 10:
                continue
            rows = make_feature_rows(hist, fut.index, {**meta.get(cid, {}), "as_of": c})
            rows["y_in"] = np.clip(fut.inflow_var.values / (rows["_lv_in"].values + EPS), 0, 20)
            rows["y_out"] = np.clip(fut.outflow_var.values / (rows["_lv_out"].values + EPS), 0, 20)
            rows["company_id"] = cid
            parts.append(rows)
    return pd.concat(parts, ignore_index=True) if parts else pd.DataFrame()


def get_model(name: str, model_path: str | None = None) -> FlowModel:
    if name == "naive":
        return NaiveMeanModel()
    if name == "seasonal_naive":
        return SeasonalNaiveModel()
    if name == "hgb":
        if model_path:
            try:
                return HGBFlowModel.load(model_path)
            except (FileNotFoundError, OSError):
                pass
        return HGBFlowModel()
    raise ValueError(f"unknown model {name}")
