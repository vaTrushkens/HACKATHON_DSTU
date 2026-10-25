"""Детектор регулярных платежей и поступлений.

Операции группируются по (категория, контрагент, подтип), для каждой группы
оценивается периодичность, типичный день и сумма. Если группа целиком не
регулярна, она делится тоньше:

* по «сигнатуре назначения» (текст без номеров, дат и сумм) — так разделяются,
  например, комиссия за ведение счёта и аренда POS-терминала от одного банка
  или несколько видов платежей в ФНС;
* по дню недели — для потоков, которые идут несколько раз в неделю
  (внесение наличной выручки по четвергам и пятницам).

Кроме периода и суммы у паттерна оценивается доля периодов, в которые платёж
действительно прошёл (``hit_rate``), и разброс срока в днях — они уходят в
прогноз как вероятность события и неопределённость даты. Результат — список
``RecurringPattern`` плюс их проекция на горизонт прогноза (``project``).
"""
from __future__ import annotations

import datetime as dt
import re
from dataclasses import asdict, dataclass, field

import numpy as np
import pandas as pd

from .calendar_ru import nth_day_of_month, shift_to_business_day
from .categories import FINANCING_CODES, FIXED_CODES, IN, MANDATORY_CODES
from .categorizer.pipeline import counterparty_key
from .categorizer.rules import is_salary_advance, normalize_text, strip_vat_tail

PERIODS = {  # название → (мин. интервал, макс. интервал в днях, длина периода)
    "weekly": (5, 9, 7),
    "biweekly": (12, 17, 14),
    "monthly": (26, 36, 30),
    "quarterly": (82, 100, 91),
}
PERIOD_DAYS_EXACT = {"weekly": 7.0, "biweekly": 14.0, "monthly": 30.44, "quarterly": 91.31}
MIN_OCCURRENCES = 3
MIN_OCCURRENCES_MANDATORY = 2  # зарплата/аренда/кредит/налоги: два повтора уже сигнал, но с низкой уверенностью
MAX_GAP_CV = 0.35  # допустимый разброс интервалов
MAX_AMOUNT_CV_VARIABLE = 0.5  # клиентские поступления/закупки считаем регулярными только при стабильной сумме
LOOKBACK_DAYS = 400
MAX_TIMING_STD = 10.0
AMOUNT_RECENT_N = 6  # сколько последних платежей усреднять при шумной сумме
# категории, где один контрагент может получать несколько разных регулярных платежей
SPLIT_BY_PURPOSE = FIXED_CODES | {"OWNER_OUT", "REV_MARKETPLACE"}
# потоки, которые бывают несколько раз в неделю по фиксированным дням (инкассация по четвергам и пятницам,
# недельная оплата рабочих); эквайринг и оплаты клиентов сюда не входят — их ведёт статистическая модель
SPLIT_BY_WEEKDAY = {"REV_CASH", "CONTRACTORS", "SALARY"}
# разовые выплаты персоналу, которые не должны искажать сумму регулярной зарплаты
# («компенсация за задержку выплаты» в назначении зарплаты сюда не относится)
SALARY_OTHER_RE = re.compile(r"отпускн|больничн|увольнен|преми[яи]|материальн\w+ помощ")

_MONTHS = r"январ|феврал|март|апрел|ма[йя]|июн|июл|август|сентябр|октябр|ноябр|декабр"
_SIG_DROP_RE = re.compile(
    r"(№|\bn)\s*[\w\-/]+"  # номера документов
    r"|\d[\d\s.,\-/:%]*"  # даты, суммы, проценты
    r"|\b(" + _MONTHS + r")\w*\b"  # месяцы
    r"|\b(г|год|года|гг|руб|коп|сумма|без|ндс|не|облагается|т|ч|вт|по|за|от|и|на|с|до|в|из|у|к|о|об|для|том|числе|тч)\b"
    r"|[«»\"'()]",
    re.I,
)
_ENP_RE = re.compile(r"единый налогов\w+ платеж|\bенп\b")


def purpose_signature(purpose: str | None, words: int = 3) -> str:
    """Устойчивая «сигнатура» назначения: первые содержательные слова без номеров, дат, сумм, месяцев
    и служебных слов («комиссия ведение счета», «плата аренду pos»)."""
    s = strip_vat_tail(normalize_text(purpose))
    s = _ENP_RE.sub("енп", s)
    s = _SIG_DROP_RE.sub(" ", s)
    s = re.sub(r"[^\w ]", " ", s)
    return " ".join([w for w in s.split() if len(w) > 1][:words])


