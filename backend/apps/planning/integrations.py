"""Адаптеры источников данных.

Единый интерфейс ``Adapter.sync(company, as_of) -> list[dict]`` возвращает
плановые события в формате ``PlannedEvent``. Сейчас реализованы:

* ``BankAdapter``      — внутренние данные банка (график кредита, зарплатный проект,
                         неисполненные платёжки). В прототипе данные выводятся из
                         выписки и помечаются источником ``bank`` — при подключении
                         к реальному API банка меняется только этот класс.
* ``RegistryAdapter``  — проверка контрагента по открытым реестрам (статус, банкротство,
                         долги перед ФНС). Прототип — детерминированная заглушка с тем же
                         контрактом, что и у DaData / API ФНС.
* ``OneCInvoicesAdapter`` — счета из 1С/ЭДО. Файлы 1С разбирает ``cashflow_core.ingest``,
                         здесь — генерация ожидаемых оплат по счетам.
"""
from __future__ import annotations

import hashlib
import re

import pandas as pd

from cashflow_core.recurring import next_dates

LOAN_RE = re.compile(r"договор\w*\s*№?\s*([\w\-/]+)", re.I)


class BankAdapter:
    source = "bank"
    title = "Банк: кредиты, зарплатный проект, платёжные поручения"

    def sync(self, patterns: list, as_of: pd.Timestamp, horizon_end: pd.Timestamp) -> list[dict]:
        events = []
        for p in patterns:
            if not p.active:
                continue
            if p.category == "LOAN_OUT" and p.period == "monthly":
                ref = ""
                for h in p.history[-1:]:
                    ref = h.get("ref", "")
                events += self._monthly(p, as_of, horizon_end, "bank_credit", f"График кредита: платёж {ref}".strip(), 1.0)
            elif p.category == "SALARY":
                events += self._monthly(p, as_of, horizon_end, "bank_payroll", p.label + " (зарплатный проект)", 0.98)
        return events

    @staticmethod
    def _monthly(p, as_of, end, kind, label, conf) -> list[dict]:
        return [
            {"kind": kind, "direction": p.direction, "amount": float(p.amount), "date": d.date(), "counterparty_name": p.counterparty_name, "counterparty_inn": p.counterparty_inn, "category": p.category, "label": label, "source": "bank", "probability": conf, "external_ref": p.key}
            for d in next_dates(p, as_of + pd.Timedelta(days=1), end)
        ]


class RegistryAdapter:
    source = "registry"
    title = "Открытые реестры: статус контрагента, банкротство, долги по налогам"
    STATUSES = ["действует"] * 23 + ["ликвидация", "банкротство", "недостоверные сведения", "долг перед ФНС"]

    def check(self, inn: str, name: str = "") -> dict:
        """Заглушка: детерминированный «ответ реестра» по ИНН. Контракт совпадает с реальным API."""
        if not inn:
            return {"inn": inn, "name": name, "status": "ИНН не указан", "risk_level": "unknown", "tax_debt": False, "pay_probability": 0.9}
        h = int(hashlib.sha256(inn.encode()).hexdigest(), 16)
        status = self.STATUSES[h % len(self.STATUSES)]
        risk = {"действует": "low", "долг перед ФНС": "medium", "недостоверные сведения": "medium", "ликвидация": "high", "банкротство": "high"}[status]
        prob = {"low": 0.95, "medium": 0.75, "high": 0.4}[risk]
        return {"inn": inn, "name": name, "status": status, "risk_level": risk, "tax_debt": status == "долг перед ФНС", "pay_probability": prob, "provider": "mock"}


ADAPTERS = {
    "bank": {"title": BankAdapter.title, "kind": "internal", "config": "bank", "actions": ["sync"], "description": "С ключом — кредиты, зарплатный проект и неисполненные платёжки из API банка по контракту из README; без ключа график кредитов и зарплата восстанавливаются из выписки."},
    "1c": {"title": "1С: счета на оплату", "kind": "external", "actions": ["import_invoices"], "description": "Файл CommerceML (выгрузка документов из 1С) или таблица счетов CSV/XLSX. Выписку 1С:Клиент-банк загружайте на странице «Операции»."},
    "edo": {"title": "ЭДО: УПД и счета-фактуры", "kind": "external", "actions": ["import_invoices"], "description": "XML в формате ФНС (Диадок, СБИС, Такском). Направление — по ИНН продавца и покупателя: наш ИНН у продавца → нам заплатят."},
    "ofd": {"title": "Онлайн-касса / ОФД: выручка к зачислению", "kind": "external", "actions": ["import_ofd"], "description": "Отчёт по сменам или чекам (CSV/XLSX). Безналичная выручка попадает в прогноз как эквайринг следующего рабочего дня."},
    "registry": {"title": RegistryAdapter.title, "kind": "external", "config": "dadata", "actions": ["sync"], "description": "С ключом DaData — ЕГРЮЛ/ЕГРИП: статус, ликвидация, банкротство; без ключа — заглушка с тем же контрактом. Проблемный статус снижает вероятность оплаты по счетам клиента."},
    "manual": {"title": "Ручной ввод предпринимателя", "kind": "manual", "actions": [], "description": "«Клиент обещал заплатить 300 000 ₽ 20-го», «в декабре куплю оборудование»."},
    "contract": {"title": "Договоры с клиентами и поставщиками", "kind": "manual", "actions": [], "description": "Сумма, график (предоплата/этапы/рассрочка), отсрочка — превращаются в ожидаемые платежи с учётом типичной задержки клиента."},
    "tax_calendar": {"title": "Налоговый календарь", "kind": "internal", "actions": [], "description": "ЕНП до 28-го, квартальные авансы УСН/НДС — рассчитываются по истории платежей."},
}
