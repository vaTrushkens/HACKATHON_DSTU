"""Инструменты LLM-ассистента.

Каждый инструмент — функция ``(company, **kwargs) -> dict`` с JSON-схемой аргументов
(function calling). Ассистент получает только структурированные данные из тех же
сервисов, что и UI, поэтому цифры в ответах бота совпадают с цифрами на экране.
Результаты намеренно компактные: модель небольшая и локальная.
"""
from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from typing import Callable

import pandas as pd
from django.core.cache import cache
from django.db.models import Count, Max

from apps.companies.models import Company
from apps.forecasting import services as fc_services
from apps.operations import services as ops_services
from apps.planning import services as planning_services
from apps.planning.models import PlannedEvent

log = logging.getLogger(__name__)
FORECAST_CACHE_SECONDS = 300
EVENT_FIELDS = ("date", "amount", "direction", "label", "counterparty_name", "source", "probability")


@dataclass
class Tool:
    name: str
    description: str
    parameters: dict  # JSON Schema
    func: Callable[..., dict]

    def spec(self) -> dict:
        return {"name": self.name, "description": self.description, "parameters": self.parameters}


def forecast_data(company: Company, horizon: int = 60) -> dict:
    """Прогноз без сохранения снапшота, с кэшем: один вопрос в чате не должен считать Монте-Карло несколько раз."""
    sig = PlannedEvent.objects.filter(company=company).aggregate(n=Count("id"), m=Max("id"))
    key = f"assistant:forecast:{company.pk}:{horizon}:{company.as_of}:{company.min_balance}:{company.updated_at.timestamp():.0f}:{sig['n']}:{sig['m']}"
    data = cache.get(key)
    if data is None:
        data = fc_services.run(company, horizon=horizon, save=False)
        cache.set(key, data, FORECAST_CACHE_SECONDS)
    return data


def get_forecast_summary(company: Company, horizon: int = 60) -> dict:
    data = forecast_data(company, horizon)
    risk, comp = data["risk"], data["forecast"]["components"]
    return {
        "as_of": data["forecast"]["as_of"], "horizon_days": horizon, "opening_balance": comp["opening_balance"], "end_balance_p50": comp["end_balance_p50"], "min_balance_p50": comp["min_balance_p50"],
        "risk_level": risk["level"], "probability": risk["probability"], "gap_date": risk["gap_date"], "deficit_p50": risk["deficit_p50"], "deficit_p10": risk["deficit_p10"], "runway_days": risk["runway_days"],
        "summary": risk["summary"], "explanation": risk["explanation"],
        "alerts": [{"level": a["level"], "title": a["title"], "message": a["message"]} for a in risk["alerts"][:5]],
        "recommendations": [{"title": r["title"], "text": r["text"]} for r in risk["recommendations"][:4]],
    }


def get_upcoming_payments(company: Company, days: int = 14, direction: str | None = None) -> dict:
    data = forecast_data(company, max(days, 30))
    as_of = pd.Timestamp(data["forecast"]["as_of"])
    ev = [e for e in data["forecast"]["events"] if pd.Timestamp(e["date"]) <= as_of + pd.Timedelta(days=days)]
    if direction in ("Поступление", "Списание"):
        ev = [e for e in ev if e["direction"] == direction]
    ev.sort(key=lambda e: (e["date"], -e["amount"]))
    return {
        "as_of": data["forecast"]["as_of"], "days": days,
        "total_in": round(sum(e["amount"] for e in ev if e["direction"] == "Поступление"), 2),
        "total_out": round(sum(e["amount"] for e in ev if e["direction"] == "Списание"), 2),
        "events": [{k: e.get(k) for k in EVENT_FIELDS} for e in ev[:20]],
    }


