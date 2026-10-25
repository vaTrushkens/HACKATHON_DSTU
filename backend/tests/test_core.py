"""Тесты ядра: парсинг, сверка остатков, категоризация, регулярность, налоги, прогноз, риск."""
import io

import numpy as np
import pandas as pd
import pytest

from cashflow_core import ingest
from cashflow_core.calendar_ru import is_business_day, shift_to_business_day
from cashflow_core.categorizer import Categorizer, rule_category
from cashflow_core.forecast import ForecastInputs, NaiveMeanModel, Scenario, run_forecast
from cashflow_core.planfact import match_events
from cashflow_core.recurring import detect_recurring, project
from cashflow_core.risk import assess
from cashflow_core.taxes import tax_calendar

CSV = "﻿company_id;operation_id;operation_date;amount;direction;counterparty_inn;counterparty_name;payment_purpose;balance_eod\n" \
      "C1;1;2026-01-12;1000.00;Поступление;0012345678;ООО «Клиент»;Оплата по счету № 1 от 05.01.2026;11000.00\n" \
      "C1;2;2026-01-12;500.00;Списание;0068275202;ПАО «Банк»;Комиссия за ведение счета;11000.00\n" \
      "C1;2;2026-01-12;500.00;Списание;0068275202;ПАО «Банк»;Комиссия за ведение счета;11000.00\n" \
      "C1;3;2026-01-13;200.00;Списание;;Физическое лицо;;10800.00\n" \
      "C1;4;2026-01-20;300.00;Списание;7727406020;Казначейство России (ФНС России);ЕНП;5500.00\n"


def _synthetic_ops(months=14, seed=1) -> pd.DataFrame:
    """Компания: ежедневный эквайринг, зарплата 10/25, аренда 5-го, ЕНП 28-го."""
    rng = np.random.default_rng(seed)
    rows, bal, i = [], 2_000_000.0, 0
    scheduled = {  # день месяца → платёж; выпадающие на выходные переносятся на рабочий день
        10: ("Списание", 400_000.0, "0068275202", "ПАО «Банк»", "Перечисление заработной платы (окончательный расчет)"),
        25: ("Списание", 250_000.0, "0068275202", "ПАО «Банк»", "Перечисление заработной платы (аванс)"),
        5: ("Списание", 180_000.0, "0011111111", "ООО «Арендодатель-1»", "Оплата аренды нежилого помещения"),
        28: ("Списание", 150_000.0, "7727406020", "Казначейство России (ФНС России)", "Единый налоговый платеж"),
        15: ("Списание", 120_000.0, "0022222222", "ООО «Продукты-Опт»", "Оплата по счету за товар"),
    }
    carried: dict = {}
    for d in pd.date_range("2025-06-01", periods=months * 30, freq="D"):
        if d.day in scheduled:
            carried.setdefault(shift_to_business_day(d, "forward"), []).append(scheduled[d.day])
        if not is_business_day(d):
            continue
        day_rows = [("Поступление", float(rng.normal(60_000, 15_000)), "0068275202", "ПАО «Банк»", "Зачисление средств по операциям эквайринга")]
        day_rows += carried.pop(d, [])
        for direction, amt, inn, name, purpose in day_rows:
            amt = max(abs(amt), 1.0)
            bal += amt if direction == "Поступление" else -amt
            i += 1
            rows.append({"operation_id": f"S{i:05d}", "operation_date": d, "amount": round(amt, 2), "direction": direction, "counterparty_inn": inn, "counterparty_name": name, "payment_purpose": purpose, "balance_eod": None})
        for r in rows[-len(day_rows):]:
            r["balance_eod"] = round(bal, 2)
    return pd.DataFrame(rows)


# ---------------- ingest ----------------
def test_read_csv_keeps_inn_leading_zeros_and_bom():
    df = ingest.read_statement_csv(io.BytesIO(CSV.encode("utf-8")))
    assert list(df.columns)[0] == "company_id"
    assert df.counterparty_inn.iloc[0] == "0012345678"