@dataclass
class RecurringPattern:
    key: str
    category: str
    direction: str
    counterparty_name: str
    counterparty_inn: str
    subtype: str  # '', 'advance', 'final', 'other'
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
    probability: float = 1.0  # вероятность, что платёж пройдёт в очередной период
    timing_std_days: float = 0.0  # типичный разброс даты, дней
    purpose_hint: str = ""  # сигнатура назначения, если группа разделена по ней

    def as_dict(self) -> dict:
        d = asdict(self)
        d["last_date"] = str(self.last_date)
        d["first_date"] = str(self.first_date)
        return d


def _subtype(row_cat: str, purpose: str) -> str:
    if row_cat == "SALARY":
        if SALARY_OTHER_RE.search(normalize_text(purpose)):
            return "other"
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


def _shift_dir(category: str, day_of_month: int | None) -> str:
    # зарплату и платежи «к концу месяца» переносят на рабочий день назад, остальное (как налоги) — вперёд
    return "backward" if category in ("SALARY", "CONTRACTORS") or (day_of_month or 0) >= 29 else "forward"


def _expected_date(period: str, category: str, dom: int | None, wd: int | None, d: pd.Timestamp) -> pd.Timestamp:
    if period in ("monthly", "quarterly"):
        return shift_to_business_day(nth_day_of_month(d.year, d.month, dom or d.day), _shift_dir(category, dom))
    return d  # для недельных паттернов отклонение считается по дню недели


def _amount_clusters(g: pd.DataFrame, tol: float = 0.2) -> list[pd.DataFrame]:
    """Делит операции группы на кластеры близких сумм (±tol от медианы кластера), от меньших к большим."""
    daily = g.groupby("operation_date").amount.sum().sort_values()
    clusters: list[list] = []
    for d, a in daily.items():
        if clusters and a <= np.median([x[1] for x in clusters[-1]]) * (1 + tol):
            clusters[-1].append((d, a))
        else:
            clusters.append([(d, a)])
    return [g[g.operation_date.isin([d for d, _ in c])] for c in clusters]


