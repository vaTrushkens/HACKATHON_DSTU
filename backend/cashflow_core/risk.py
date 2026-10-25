"""Оценка риска кассового разрыва: дата, сумма, вероятность, объяснение, рекомендации.

Тексты формируются шаблонами из рассчитанных чисел. Слой намеренно отделён от
движка: позже LLM-ассистент сможет переформулировать объяснение, но все цифры
по-прежнему будут браться только отсюда.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass, field

import numpy as np
import pandas as pd

from .categories import LABELS, MANDATORY_CODES
from .forecast.engine import ForecastResult

# Пороги уровня риска по вероятности хотя бы одного разрыва за горизонт. База разрывов в данных ≈ 5 %,
# поэтому 20 % — уже четырёхкратное превышение («есть риск»), 50 % — «высокий риск».
P_HIGH = 0.5
P_MEDIUM = 0.2
P_DATE = 0.5  # дата разрыва — первый день, когда вероятность ≥ 50%


@dataclass
class Alert:
    level: str  # high | medium | low | info
    kind: str  # cash_gap | low_runway | big_payment | overdue_income | data_quality
    title: str
    message: str
    date: str | None = None
    amount: float | None = None
    probability: float | None = None
    details: dict = field(default_factory=dict)


@dataclass
class RiskAssessment:
    level: str  # high | medium | low
    probability: float  # P(min остаток за горизонт < порога)
    gap_date: str | None
    gap_date_p10: str | None  # пессимистичная дата (первый день, когда p10 < порога)
    deficit_p50: float
    deficit_p10: float
    runway_days: int | None  # сколько дней хватит денег по медиане
    min_balance_p50: float
    explanation: str
    waterfall: list[dict]
    recommendations: list[dict]
    alerts: list[Alert]
    summary: str

    def as_dict(self) -> dict:
        d = asdict(self)
        return d


def assess(res: ForecastResult, overdue_events: pd.DataFrame | None = None, delays: pd.DataFrame | None = None) -> RiskAssessment:
    daily = res.daily
    thr = res.min_balance
    paths = res.paths
    prob_any = float((paths.min(axis=1) < thr).mean())
    if res.components.get("probability_kind") == "calibrated":  # см. calibration.apply_to_result
        prob_any = float(res.components["prob_gap_any_day"])
    first_p50 = daily.index[daily.prob_below >= P_DATE]
    first_p10 = daily.index[daily.p10 < thr]
    gap_date = first_p50[0] if len(first_p50) else None
    gap_date_p10 = first_p10[0] if len(first_p10) else None

    # дефицит: сколько не хватит в худшей точке траектории
    min_p50 = float(daily.p50.min())
    min_p10 = float(daily.p10.min())
    # Distribution of worst balance per path, not minimum of pointwise quantiles.
    path_min = paths.min(axis=1)
    deficit_p50 = max(thr - float(np.percentile(path_min, 50)), 0.0)
    deficit_p10 = max(thr - float(np.percentile(path_min, 10)), 0.0)
    runway = int((daily.index[(daily.p50 < thr)][0] - res.as_of).days) if (daily.p50 < thr).any() else None

    if prob_any >= P_HIGH:
        level = "high"
    elif prob_any >= P_MEDIUM:
        level = "medium"
    else:
        level = "low"

    ref_date = gap_date or gap_date_p10 or daily.index[-1]
    waterfall = _waterfall(res, ref_date)
    explanation = _explain(res, level, prob_any, gap_date, gap_date_p10, deficit_p50, deficit_p10, waterfall)
    recs = _recommend(res, level, deficit_p50, deficit_p10, ref_date, delays)
    alerts = _alerts(res, level, prob_any, gap_date, gap_date_p10, deficit_p50, deficit_p10, runway, overdue_events)
    summary = _summary(level, prob_any, gap_date, gap_date_p10, deficit_p50, deficit_p10, res)
    return RiskAssessment(
        level=level,
        probability=round(prob_any, 3),
        gap_date=_s(gap_date),
        gap_date_p10=_s(gap_date_p10),
        deficit_p50=round(deficit_p50, 2),
        deficit_p10=round(deficit_p10, 2),
        runway_days=runway,
        min_balance_p50=round(min_p50, 2),
        explanation=explanation,
        waterfall=waterfall,
        recommendations=recs,
        alerts=alerts,
        summary=summary,
    )


# ---------------------------------------------------------------------------
def _s(d) -> str | None:
    return pd.Timestamp(d).strftime("%Y-%m-%d") if d is not None else None


def fmt_money(x: float) -> str:
    x = float(x)
    if abs(x) >= 1_000_000:
        return f"{x / 1_000_000:.1f} млн ₽".replace(".", ",")
    if abs(x) >= 1_000:
        return f"{x / 1_000:.0f} тыс. ₽"
    return f"{x:.0f} ₽"


def fmt_date(d) -> str:
    d = pd.Timestamp(d)
    months = ["января", "февраля", "марта", "апреля", "мая", "июня", "июля", "августа", "сентября", "октября", "ноября", "декабря"]
    return f"{d.day} {months[d.month - 1]}"


def _waterfall(res: ForecastResult, until: pd.Timestamp) -> list[dict]:
    """От остатка сегодня до остатка на дату риска: крупные события + агрегированные потоки."""
    d = res.daily[res.daily.index <= until]
    ev = res.events[res.events.date <= until] if not res.events.empty else res.events
    rows = [{"label": "Остаток сегодня", "amount": round(res.opening_balance, 2), "kind": "start"}]
    rows.append({"label": "Ожидаемая выручка и прочие поступления", "amount": round(float(d.stoch_in.sum()), 2), "kind": "stoch_in", "note": "оценка модели"})
    if not ev.empty:
        big = ev.copy()
        big["signed"] = np.where(big.direction == "Поступление", big.amount, -big.amount) * big.get("probability", 1.0)
        grouped = big.groupby(["category", "direction"]).agg(amount=("signed", "sum"), n=("signed", "size"), first_date=("date", "min"), label=("label", "first")).reset_index()
        grouped = grouped.sort_values("amount")
        for g in grouped.itertuples():
            label = LABELS.get(g.category, g.category) if g.n > 1 else g.label
            rows.append({"label": label, "amount": round(float(g.amount), 2), "kind": "event", "category": g.category, "date": _s(g.first_date), "count": int(g.n)})
    rows.append({"label": "Операционные расходы (закупки, реклама и пр.)", "amount": -round(float(d.stoch_out.sum()), 2), "kind": "stoch_out", "note": "оценка модели"})
    # Quantiles are not additive. Reconcile the displayed contributions to p50 explicitly.
    subtotal = sum(row['amount'] for row in rows)
    adjustment = round(float(d.p50.iloc[-1]) - subtotal, 2)
    rows.append({"label": "Поправка от суммы средних к медиане", "amount": adjustment, "kind": "uncertainty"})
    rows.append({"label": f"Остаток на {fmt_date(until)} (медиана)", "amount": round(float(d.p50.iloc[-1]), 2), "kind": "end"})
    return rows


def _explain(res, level, prob, gap_date, gap_date_p10, deficit_p50, deficit_p10, waterfall) -> str:
    thr_txt = "ниже нуля" if res.min_balance == 0 else f"ниже {fmt_money(res.min_balance)}"
    if res.opening_balance < res.min_balance:
        outs = sorted([w for w in waterfall if w["kind"] in ("event", "stoch_out") and w["amount"] < 0], key=lambda w: w["amount"])[:2]
        causes = ", ".join(f"{w['label'].lower()} ({fmt_money(-w['amount'])})" for w in outs)
        return (
            f"Счёт уже {thr_txt}: {fmt_money(res.opening_balance)} (овердрафт). За горизонт прогноза медианный остаток дойдёт до {fmt_money(res.components['end_balance_p50'])}; "
            f"ближайшие крупные списания: {causes}." if causes else f"Счёт уже {thr_txt}: {fmt_money(res.opening_balance)}."
        )
    if level == "low" and gap_date_p10 is None:
        return (
            f"В ближайшие {len(res.daily)} дней денег должно хватить: даже в неблагоприятном сценарии остаток не опускается {thr_txt}. "
            f"Вероятность нехватки — {prob:.0%}."
        )
    outs = [w for w in waterfall if w["kind"] in ("event", "stoch_out") and w["amount"] < 0]
    big_outs = sorted(outs, key=lambda w: w["amount"])[:3]
    causes = ", ".join(
        (f"операционные расходы ({fmt_money(-w['amount'])}, оценка по истории)" if w["kind"] == "stoch_out" else f"{w['label'].lower()} ({fmt_money(-w['amount'])})") for w in big_outs
    )
    if gap_date is not None:
        head = f"Оценка вероятности хотя бы одного разрыва за {len(res.daily)} дней — {prob:.0%}. Медианный остаток опускается {thr_txt} {fmt_date(gap_date)}; пиковая нехватка по траекториям — около {fmt_money(deficit_p50)}, в плохом случае — до {fmt_money(deficit_p10)}."
    else:
        timing = f" Неблагоприятный дневной сценарий пересекает порог {fmt_date(gap_date_p10)}." if gap_date_p10 is not None else " Дата разрыва различается между траекториями."
        head = f"Оценка вероятности хотя бы одного разрыва за {len(res.daily)} дней — {prob:.0%}, хотя медианный остаток по дням выше порога.{timing} Пиковая нехватка в плохом случае — до {fmt_money(deficit_p10)}."
    why = f" Основные списания до этой даты: {causes}." if causes else ""
    stoch_in = next((w for w in waterfall if w["kind"] == "stoch_in"), None)
    inflow_txt = f" Ожидаемые поступления за период — около {fmt_money(stoch_in['amount'])}, этого недостаточно, чтобы покрыть все платежи." if stoch_in and level != "low" else ""
    return head + why + inflow_txt


def _summary(level, prob, gap_date, gap_date_p10, deficit_p50, deficit_p10, res) -> str:
    if res.opening_balance < res.min_balance:
        back = res.daily.index[res.daily.p50 >= res.min_balance]
        tail = f", выход в плюс ожидается к {fmt_date(back[0])}" if len(back) else ", выхода в плюс в горизонте прогноза не видно"
        return f"Остаток уже ниже порога на {fmt_money(res.min_balance - res.opening_balance)}{tail}"
    if level == "high" and gap_date is not None:
        return f"Через {(pd.Timestamp(gap_date) - res.as_of).days} дн. ({fmt_date(gap_date)}) может не хватить {fmt_money(deficit_p50)}"
    if level in ("medium", "high"):
        d = gap_date or gap_date_p10
        return f"Риск нехватки {prob:.0%}" + (f", ближе к {fmt_date(d)}" if d is not None else "")
    if gap_date_p10 is not None:
        return f"Риск низкий ({prob:.0%}), но в плохом случае к {fmt_date(gap_date_p10)} может не хватить до {fmt_money(deficit_p10)}"
    return "Денег хватает на весь горизонт прогноза"


def _recommend(res: ForecastResult, level: str, deficit_p50: float, deficit_p10: float, ref_date, delays: pd.DataFrame | None) -> list[dict]:
    recs: list[dict] = []
    if level == "low":
        recs.append({"kind": "ok", "title": "Можно планировать спокойно", "text": "Если появятся крупные траты или новые счета — добавьте их в календарь, прогноз пересчитается.", "amount": None})
        return recs
    need = deficit_p50 if deficit_p50 > 0 else deficit_p10
    # 1. ускорить поступления от клиентов с задержками
    if delays is not None and not delays.empty:
        slow = delays[delays.median_delay >= 7].head(3)
        for r in slow.itertuples():
            recs.append(
                {
                    "kind": "collect",
                    "title": f"Напомнить об оплате: {r.counterparty_name}",
                    "text": f"Этот клиент обычно платит с задержкой ~{int(r.median_delay)} дн. Ускорение оплаты на неделю сдвинет поступления до даты риска.",
                    "amount": None,
                }
            )
    # 2. перенести необязательные платежи
    ev = res.events[(res.events.date <= pd.Timestamp(ref_date)) & (res.events.direction == "Списание") & ~res.events.category.isin(MANDATORY_CODES)] if not res.events.empty else res.events
    if ev is not None and not ev.empty:
        ev = ev[ev.amount >= max(0.1 * need, 10_000)].drop_duplicates("label")
        top = ev.sort_values("amount", ascending=False).head(2)
        for e in top.itertuples():
            recs.append({"kind": "postpone", "title": f"Перенести платёж: {e.label}", "text": f"{fmt_money(e.amount)} запланировано на {fmt_date(e.date)}. Перенос на 1–2 недели снизит пиковую нехватку.", "amount": float(e.amount)})
    # 3. предоплата / отсрочка у поставщиков
    recs.append({"kind": "terms", "title": "Договориться об отсрочке с поставщиками", "text": "Отсрочка закупок на 2 недели или предоплата от крупных клиентов закрывает часть разрыва без кредита.", "amount": None})
    # 4. банковский продукт
    if need > 0:
        recs.append(
            {
                "kind": "credit",
                "title": f"Подготовить овердрафт / кредитную линию на {fmt_money(need * 1.2)}",
                "text": f"Нехватка оценивается в {fmt_money(need)} (с запасом 20% — {fmt_money(need * 1.2)}). Оформить лимит лучше заранее: в момент разрыва это дороже и дольше.",
                "amount": round(need * 1.2, -3),
            }
        )
    return recs


def _alerts(res, level, prob, gap_date, gap_date_p10, deficit_p50, deficit_p10, runway, overdue: pd.DataFrame | None) -> list[Alert]:
    alerts: list[Alert] = []
    if level == "high":
        alerts.append(Alert("high", "cash_gap", f"Кассовый разрыв {fmt_date(gap_date)}" if gap_date is not None else "Высокий риск кассового разрыва", f"Вероятность {prob:.0%}, нехватка около {fmt_money(deficit_p50)} (до {fmt_money(deficit_p10)} в плохом случае).", _s(gap_date), round(deficit_p50, 2), round(prob, 3)))
    elif level == "medium":
        d = gap_date or gap_date_p10
        alerts.append(Alert("medium", "cash_gap", "Возможна нехватка денег", f"Вероятность {prob:.0%}" + (f", ближе к {fmt_date(d)}" if d is not None else "") + f". В плохом случае не хватит до {fmt_money(deficit_p10)}.", _s(d), round(deficit_p10, 2), round(prob, 3)))
    elif gap_date_p10 is not None:
        # Ранний сигнал: вероятность пока низкая, но пессимистичные траектории уже пересекают порог.
        alerts.append(Alert("low", "early_warning", f"Ранний сигнал: {fmt_date(gap_date_p10)}", f"В неблагоприятном сценарии (худшие 10 % траекторий) остаток уйдёт ниже порога {fmt_date(gap_date_p10)}, нехватка до {fmt_money(deficit_p10)}. Вероятность пока {prob:.0%}: следите за поступлениями и крупными платежами до этой даты.", _s(gap_date_p10), round(deficit_p10, 2), round(prob, 3)))
    if runway is not None and runway <= 14 and level != "high":
        alerts.append(Alert("medium", "low_runway", f"Денег хватит примерно на {runway} дн.", "По медианному прогнозу остаток станет ниже порога раньше чем через две недели.", None, None, None))
    # крупные платежи впереди относительно текущего остатка
    if not res.events.empty:
        big = res.events[(res.events.direction == "Списание") & (res.events.amount > 0.4 * max(res.opening_balance, 1))].head(3)
        for e in big.itertuples():
            alerts.append(Alert("info", "big_payment", f"Крупный платёж {fmt_date(e.date)}: {e.label}", f"{fmt_money(e.amount)} — это {e.amount / max(res.opening_balance, 1):.0%} текущего остатка.", _s(e.date), float(e.amount), None, {"source": e.source}))
    if overdue is not None and not overdue.empty:
        for o in overdue.head(5).itertuples():
            incoming = o.direction == "Поступление"
            title = "Не пришла оплата" if incoming else "Не исполнен обязательный платёж"
            note = "Поступление перенесено с пониженной вероятностью." if incoming else "Долг сохранён в прогнозе на ближайший день."
            due = getattr(o, 'original_date', o.date)
            alerts.append(Alert("medium", "overdue_income" if incoming else "overdue_expense", f"{title}: {o.counterparty_name or o.label}", f"Срок {fmt_date(due)}, сумма {fmt_money(o.amount)}. {note}", _s(due), float(o.amount), None))
    for note in res.data_quality.get("notes", []):
        alerts.append(Alert("info", "data_quality", "Качество данных", note))
    return alerts