def test_clean_removes_duplicates_and_detects_gap():
    df, rep = ingest.clean_operations(ingest.read_statement_csv(io.BytesIO(CSV.encode("utf-8"))))
    assert rep.rows_duplicates == 1
    assert len(df) == 4
    assert rep.empty_inn == 1 and rep.empty_purpose == 1
    # 13.01 → 20.01: 10800 − 300 = 10500, а в выписке 5500 → пропуск −5000
    assert rep.gap_days == 1 and rep.gaps[0]["gap_amount"] == -5000.0


def test_opening_balance():
    df, _ = ingest.clean_operations(ingest.read_statement_csv(io.BytesIO(CSV.encode("utf-8"))))
    assert ingest.opening_balance(df) == pytest.approx(10500.0)


def test_1c_client_bank_parser():
    text = "1CClientBankExchange\nВерсияФормата=1.03\nРасчСчет=40702810000000000001\nНачальныйОстаток=1000.00\n" \
           "СекцияДокумент=Платежное поручение\nНомер=1\nДата=15.01.2026\nСумма=250.50\nПлательщикСчет=40702810000000000001\nПолучательИНН=7727406020\nПолучатель1=ФНС\nНазначениеПлатежа=ЕНП\nДатаСписано=15.01.2026\nКонецДокумента\n" \
           "СекцияДокумент=Платежное поручение\nНомер=2\nДата=16.01.2026\nСумма=1000\nПлательщикСчет=40802810000000000002\nПлательщикИНН=0012345678\nПлательщик1=ООО Клиент\nНазначениеПлатежа=Оплата по счету\nДатаПоступило=16.01.2026\nКонецДокумента\nКонецФайла"
    df = ingest.read_1c_client_bank(text)
    assert list(df.direction) == ["Списание", "Поступление"]
    assert df.balance_eod.tolist() == pytest.approx([749.5, 1749.5])
    assert df.counterparty_inn.tolist() == ["7727406020", "0012345678"]


# ---------------- categorization ----------------
@pytest.mark.parametrize("direction,purpose,cp,expected", [
    ("Списание", "Единый налоговый платеж", "Казначейство России", "TAXES"),
    ("Списание", "ЕНП", "Казначейство", "TAXES"),
    ("Списание", "Перечисление заработной платы (аванс) за октябрь", "ПАО «Банк»", "SALARY"),
    ("Списание", "Оплата аренды нежилого помещения по договору", "АО «Арендодатель-1»", "RENT"),
    ("Списание", "Погашение кредита по договору № КД-1: основной долг, проценты", "ПАО «Банк»", "LOAN_OUT"),
    ("Списание", "Лизинговый платеж по договору лизинга", "АО «Лизинговая компания»", "LOAN_OUT"),
    ("Списание", "Возврат беспроцентного займа учредителю", "Учредитель", "OWNER_OUT"),
    ("Списание", "Оплата по счету за товар", "ООО «Продукты-Опт»", "SUPPLIERS"),
    ("Списание", "Комиссия за ведение счета", "ПАО «Банк»", "BANK_FEES"),
    ("Поступление", "Зачисление средств по операциям эквайринга", "ПАО «Банк»", "REV_ACQUIRING"),
    ("Поступление", "Зачисление по операциям оплаты через СБП (QR-код)", "ПАО «Банк»", "REV_ACQUIRING"),
    ("Поступление", "Предоставление беспроцентного займа учредителем", "Учредитель (физическое лицо)", "OWNER_IN"),
    ("Поступление", "Выдача кредита (транш) по договору кредитной линии", "ПАО «Банк»", "LOAN_IN"),
    ("Поступление", "Оплата по счету № 5 от 01.02.2026 за услуги", "ООО «Клиент»", "REV_CLIENTS"),
    ("Поступление", "Перечисление денежных средств по агентскому договору за вычетом агентского вознаграждения", "ООО «Маркетплейс»", "REV_MARKETPLACE"),
    ("Поступление", "", "ООО «Неизвестный»", "OTHER_IN"),
])
def test_rules(direction, purpose, cp, expected):
    assert rule_category(direction, purpose, cp)[0] == expected