def run_what_if(company: Company, revenue_change_pct: float = 0, fixed_cost_change_pct: float = 0, delay_client_days: int = 0, extra_expenses: list | None = None, extra_incomes: list | None = None) -> dict:
    scenario = {"name": "Сценарий ассистента", "revenue_change_pct": revenue_change_pct, "fixed_cost_change_pct": fixed_cost_change_pct, "delay_client_days": delay_client_days, "extra_expenses": extra_expenses or [], "extra_incomes": extra_incomes or []}
    res = fc_services.compare_scenario(company, scenario)
    b, s = res["base"]["risk"], res["scenario"]["risk"]
    pick = lambda r: {"probability": r["probability"], "gap_date": r["gap_date"], "deficit_p50": r["deficit_p50"], "summary": r["summary"]}  # noqa: E731
    return {
        "scenario": scenario,
        "base_without_scenario": pick(b),
        "with_scenario": pick(s),
        "change_due_to_scenario": {"probability_points": round((s["probability"] - b["probability"]) * 100, 1), "deficit_p50_rub": res["delta"]["deficit_p50"], "end_balance_rub": res["delta"]["end_balance_p50"]},
        "note": "probability — абсолютная вероятность разрыва (0–1) в каждом варианте; change_due_to_scenario — разница сценария с базой",
    }


def add_planned_event(company: Company, direction: str, amount: float, date: str, label: str, counterparty_name: str = "", probability: float = 1.0) -> dict:
    e = PlannedEvent.objects.create(company=company, kind="manual", direction=direction, amount=amount, date=date, label=label, counterparty_name=counterparty_name, category="OTHER_IN" if direction == "Поступление" else "OTHER_OUT", source="manual", probability=probability)
    return {"id": e.id, "status": "created", "note": "Событие добавлено в календарь платежей и учтено в прогнозе"}


def get_category_breakdown(company: Company, months: int = 3) -> dict:
    s = ops_services.category_summary(company, fc_services.resolve_as_of(company), months)
    # только среднемесячные суммы: малые модели путают итог за период со значением «в месяц»
    trim = lambda rows: [{"label": r["label"], "monthly_avg": r["monthly_avg"]} for r in rows[:8]]  # noqa: E731
    return {"months": months, "unit": "рублей в месяц, в среднем", "inflow": trim(s.get("inflow") or []), "outflow": trim(s.get("outflow") or [])}


def get_tax_calendar(company: Company, days: int = 90) -> dict:
    cal = planning_services.tax_calendar_for(company, fc_services.resolve_as_of(company), days)
    return {"regime": cal.get("regime"), "events": (cal.get("events") or [])[:10]}


TOOLS: list[Tool] = [
    Tool("get_forecast_summary", "Прогноз остатка на счёте и оценка риска кассового разрыва: дата, сумма нехватки, вероятность, предупреждения и рекомендации.", {"type": "object", "properties": {"horizon": {"type": "integer", "description": "Горизонт в днях: 30, 60 или 90", "default": 60}}}, get_forecast_summary),
    Tool("get_upcoming_payments", "Ожидаемые платежи и поступления на ближайшие дни с датой, суммой и источником (выписка, налоги, счета, ручной ввод).", {"type": "object", "properties": {"days": {"type": "integer", "description": "Сколько дней вперёд", "default": 14}, "direction": {"type": "string", "enum": ["Поступление", "Списание"], "description": "Только поступления или только списания"}}}, get_upcoming_payments),
    Tool("run_what_if", "Сценарий «что если»: как изменится риск разрыва при падении выручки, росте расходов, задержке оплаты клиентом или внеплановом расходе. Вызывай сразу с тем, что назвал пользователь: если клиент или сумма не указаны, задержка применяется ко всем ожидаемым поступлениям от клиентов — ничего уточнять не нужно.", {"type": "object", "properties": {"revenue_change_pct": {"type": "number", "description": "Изменение выручки в процентах, например -20"}, "fixed_cost_change_pct": {"type": "number", "description": "Изменение постоянных расходов в процентах, например 10"}, "delay_client_days": {"type": "integer", "description": "Задержка оплаты всеми клиентами в днях, например 14"}, "extra_expenses": {"type": "array", "description": "Внеплановые расходы", "items": {"type": "object", "properties": {"date": {"type": "string"}, "amount": {"type": "number"}, "label": {"type": "string"}}}}, "extra_incomes": {"type": "array", "description": "Внеплановые поступления", "items": {"type": "object", "properties": {"date": {"type": "string"}, "amount": {"type": "number"}, "label": {"type": "string"}}}}}}, run_what_if),
    Tool("add_planned_event", "Добавить ожидаемое событие в календарь платежей, например «клиент обещал заплатить 300 000 20 июля» или «ремонт 150 000 5 августа».", {"type": "object", "properties": {"direction": {"type": "string", "enum": ["Поступление", "Списание"]}, "amount": {"type": "number"}, "date": {"type": "string", "description": "Дата в формате YYYY-MM-DD"}, "label": {"type": "string"}, "counterparty_name": {"type": "string"}, "probability": {"type": "number", "description": "Вероятность от 0 до 1", "default": 1}}, "required": ["direction", "amount", "date", "label"]}, add_planned_event),
    Tool("get_category_breakdown", "Структура доходов и расходов по категориям в среднем за месяц.", {"type": "object", "properties": {"months": {"type": "integer", "description": "За сколько последних месяцев", "default": 3}}}, get_category_breakdown),
    Tool("get_tax_calendar", "Налоговый календарь: режим налогообложения и ближайшие налоговые платежи с датами и суммами.", {"type": "object", "properties": {"days": {"type": "integer", "default": 90}}}, get_tax_calendar),
]
TOOLS_BY_NAME = {t.name: t for t in TOOLS}


