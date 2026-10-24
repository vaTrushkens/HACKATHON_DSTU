"""Таблица счетов (CSV / Excel): выгрузка журнала «Счета покупателям» или «Счета от поставщиков» из 1С, СБИС, Моего Склада.

Колонки распознаются по названию (номер, дата, срок оплаты, сумма, контрагент, ИНН, направление) — порядок не важен.
"""
from __future__ import annotations

from .base import SourceInvoice, find_col, parse_amount, parse_date, read_table

ALIASES = {
    "due": ["срок оплаты", "срок", "оплатить до", "дата оплаты", "due"],
    "date": ["дата счета", "дата счёта", "дата", "date", "выставлен"],
    "number": ["номер", "№", "number", "счет", "счёт", "invoice", "документ"],
    "amount": ["сумма с ндс", "сумма", "итого", "amount", "total", "к оплате"],
    "name": ["контрагент", "клиент", "покупатель", "поставщик", "наименование", "counterparty", "customer", "supplier", "name"],
    "inn": ["инн", "inn"],
    "direction": ["направление", "direction", "тип", "type", "вид"],
}


def _direction(value: str) -> str:
    v = (value or "").strip().lower()
    if not v:
        return ""
    if any(k in v for k in ("поступ", "выстав", "исходящ", "клиент", "покупател", "in", "income", "sale")):
        return "Поступление"
    if any(k in v for k in ("спис", "получ", "входящ", "поставщ", "out", "expense", "purchase")):
        return "Списание"
    return ""


def parse_invoices_table(filename: str, data: bytes) -> list[SourceInvoice]:
    df = read_table(filename, data)
    used: set[str] = set()
    cols = {}
    for key in ("due", "date", "number", "amount", "name", "inn", "direction"):
        c = find_col(df, ALIASES[key], exclude=used)
        if c:
            cols[key] = c
            used.add(c)
    if "amount" not in cols:
        raise ValueError("В таблице не найдена колонка с суммой (ожидаются «Сумма», «Итого», «amount»)")
    out: list[SourceInvoice] = []
    for i, row in df.iterrows():
        amount = parse_amount(row[cols["amount"]])
        if amount is None or amount <= 0:
            continue
        number = str(row[cols["number"]]).strip() if "number" in cols else str(i + 1)
        out.append(
            SourceInvoice(
                number=number, date=parse_date(row[cols["date"]]) if "date" in cols else None, amount=amount,
                counterparty_name=str(row[cols["name"]]).strip() if "name" in cols else "", counterparty_inn=str(row[cols["inn"]]).strip() if "inn" in cols else "",
                due_date=parse_date(row[cols["due"]]) if "due" in cols else None, direction=_direction(row[cols["direction"]]) if "direction" in cols else "",
                label=f"Счёт № {number}", external_id=number,
            )
        )
    return out