def test_categorizer_propagates_by_counterparty():
    ops = _synthetic_ops(months=6)
    idx = ops.index[ops.counterparty_name == "ООО «Продукты-Опт»"]  # на имя контрагента правил нет
    ops.loc[idx[-2:], "payment_purpose"] = ""  # у двух последних платежей пустое назначение
    res = Categorizer().transform(ops)
    assert (res.loc[idx, "category"] == "SUPPLIERS").all()
    assert (res.loc[idx[-2:], "category_source"] == "counterparty").all()


# ---------------- recurring & taxes ----------------
def test_detect_recurring_and_project():
    ops = _synthetic_ops()
    ops = pd.concat([ops, Categorizer().transform(ops)], axis=1)
    as_of = pd.Timestamp("2026-06-30")
    pats = detect_recurring(ops, as_of)
    labels = {p.label: p for p in pats}
    assert "Зарплата: аванс" in labels and "Зарплата: окончательный расчёт" in labels
    assert any(p.category == "RENT" for p in pats)
    sal = labels["Зарплата: окончательный расчёт"]
    assert sal.period == "monthly" and sal.day_of_month == 10 and sal.amount == pytest.approx(400_000)
    ev = project(pats, as_of + pd.Timedelta(days=1), as_of + pd.Timedelta(days=45))
    assert (ev.category != "TAXES").all()  # налоги считает календарь
    assert ((ev.date > as_of) & (ev.date <= as_of + pd.Timedelta(days=45))).all()
    assert all(is_business_day(d) for d in ev.date)


def _ops_rows(rows: list[tuple]) -> pd.DataFrame:
    """Операции с уже проставленной категорией: (дата, сумма, направление, ИНН, имя, назначение, категория)."""
    out = pd.DataFrame(rows, columns=["operation_date", "amount", "direction", "counterparty_inn", "counterparty_name", "payment_purpose", "category"])
    out["operation_date"] = pd.to_datetime(out.operation_date)
    out["operation_id"] = [f"R{i}" for i in range(len(out))]
    out["balance_eod"] = 1_000_000.0
    return out


def _monthly(day: int, months: int, amount, direction, inn, name, purpose, category, start="2025-06-01") -> list[tuple]:
    rows = []
    for m in pd.period_range(start, periods=months, freq="M"):
        d = shift_to_business_day(pd.Timestamp(year=m.year, month=m.month, day=day), "forward")
        rows.append((d, amount(m) if callable(amount) else amount, direction, inn, name, purpose(m) if callable(purpose) else purpose, category))
    return rows


MONTHS_RU = ["январь", "февраль", "март", "апрель", "май", "июнь", "июль", "август", "сентябрь", "октябрь", "ноябрь", "декабрь"]


def test_recurring_splits_different_payments_of_one_counterparty_by_purpose():
    # два разных ежемесячных платежа одному банку: группа целиком не периодична, по назначению — два паттерна
    rows = _monthly(1, 13, 990.0, "Списание", "0068275202", "ПАО «Банк»", lambda m: f"Комиссия за ведение счета за {MONTHS_RU[m.month - 1]} {m.year} г. Без НДС", "BANK_FEES")
    rows += _monthly(3, 13, 4500.0, "Списание", "0068275202", "ПАО «Банк»", lambda m: f"Плата за аренду POS-терминала за {m.month:02d}.{m.year}. В том числе НДС (22%) 811.48 руб.", "BANK_FEES")
    pats = detect_recurring(_ops_rows(rows), pd.Timestamp("2026-06-30"))
    fees = sorted([p for p in pats if p.category == "BANK_FEES"], key=lambda p: p.amount)
    assert [p.amount for p in fees] == [990.0, 4500.0]
    assert all(p.period == "monthly" and p.occurrences == 13 and p.probability == 1.0 for p in fees)
    assert fees[0].purpose_hint != fees[1].purpose_hint and "pos" in fees[1].purpose_hint
    assert fees[0].key != fees[1].key and fees[0].label != fees[1].label
    assert fees[0].day_of_month == 1 and fees[1].day_of_month == 3