def _fit(g: pd.DataFrame, key: str, cat: str, as_of: pd.Timestamp, purpose_hint: str = "", fixed_weekday: int | None = None) -> RecurringPattern | None:
    """Оценивает периодичность группы операций; None — группа не регулярна."""
    # несколько операций в один день с одним контрагентом считаем одним платежом
    daily = g.groupby("operation_date").agg(amount=("amount", "sum")).reset_index().sort_values("operation_date")
    min_n = MIN_OCCURRENCES_MANDATORY if cat in MANDATORY_CODES else MIN_OCCURRENCES
    if len(daily) < min_n:
        return None
    dates = daily.operation_date.values.astype("datetime64[D]")
    gaps = np.diff(dates).astype(int)
    period, cv = _classify_period(gaps)
    if period is None or cv > MAX_GAP_CV:
        return None
    period_len = PERIODS[period][2]
    amounts = daily.amount.values.astype(float)
    amount_cv = float(np.std(amounts) / np.mean(amounts)) if np.mean(amounts) else 0.0
    if cat not in FIXED_CODES and amount_cv > MAX_AMOUNT_CV_VARIABLE:
        return None
    # сумма: при стабильных платежах — медиана последних трёх (быстро подхватывает индексацию аренды),
    # при шумных — медиана последних шести
    amount = float(np.median(amounts[-3:] if (amount_cv <= 0.25 or AMOUNT_RECENT_N == 3) else amounts[-AMOUNT_RECENT_N:]))
    idx = pd.DatetimeIndex(dates)
    last, first = idx[-1], idx[0]
    active = (as_of - last).days <= period_len * 1.6
    dom = wd = None
    if period in ("monthly", "quarterly"):
        dom = int(np.median(idx.day))
        # платежи «в последний рабочий день месяца»: день = 31, обрезается по длине месяца
        # (ЕНП 28-го сюда не попадает: медиана дня должна быть ≥ 29)
        if dom >= 29 and (idx.day >= idx.days_in_month - 2).mean() >= 0.7:
            dom = 31
    else:
        wd = int(fixed_weekday if fixed_weekday is not None else np.median(idx.weekday))
    # доля периодов, в которые платёж действительно прошёл
    expected_n = int(round((last - first).days / PERIOD_DAYS_EXACT[period])) + 1
    hit_rate = float(min(1.0, len(daily) / max(expected_n, 1)))
    # разброс срока: отклонение фактической даты от «ожидаемой по правилу»
    if period in ("monthly", "quarterly"):
        devs = np.array([(d - _expected_date(period, cat, dom, wd, d)).days for d in idx], dtype=float)
    else:
        devs = np.array([((d.weekday() - wd + 3) % 7) - 3 for d in idx], dtype=float)
    devs = np.clip(devs, -15, 15)
    timing_std = float(min(np.std(devs), MAX_TIMING_STD)) if len(devs) > 1 else 0.0
    conf = min(1.0, 0.5 + 0.08 * len(daily)) * (1 - min(cv, 0.35)) * (1 - min(amount_cv, 0.5) * 0.5) * (0.6 + 0.4 * hit_rate)
    if len(daily) < MIN_OCCURRENCES:
        conf = min(conf, 0.45)  # два повтора — только гипотеза
    # обязательные платежи с устойчивой историей считаем неизбежными; остальное — по частоте появления
    if cat in MANDATORY_CODES and len(daily) >= MIN_OCCURRENCES and hit_rate >= 0.75:
        probability = 1.0
    elif len(daily) < MIN_OCCURRENCES:
        probability = 0.75
    else:
        probability = float(np.clip(hit_rate, 0.3, 1.0))
    p = RecurringPattern(
        key=key,
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
        first_date=first.date(),
        confidence=round(float(conf), 3),
        active=bool(active),
        history=[{"date": str(pd.Timestamp(d).date()), "amount": float(a)} for d, a in zip(dates[-6:], amounts[-6:])],
        probability=round(probability, 3),
        timing_std_days=round(timing_std, 1),
        purpose_hint=purpose_hint,
    )
    p.label = make_label(p)
    return p


def detect_recurring(ops: pd.DataFrame, as_of: pd.Timestamp | None = None) -> list[RecurringPattern]:
    """``ops`` — операции с колонками category/direction/... (после категоризации)."""
    if ops.empty:
        return []
    as_of = pd.Timestamp(as_of or ops.operation_date.max()).normalize()
    df = ops[(ops.operation_date <= as_of) & (ops.operation_date > as_of - pd.Timedelta(days=LOOKBACK_DAYS))].copy()
    # регулярность имеет смысл для фиксированных категорий и для стабильных контрагентов-клиентов
    df = df[~df.category.isin(FINANCING_CODES)]
    if df.empty:
        return []
    df["payment_purpose"] = df.payment_purpose.fillna("").astype(str)
    df["counterparty_name"] = df.counterparty_name.fillna("").astype(str)
    df["counterparty_inn"] = df.counterparty_inn.fillna("").astype(str)
    df["cp_key"] = counterparty_key(df)
    df["subtype"] = [_subtype(c, p) for c, p in zip(df.category, df.payment_purpose)]
    # зарплата и налоги проходят через банк/ФНС/физлиц — группируем по категории, а не по контрагенту
    df["grp"] = np.where(df.category.isin(["SALARY", "TAXES"]), df.category + ":" + df.subtype, df.category + ":" + df.cp_key + ":" + df.subtype)

    patterns: list[RecurringPattern] = []
    for grp, g in df.groupby("grp"):
        g = g.sort_values("operation_date")
        cat = g.category.iloc[0]
        if cat == "SALARY" and g.subtype.iloc[0] == "other":
            continue  # отпускные, премии, компенсации — не регулярные
        whole = _fit(g, grp, cat, as_of)
        if whole is not None:
            patterns.append(whole)
            continue
        # группа целиком не регулярна — пробуем разделить на несколько регулярных потоков
        n_days = g.operation_date.nunique()
        if cat in SPLIT_BY_PURPOSE and n_days >= MIN_OCCURRENCES_MANDATORY * 2:
            found = []
            sig = g.payment_purpose.map(purpose_signature)
            if sig.nunique() > 1:
                for s, sub in g.groupby(sig):
                    p = _fit(sub, f"{grp}#{s[:40]}", cat, as_of, purpose_hint=s)
                    if p is not None:
                        found.append(p)
            if not found:
                # назначения нестабильны (уникальные номера, разные формулировки) — делим по уровню суммы
                clusters = _amount_clusters(g)
                for i, sub in enumerate(clusters if len(clusters) > 1 else []):
                    p = _fit(sub, f"{grp}#amt{i + 1}", cat, as_of, purpose_hint=f"≈{np.median(sub.groupby('operation_date').amount.sum()):,.0f} ₽".replace(",", " "))
                    if p is not None:
                        found.append(p)
            patterns.extend(found)
            continue
        if cat in SPLIT_BY_WEEKDAY and n_days >= 2 * MIN_OCCURRENCES:
            gaps = np.diff(np.sort(g.operation_date.unique()).astype("datetime64[D]")).astype(int)
            if len(gaps) and np.median(gaps) < PERIODS["weekly"][0]:  # несколько платежей в неделю
                for wd, sub in g.groupby(g.operation_date.dt.weekday):
                    p = _fit(sub, f"{grp}#wd{wd}", cat, as_of, fixed_weekday=int(wd))
                    if p is not None and p.period in ("weekly", "biweekly"):
                        patterns.append(p)
    patterns.sort(key=lambda p: (-p.amount if p.direction != IN else 0, p.category))
    return patterns


