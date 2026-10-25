"""Калибровка вероятности кассового разрыва.

Монте-Карло даёт «сырую» вероятность: долю траекторий, пробивших порог. На истории
она систематически завышена (в корзине 80–100 % разрыв случался в ~24 % случаев).
Изотоническая регрессия по результатам бэктеста переводит сырую вероятность
в наблюдаемую частоту. Калибровка обучается на периоде ДО тестового, иначе
метрики теста будут оптимистичными.

Если файл калибровки отсутствует, сервис показывает сырую вероятность и помечает это.
"""
from __future__ import annotations

from dataclasses import dataclass, field

import joblib
import numpy as np
from sklearn.isotonic import IsotonicRegression

MIN_SAMPLES = 30


@dataclass
class ProbabilityCalibrator:
    model: IsotonicRegression
    info: dict = field(default_factory=dict)  # период обучения, число срезов, модель потоков

    @classmethod
    def fit(cls, probs, outcomes, info: dict | None = None) -> "ProbabilityCalibrator":
        p = np.asarray(probs, dtype=float)
        y = np.asarray(outcomes, dtype=float)
        if len(p) < MIN_SAMPLES or y.min() == y.max():
            raise ValueError(f"Для калибровки нужно ≥ {MIN_SAMPLES} срезов с разрывами и без них")
        model = IsotonicRegression(y_min=0.0, y_max=1.0, out_of_bounds="clip").fit(p, y)
        brier_raw = float(np.mean((p - y) ** 2))
        brier_cal = float(np.mean((model.predict(p) - y) ** 2))
        return cls(model, {**(info or {}), "samples": int(len(p)), "gap_rate": round(float(y.mean()), 4), "brier_raw": round(brier_raw, 4), "brier_calibrated": round(brier_cal, 4)})

    def __call__(self, p):
        scalar = np.isscalar(p)
        out = self.model.predict(np.atleast_1d(np.asarray(p, dtype=float)))
        return float(out[0]) if scalar else out

    def save(self, path: str) -> None:
        joblib.dump(self, path)

    @classmethod
    def load(cls, path: str) -> "ProbabilityCalibrator":
        return joblib.load(path)


def apply_to_result(res, calibrator: ProbabilityCalibrator) -> None:
    """Заменяет сырые вероятности в результате прогноза калиброванными (на месте)."""
    res.daily["prob_below"] = calibrator(res.daily["prob_below"].values)
    res.components["prob_gap_any_day_raw"] = res.components["prob_gap_any_day"]
    res.components["prob_gap_any_day"] = round(float(calibrator(res.components["prob_gap_any_day"])), 4)
    res.components["probability_kind"] = "calibrated"
    res.components["calibration"] = calibrator.info
    for step in res.calculation_log:
        if step.get("step") == 6:
            step["probability_kind"] = "calibrated"
            step["calibration"] = calibrator.info
