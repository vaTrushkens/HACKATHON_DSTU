"""Сервисы планирования: синхронизация регулярных паттернов, плановые события, план-факт."""
from __future__ import annotations

import logging
from decimal import Decimal

import pandas as pd
from django.db import transaction

from apps.companies.models import Company
from apps.operations import services as ops_services
from cashflow_core import planfact
from cashflow_core.recurring import RecurringPattern as CorePattern
from cashflow_core.recurring import counterparty_payment_delays, detect_recurring
from cashflow_core.taxes import detect_regime, tax_calendar

from .integrations import BankAdapter, RegistryAdapter
from .models import CounterpartyCheck, PlannedEvent, RecurringPattern

log = logging.getLogger(__name__)
EVENT_COLS = ["date", "amount", "direction", "category", "label", "counterparty_name", "source", "confidence", "pattern_key", "probability", "timing_std_days"]


@transaction.atomic
def sync_recurring_patterns(company: Company, as_of=None) -> int:
    ops = ops_services.ops_frame(company)
    if ops.empty:
        return 0
    as_of = pd.Timestamp(as_of or company.as_of or ops.operation_date.max())
    found = detect_recurring(ops, as_of)
    existing = {p.key: p for p in RecurringPattern.objects.filter(company=company)}
    seen = set()
    for p in found:
        seen.add(p.key)
        row = existing.get(p.key)
        fields = dict(
            category=p.category, direction=p.direction, counterparty_name=p.counterparty_name[:255], counterparty_inn=p.counterparty_inn[:12], subtype=p.subtype, label=p.label[:255],
            period=p.period, day_of_month=p.day_of_month, weekday=p.weekday, amount=Decimal(str(p.amount)), amount_cv=p.amount_cv, occurrences=p.occurrences,
            first_date=p.first_date, last_date=p.last_date, confidence=p.confidence, active=p.active, history=p.history,
        )
        if row:
            for k, v in fields.items():
                setattr(row, k, v)
            row.save()
        else:
            RecurringPattern.objects.create(company=company, key=p.key, **fields)
    RecurringPattern.objects.filter(company=company).exclude(key__in=seen).update(active=False)
    regime = detect_regime(ops[ops.operation_date <= as_of], company.legal_form)
    Company.objects.filter(pk=company.pk).update(tax_regime=regime["regime"][:128])
    return len(found)


def core_patterns(company: Company) -> list[CorePattern]:
    out = []
    for r in RecurringPattern.objects.filter(company=company, user_disabled=False):
        out.append(
            CorePattern(
                key=r.key, category=r.category, direction=r.direction, counterparty_name=r.counterparty_name, counterparty_inn=r.counterparty_inn, subtype=r.subtype, period=r.period,
                day_of_month=r.day_of_month, weekday=r.weekday, amount=float(r.amount), amount_cv=r.amount_cv, occurrences=r.occurrences, last_date=r.last_date, first_date=r.first_date,
                confidence=r.confidence, active=r.active, label=r.label, history=r.history,
            )
        )
    return out


def planned_events_frame(company: Company, as_of: pd.Timestamp, end: pd.Timestamp) -> pd.DataFrame:
    """Ожидаемые события для движка прогноза (только не исполненные и не отменённые)."""
    qs = PlannedEvent.objects.filter(company=company, status__in=["expected", "overdue"], date__gt=as_of.date(), date__lte=end.date())
    rows = []
    for e in qs:
        rows.append(
            {
                "date": pd.Timestamp(e.date), "amount": float(e.amount), "direction": e.direction, "category": e.category, "label": e.label, "counterparty_name": e.counterparty_name,
                "source": e.source, "confidence": e.probability, "pattern_key": f"event:{e.id}", "probability": e.probability, "timing_std_days": e.timing_std_days,
            }
        )
    # просроченные ожидаемые поступления переносим «на завтра» с пониженной вероятностью: деньги ещё могут прийти
    for e in PlannedEvent.objects.filter(company=company, status="overdue", date__lte=as_of.date(), direction="Поступление"):
        rows.append(
            {
                "date": as_of + pd.Timedelta(days=5), "amount": float(e.amount), "direction": e.direction, "category": e.category, "label": e.label + " (просрочено)", "counterparty_name": e.counterparty_name,
                "source": e.source, "confidence": 0.5, "pattern_key": f"event:{e.id}", "probability": min(e.probability, 0.5), "timing_std_days": max(e.timing_std_days, 5.0),
            }
        )
    return pd.DataFrame(rows, columns=EVENT_COLS)