def ollama_specs() -> list[dict]:
    return [{"type": "function", "function": t.spec()} for t in TOOLS]


def _coerce(schema: dict, args: dict) -> dict:
    """Малые модели часто присылают числа строками — приводим к типам из схемы и отбрасываем лишние поля."""
    props = schema.get("properties", {})
    out = {}
    for k, v in (args or {}).items():
        p = props.get(k)
        if p is None or v is None:
            continue
        t = p.get("type")
        try:
            if t == "integer":
                v = int(float(v))
            elif t == "number":
                v = float(v)
            elif t == "string":
                v = str(v)
            elif t == "array" and isinstance(v, str):
                v = json.loads(v)
        except (TypeError, ValueError):
            pass
        if "enum" in p and v not in p["enum"]:
            v = _enum_match(p["enum"], str(v))
            if v is None:
                continue
        out[k] = v
    return out


DIRECTION_WORDS = {"Поступление": ("поступ", "приход", "income", "inflow", "in", "revenue", "доход"), "Списание": ("спис", "расход", "платеж", "платёж", "expense", "outflow", "out", "payment")}


def _enum_match(enum: list, raw: str):
    """«поступления» / «income» / «expense» → значение из enum; иначе None (аргумент отбрасывается)."""
    low = raw.strip().lower()
    for e in enum:
        if low.startswith(e.lower()[:5]) or any(low.startswith(w) for w in DIRECTION_WORDS.get(e, ())):
            return e
    return None


def call(company: Company, name: str, args: dict | None) -> dict:
    tool = TOOLS_BY_NAME.get(name)
    if not tool:
        return {"error": f"неизвестный инструмент {name}", "available": list(TOOLS_BY_NAME)}
    try:
        return tool.func(company, **_coerce(tool.parameters, args or {}))
    except Exception as e:  # noqa: BLE001 — ошибка инструмента должна вернуться модели, а не уронить чат
        log.exception("assistant tool %s failed", name)
        return {"error": f"инструмент {name} не выполнен: {e}"}


def build_context(company: Company) -> dict:
    """Компактный контекст для системного промпта ассистента (без сырых операций)."""
    summary = get_forecast_summary(company)
    upcoming = get_upcoming_payments(company, 7)["events"][:8]
    patterns = planning_services.core_patterns(company)
    return {
        "company": {"name": company.name, "industry": company.industry, "legal_form": company.legal_form, "tax_regime": company.tax_regime},
        "forecast": summary,
        "upcoming_7d": upcoming,
        "recurring": [{"label": p.label, "amount": p.amount, "period": p.period, "day": p.day_of_month} for p in patterns if p.active][:8],
        "tools": [t.spec() for t in TOOLS],
    }