def test_recurring_falls_back_to_amount_clusters_when_purposes_are_unique():
    # назначения с уникальными словами в каждом платеже: по тексту не сгруппировать, по уровню суммы — два паттерна
    rows = _monthly(1, 13, 990.0, "Списание", "0068275202", "ПАО «Банк»", lambda m: f"Комиссия за операцию {chr(97 + m.month) * 3}", "BANK_FEES")
    rows += _monthly(3, 13, 4500.0, "Списание", "0068275202", "ПАО «Банк»", lambda m: f"Плата за операцию {chr(110 + m.month) * 3}", "BANK_FEES")
    pats = detect_recurring(_ops_rows(rows), pd.Timestamp("2026-06-30"))
    fees = sorted([p for p in pats if p.category == "BANK_FEES"], key=lambda p: p.amount)
    assert [p.amount for p in fees] == [990.0, 4500.0]
    assert [p.key for p in fees] == ["BANK_FEES:0068275202:#amt1", "BANK_FEES:0068275202:#amt2"]
    assert "₽" in fees[0].label


def test_recurring_splits_weekly_cash_deposits_by_weekday():
    rows = []
    for week in pd.date_range("2026-01-05", periods=20, freq="7D"):  # понедельники
        rows.append((week + pd.Timedelta(days=3), 100_000.0, "Поступление", "0063434610", "ООО «Инкассация»", "Взнос наличных. Торговая выручка", "REV_CASH"))
        rows.append((week + pd.Timedelta(days=4), 20_000.0, "Поступление", "0063434610", "ООО «Инкассация»", "Взнос наличных. Торговая выручка", "REV_CASH"))
    pats = detect_recurring(_ops_rows(rows), pd.Timestamp("2026-05-25"))
    by_wd = {p.weekday: p for p in pats if p.category == "REV_CASH"}
    assert set(by_wd) == {3, 4}
    assert by_wd[3].amount == 100_000 and by_wd[4].amount == 20_000 and by_wd[3].period == "weekly"
    assert "четвергам" in by_wd[3].label
    ev = project(pats, pd.Timestamp("2026-05-26"), pd.Timestamp("2026-06-21"))
    assert len(ev) == 8 and all(is_business_day(d) for d in ev.date)  # 12 июня — праздник, перенос на рабочий день
    assert ev.groupby("pattern_key").amount.sum().tolist() == [400_000, 80_000]


def test_mandatory_payment_is_detected_from_two_occurrences_only():
    # компания открылась недавно: два платежа аренды уже дают паттерн (с низкой уверенностью), два платежа за услуги — нет
    rows = _monthly(10, 2, 150_000.0, "Списание", "0011111111", "ООО «Арендодатель»", "Оплата аренды помещения", "RENT", start="2026-04-01")
    rows += _monthly(12, 2, 9_000.0, "Списание", "0022222222", "ООО «Клининг»", "Оплата по счету за уборку", "SERVICES", start="2026-04-01")
    pats = detect_recurring(_ops_rows(rows), pd.Timestamp("2026-05-31"))
    assert [p.category for p in pats] == ["RENT"]
    rent = pats[0]
    assert rent.occurrences == 2 and rent.confidence <= 0.45 and rent.probability == 0.75 and rent.active


def test_project_passes_hit_rate_as_probability_and_timing_spread():
    # подписка оплачивается 10 месяцев из 12 → вероятность ≈ 0.83; зарплата — всегда 1.0
    rows = [r for i, r in enumerate(_monthly(15, 12, 12_000.0, "Списание", "0033333333", "ООО «Облако»", "Оплата подписки на облачный сервис", "SERVICES")) if i not in (3, 8)]
    rows += _monthly(10, 12, 400_000.0, "Списание", "0068275202", "ПАО «Банк»", "Перечисление заработной платы (окончательный расчет)", "SALARY")
    pats = detect_recurring(_ops_rows(rows), pd.Timestamp("2026-05-31"))
    by_cat = {p.category: p for p in pats}
    assert by_cat["SERVICES"].probability == pytest.approx(10 / 12, abs=0.01)
    assert by_cat["SALARY"].probability == 1.0
    ev = project(pats, pd.Timestamp("2026-06-01"), pd.Timestamp("2026-06-30"))
    assert {"probability", "timing_std_days"} <= set(ev.columns)
    assert ev.set_index("category").probability.to_dict() == {"SERVICES": pytest.approx(10 / 12, abs=0.01), "SALARY": 1.0}
    assert (ev.timing_std_days >= 0).all()


