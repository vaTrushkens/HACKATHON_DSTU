"""Трёхступенчатая категоризация: правила → распространение по контрагенту → ML.

Результат — колонки ``category``, ``category_confidence``, ``category_source``
(rule | counterparty | ml | fallback | manual).
"""
from __future__ import annotations

from dataclasses import dataclass

import pandas as pd

from ..categories import fallback_for
from .ml import MLCategorizer
from .rules import rule_category

WEAK_RULE_CONF = 0.75  # ниже этого порога ML может переопределить правило
ML_MIN_PROB = 0.6


def apply_rules(df: pd.DataFrame) -> pd.DataFrame:
    res = [rule_category(d, p, c) for d, p, c in zip(df["direction"], df["payment_purpose"], df["counterparty_name"])]
    out = pd.DataFrame(res, columns=["category", "category_confidence", "category_source"], index=df.index)
    return out


def counterparty_key(df: pd.DataFrame) -> pd.Series:
    """Ключ контрагента: ИНН, а если его нет — нормализованное имя."""
    inn = df["counterparty_inn"].fillna("").astype(str).str.strip()
    name = df["counterparty_name"].fillna("").astype(str).str.lower().str.replace("ё", "е").str.strip()
    return inn.where(inn != "", "name:" + name)


def propagate_by_counterparty(df: pd.DataFrame, cats: pd.DataFrame) -> pd.DataFrame:
    """Если у контрагента ≥80% уверенно размеченных операций в одной категории,
    присваиваем её его неразмеченным операциям (пустое назначение и т.п.)."""
    cats = cats.copy()
    key = counterparty_key(df)
    confident = cats["category_confidence"] >= 0.85
    stats = (
        pd.DataFrame({"key": key[confident], "dir": df["direction"][confident], "cat": cats["category"][confident]})
        .groupby(["key", "dir"])["cat"]
        .agg(lambda s: (s.value_counts().index[0], s.value_counts().iloc[0] / len(s), len(s)))
    )
    if stats.empty:
        return cats
    stats = stats[stats.map(lambda t: t[1] >= 0.8 and t[2] >= 3)]
    lookup = {k: v[0] for k, v in stats.items()}
    weak = cats["category_confidence"] < WEAK_RULE_CONF
    for i in df.index[weak]:
        cat = lookup.get((key[i], df.at[i, "direction"]))
        if cat:
            cats.at[i, "category"] = cat
            cats.at[i, "category_confidence"] = 0.8
            cats.at[i, "category_source"] = "counterparty"
    return cats


@dataclass
class Categorizer:
    ml: MLCategorizer | None = None

    def fit(self, df: pd.DataFrame) -> "Categorizer":
        cats = apply_rules(df)
        self.ml = MLCategorizer().fit(df, cats["category"], cats["category_confidence"])
        return self

    def transform(self, df: pd.DataFrame) -> pd.DataFrame:
        cats = apply_rules(df)
        cats = propagate_by_counterparty(df, cats)
        if self.ml and self.ml.models:
            weak = cats["category_confidence"] < WEAK_RULE_CONF
            if weak.any():
                codes, probs = self.ml.predict(df[weak])
                ok = (probs >= ML_MIN_PROB) & codes.notna()
                idx = codes.index[ok]
                cats.loc[idx, "category"] = codes[ok]
                cats.loc[idx, "category_confidence"] = probs[ok].round(3)
                cats.loc[idx, "category_source"] = "ml"
        # страховка: категория всегда соответствует направлению
        from ..categories import BY_CODE

        wrong_dir = [i for i, (c, d) in enumerate(zip(cats["category"], df["direction"])) if BY_CODE[c].direction != ("in" if d == "Поступление" else "out")]
        if wrong_dir:
            idx = cats.index[wrong_dir]
            cats.loc[idx, "category"] = [fallback_for("in" if d == "Поступление" else "out") for d in df.loc[idx, "direction"]]
            cats.loc[idx, "category_confidence"] = 0.0
            cats.loc[idx, "category_source"] = "fallback"
        return cats

    def metrics(self) -> dict:
        return self.ml.metrics if self.ml else {}


def categorize_frame(df: pd.DataFrame, categorizer: Categorizer | None = None) -> pd.DataFrame:
    """Удобная обёртка: возвращает копию df с колонками категорий."""
    cat = categorizer or Categorizer()
    out = df.copy()
    res = cat.transform(df)
    for c in res.columns:
        out[c] = res[c]
    return out
