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

from django.utils import timezone

from .connectors import ConnectorError, connector
from .integrations import BankAdapter, RegistryAdapter
from .models import CounterpartyCheck, IntegrationConfig, PlannedEvent, RecurringPattern

CHECK_TTL = pd.Timedelta(days=30)

log = logging.getLogger(__name__)
EVENT_COLS = ["date", "amount", "direction", "category", "label", "counterparty_name", "counterparty_inn", "source", "confidence", "pattern_key", "probability", "timing_std_days", "external_ref", "obligation_key", "original_date"]


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
            probability=p.probability, timing_std_days=p.timing_std_days, purpose_hint=p.purpose_hint[:64],
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
                probability=r.probability, timing_std_days=r.timing_std_days, purpose_hint=r.purpose_hint,
            )
        )
    return out


def planned_events_frame(company: Company, as_of: pd.Timestamp, end: pd.Timestamp) -> pd.DataFrame:
    """Reconstruct unpaid obligations as of the cutoff without mutating present status."""
    qs = list(PlannedEvent.objects.filter(company=company, date__lte=end.date()).exclude(status="cancelled"))
    if not qs:
        return pd.DataFrame(columns=EVENT_COLS)
    expected = pd.DataFrame([{"id": e.id, "date": pd.Timestamp(e.date), "amount": float(e.amount), "direction": e.direction, "category": e.category, "counterparty_inn": e.counterparty_inn, "counterparty_name": e.counterparty_name} for e in qs])
    matched = planfact.match_events(expected, ops_services.ops_frame(company, until=as_of.date()), as_of).set_index("id")
    rows = []
    for e in qs:
        if matched.loc[e.id, "status"] == "matched":
            continue
        overdue = pd.Timestamp(e.date) <= as_of
        incoming = e.direction == "Поступление"
        date = as_of + pd.Timedelta(days=5 if incoming else 1) if overdue else pd.Timestamp(e.date)
        if date > end:
            continue
        rows.append({"date": date, "original_date": str(e.date), "amount": float(e.amount), "direction": e.direction, "category": e.category,
            "label": e.label + (" (не оплачено в срок)" if overdue else ""), "counterparty_name": e.counterparty_name, "counterparty_inn": e.counterparty_inn,
            "source": e.source, "confidence": e.probability, "pattern_key": f"event:{e.id}", "external_ref": e.external_ref,
            "obligation_key": e.meta.get("obligation_key", ""),
            "probability": min(e.probability, .5) if overdue and incoming else (1.0 if overdue else e.probability),
            "timing_std_days": max(e.timing_std_days, 5.) if overdue and incoming else (0.0 if overdue else e.timing_std_days)})
    return pd.DataFrame(rows, columns=EVENT_COLS)


@transaction.atomic
def refresh_event_statuses(company: Company, today=None) -> dict:
    """План-факт по событиям: сопоставляет ожидаемые события с фактическими операциями."""
    ops = ops_services.ops_frame(company)
    today = pd.Timestamp(today or company.as_of or (ops.operation_date.max() if len(ops) else pd.Timestamp.today()))
    qs = list(PlannedEvent.objects.filter(company=company).exclude(status="cancelled"))
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
        e.matched_date = None
        e.matched_operation = None
        if r.status == "matched":
            e.status = "matched"
            e.matched_date = pd.Timestamp(r.matched_date).date()
            from apps.operations.models import Operation

            e.matched_operation = Operation.objects.filter(company=company, operation_id=r.matched_operation_id, operation_date=e.matched_date).first()
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
    out = {}
    for key, direction in (("clients", "Поступление"), ("suppliers", "Списание")):
        sub = hist[(hist.direction == direction) & ~hist.category.isin(["OWNER_IN", "OWNER_OUT", "LOAN_IN", "LOAN_OUT", "TAXES", "SALARY", "BANK_FEES", "REV_ACQUIRING", "REV_CASH"])]
        g = sub.groupby(["counterparty_inn", "counterparty_name"]).agg(total=("amount", "sum"), n=("amount", "size"), last_date=("operation_date", "max")).reset_index().sort_values("total", ascending=False).head(15)
        total_all = float(sub.amount.sum()) or 1.0
        rows = []
        for r in g.itertuples():
            cp_key = r.counterparty_inn or f"name:{r.counterparty_name.lower()}"
            d = delays.loc[cp_key] if (len(delays) and cp_key in delays.index) else None
            check = check_counterparty(company, r.counterparty_inn, r.counterparty_name) if key == "clients" else None
            rows.append(
                {
                    "inn": r.counterparty_inn, "name": r.counterparty_name, "total_6m": round(float(r.total), 2), "share": round(float(r.total) / total_all, 3), "n": int(r.n), "last_date": str(r.last_date.date()),
                    "median_delay": float(d.median_delay) if d is not None else None, "p90_delay": float(d.p90_delay) if d is not None else None, "registry": check,
                }
            )
        out[key] = rows
    return out