def test_salary_with_delay_compensation_stays_in_final_group():
    purposes = lambda m: f"Перечисление заработной платы (окончательный расчет) за {m.strftime('%B')}" + (" с учетом компенсации за задержку выплаты (ст. 236 ТК РФ)" if m.month in (3, 4) else "")
    rows = _monthly(15, 12, 300_000.0, "Списание", "0068275202", "ПАО «Банк»", purposes, "SALARY")
    rows += _monthly(15, 3, 50_000.0, "Списание", "0068275202", "ПАО «Банк»", "Перечисление отпускных", "SALARY", start="2026-01-01")
    pats = detect_recurring(_ops_rows(rows), pd.Timestamp("2026-05-31"))
    sal = [p for p in pats if p.category == "SALARY"]
    assert len(sal) == 1 and sal[0].subtype == "final" and sal[0].occurrences == 12 and sal[0].amount == 300_000


def test_next_dates_quarterly_keeps_phase_of_last_payment():
    from cashflow_core.recurring import RecurringPattern, next_dates

    p = RecurringPattern(key="k", category="SERVICES", direction="Списание", counterparty_name="X", counterparty_inn="", subtype="", period="quarterly", day_of_month=15, weekday=None, amount=1.0, amount_cv=0.0, occurrences=4, last_date=pd.Timestamp("2026-04-15").date(), first_date=pd.Timestamp("2025-07-15").date(), confidence=0.8, active=True)
    dates = next_dates(p, pd.Timestamp("2026-06-01"), pd.Timestamp("2026-11-30"))
    assert [d.strftime("%m-%d") for d in dates] == ["07-15", "10-15"]


def test_end_of_month_pattern_is_clamped_and_shifted_backward():
    rows = []
    for m in pd.period_range("2025-06-01", periods=12, freq="M"):
        rows.append((shift_to_business_day(m.end_time.normalize(), "backward"), 30_000.0, "Списание", "0044444444", "ООО «Связь»", "Оплата услуг связи", "UTILITIES"))
    pats = detect_recurring(_ops_rows(rows), pd.Timestamp("2026-05-31"))
    assert pats[0].day_of_month == 31
    ev = project(pats, pd.Timestamp("2026-06-01"), pd.Timestamp("2026-08-31"))
    assert ev.date.tolist() == [shift_to_business_day(pd.Timestamp(d), "backward") for d in ("2026-06-30", "2026-07-31", "2026-08-31")]


def test_tax_calendar_uses_28th_shifted_to_business_day():
    ops = _synthetic_ops()
    ops = pd.concat([ops, Categorizer().transform(ops)], axis=1)
    as_of = pd.Timestamp("2026-06-30")
    cal = tax_calendar(ops, as_of, as_of + pd.Timedelta(days=60))
    assert len(cal) == 2
    assert all(is_business_day(d) for d in cal.date)
    assert cal.date.iloc[0] == shift_to_business_day(pd.Timestamp("2026-07-28"), "forward")
    assert cal.amount.iloc[0] == pytest.approx(150_000, rel=0.05)


def test_shift_to_business_day():
    assert shift_to_business_day(pd.Timestamp("2026-02-28"), "forward") == pd.Timestamp("2026-03-02")  # суббота → понедельник
    assert shift_to_business_day(pd.Timestamp("2026-01-04"), "forward") >= pd.Timestamp("2026-01-09")  # новогодние каникулы


# ---------------- forecast & risk ----------------
def _forecast(ops, as_of, scenario=None, min_balance=0.0):
    pats = detect_recurring(ops, as_of)
    inp = ForecastInputs(ops=ops, as_of=as_of, horizon_days=45, patterns=pats, min_balance=min_balance, scenario=scenario or Scenario())
    return run_forecast(inp, NaiveMeanModel(), n_paths=300, seed=1)


