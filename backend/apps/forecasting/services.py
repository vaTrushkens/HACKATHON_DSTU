"""Сервисы прогноза: сборка входов из БД, запуск движка, сохранение снапшотов, план-факт."""
from __future__ import annotations

import logging
from decimal import Decimal
from functools import lru_cache

import pandas as pd
from django.conf import settings
from django.db import transaction

from apps.companies.models import Company
from apps.operations import services as ops_services
from apps.planning import services as planning_services
from cashflow_core import risk as risk_mod
from cashflow_core.forecast import ForecastInputs, Scenario, get_model, run_forecast
from cashflow_core.forecast.models import FlowModel
from cashflow_core.forecast.series import daily_flows
from cashflow_core.planfact import weekly_plan_fact
from cashflow_core.recurring import counterparty_payment_delays, detect_recurring

from .models import BacktestResult, ForecastPoint, ForecastRun

log = logging.getLogger(__name__)
FLOW_MODEL_PATH = settings.MODELS_DIR / "flow_hgb.joblib"


@lru_cache(maxsize=1)
def get_flow_model() -> FlowModel:
    if FLOW_MODEL_PATH.exists():
        return get_model("hgb", str(FLOW_MODEL_PATH))
    log.info("flow model not trained yet, using seasonal_naive")
    return get_model("seasonal_naive")


def reset_model_cache() -> None:
    get_flow_model.cache_clear()


def resolve_as_of(company: Company, as_of=None) -> pd.Timestamp:
    if as_of:
        return pd.Timestamp(as_of).normalize()
    return pd.Timestamp(company.as_of or pd.Timestamp.today()).normalize()


def build_inputs(company: Company, as_of: pd.Timestamp, horizon: int, scenario: Scenario, min_balance: float, ops: pd.DataFrame | None = None) -> ForecastInputs:
    ops = ops if ops is not None else ops_services.ops_frame(company)
    last_op = ops.operation_date.max() if len(ops) else as_of
    if as_of < last_op - pd.Timedelta(days=7):
        # прогноз «из прошлого» (демо, план-факт): паттерны ищем только по данным до as_of, без утечки будущего
        patterns = detect_recurring(ops[ops.operation_date <= as_of], as_of)
    else:
        patterns = planning_services.core_patterns(company)
    end = as_of + pd.Timedelta(days=horizon)
    planned = planning_services.planned_events_frame(company, as_of, end)
    # события из банка/счетов имеют приоритет над выявленными паттернами той же категории
    if not planned.empty:
        patterns = _dedupe_patterns(patterns, planned)
    return ForecastInputs(
        ops=ops, as_of=as_of, horizon_days=horizon, patterns=patterns, planned_events=planned,
        meta={"industry": company.industry, "legal_form": company.legal_form, "company_id": company.code},
        min_balance=min_balance, scenario=scenario,
    )


def _dedupe_patterns(patterns, planned: pd.DataFrame):
    bank_cats = set(planned[planned.source == "bank"].category.unique())
    return [p for p in patterns if p.category not in bank_cats]


def run(company: Company, as_of=None, horizon: int | None = None, scenario: dict | None = None, min_balance=None, save: bool = True, model: FlowModel | None = None) -> dict:
    as_of = resolve_as_of(company, as_of)
    horizon = horizon or settings.FORECAST_DEFAULT_HORIZON
    sc = Scenario.from_dict(scenario)
    thr = float(min_balance if min_balance is not None else company.min_balance)
    ops = ops_services.ops_frame(company)
    inp = build_inputs(company, as_of, horizon, sc, thr, ops)
    res = run_forecast(inp, model or get_flow_model(), n_paths=settings.FORECAST_N_PATHS)
    delays = counterparty_payment_delays(ops[ops.operation_date <= as_of]) if len(ops) else None
    overdue = planning_services.overdue_events_frame(company)
    assessment = risk_mod.assess(res, overdue_events=overdue if len(overdue) else None, delays=delays)
    payload = {"forecast": res.to_dict(), "risk": assessment.as_dict(), "run_id": None}
    if save:
        payload["run_id"] = _save_run(company, res, assessment, is_baseline=sc.is_base())
    return payload


@transaction.atomic
def _save_run(company: Company, res, assessment, is_baseline: bool) -> int:
    run_obj = ForecastRun.objects.create(
        company=company, as_of=res.as_of.date(), horizon_days=len(res.daily), model_name=res.model_name, is_baseline=is_baseline,
        scenario=res.scenario.as_dict(), min_balance=Decimal(str(res.min_balance)), opening_balance=Decimal(str(round(res.opening_balance, 2))),
        components=res.components, data_quality=res.data_quality, risk=assessment.as_dict(), events=res.to_dict()["events"],
    )
    pts = [ForecastPoint(run=run_obj, date=d.date(), p10=float(r.p10), p50=float(r.p50), p90=float(r.p90), mean=float(r["mean"]), prob_below=float(r.prob_below), det_in=float(r.det_in), det_out=float(r.det_out), stoch_in=float(r.stoch_in), stoch_out=float(r.stoch_out)) for d, r in res.daily.iterrows()]
    ForecastPoint.objects.bulk_create(pts, batch_size=500)
    # храним не больше 20 базовых снапшотов на компанию
    old = ForecastRun.objects.filter(company=company, is_baseline=True).order_by("-created_at")[20:].values_list("id", flat=True)
    if old:
        ForecastRun.objects.filter(id__in=list(old)).delete()
    return run_obj.id