def check_counterparty(company: Company, inn: str, name: str) -> dict:
    """Статус контрагента: DaData (ЕГРЮЛ/ЕГРИП), если есть ключ, иначе детерминированная заглушка. Кэш 30 дней."""
    client = connector(company, "dadata")
    provider = client.provider if client.configured else "mock"
    if not inn:
        return {**RegistryAdapter().check(inn, name), "provider": provider}
    rec = CounterpartyCheck.objects.filter(inn=inn).first()
    stale = rec is None or rec.provider != provider or (timezone.now() - rec.checked_at) > CHECK_TTL
    if stale:
        try:
            res = client.check(inn, name) if client.configured else RegistryAdapter().check(inn, name)
        except ConnectorError as e:
            log.warning("registry check failed for %s: %s", inn, e)
            if rec is None:
                res = {**RegistryAdapter().check(inn, name), "provider": "mock", "error": str(e)}
            else:
                res = None
        if res is not None:
            rec, _ = CounterpartyCheck.objects.update_or_create(inn=inn, defaults={"name": (res.get("name") or name)[:255], "status": res["status"], "risk_level": res["risk_level"], "tax_debt": res.get("tax_debt", False), "details": res, "provider": res.get("provider", "mock")})
    return {"status": rec.status, "risk_level": rec.risk_level, "tax_debt": rec.tax_debt, "pay_probability": rec.details.get("pay_probability", 0.9), "checked_at": rec.checked_at.isoformat(), "provider": rec.provider, "name": rec.name}


def _mark_sync(company: Company, kind: str, error: str = "") -> None:
    IntegrationConfig.objects.update_or_create(company=company, kind=kind, defaults={"last_sync": timezone.now(), "last_status": "error" if error else "ok", "last_error": error[:2000]})


def _replace_events(company: Company, source: str, events: list[dict]) -> int:
    with transaction.atomic():  # атомарна только замена событий: статус ошибки коннектора должен сохраниться при исключении
        # Заменяем все события источника, включая уже сопоставленные с фактом: иначе повторная
        # синхронизация создаёт дубли, а статусы восстанавливает refresh_event_statuses.
        PlannedEvent.objects.filter(company=company, source=source).exclude(status="cancelled").delete()
        objs = [PlannedEvent(company=company, **{k: (Decimal(str(v)) if k == "amount" else v) for k, v in e.items()}) for e in events]
        PlannedEvent.objects.bulk_create(objs)
    return len(objs)


def sync_bank(company: Company, as_of: pd.Timestamp, horizon_days: int) -> dict:
    """События банка: из API по контракту ``BankClient``, если он настроен, иначе восстановление из выписки."""
    end = as_of + pd.Timedelta(days=horizon_days)
    client = connector(company, "bank")
    if client.configured:
        try:
            events = client.events(company.inn, as_of.date(), end.date())
        except ConnectorError as e:
            _mark_sync(company, "bank", str(e))
            raise
        _mark_sync(company, "bank")
        return {"mode": "api", "events": _replace_events(company, "bank", events)}
    ops = ops_services.ops_frame(company)
    # как и в прогнозе: для даты «в прошлом» паттерны берём только из данных до as_of
    patterns = detect_recurring(ops[ops.operation_date <= as_of], as_of) if (len(ops) and as_of < ops.operation_date.max() - pd.Timedelta(days=7)) else core_patterns(company)
    return {"mode": "derived", "events": _replace_events(company, "bank", BankAdapter().sync(patterns, as_of, end))}


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
    n = 0
    for e in PlannedEvent.objects.filter(company=company, kind="invoice_in", status="expected"):
        check = check_counterparty(company, e.counterparty_inn, e.counterparty_name)
        prob = min(e.probability, check["pay_probability"])
        if prob != e.probability:
            e.probability = prob
            e.meta = {**e.meta, "registry": check}
            e.save(update_fields=["probability", "meta", "updated_at"])
            n += 1
    return n
