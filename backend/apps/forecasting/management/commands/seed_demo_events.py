"""Демо-данные для задачи ★1: синтетические выставленные счета.

В датасете нет счетов, поэтому для демонстрации они восстанавливаются из будущих
фактических оплат клиентов после даты «сегодня»: дата счёта берётся из назначения
(«по счёту № 12 от 01.07.2026»), срок оплаты — +14 дней, ожидаемая дата — срок +
типичная задержка клиента. Так можно измерить, насколько знание о счетах улучшает прогноз.

    python manage.py seed_demo_events --company C037 --as-of 2026-07-01
"""
from __future__ import annotations

import re
from decimal import Decimal

import pandas as pd
from core.commands import LoggedCommand

from apps.companies.models import Company
from apps.operations import services as ops_services
from apps.planning import services as planning_services
from apps.planning.models import PlannedEvent
from cashflow_core.recurring import counterparty_payment_delays

DOC_DATE_RE = re.compile(r"от (\d{2}\.\d{2}\.\d{4})")


class Command(LoggedCommand):
    def add_arguments(self, parser):
        parser.add_argument("--company", required=True)
        parser.add_argument("--as-of", required=True)
        parser.add_argument("--days", type=int, default=45)
        parser.add_argument("--min-amount", type=float, default=30000)

    def handle(self, *args, **opts):
        company = Company.objects.get(code=opts["company"])
        as_of = pd.Timestamp(opts["as_of"])
        company.as_of_override = as_of.date()
        company.save(update_fields=["as_of_override"])
        ops = ops_services.ops_frame(company)
        delays = counterparty_payment_delays(ops[ops.operation_date <= as_of]).set_index("cp_key")
        fut = ops[(ops.operation_date > as_of) & (ops.operation_date <= as_of + pd.Timedelta(days=opts["days"])) & (ops.category == "REV_CLIENTS") & (ops.amount >= opts["min_amount"])]
        PlannedEvent.objects.filter(company=company, source="invoice:1c").delete()
        n = 0
        for r in fut.itertuples():
            m = DOC_DATE_RE.search(r.payment_purpose or "")
            issued = pd.Timestamp(pd.to_datetime(m.group(1), format="%d.%m.%Y")) if m else r.operation_date - pd.Timedelta(days=20)
            if issued > as_of:  # счёт выставлен после «сегодня» — предприниматель о нём ещё не знает
                continue
            cp_key = r.counterparty_inn or f"name:{r.counterparty_name.lower()}"
            d = delays.loc[cp_key] if cp_key in delays.index else None
            median_delay = float(d.median_delay) if d is not None else 10.0
            std = max(float(d.p90_delay - d.median_delay) / 1.3, 2.0) if d is not None else 5.0
            due = issued + pd.Timedelta(days=14)
            expected = max(due + pd.Timedelta(days=max(median_delay - 14, 0)), as_of + pd.Timedelta(days=1))
            PlannedEvent.objects.create(
                company=company, kind="invoice_in", direction="Поступление", amount=Decimal(str(round(r.amount, 2))), date=expected.date(), issued_date=issued.date(), due_date=due.date(),
                counterparty_inn=r.counterparty_inn, counterparty_name=r.counterparty_name, category="REV_CLIENTS", label=f"Счёт клиенту: {r.counterparty_name}", source="invoice:1c",
                probability=0.9, timing_std_days=round(std, 1), note="Синтетический счёт для демо (восстановлен из назначения платежа)",
            )
            n += 1
        planning_services.sync_bank(company, as_of, opts["days"] + 30)
        planning_services.apply_registry_to_invoices(company)
        self.stdout.write(self.style.SUCCESS(f"{company.code}: создано счетов {n}, as_of = {as_of.date()}"))
