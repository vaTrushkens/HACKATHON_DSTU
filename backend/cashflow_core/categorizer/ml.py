"""ML-доразметка: символьные n-граммы назначения + контрагент → логистическая регрессия.

Модель обучается на операциях, которые правила разметили с высокой уверенностью,
и применяется там, где правила промолчали или сработало слабое правило.
Отдельная модель на каждое направление: словари поступлений и списаний почти не пересекаются.
"""
from __future__ import annotations

from dataclasses import dataclass, field

import joblib
import numpy as np
import pandas as pd
from sklearn.feature_extraction.text import TfidfVectorizer
from sklearn.linear_model import LogisticRegression
from sklearn.metrics import accuracy_score, f1_score
from sklearn.model_selection import train_test_split
from sklearn.pipeline import Pipeline

from ..categories import IN, OUT
from .rules import normalize_text

# Порог 0.85 (а не 0.9): иначе в обучение не попадают закупки по правилу «за товар/продукцию/…» (0.85),
# и модель выучивает «оплата по счету №» как «Услуги» — ручная разметка показала 13 таких ошибок.
MIN_TRAIN_CONF = 0.85
DIRECTION_VALUES = {IN: "Поступление", OUT: "Списание"}  # значения колонки direction в данных


def _text(df: pd.DataFrame) -> pd.Series:
    inn_kind = df["counterparty_inn"].fillna("").str.len().map({10: "юл", 12: "ип_физ"}).fillna("нет_инн")
    return (
        df["payment_purpose"].fillna("").map(normalize_text)
        + " | "
        + df["counterparty_name"].fillna("").map(normalize_text)
        + " | "
        + inn_kind
    )


def _make_pipeline() -> Pipeline:
    return Pipeline(
        [
            ("tfidf", TfidfVectorizer(analyzer="char_wb", ngram_range=(2, 4), min_df=3, sublinear_tf=True, max_features=200_000)),
            ("clf", LogisticRegression(max_iter=2000, C=5.0, class_weight="balanced")),
        ]
    )


@dataclass
class MLCategorizer:
    models: dict[str, Pipeline] = field(default_factory=dict)
    metrics: dict = field(default_factory=dict)

    def fit(self, df: pd.DataFrame, labels: pd.Series, confidence: pd.Series) -> "MLCategorizer":
        """``labels``/``confidence`` — результат правил; обучаемся только на уверенных строках."""
        for direction in (IN, OUT):
            mask = (df["direction"] == DIRECTION_VALUES[direction]) & (confidence >= MIN_TRAIN_CONF)
            sub = df[mask]
            y = labels[mask]
            if y.nunique() < 2 or len(sub) < 50:
                continue
            X = _text(sub)
            X_tr, X_te, y_tr, y_te = train_test_split(X, y, test_size=0.2, random_state=42, stratify=y)
            pipe = _make_pipeline().fit(X_tr, y_tr)
            pred = pipe.predict(X_te)
            self.metrics[direction] = {
                "train_rows": int(len(X_tr)),
                "test_rows": int(len(X_te)),
                "accuracy": round(float(accuracy_score(y_te, pred)), 4),
                "macro_f1": round(float(f1_score(y_te, pred, average="macro")), 4),
                "classes": sorted(y.unique().tolist()),
            }
            self.models[direction] = pipe
        return self

    def predict(self, df: pd.DataFrame) -> tuple[pd.Series, pd.Series]:
        codes = pd.Series(index=df.index, dtype=object)
        probs = pd.Series(0.0, index=df.index)
        for direction, pipe in self.models.items():
            mask = df["direction"] == DIRECTION_VALUES[direction]
            if not mask.any():
                continue
            P = pipe.predict_proba(_text(df[mask]))
            idx = P.argmax(axis=1)
            codes[mask] = pipe.classes_[idx]
            probs[mask] = P[np.arange(len(idx)), idx]
        return codes, probs

    def save(self, path: str) -> None:
        joblib.dump({"models": self.models, "metrics": self.metrics}, path)

    @classmethod
    def load(cls, path: str) -> "MLCategorizer":
        data = joblib.load(path)
        return cls(models=data["models"], metrics=data.get("metrics", {}))