def make_label(p: RecurringPattern) -> str:
    from .categories import LABELS

    base = LABELS.get(p.category, p.category)
    hint = f": {p.purpose_hint}" if p.purpose_hint else ""
    if p.category == "SALARY":
        return "Зарплата: аванс" if p.subtype == "advance" else "Зарплата: окончательный расчёт"
    if p.category == "TAXES":
        return "Единый налоговый платёж" if (not p.purpose_hint or "енп" in p.purpose_hint) else f"Налоги и взносы{hint}"
    name = p.counterparty_name or ""
    if p.weekday is not None and p.key.endswith(f"#wd{p.weekday}"):
        hint = f" по {['понедельникам', 'вторникам', 'средам', 'четвергам', 'пятницам', 'субботам', 'воскресеньям'][p.weekday]}"
    return f"{base} — {name}{hint}" if name else f"{base}{hint}"


def project(patterns: list[RecurringPattern], start: pd.Timestamp, end: pd.Timestamp, skip_taxes: bool = True) -> pd.DataFrame:
    """Разворачивает активные паттерны в ожидаемые события на [start, end].

    Налоги по умолчанию пропускаются: их считает ``taxes.py`` по календарю.
    ``probability`` и ``timing_std_days`` уходят в Монте-Карло: событие разыгрывается
    с вероятностью появления, дата — с наблюдённым разбросом.
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
                    "counterparty_inn": p.counterparty_inn,
                    "source": "recurring",
                    "confidence": p.confidence,
                    "pattern_key": p.key,
                    "probability": float(p.probability),
                    "timing_std_days": float(p.timing_std_days),
                }
            )
    cols = ["date", "amount", "direction", "category", "label", "counterparty_name", "counterparty_inn", "source", "confidence", "pattern_key", "probability", "timing_std_days"]
    return pd.DataFrame(rows, columns=cols)


def next_dates(p: RecurringPattern, start: pd.Timestamp, end: pd.Timestamp) -> list[pd.Timestamp]:
    """Даты паттерна внутри [start, end]. Якорь — последнее наблюдение; если оно позже start
    (прогноз «из прошлого» для демо/бэктеста), шагаем от него назад, чтобы покрыть окно."""
    shift_dir = _shift_dir(p.category, p.day_of_month)
    last = pd.Timestamp(p.last_date)
    lower = max(start, last + pd.Timedelta(days=1)) if last < start else start  # уже наблюдённое не повторяем
    out = []
    if p.period in ("monthly", "quarterly"):
        step = pd.DateOffset(months=1 if p.period == "monthly" else 3)
        # фаза квартала — от последнего наблюдения, а не от начала окна
        cur = pd.Timestamp(year=last.year, month=last.month, day=1)
        while cur > start:
            cur -= step
        while cur <= end:
            d = shift_to_business_day(nth_day_of_month(cur.year, cur.month, p.day_of_month or last.day), shift_dir)
            if lower <= d <= end:
                out.append(d)
            cur += step
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
