"""Инструменты для будущего LLM-ассистента.

Чат-бот пока не подключён, но архитектура под него готова:

* каждый инструмент — обычная функция ``(company, **kwargs) -> dict`` с JSON-схемой
  аргументов, то есть готовое описание для function calling любого провайдера;
* ассистент получает только структурированные данные из тех же сервисов, что и UI,
  поэтому цифры в ответах бота всегда совпадают с цифрами на экране;
* ``build_context`` собирает компактный контекст компании (остаток, риск, ближайшие
  платежи) для системного промпта.

Подключение провайдера: реализовать ``LLMClient`` в ``llm.py`` по настройкам
``LLM_PROVIDER/LLM_MODEL/LLM_API_KEY`` и вызвать инструменты из ``TOOLS``.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Callable

import pandas as pd

from apps.companies.models import Company
from apps.forecasting import services as fc_services
from apps.operations import services as ops_services
from apps.planning import services as planning_services
from apps.planning.models import PlannedEvent


@dataclass
class Tool:
    name: str
    description: str
    parameters: dict  # JSON Schema
    func: Callable[..., dict]

    def spec(self) -> dict:
        return {"name": self.name, "description": self.description, "parameters": self.parameters}


def get_forecast_summary(company: Company, horizon: int = 60) -> dict:
    data = fc_services.run(company, horizon=horizon, save=False)
    risk = data["risk"]
    comp = data["forecast"]["components"]
    return {
        "as_of": data["forecast"]["as_of"], "opening_balance": comp["opening_balance"], "end_balance_p50": comp["end_balance_p50"], "min_balance_p50": comp["min_balance_p50"],
        "risk_level": risk["level"], "probability": risk["probability"], "gap_date": risk["gap_date"], "deficit_p50": risk["deficit_p50"], "deficit_p10": risk["deficit_p10"],
        "summary": risk["summary"], "explanation": risk["explanation"], "recommendations": risk["recommendations"],
    }


def get_upcoming_payments(company: Company, days: int = 30) -> dict:
    data = fc_services.run(company, horizon=max(days, 30), save=False)
    as_of = pd.Timestamp(data["forecast"]["as_of"])
    ev = [e for e in data["forecast"]["events"] if pd.Timestamp(e["date"]) <= as_of + pd.Timedelta(days=days)]
    return {"events": ev}


def run_what_if(company: Company, scenario: dict) -> dict:
    res = fc_services.compare_scenario(company, scenario)
    return {"delta": res["delta"], "base_risk": res["base"]["risk"]["summary"], "scenario_risk": res["scenario"]["risk"]["summary"]}


def add_planned_event(company: Company, direction: str, amount: float, date: str, label: str, counterparty_name: str = "", probability: float = 1.0) -> dict:
    e = PlannedEvent.objects.create(company=company, kind="manual", direction=direction, amount=amount, date=date, label=label, counterparty_name=counterparty_name, category="OTHER_IN" if direction == "Поступление" else "OTHER_OUT", source="manual", probability=probability)
    return {"id": e.id, "status": "created"}


def get_category_breakdown(company: Company, months: int = 3) -> dict:
    return ops_services.category_summary(company, fc_services.resolve_as_of(company), months)


TOOLS: list[Tool] = [
    Tool("get_forecast_summary", "Прогноз остатка и оценка риска кассового разрыва", {"type": "object", "properties": {"horizon": {"type": "integer", "default": 60}}}, get_forecast_summary),
    Tool("get_upcoming_payments", "Ожидаемые платежи и поступления на ближайшие дни с источником каждого", {"type": "object", "properties": {"days": {"type": "integer", "default": 30}}}, get_upcoming_payments),
    Tool("run_what_if", "Сценарий «что если»: задержка клиента, падение выручки, внеплановый расход", {"type": "object", "properties": {"scenario": {"type": "object"}}, "required": ["scenario"]}, run_what_if),
    Tool("add_planned_event", "Добавить ожидаемое событие («клиент обещал заплатить 300 000 20-го»)", {"type": "object", "properties": {"direction": {"type": "string", "enum": ["Поступление", "Списание"]}, "amount": {"type": "number"}, "date": {"type": "string", "format": "date"}, "label": {"type": "string"}, "counterparty_name": {"type": "string"}, "probability": {"type": "number"}}, "required": ["direction", "amount", "date", "label"]}, add_planned_event),
    Tool("get_category_breakdown", "Структура доходов и расходов по категориям", {"type": "object", "properties": {"months": {"type": "integer", "default": 3}}}, get_category_breakdown),
]
TOOLS_BY_NAME = {t.name: t for t in TOOLS}


def build_context(company: Company) -> dict:
    """Компактный контекст для системного промпта ассистента (без сырых операций)."""
    summary = get_forecast_summary(company)
    upcoming = get_upcoming_payments(company, 14)["events"][:10]
    patterns = planning_services.core_patterns(company)
    return {
        "company": {"name": company.name, "industry": company.industry, "legal_form": company.legal_form, "tax_regime": company.tax_regime},
        "forecast": summary,
        "upcoming_14d": upcoming,
        "recurring": [{"label": p.label, "amount": p.amount, "period": p.period, "day": p.day_of_month} for p in patterns if p.active][:12],
        "tools": [t.spec() for t in TOOLS],
    }
