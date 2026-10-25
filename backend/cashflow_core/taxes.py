"""Налоговый календарь: оценка предстоящих платежей ЕНП по истории и календарю.

Режим налогообложения в данных не указан, поэтому он определяется эвристически:
доля поступлений «в т.ч. НДС» → ОСНО, иначе УСН/ЕСХН. Сумма ЕНП на будущий
месяц берётся из того же месяца прошлого года (учитывает квартальные авансы
УСН и НДС), скорректированная на рост налоговой нагрузки; если прошлого года
нет — медиана последних платежей с поправкой на «квартальные» месяцы.
"""
from __future__ import annotations

import re

import numpy as np
import pandas as pd

from .calendar_ru import nth_day_of_month, shift_to_business_day

ENP_DAY = 28
QUARTER_MONTHS = {1, 4, 7, 10}  # авансы УСН / НДС по кварталам; годовой УСН (март/апрель) учитывается через «как год назад»

_VAT_RE = re.compile(r"в т\.?\s?ч\.? ндс|в том числе ндс|ндс \d{1,2}\s?%|ндс-?\s?\d{1,2}%")


def detect_regime(ops: pd.DataFrame, legal_form: str | None = None) -> dict:
    inflows = ops[ops.direction == "Поступление"]
    purposes = inflows.payment_purpose.fillna("").str.lower().str.replace("ё", "е")
    vat_share = float(purposes.map(lambda s: bool(_VAT_RE.search(s))).mean()) if len(purposes) else 0.0
    taxes = ops[ops.category == "TAXES"]
    revenue = inflows[inflows.category.str.startswith("REV_")].amount.sum()
    burden = float(taxes.amount.sum() / revenue) if revenue else 0.0
    if vat_share > 0.3:
        regime = "ОСНО"
    elif burden > 0.0 and burden < 0.045:
        regime = "УСН «доходы минус расходы» / ЕСХН (предположительно)"
    elif burden > 0.0:
        regime = "УСН «доходы» (предположительно)"
    else:
        regime = "не определён"
    return {
        "regime": regime,
        "vat_share": round(vat_share, 3),
        "tax_burden": round(burden, 4),
        "legal_form": legal_form,
        "has_tax_history": bool(len(taxes)),
    }


def tax_calendar(ops: pd.DataFrame, as_of: pd.Timestamp, end: pd.Timestamp, legal_form: str | None = None) -> pd.DataFrame:
    """Ожидаемые платежи ЕНП на (as_of, end]."""
    cols = ["date", "amount", "direction", "category", "label", "counterparty_name", "source", "confidence", "pattern_key"]
    taxes = ops[(ops.category == "TAXES") & (ops.operation_date <= as_of)].copy()
    if taxes.empty:
        return pd.DataFrame(columns=cols)
    taxes["ym"] = taxes.operation_date.dt.to_period("M")
    monthly = taxes.groupby("ym").amount.sum()
    paid_months = set(taxes.ym.unique())  # ЕНП за этот месяц уже уплачен до as_of
    rows = []
    cur = pd.Period(as_of, "M")
    last_period = pd.Period(end, "M")
    while cur <= last_period:
        due = shift_to_business_day(nth_day_of_month(cur.year, cur.month, ENP_DAY), "forward")
        if as_of < due <= end and cur not in paid_months:
            amount, conf, basis = _estimate_enp(monthly, cur)
            if amount > 0:
                rows.append(
                    {
                        "date": due,
                        "amount": round(amount, 2),
                        "direction": "Списание",
                        "category": "TAXES",
                        "label": f"ЕНП за {_month_name(cur.month)} ({basis})",
                        "counterparty_name": "Казначейство России (ФНС России)",
                        "source": "tax_calendar",
                        "confidence": conf,
                        "pattern_key": f"TAXES:{cur}",
                    }
                )
        cur += 1
    return pd.DataFrame(rows, columns=cols)


def _estimate_enp(monthly: pd.Series, target: pd.Period) -> tuple[float, float, str]:
    recent = monthly[monthly.index < target].tail(6)
    if recent.empty:
        return 0.0, 0.0, "нет истории"
    last_year = target - 12
    if last_year in monthly.index:
        # рост нагрузки: последние месяцы против тех же месяцев год назад (только пары, где есть оба)
        pairs = [(recent[p], monthly[p - 12]) for p in recent.index if (p - 12) in monthly.index and monthly[p - 12] > 0]
        growth = float(sum(a for a, _ in pairs) / sum(b for _, b in pairs)) if pairs else 1.0
        growth = float(np.clip(growth, 0.5, 2.0))
        return float(monthly[last_year] * growth), 0.75, "как год назад"
    base = float(np.median(recent.tail(3)))
    is_q = target.month in QUARTER_MONTHS
    hist_q = monthly[[p.month in QUARTER_MONTHS for p in monthly.index]]
    hist_r = monthly[[p.month not in QUARTER_MONTHS for p in monthly.index]]
    if is_q:
        uplift = float(hist_q.median() / hist_r.median()) if len(hist_q) and len(hist_r) and hist_r.median() > 0 else 1.6
        return base * float(np.clip(uplift, 1.0, 4.0)), 0.55, "квартальный аванс, оценка"
    return base, 0.65, "медиана последних месяцев"


def _month_name(m: int) -> str:
    return ["январь", "февраль", "март", "апрель", "май", "июнь", "июль", "август", "сентябрь", "октябрь", "ноябрь", "декабрь"][m - 1]