def test_forecast_shapes_and_monotone_quantiles():
    ops = _synthetic_ops()
    ops = pd.concat([ops, Categorizer().transform(ops)], axis=1)
    res = _forecast(ops, pd.Timestamp("2026-06-30"))
    assert len(res.daily) == 45
    assert (res.daily.p10 <= res.daily.p50).all() and (res.daily.p50 <= res.daily.p90).all()
    assert res.paths.shape == (300, 45)
    assert res.components["det_out_total"] > 0  # зарплата, аренда, ЕНП попали в детерминированный слой
    assert set(res.events.source) <= {"recurring", "tax_calendar"}


def test_financing_inflows_are_excluded_from_forecast():
    ops = _synthetic_ops()
    ops.loc[len(ops)] = {"operation_id": "X1", "operation_date": pd.Timestamp("2026-06-29"), "amount": 5_000_000, "direction": "Поступление", "counterparty_inn": "", "counterparty_name": "Учредитель (физическое лицо)", "payment_purpose": "Предоставление беспроцентного займа учредителем", "balance_eod": 9_000_000}
    ops = pd.concat([ops, Categorizer().transform(ops)], axis=1)
    res = _forecast(ops, pd.Timestamp("2026-06-30"))
    assert res.components["det_in_total"] == 0
    assert not (res.events.category == "OWNER_IN").any()


def test_risk_detects_gap_when_big_payment_exceeds_balance():
    ops = _synthetic_ops()
    ops = pd.concat([ops, Categorizer().transform(ops)], axis=1)
    as_of = pd.Timestamp("2026-06-30")
    sc = Scenario(extra_expenses=[{"date": "2026-07-07", "amount": 15_000_000, "label": "Штраф"}])
    res = _forecast(ops, as_of, scenario=sc)
    risk = assess(res)
    assert risk.level == "high" and risk.probability > 0.9
    assert risk.gap_date == "2026-07-07"
    assert risk.deficit_p50 > 5_000_000  # 15 млн штрафа при остатке ~7 млн
    assert any(a.kind == "cash_gap" for a in risk.alerts)
    assert any(r["kind"] == "credit" for r in risk.recommendations)


def test_scenario_revenue_drop_lowers_balance():
    ops = _synthetic_ops()
    ops = pd.concat([ops, Categorizer().transform(ops)], axis=1)
    as_of = pd.Timestamp("2026-06-30")
    base = _forecast(ops, as_of)
    worse = _forecast(ops, as_of, Scenario(revenue_change_pct=-30))
    assert worse.components["end_balance_p50"] < base.components["end_balance_p50"]


def test_short_history_falls_back_to_profile():
    ops = _synthetic_ops(months=1)
    ops = pd.concat([ops, Categorizer().transform(ops)], axis=1)
    res = _forecast(ops, ops.operation_date.max())
    assert res.model_name == "short_history_mean"
    assert res.data_quality["level"] == "low"


# ---------------- plan-fact ----------------
def test_match_events_marks_matched_and_overdue():
    today = pd.Timestamp("2026-07-20")
    expected = pd.DataFrame([
        {"id": 1, "date": pd.Timestamp("2026-07-10"), "amount": 100_000.0, "direction": "Поступление", "category": "REV_CLIENTS", "counterparty_inn": "0012345678", "counterparty_name": "ООО «Клиент»"},
        {"id": 2, "date": pd.Timestamp("2026-07-12"), "amount": 50_000.0, "direction": "Поступление", "category": "REV_CLIENTS", "counterparty_inn": "0099999999", "counterparty_name": "ООО «Должник»"},
        {"id": 3, "date": pd.Timestamp("2026-07-25"), "amount": 70_000.0, "direction": "Поступление", "category": "REV_CLIENTS", "counterparty_inn": "0012345678", "counterparty_name": "ООО «Клиент»"},
    ])
    actual = pd.DataFrame([{"operation_id": "A1", "operation_date": pd.Timestamp("2026-07-11"), "amount": 98_000.0, "direction": "Поступление", "category": "REV_CLIENTS", "counterparty_inn": "0012345678", "counterparty_name": "ООО «Клиент»", "payment_purpose": "Оплата"}])
    res = match_events(expected, actual, today).set_index("id")
    assert res.loc[1, "status"] == "matched" and res.loc[1, "matched_operation_id"] == "A1"
    assert res.loc[2, "status"] == "overdue"
    assert res.loc[3, "status"] == "pending"
