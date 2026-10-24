"""Производственный календарь РФ: рабочие дни и перенос дат платежей."""
from __future__ import annotations

import datetime as dt
from functools import lru_cache

import holidays
import numpy as np
import pandas as pd


@lru_cache(maxsize=8)
def _ru_holidays(year_from: int, year_to: int) -> set[dt.date]:
    years = range(year_from, year_to + 1)
    return set(holidays.country_holidays("RU", years=years).keys())


def is_business_day(d: dt.date | pd.Timestamp) -> bool:
    d = pd.Timestamp(d).date()
    if d.weekday() >= 5:
        return False
    return d not in _ru_holidays(d.year - 1, d.year + 1)


def business_days(start: dt.date, end: dt.date) -> pd.DatetimeIndex:
    """Все рабочие дни в [start, end]."""
    days = pd.date_range(start, end, freq="D")
    mask = np.array([is_business_day(d) for d in days])
    return days[mask]


def shift_to_business_day(d: dt.date | pd.Timestamp, direction: str = "forward") -> pd.Timestamp:
    """Переносит дату на ближайший рабочий день.

    ``forward`` — как для налогов (срок «не позднее» переносится на следующий
    рабочий день), ``backward`` — как обычно платят зарплату (перед выходными).
    """
    d = pd.Timestamp(d).normalize()
    step = pd.Timedelta(days=1 if direction == "forward" else -1)
    for _ in range(10):
        if is_business_day(d):
            return d
        d += step
    return d


def nth_day_of_month(year: int, month: int, day: int) -> pd.Timestamp:
    """День месяца с обрезкой по длине месяца (31 → 30 для апреля и т.п.)."""
    last = pd.Timestamp(year=year, month=month, day=1) + pd.offsets.MonthEnd(0)
    return pd.Timestamp(year=year, month=month, day=min(day, last.day))


def business_days_in_month(year: int, month: int) -> int:
    first = pd.Timestamp(year=year, month=month, day=1)
    return len(business_days(first.date(), (first + pd.offsets.MonthEnd(0)).date()))
