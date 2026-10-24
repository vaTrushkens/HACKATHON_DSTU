"""Импорт внешних данных в календарь платежей: счета из 1С/ЭДО/таблиц и выручка касс из отчёта ОФД.

Каждый документ становится ``PlannedEvent`` с источником и внешней ссылкой; повторная загрузка того же
файла ничего не дублирует. Для входящих оплат дата сдвигается на типичную задержку этого клиента.
"""
from __future__ import annotations

import datetime as dt
from decimal import Decimal

import pandas as pd

from apps.companies.models import Company
from apps.operations import services as ops_services
from cashflow_core.recurring import counterparty_payment_delays
from cashflow_core.sources.base import SourceInvoice, detect_format
from cashflow_core.sources.commerceml import parse_commerceml
from cashflow_core.sources.invoices_table import parse_invoices_table
from cashflow_core.sources.ofd import acquiring_credits, parse_ofd
from cashflow_core.sources.upd import parse_upd

from .models import PlannedEvent

SOURCE_BY_FORMAT = {"commerceml": "1c", "upd": "edo", "csv": "1c", "xlsx": "1c"}


def parse_invoices(filename: str, data: bytes) -> tuple[str, list[SourceInvoice]]:
    fmt = detect_format(filename, data)
    if fmt == "commerceml":
        return "1c", parse_commerceml(data)
    if fmt == "upd":
        return "edo", parse_upd(data)
    if fmt == "xml":
        raise ValueError("XML не распознан: ожидается CommerceML (1С) или УПД в формате ФНС (ЭДО)")
    return "1c", parse_invoices_table(filename, data)


def client_delay(company: Company, inn: str, name: str) -> tuple[float | None, float | None]:
    ops = ops_services.ops_frame(company)
    if ops.empty:
        return None, None
    d = counterparty_payment_delays(ops)
    row = d[d.cp_key == (inn or f"name:{name.lower()}")]
    if not len(row):
        return None, None
    med = float(row.median_delay.iloc[0])
    return med, max(float(row.p90_delay.iloc[0] - med) / 1.3, 2.0)


def resolve_direction(inv: SourceInvoice, company: Company, mode: str) -> tuple[str, str]:
    """(направление, как определено): явно | по роли контрагента | по ИНН | по колонке | по умолчанию."""
    if mode == "in":
        return "Поступление", "explicit"
    if mode == "out":
        return "Списание", "explicit"
    if inv.direction:
        return inv.direction, "column"
    role = inv.counterparty_role.lower()
    if role.startswith("покупател"):
        return "Поступление", "role"
    if role.startswith(("продав", "поставщ")):
        return "Списание", "role"
    if company.inn and inv.seller_inn == company.inn:
        return "Поступление", "inn"
    if company.inn and inv.buyer_inn == company.inn:
        return "Списание", "inn"
    return "Поступление", "default"


def import_invoices(company: Company, filename: str, data: bytes, direction: str = "auto", deferral_days: int = 14, user=None) -> dict:
    source, invoices = parse_invoices(filename, data)
    existing = set(PlannedEvent.objects.filter(company=company).exclude(status="cancelled").values_list("external_ref", flat=True))
    report = {"source": source, "rows_total": len(invoices), "created": 0, "duplicates": 0, "skipped": 0, "incoming": 0, "outgoing": 0, "direction_default": 0, "errors": []}
    delays: dict[str, tuple] = {}
    objs = []
    for inv in invoices:
        if inv.amount <= 0 or not (inv.date or inv.due_date):
            report["skipped"] += 1
            continue
        dirn, how = resolve_direction(inv, company, direction)
        if how == "default":
            report["direction_default"] += 1
        if dirn == "Поступление":
            name, inn = (inv.buyer_name, inv.buyer_inn) if inv.buyer_inn or inv.buyer_name else (inv.counterparty_name, inv.counterparty_inn)
        else:
            name, inn = (inv.seller_name, inv.seller_inn) if inv.seller_inn or inv.seller_name else (inv.counterparty_name, inv.counterparty_inn)
        ref = f"{source}:{inv.external_id or inv.number}:{inv.date or inv.due_date}"
        if ref in existing:
            report["duplicates"] += 1
            continue
        existing.add(ref)
        due = inv.due_date or (inv.date + dt.timedelta(days=deferral_days))
        expected, std = due, 0.0
        if dirn == "Поступление":
            key = inn or f"name:{name.lower()}"
            if key not in delays:
                delays[key] = client_delay(company, inn, name)
            med, dstd = delays[key]
            if med is not None:
                expected, std = due + dt.timedelta(days=int(round(med))), dstd
        objs.append(
            PlannedEvent(
                company=company, kind="invoice_in" if dirn == "Поступление" else "invoice_out", direction=dirn, amount=Decimal(str(round(inv.amount, 2))), date=expected, issued_date=inv.date, due_date=due,
                counterparty_inn=(inn or "")[:12], counterparty_name=(name or "")[:255], category="REV_CLIENTS" if dirn == "Поступление" else "SUPPLIERS", label=(inv.label or f"Счёт № {inv.number}")[:255],
                source=source, probability=0.95 if dirn == "Поступление" else 1.0, timing_std_days=std or 0.0, external_ref=ref[:128], meta={"kind": inv.kind, "direction_by": how}, created_by=user,
            )
        )
        report["incoming" if dirn == "Поступление" else "outgoing"] += 1
    PlannedEvent.objects.bulk_create(objs)
    report["created"] = len(objs)
    return report


def import_ofd(company: Company, filename: str, data: bytes, fee_pct: float = 0.0, user=None) -> dict:
    rows = parse_ofd(filename, data)
    credits = acquiring_credits(rows, fee_pct)
    as_of = pd.Timestamp(company.as_of or pd.Timestamp.today()).date()
    existing = set(PlannedEvent.objects.filter(company=company, source="ofd").exclude(status="cancelled").values_list("external_ref", flat=True))
    objs = []
    report = {"source": "ofd", "days": len(rows), "card_total": round(sum(r["card"] for r in rows), 2), "cash_total": round(sum(r["cash"] for r in rows), 2), "created": 0, "duplicates": 0, "already_in_statement": 0}
    for c in credits:
        ref = f"ofd:{c['sale_date']}"
        if ref in existing:
            report["duplicates"] += 1
            continue
        if c["date"] <= as_of:
            report["already_in_statement"] += 1  # зачисление уже должно быть в выписке — в прогноз не берём
            continue
        objs.append(
            PlannedEvent(
                company=company, kind="ofd_acquiring", direction="Поступление", amount=Decimal(str(c["amount"])), date=c["date"], issued_date=c["sale_date"], due_date=c["date"],
                counterparty_name="Эквайринг", category="REV_ACQUIRING", label=f"Эквайринг за {c['sale_date']:%d.%m} (отчёт ОФД)", source="ofd", probability=0.98, external_ref=ref, meta={"cash": c["cash"], "fee_pct": fee_pct}, created_by=user,
            )
        )
    PlannedEvent.objects.bulk_create(objs)
    report["created"] = len(objs)
    return report