@transaction.atomic
def refresh_event_statuses(company: Company, today=None) -> dict:
    """План-факт по событиям: сопоставляет ожидаемые события с фактическими операциями."""
    ops = ops_services.ops_frame(company)
    today = pd.Timestamp(today or company.as_of or (ops.operation_date.max() if len(ops) else pd.Timestamp.today()))
    qs = list(PlannedEvent.objects.filter(company=company, status__in=["expected", "overdue"]))
    if not qs:
        return {"matched": 0, "overdue": 0}
    expected = pd.DataFrame(
        [{"id": e.id, "date": pd.Timestamp(e.date), "amount": float(e.amount), "direction": e.direction, "category": e.category, "counterparty_inn": e.counterparty_inn, "counterparty_name": e.counterparty_name} for e in qs]
    )
    actual = ops[ops.operation_date > expected.date.min() - pd.Timedelta(days=7)] if len(ops) else ops
    res = planfact.match_events(expected, actual, today)
    by_id = {e.id: e for e in qs}
    counts = {"matched": 0, "overdue": 0}
    for r in res.itertuples():
        e = by_id[r.id]
        if r.status == "matched":
            e.status = "matched"
            e.matched_date = pd.Timestamp(r.matched_date).date()
            from apps.operations.models import Operation

            e.matched_operation = Operation.objects.filter(company=company, operation_id=r.matched_operation_id).first()
            counts["matched"] += 1
        elif r.status == "overdue":
            e.status = "overdue"
            counts["overdue"] += 1
        else:
            e.status = "expected"
        e.save(update_fields=["status", "matched_date", "matched_operation", "updated_at"])
    return counts


def overdue_events_frame(company: Company) -> pd.DataFrame:
    qs = PlannedEvent.objects.filter(company=company, status="overdue")
    return pd.DataFrame([{"date": pd.Timestamp(e.date), "amount": float(e.amount), "label": e.label, "counterparty_name": e.counterparty_name, "direction": e.direction} for e in qs], columns=["date", "amount", "label", "counterparty_name", "direction"])


def tax_calendar_for(company: Company, as_of: pd.Timestamp, horizon_days: int) -> dict:
    ops = ops_services.ops_frame(company)
    if ops.empty:
        return {"regime": None, "events": []}
    hist = ops[ops.operation_date <= as_of]
    cal = tax_calendar(hist, as_of, as_of + pd.Timedelta(days=horizon_days), company.legal_form)
    if not cal.empty:
        cal["date"] = cal.date.dt.strftime("%Y-%m-%d")
    hist_tax = hist[hist.category == "TAXES"].copy()
    hist_tax["month"] = hist_tax.operation_date.dt.to_period("M").astype(str)
    return {
        "regime": detect_regime(hist, company.legal_form),
        "events": cal.round(2).to_dict(orient="records"),
        "history": hist_tax.groupby("month").amount.sum().round(2).reset_index().to_dict(orient="records"),
    }


def counterparties_overview(company: Company, as_of: pd.Timestamp) -> dict:
    ops = ops_services.ops_frame(company)
    if ops.empty:
        return {"clients": [], "suppliers": []}
    hist = ops[(ops.operation_date <= as_of) & (ops.operation_date > as_of - pd.Timedelta(days=182))]
    delays = counterparty_payment_delays(ops[ops.operation_date <= as_of]).set_index("cp_key") if len(ops) else pd.DataFrame()
    registry = RegistryAdapter()
    out = {}
    for key, direction in (("clients", "Поступление"), ("suppliers", "Списание")):
        sub = hist[(hist.direction == direction) & ~hist.category.isin(["OWNER_IN", "OWNER_OUT", "LOAN_IN", "LOAN_OUT", "TAXES", "SALARY", "BANK_FEES", "REV_ACQUIRING", "REV_CASH"])]
        g = sub.groupby(["counterparty_inn", "counterparty_name"]).agg(total=("amount", "sum"), n=("amount", "size"), last_date=("operation_date", "max")).reset_index().sort_values("total", ascending=False).head(15)
        total_all = float(sub.amount.sum()) or 1.0
        rows = []
        for r in g.itertuples():
            cp_key = r.counterparty_inn or f"name:{r.counterparty_name.lower()}"
            d = delays.loc[cp_key] if (len(delays) and cp_key in delays.index) else None
            check = _cached_check(registry, r.counterparty_inn, r.counterparty_name) if key == "clients" else None
            rows.append(
                {
                    "inn": r.counterparty_inn, "name": r.counterparty_name, "total_6m": round(float(r.total), 2), "share": round(float(r.total) / total_all, 3), "n": int(r.n), "last_date": str(r.last_date.date()),
                    "median_delay": float(d.median_delay) if d is not None else None, "p90_delay": float(d.p90_delay) if d is not None else None, "registry": check,
                }
            )
        out[key] = rows
    return out


