"""Калибровка вероятности, сценарий задержки для модельной выручки, метрики по горизонтам, дедупликация снапшотов."""
import json
from dataclasses import replace

import numpy as np
import pandas as pd
import pytest
from django.conf import settings

from cashflow_core.backtest import BacktestConfig, evaluate_company, summarize
from cashflow_core.calibration import ProbabilityCalibrator, apply_to_result
from cashflow_core.categorizer import Categorizer
from cashflow_core.forecast import ForecastInputs, NaiveMeanModel, Scenario, run_forecast
from cashflow_core.recurring import detect_recurring
from cashflow_core.risk import assess

from .test_api import client_and_company  # noqa: F401 — фикстура
from .test_core import _synthetic_ops


def categorized(months=14):
    raw = _synthetic_ops(months=months)
    return pd.concat([raw, Categorizer().transform(raw)], axis=1)


def test_tests_run_on_sqlite_regardless_of_env():
    assert "sqlite" in settings.DATABASES["default"]["ENGINE"]


def test_calibrator_maps_overconfident_probabilities_down():
    rng = np.random.default_rng(0)
    raw = rng.uniform(0, 1, 400)
    outcomes = rng.uniform(0, 1, 400) < raw * 0.3  # разрыв случается втрое реже, чем «обещает» сырая вероятность
    cal = ProbabilityCalibrator.fit(raw, outcomes, {"test_end": "2026-03-31"})
    assert cal.info["brier_calibrated"] < cal.info["brier_raw"]
    assert cal(0.9) < 0.6 and 0 <= cal(0.0) <= cal(1.0) <= 1
    assert np.all(np.diff(cal(np.linspace(0, 1, 11))) >= 0)  # монотонно
    with pytest.raises(ValueError):
        ProbabilityCalibrator.fit([0.5] * 40, [True] * 40)


def test_calibrated_probability_reaches_risk_and_backtest():
    ops = categorized()
    as_of = pd.Timestamp("2026-06-30")
    inp = ForecastInputs(ops, as_of, 30, patterns=detect_recurring(ops, as_of), include_taxes=False, planned_events=pd.DataFrame([{"date": as_of + pd.Timedelta(days=3), "amount": 3_000_000.0, "direction": "Списание", "category": "OTHER_OUT", "label": "Крупная покупка", "source": "manual"}]))
    res = run_forecast(inp, NaiveMeanModel(), n_paths=100)
    raw = res.components["prob_gap_any_day"]
    assert raw > 0.5
    cal = ProbabilityCalibrator.fit(np.linspace(0, 1, 60), np.linspace(0, 1, 60) > 0.8)  # «разрыв только при сырой > 80%»
    apply_to_result(res, cal)
    risk = assess(res)
    assert res.components["probability_kind"] == "calibrated" and res.components["prob_gap_any_day_raw"] == raw
    assert risk.probability == pytest.approx(res.components["prob_gap_any_day"], abs=1e-3)
    json.dumps(res.to_dict(), allow_nan=False)
    rows = evaluate_company(ops, {"company_id": "t"}, {"naive": NaiveMeanModel()}, BacktestConfig(n_paths=30), cutoffs=[pd.Timestamp("2026-06-01")], calibrator=cal)
    assert rows and rows[0].gap_prob == pytest.approx(cal(rows[0].extras["raw_gap_prob"]), abs=1e-3)


def test_client_delay_scenario_moves_statistical_inflows():
    rows = []
    for i, month in enumerate(pd.period_range("2025-07", "2026-06", freq="M")):
        for d in pd.bdate_range(month.start_time, month.end_time):
            rows.append({"operation_id": f"{i}-{d.day}", "operation_date": d, "amount": 10_000.0 + (d.day % 3) * 1000, "direction": "Поступление", "category": "REV_CLIENTS", "counterparty_inn": "1234567890", "counterparty_name": "Client", "payment_purpose": "Оплата услуг", "balance_eod": 1e6})
    ops = pd.DataFrame(rows)
    as_of = pd.Timestamp("2026-06-30")
    base = run_forecast(ForecastInputs(ops, as_of, 30, include_taxes=False), NaiveMeanModel(), n_paths=50)
    delayed = run_forecast(ForecastInputs(ops, as_of, 30, include_taxes=False, scenario=Scenario(delay_client_days=14)), NaiveMeanModel(), n_paths=50)
    assert delayed.daily.stoch_in.iloc[:10].sum() == 0  # первые две недели клиенты не платят
    assert delayed.daily.stoch_in.sum() < base.daily.stoch_in.sum()  # хвост ушёл за горизонт
    assert delayed.daily.p50.iloc[9] < base.daily.p50.iloc[9]
    assert delayed.calculation_log[2]["delayed_client_share"] == 1.0
    other = run_forecast(ForecastInputs(ops, as_of, 30, include_taxes=False, scenario=Scenario(delay_client_days=14, delay_client_key="Другой")), NaiveMeanModel(), n_paths=50)
    assert other.daily.stoch_in.sum() == pytest.approx(base.daily.stoch_in.sum())


def test_backtest_reports_short_horizon_errors():
    rows = evaluate_company(categorized(), {"company_id": "t"}, {"naive": NaiveMeanModel()}, BacktestConfig(n_paths=30), cutoffs=[pd.Timestamp("2026-06-01")])
    assert rows[0].extras["mae_norm_h7"] is not None
    by_h = summarize(rows)["models"][0]["by_horizon"]
    assert [b["h"] for b in by_h] == [7, 14, "full"]
    json.dumps(by_h, allow_nan=False)


def test_repeated_forecast_reuses_snapshot(client_and_company):  # noqa: F811
    from apps.forecasting.models import ForecastRun
    from apps.forecasting.services import run
    from apps.operations.services import import_operations
    from apps.planning.models import PlannedEvent
    _, _, company = client_and_company
    import_operations(company, _synthetic_ops(months=6))
    first = run(company, as_of="2025-11-15", horizon=30, model=NaiveMeanModel())["run_id"]
    assert run(company, as_of="2025-11-15", horizon=30, model=NaiveMeanModel())["run_id"] == first
    PlannedEvent.objects.create(company=company, date="2025-11-20", amount=1000, direction="Списание", category="OTHER_OUT", label="x")
    assert run(company, as_of="2025-11-15", horizon=30, model=NaiveMeanModel())["run_id"] != first
    assert ForecastRun.objects.filter(company=company).count() == 2
