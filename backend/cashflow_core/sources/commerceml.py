"""CommerceML 2 — стандарт обмена 1С: выгрузка счетов на оплату и заказов (``КоммерческаяИнформация/Документ``).

Роль контрагента берётся из ``Контрагент/Роль``; если её нет — из роли документа (наша роль):
«Продавец» в документе означает, что контрагент — покупатель, и счёт нам оплатят.
Срок оплаты — реквизит документа «Срок оплаты» / «Дата оплаты» / «Оплатить до», если он выгружен.
"""
from __future__ import annotations

import xml.etree.ElementTree as ET

from .base import SourceInvoice, child, local, parse_amount, parse_date, text

DUE_KEYS = ("срок оплаты", "дата оплаты", "оплатить до")
INVERSE_ROLE = {"продавец": "Покупатель", "покупатель": "Продавец", "поставщик": "Покупатель"}


def parse_commerceml(data: bytes) -> list[SourceInvoice]:
    root = ET.fromstring(data)
    out: list[SourceInvoice] = []
    for doc in root.iter():
        if local(doc.tag) != "Документ":
            continue
        amount = parse_amount(text(doc, "Сумма"))
        if amount is None or amount <= 0:
            continue
        cps = child(doc, "Контрагенты")
        cp = child(cps, "Контрагент") if cps is not None else None
        name = text(cp, "Наименование") or text(cp, "ОфициальноеНаименование")
        inn = text(cp, "ИНН")
        role = text(cp, "Роль")
        if not role:
            role = INVERSE_ROLE.get(text(doc, "Роль").lower(), "")
        due = None
        reqs = child(doc, "ЗначенияРеквизитов")
        if reqs is not None:
            for r in reqs:
                if any(k in text(r, "Наименование").lower() for k in DUE_KEYS):
                    due = parse_date(text(r, "Значение"))
        number = text(doc, "Номер")
        op = text(doc, "ХозОперация") or "Счёт"
        out.append(
            SourceInvoice(
                number=number, date=parse_date(text(doc, "Дата")), amount=amount, counterparty_name=name, counterparty_inn=inn, counterparty_role=role.capitalize(),
                due_date=due, label=f"{op} № {number}".strip() if number else op, external_id=text(doc, "Ид") or number, kind=op,
            )
        )
    return out
