"""Сценарии «что если»: модификаторы входов прогноза."""
from __future__ import annotations

from dataclasses import asdict, dataclass, field

import pandas as pd


@dataclass
class Scenario:
    name: str = "Базовый"
    revenue_change_pct: float = 0.0  # −30 → выручка упадёт на 30%
    fixed_cost_change_pct: float = 0.0  # +10 → постоянные расходы вырастут на 10%
    delay_client_days: int = 0  # задержка оплаты клиентом
    delay_client_key: str | None = None  # ИНН/имя клиента; None + days>0 → все ожидаемые поступления от клиентов
    extra_expenses: list[dict] = field(default_factory=list)  # [{"date": "2026-10-15", "amount": 300000, "label": "Ремонт"}]
    extra_incomes: list[dict] = field(default_factory=list)
    exclude_event_keys: list[str] = field(default_factory=list)  # «клиент не заплатит вовсе»

    def as_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict | None) -> "Scenario":
        if not d:
            return cls()
        allowed = {k: v for k, v in d.items() if k in cls.__dataclass_fields__}
        return cls(**allowed)

    def is_base(self) -> bool:
        return not any([self.revenue_change_pct, self.fixed_cost_change_pct, self.delay_client_days, self.extra_expenses, self.extra_incomes, self.exclude_event_keys])


def apply_to_events(events: pd.DataFrame, sc: Scenario, as_of: pd.Timestamp, end: pd.Timestamp) -> pd.DataFrame:
    """Применяет сценарий к детерминированным событиям (регулярные, налоги, счета, ручные)."""
    ev = events.copy()
    if ev.empty:
        ev = pd.DataFrame(columns=["date", "amount", "direction", "category", "label", "counterparty_name", "source", "confidence", "pattern_key"])
    if sc.exclude_event_keys:
        ev = ev[~ev.pattern_key.astype(str).isin(sc.exclude_event_keys)]
    if sc.fixed_cost_change_pct:
        mask = (ev.direction == "Списание") & ev.category.isin(["RENT", "UTILITIES", "SERVICES", "SALARY", "CONTRACTORS", "LOAN_OUT", "BANK_FEES"])
        ev.loc[mask, "amount"] = ev.loc[mask, "amount"] * (1 + sc.fixed_cost_change_pct / 100)
    if sc.delay_client_days:
        mask = (ev.direction == "Поступление") & ev.category.isin(["REV_CLIENTS", "REV_MARKETPLACE", "OTHER_IN"])
        if sc.delay_client_key:
            key = str(sc.delay_client_key).lower()
            mask &= ev.counterparty_name.fillna("").str.lower().str.contains(key, regex=False) | ev.pattern_key.astype(str).str.lower().str.contains(key, regex=False)
        ev.loc[mask, "date"] = ev.loc[mask, "date"] + pd.Timedelta(days=int(sc.delay_client_days))
        ev.loc[mask, "label"] = ev.loc[mask, "label"] + f" (задержка {sc.delay_client_days} дн.)"
    extra = []
    for item in sc.extra_expenses:
        extra.append(_manual_row(item, "Списание", "OTHER_OUT", "scenario"))
    for item in sc.extra_incomes:
        extra.append(_manual_row(item, "Поступление", "OTHER_IN", "scenario"))
    if extra:
        ev = pd.concat([ev, pd.DataFrame(extra)], ignore_index=True)
    ev = ev[(ev.date > as_of) & (ev.date <= end)]
    return ev.reset_index(drop=True)


def _manual_row(item: dict, direction: str, category: str, source: str) -> dict:
    return {
        "date": pd.Timestamp(item["date"]).normalize(),
        "amount": float(item["amount"]),
        "direction": direction,
        "category": item.get("category", category),
        "label": item.get("label", "Сценарное событие"),
        "counterparty_name": item.get("counterparty_name", ""),
        "source": source,
        "confidence": 1.0,
        "pattern_key": f"scenario:{item.get('label', '')}:{item['date']}",
    }
