"""График платежей по договору → ожидаемые события прогноза.

Поддерживаемые условия:

* ``prepay_final`` — предоплата X % при старте, остаток после закрытия (акта) с отсрочкой;
* ``equal``        — равные платежи ежемесячно (аренда, абонентка, рассрочка поставщику);
* ``postpay``      — вся сумма после закрытия с отсрочкой N дней;
* ``milestones``   — произвольные этапы [{date, pct | amount, label}].

Для поступлений ожидаемая дата сдвигается на типичную задержку клиента (если известна),
а разброс даты берётся из истории его оплат — так договор превращается в события
с реалистичной неопределённостью, а не в «точку на календаре».
"""
from __future__ import annotations

from dataclasses import dataclass, field
import math

import pandas as pd

from .calendar_ru import nth_day_of_month, shift_to_business_day


@dataclass
class ContractTerms:
    direction: str  # Поступление | Списание
    total_amount: float
    start_date: pd.Timestamp
    schedule_type: str = "prepay_final"  # prepay_final | equal | postpay | milestones
    end_date: pd.Timestamp | None = None
    prepayment_pct: float = 30.0
    deferral_days: int = 0  # отсрочка оплаты после акта/поставки
    installments: int = 1
    day_of_month: int | None = None
    milestones: list[dict] = field(default_factory=list)
    counterparty_name: str = ""
    label: str = "Договор"
    client_median_delay: float | None = None  # из истории оплат клиента
    client_delay_std: float | None = None


def contract_schedule(t: ContractTerms) -> list[dict]:
    """Список событий: date, amount, label, probability, timing_std_days."""
    start = pd.Timestamp(t.start_date).normalize()
    end = pd.Timestamp(t.end_date).normalize() if t.end_date is not None else start
    if t.direction not in ("Поступление", "Списание"):
        raise ValueError("Некорректное направление")
    if not math.isfinite(float(t.total_amount)) or t.total_amount <= 0:
        raise ValueError("Сумма договора должна быть положительной")
    if pd.isna(start) or pd.isna(end) or end < start:
        raise ValueError("Дата окончания не может быть раньше начала")
    if not math.isfinite(t.prepayment_pct) or not 0 <= t.prepayment_pct <= 100:
        raise ValueError("Предоплата должна быть от 0 до 100%")
    if not 0 <= t.deferral_days <= 3650 or not 1 <= t.installments <= 120:
        raise ValueError("Отсрочка: 0–3650 дней; число платежей: 1–120")
    if t.day_of_month is not None and not 1 <= t.day_of_month <= 31:
        raise ValueError("День месяца должен быть от 1 до 31")
    deferral = pd.Timedelta(days=int(t.deferral_days))
    rows: list[tuple[pd.Timestamp, float, str]] = []

    if t.schedule_type == "prepay_final":
        pre = round(t.total_amount * t.prepayment_pct / 100, 2)
        if pre > 0:
            rows.append((start, pre, f"{t.label}: предоплата {t.prepayment_pct:g}%"))
        rest = round(t.total_amount - pre, 2)
        if rest > 0:
            rows.append((end + deferral, rest, f"{t.label}: окончательный расчёт"))
    elif t.schedule_type == "postpay":
        rows.append((end + deferral, round(t.total_amount, 2), f"{t.label}: оплата по акту"))
    elif t.schedule_type == "equal":
        n = max(int(t.installments), 1)
        per = round(t.total_amount / n, 2)
        dom = t.day_of_month or start.day
        cur = start
        for i in range(n):
            d = nth_day_of_month(cur.year, cur.month, dom) if i > 0 or t.day_of_month else cur
            if d < start:
                cur = cur + pd.DateOffset(months=1)
                d = nth_day_of_month(cur.year, cur.month, dom)
            rows.append((d + deferral, per if i < n - 1 else round(t.total_amount - per * (n - 1), 2), f"{t.label}: платёж {i + 1}/{n}"))
            cur = (pd.Timestamp(year=cur.year, month=cur.month, day=1) + pd.DateOffset(months=1))
    elif t.schedule_type == "milestones":
        if not isinstance(t.milestones, list) or not t.milestones:
            raise ValueError("Укажите этапы договора")
        for i, m in enumerate(t.milestones, start=1):
            if not isinstance(m, dict):
                raise ValueError("Этап договора должен быть объектом")
            amount = float(m["amount"]) if m.get("amount") is not None else round(t.total_amount * float(m.get("pct", 0)) / 100, 2)
            date = pd.Timestamp(m["date"]).normalize()
            if pd.isna(date) or date < start or (t.end_date is not None and date > end) or not math.isfinite(amount) or amount <= 0:
                raise ValueError("Некорректная дата или сумма этапа")
            rows.append((date + deferral, amount, m.get("label") or f"{t.label}: этап {i}"))
        if abs(sum(amount for _, amount, _ in rows) - t.total_amount) > .01:
            raise ValueError("Сумма этапов должна совпадать с суммой договора")
    else:
        raise ValueError(f"unknown schedule_type {t.schedule_type}")

    is_in = t.direction == "Поступление"
    out = []
    for d, amount, label in rows:
        if amount <= 0:
            raise ValueError("Сумма каждого платежа должна быть положительной; уменьшите число платежей")
        if is_in and t.client_median_delay:
            d = d + pd.Timedelta(days=int(round(t.client_median_delay)))
        d = shift_to_business_day(d, "forward")
        out.append(
            {
                "date": d,
                "amount": float(amount),
                "label": label,
                "probability": 0.9 if is_in else 1.0,
                "timing_std_days": float(t.client_delay_std) if (is_in and t.client_delay_std) else (3.0 if is_in else 0.0),
            }
        )
    return out
