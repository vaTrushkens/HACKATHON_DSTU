"""Детектор регулярных платежей и поступлений.

Операции группируются по (категория, контрагент, подтип), для каждой группы
оценивается периодичность, типичный день и сумма. Результат — список
``RecurringPattern`` плюс их проекция на горизонт прогноза (``project``).
"""
from __future__ import annotations

import datetime as dt
from dataclasses import asdict, dataclass, field

import numpy as np
import pandas as pd

from .calendar_ru import nth_day_of_month, shift_to_business_day
from .categories import FINANCING_CODES, FIXED_CODES, IN
from .categorizer.pipeline import counterparty_key
from .categorizer.rules import is_salary_advance, normalize_text

PERIODS = {  # название → (мин. интервал, макс. интервал в днях, длина периода)
    "weekly": (5, 9, 7),
    "biweekly": (12, 17, 14),
    "monthly": (26, 36, 30),
    "quarterly": (82, 100, 91),
}
MIN_OCCURRENCES = 3
MAX_GAP_CV = 0.35  # допустимый разброс интервалов
LOOKBACK_DAYS = 400


@dataclass
class RecurringPattern:
    key: str
    category: str
    direction: str
    counterparty_name: str
    counterparty_inn: str
    subtype: str  # '', 'advance', 'final'
    period: str
    day_of_month: int | None
    weekday: int | None
    amount: float
    amount_cv: float
    occurrences: int
    last_date: dt.date
    first_date: dt.date
    confidence: float
    active: bool
    label: str = ""
    history: list[dict] = field(default_factory=list)

    def as_dict(self) -> dict:
        d = asdict(self)
        d["last_date"] = str(self.last_date)
        d["first_date"] = str(self.first_date)
        return d


def _subtype(row_cat: str, purpose: str) -> str:
    if row_cat == "SALARY":
        return "advance" if is_salary_advance(purpose) else "final"
    return ""


def _classify_period(gaps: np.ndarray) -> tuple[str | None, float]:
    if len(gaps) == 0:
        return None, 1.0
    med = float(np.median(gaps))
    cv = float(np.median(np.abs(gaps - med)) / med) if med else 1.0
    for name, (lo, hi, _) in PERIODS.items():
        if lo <= med <= hi:
            return name, cv
    return None, cv


def detect_recurring(ops: pd.DataFrame, as_of: pd.Timestamp | None = None) -> list[RecurringPattern]:
    """``ops`` — операции с колонками category/direction/... (после категоризации)."""
    if ops.empty:
        return []
    as_of = pd.Timestamp(as_of or ops.operation_date.max()).normalize()
    df = ops[(ops.operation_date <= as_of) & (ops.operation_date > as_of - pd.Timedelta(days=LOOKBACK_DAYS))].copy()
    # регулярность имеет смысл для фиксированных категорий и для стабильных контрагентов-клиентов
    df = df[~df.category.isin(FINANCING_CODES)]
    df["cp_key"] = counterparty_key(df)
    df["subtype"] = [_subtype(c, p) for c, p in zip(df.category, df.payment_purpose)]
    # зарплата и налоги проходят через банк/ФНС — группируем по категории, а не по контрагенту
    df["grp"] = np.where(df.category.isin(["SALARY", "TAXES"]), df.category + ":" + df.subtype, df.category + ":" + df.cp_key + ":" + df.subtype)

    patterns: list[RecurringPattern] = []
    for grp, g in df.groupby("grp"):
        g = g.sort_values("operation_date")
        # несколько операций в один день с одним контрагентом считаем одним платежом
        daily = g.groupby("operation_date").agg(amount=("amount", "sum"), purpose=("payment_purpose", "first")).reset_index()
        if len(daily) < MIN_OCCURRENCES:
            continue
        dates = daily.operation_date.values.astype("datetime64[D]")
        gaps = np.diff(dates).astype(int)
        period, cv = _classify_period(gaps)
        if period is None or cv > MAX_GAP_CV:
            continue
        period_len = PERIODS[period][2]
        amounts = daily.amount.values
        recent = amounts[-3:]
        amount = float(np.median(recent))
        amount_cv = float(np.std(amounts) / np.mean(amounts)) if np.mean(amounts) else 0.0
        last = pd.Timestamp(dates[-1])
        active = (as_of - last).days <= period_len * 1.6
        cat = g.category.iloc[0]
        # клиентские поступления считаем регулярными только при стабильной сумме
        if cat not in FIXED_CODES and amount_cv > 0.5:
            continue
        conf = min(1.0, 0.5 + 0.08 * len(daily)) * (1 - min(cv, 0.35)) * (1 - min(amount_cv, 0.5) * 0.5)
        dom = int(np.median(pd.DatetimeIndex(dates).day)) if period in ("monthly", "quarterly") else None
        wd = int(np.median(pd.DatetimeIndex(dates).weekday)) if period in ("weekly", "biweekly") else None
        p = RecurringPattern(
            key=grp,
            category=cat,
            direction=g.direction.iloc[0],
            counterparty_name=g.counterparty_name.iloc[0],
            counterparty_inn=g.counterparty_inn.iloc[0],
            subtype=g.subtype.iloc[0],
            period=period,
            day_of_month=dom,
            weekday=wd,
            amount=round(amount, 2),
            amount_cv=round(amount_cv, 3),
            occurrences=int(len(daily)),
            last_date=last.date(),
            first_date=pd.Timestamp(dates[0]).date(),
            confidence=round(float(conf), 3),
            active=bool(active),
            history=[{"date": str(pd.Timestamp(d).date()), "amount": float(a)} for d, a in zip(dates[-6:], amounts[-6:])],
        )
        p.label = make_label(p)
        patterns.append(p)
    patterns.sort(key=lambda p: (-p.amount if p.direction != IN else 0, p.category))
    return patterns