def compare_scenario(company: Company, scenario: dict, as_of=None, horizon: int | None = None, min_balance=None) -> dict:
    base = run(company, as_of, horizon, None, min_balance, save=False)
    alt = run(company, as_of, horizon, scenario, min_balance, save=False)
    b, a = base["risk"], alt["risk"]
    return {
        "base": base,
        "scenario": alt,
        "delta": {
            "probability": round(a["probability"] - b["probability"], 3),
            "deficit_p50": round(a["deficit_p50"] - b["deficit_p50"], 2),
            "gap_date_base": b["gap_date"],
            "gap_date_scenario": a["gap_date"],
            "end_balance_p50": round(alt["forecast"]["components"]["end_balance_p50"] - base["forecast"]["components"]["end_balance_p50"], 2),
        },
    }


def plan_fact(company: Company, run_id: int | None = None, as_of=None) -> dict:
    """Сравнение сохранённого прогноза с фактом. По умолчанию — самый старый базовый снапшот,
    у которого уже есть ≥ 7 дней факта."""
    ops = ops_frame = ops_services.ops_frame(company)
    if ops.empty:
        return {"run": None, "weeks": [], "events": []}
    last_fact = ops.operation_date.max()
    qs = ForecastRun.objects.filter(company=company, is_baseline=True)
    if run_id:
        run_obj = qs.filter(id=run_id).first()
    else:
        run_obj = qs.filter(as_of__lte=(last_fact - pd.Timedelta(days=7)).date()).order_by("-as_of").first()
    if run_obj is None:
        return {"run": None, "weeks": [], "events": [], "detail": "Нет снапшота прогноза с достаточным количеством факта. Сдвиньте дату «сегодня» компании назад или дождитесь новых операций."}
    # сверяем ожидаемые события с фактом по состоянию на последнюю известную операцию
    planning_services.refresh_event_statuses(company, today=last_fact)
    plan = pd.DataFrame(list(run_obj.points.values("date", "p10", "p50", "p90", "det_in", "det_out", "stoch_in", "stoch_out")))
    plan["date"] = pd.to_datetime(plan["date"])
    plan = plan.set_index("date")
    actual = daily_flows(ops_frame, last_fact)
    actual_cal = actual.reindex(pd.date_range(plan.index.min(), min(plan.index.max(), last_fact)))
    actual_cal[["inflow", "outflow"]] = actual_cal[["inflow", "outflow"]].fillna(0.0)
    actual_cal["balance"] = actual_cal.balance.ffill()
    weeks = weekly_plan_fact(plan, actual_cal)
    if not weeks.empty:
        weeks["week_end"] = weeks.week_end.dt.strftime("%Y-%m-%d")
        weeks = weeks.where(pd.notna(weeks), None)
    # события снапшота против факта
    from apps.planning.models import PlannedEvent

    ev_status = list(PlannedEvent.objects.filter(company=company, date__gte=run_obj.as_of).exclude(status="cancelled").values("id", "date", "amount", "direction", "label", "status", "matched_date", "source", "counterparty_name"))
    for e in ev_status:
        e["amount"] = float(e["amount"])
        e["date"] = str(e["date"])
        e["matched_date"] = str(e["matched_date"]) if e["matched_date"] else None
    daily_cmp = plan[["p10", "p50", "p90"]].join(actual_cal[["balance"]]).reset_index().rename(columns={"index": "date"})
    daily_cmp["date"] = daily_cmp["date"].dt.strftime("%Y-%m-%d")
    return {
        "run": {"id": run_obj.id, "as_of": str(run_obj.as_of), "horizon_days": run_obj.horizon_days, "model": run_obj.model_name, "risk": run_obj.risk, "created_at": run_obj.created_at.isoformat()},
        "weeks": weeks.round(2).to_dict(orient="records") if not weeks.empty else [],
        "daily": daily_cmp.round(2).where(pd.notna(daily_cmp), None).to_dict(orient="records"),
        "events": ev_status,
        "fact_until": last_fact.strftime("%Y-%m-%d"),
    }


def latest_backtest() -> dict | None:
    rec = BacktestResult.objects.first()
    if not rec:
        return None
    return {"id": rec.id, "name": rec.name, "created_at": rec.created_at.isoformat(), "config": rec.config, "summary": rec.summary, "categorizer_metrics": rec.categorizer_metrics, "rows_sample": rec.rows[:200]}
