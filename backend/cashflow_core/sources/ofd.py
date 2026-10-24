"""Отчёт ОФД / онлайн-кассы (CSV или Excel): выручка по сменам или чекам.

Безналичная выручка (карты, СБП, электронные средства) зачисляется на счёт на следующий рабочий день
за вычетом комиссии эквайринга — это известное будущее поступление, которого ещё нет в выписке.
Наличные на расчётный счёт не попадают и в прогноз не идут (показываются только справочно).

Поддерживаются две раскладки: отдельные колонки «Наличные» / «Безналичные» или колонка «Тип оплаты» + «Сумма».
"""
from __future__ import annotations

import datetime as dt
from collections import defaultdict

import pandas as pd

from cashflow_core.calendar_ru import shift_to_business_day

from .base import find_col, parse_amount, parse_date, read_table

CASH_WORDS = ("налич", "cash")


def parse_ofd(filename: str, data: bytes) -> list[dict]:
    """[{date, card, cash}] по дням продаж."""
    df = read_table(filename, data)
    date_col = find_col(df, ["дата смены", "дата чека", "дата продажи", "дата", "date"])
    if not date_col:
        raise ValueError("В отчёте не найдена колонка с датой («Дата смены», «Дата чека», «Дата»)")
    card_col = find_col(df, ["безнал", "электрон", "карт", "сбп", "card", "cashless"])
    cash_col = find_col(df, ["налич", "cash"], exclude={card_col} if card_col else set())
    type_col = find_col(df, ["тип оплаты", "способ оплаты", "форма оплаты", "вид оплаты", "payment"])
    amount_col = find_col(df, ["сумма", "итого", "выручка", "amount", "total"], exclude={c for c in (card_col, cash_col) if c})
    per_date: dict[dt.date, dict[str, float]] = defaultdict(lambda: {"card": 0.0, "cash": 0.0})
    for _, row in df.iterrows():
        d = parse_date(row[date_col])
        if not d:
            continue
        if card_col or cash_col:
            per_date[d]["card"] += (parse_amount(row[card_col]) or 0.0) if card_col else 0.0
            per_date[d]["cash"] += (parse_amount(row[cash_col]) or 0.0) if cash_col else 0.0
        elif amount_col:
            amt = parse_amount(row[amount_col]) or 0.0
            t = str(row[type_col]).lower() if type_col else ""
            is_cash = any(w in t for w in CASH_WORDS) and "безнал" not in t  # «безналичными» тоже содержит «налич»
            per_date[d]["cash" if is_cash else "card"] += amt
    return [{"date": d, "card": round(v["card"], 2), "cash": round(v["cash"], 2)} for d, v in sorted(per_date.items())]


def acquiring_credits(rows: list[dict], fee_pct: float = 0.0) -> list[dict]:
    """Ожидаемые зачисления эквайринга: следующий рабочий день после продажи, минус комиссия."""
    out = []
    for r in rows:
        if r["card"] <= 0:
            continue
        credit = shift_to_business_day(pd.Timestamp(r["date"]) + pd.Timedelta(days=1), "forward").date()
        out.append({"sale_date": r["date"], "date": credit, "amount": round(r["card"] * (1 - fee_pct / 100), 2), "cash": r["cash"]})
    return out