def _cached_check(registry: RegistryAdapter, inn: str, name: str) -> dict:
    if not inn:
        return registry.check(inn, name)
    rec = CounterpartyCheck.objects.filter(inn=inn).first()
    if rec is None:
        res = registry.check(inn, name)
        rec = CounterpartyCheck.objects.create(inn=inn, name=name[:255], status=res["status"], risk_level=res["risk_level"], tax_debt=res["tax_debt"], details=res, provider=res.get("provider", "mock"))
    return {"status": rec.status, "risk_level": rec.risk_level, "tax_debt": rec.tax_debt, "pay_probability": rec.details.get("pay_probability", 0.9), "checked_at": rec.checked_at.isoformat()}


@transaction.atomic
def sync_bank(company: Company, as_of: pd.Timestamp, horizon_days: int) -> int:
    """Имитация внутренней интеграции с банком: события помечаются источником ``bank``."""
    ops = ops_services.ops_frame(company)
    # как и в прогнозе: для даты «в прошлом» паттерны берём только из данных до as_of
    patterns = detect_recurring(ops[ops.operation_date <= as_of], as_of) if (len(ops) and as_of < ops.operation_date.max() - pd.Timedelta(days=7)) else core_patterns(company)
    events = BankAdapter().sync(patterns, as_of, as_of + pd.Timedelta(days=horizon_days))
    PlannedEvent.objects.filter(company=company, source="bank", status="expected").delete()
    objs = [PlannedEvent(company=company, **{k: (Decimal(str(v)) if k == "amount" else v) for k, v in e.items()}) for e in events]
    PlannedEvent.objects.bulk_create(objs)
    return len(objs)


@transaction.atomic
def generate_contract_events(contract) -> int:
    """Строит график платежей по договору и заменяет его события в календаре."""
    from cashflow_core.contracts import ContractTerms, contract_schedule

    from .models import PlannedEvent

    company = contract.company
    PlannedEvent.objects.filter(company=company, external_ref=contract.ref, status__in=["expected", "overdue"]).delete()
    delay = std = None
    if contract.direction == "Поступление":
        ops = ops_services.ops_frame(company)
        if len(ops):
            d = counterparty_payment_delays(ops)
            key = contract.counterparty_inn or f"name:{contract.counterparty_name.lower()}"
            row = d[d.cp_key == key]
            if len(row):
                delay = float(row.median_delay.iloc[0])
                std = max(float(row.p90_delay.iloc[0] - row.median_delay.iloc[0]) / 1.3, 2.0)
    terms = ContractTerms(
        direction=contract.direction, total_amount=float(contract.total_amount), start_date=pd.Timestamp(contract.start_date), end_date=pd.Timestamp(contract.end_date) if contract.end_date else None,
        schedule_type=contract.schedule_type, prepayment_pct=contract.prepayment_pct, deferral_days=contract.deferral_days, installments=contract.installments, day_of_month=contract.day_of_month,
        milestones=contract.milestones, counterparty_name=contract.counterparty_name, label=f"Договор {contract.number}".strip() if contract.number else f"Договор с {contract.counterparty_name}",
        client_median_delay=delay, client_delay_std=std,
    )
    objs = [
        PlannedEvent(
            company=company, kind="contract", direction=contract.direction, amount=Decimal(str(round(e["amount"], 2))), date=e["date"].date(), due_date=e["date"].date(),
            counterparty_inn=contract.counterparty_inn, counterparty_name=contract.counterparty_name, category=contract.category, label=e["label"], source="contract",
            probability=e["probability"], timing_std_days=e["timing_std_days"], external_ref=contract.ref, created_by=contract.created_by,
        )
        for e in contract_schedule(terms)
    ]
    PlannedEvent.objects.bulk_create(objs)
    return len(objs)


def apply_registry_to_invoices(company: Company) -> int:
    """Снижает вероятность оплаты по счетам клиентов с проблемным статусом в реестре."""
    registry = RegistryAdapter()
    n = 0
    for e in PlannedEvent.objects.filter(company=company, kind="invoice_in", status="expected"):
        check = _cached_check(registry, e.counterparty_inn, e.counterparty_name)
        prob = min(e.probability, check["pay_probability"])
        if prob != e.probability:
            e.probability = prob
            e.meta = {**e.meta, "registry": check}
            e.save(update_fields=["probability", "meta", "updated_at"])
            n += 1
    return n
