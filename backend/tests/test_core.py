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