def make_label(p: RecurringPattern) -> str:
    from .categories import LABELS

    base = LABELS.get(p.category, p.category)
    if p.category == "SALARY":
        return "Зарплата: аванс" if p.subtype == "advance" else "Зарплата: окончательный расчёт"
    if p.category == "TAXES":
        return "Единый налоговый платёж"
    name = p.counterparty_name or ""
    return f"{base} — {name}" if name else base


def project(patterns: list[RecurringPattern], start: pd.Timestamp, end: pd.Timestamp, skip_taxes: bool = True) -> pd.DataFrame:
    """Разворачивает активные паттерны в ожидаемые события на [start, end].

    Налоги по умолчанию пропускаются: их считает ``taxes.py`` по календарю.
    """
    rows = []
    for p in patterns:
        if not p.active or (skip_taxes and p.category == "TAXES"):
            continue
        for d in _next_dates(p, start, end):
            rows.append(
                {
                    "date": d,
                    "amount": p.amount,
                    "direction": p.direction,
                    "category": p.category,
                    "label": p.label,
                    "counterparty_name": p.counterparty_name,
                    "source": "recurring",
                    "confidence": p.confidence,
                    "pattern_key": p.key,
                }
            )
    cols = ["date", "amount", "direction", "category", "label", "counterparty_name", "source", "confidence", "pattern_key"]
    return pd.DataFrame(rows, columns=cols)


def next_dates(p: RecurringPattern, start: pd.Timestamp, end: pd.Timestamp) -> list[pd.Timestamp]:
    """Даты паттерна внутри [start, end]. Якорь — последнее наблюдение; если оно позже start
    (прогноз «из прошлого» для демо/бэктеста), шагаем от него назад, чтобы покрыть окно."""
    shift_dir = "backward" if p.category in ("SALARY", "CONTRACTORS") else "forward"
    last = pd.Timestamp(p.last_date)
    lower = max(start, last + pd.Timedelta(days=1)) if last < start else start  # уже наблюдённое не повторяем
    out = []
    if p.period in ("monthly", "quarterly"):
        step = 1 if p.period == "monthly" else 3
        cur = pd.Timestamp(year=start.year, month=start.month, day=1) - pd.DateOffset(months=step)
        while cur <= end:
            d = shift_to_business_day(nth_day_of_month(cur.year, cur.month, p.day_of_month or last.day), shift_dir)
            if lower <= d <= end:
                out.append(d)
            cur += pd.DateOffset(months=step)
    else:
        step = pd.Timedelta(days=PERIODS[p.period][2])
        cur = last
        while cur > start:  # откатываемся к окну
            cur -= step
        while cur <= end:
            d = shift_to_business_day(cur, shift_dir)
            if lower <= d <= end:
                out.append(d)
            cur += step
    return sorted(set(out))


_next_dates = next_dates


def counterparty_payment_delays(ops: pd.DataFrame) -> pd.DataFrame:
    """Задержка оплаты клиентами: разница между датой счёта/УПД в назначении и датой платежа.

    Используется при планировании выставленных счетов: срок оплаты сдвигается на
    типичную задержку конкретного клиента (или медиану по компании).
    """
    import re

    inflows = ops[(ops.direction == "Поступление") & (ops.category == "REV_CLIENTS")]
    rows = []
    pat = re.compile(r"от (\d{2}\.\d{2}\.\d{4})")
    for r in inflows.itertuples():
        m = pat.search(normalize_text(r.payment_purpose))
        if not m:
            continue
        try:
            doc_date = pd.Timestamp(dt.datetime.strptime(m.group(1), "%d.%m.%Y"))
        except ValueError:
            continue
        delay = (r.operation_date - doc_date).days
        if 0 <= delay <= 180:
            rows.append({"cp_key": r.counterparty_inn or f"name:{normalize_text(r.counterparty_name)}", "counterparty_name": r.counterparty_name, "delay_days": delay, "amount": r.amount})
    if not rows:
        return pd.DataFrame(columns=["cp_key", "counterparty_name", "n", "median_delay", "p90_delay", "total_amount"])
    d = pd.DataFrame(rows)
    return (
        d.groupby("cp_key")
        .agg(counterparty_name=("counterparty_name", "first"), n=("delay_days", "size"), median_delay=("delay_days", "median"), p90_delay=("delay_days", lambda s: float(np.percentile(s, 90))), total_amount=("amount", "sum"))
        .reset_index()
        .sort_values("total_amount", ascending=False)
    )
