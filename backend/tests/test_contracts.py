import pandas as pd
import pytest

from cashflow_core.calendar_ru import is_business_day
from cashflow_core.categorizer import rule_category
from cashflow_core.contracts import ContractTerms, contract_schedule


def test_prepay_final_with_deferral_and_client_delay():
    t = ContractTerms(direction="Поступление", total_amount=1_000_000, start_date=pd.Timestamp("2026-10-01"), end_date=pd.Timestamp("2026-11-30"), schedule_type="prepay_final", prepayment_pct=30, deferral_days=10, client_median_delay=5, client_delay_std=4)
    ev = contract_schedule(t)
    assert [e["amount"] for e in ev] == [300_000, 700_000]
    assert ev[0]["date"] >= pd.Timestamp("2026-10-06")  # старт + задержка клиента, рабочий день
    assert ev[1]["date"] >= pd.Timestamp("2026-12-15")  # закрытие + отсрочка 10 + задержка 5
    assert all(is_business_day(e["date"]) for e in ev)
    assert ev[0]["probability"] == 0.9 and ev[0]["timing_std_days"] == 4


def test_equal_installments_sum_matches_total():
    t = ContractTerms(direction="Списание", total_amount=100_000, start_date=pd.Timestamp("2026-10-05"), schedule_type="equal", installments=3, day_of_month=5)
    ev = contract_schedule(t)
    assert len(ev) == 3
    assert round(sum(e["amount"] for e in ev), 2) == 100_000
    assert ev[0]["probability"] == 1.0 and ev[0]["timing_std_days"] == 0.0
    assert [e["date"].month for e in ev] == [10, 11, 12]


def test_postpay_and_milestones():
    post = contract_schedule(ContractTerms(direction="Поступление", total_amount=500_000, start_date=pd.Timestamp("2026-10-01"), end_date=pd.Timestamp("2026-10-20"), schedule_type="postpay", deferral_days=30))
    assert len(post) == 1 and post[0]["date"] >= pd.Timestamp("2026-11-19")
    ms = contract_schedule(ContractTerms(direction="Поступление", total_amount=1_000_000, start_date=pd.Timestamp("2026-10-01"), schedule_type="milestones", milestones=[{"date": "2026-10-15", "pct": 40}, {"date": "2026-12-01", "amount": 600_000, "label": "Финал"}]))
    assert [e["amount"] for e in ms] == [400_000, 600_000] and ms[1]["label"] == "Финал"


def test_unknown_schedule():
    with pytest.raises(ValueError):
        contract_schedule(ContractTerms(direction="Списание", total_amount=1, start_date=pd.Timestamp("2026-10-01"), schedule_type="weird"))


@pytest.mark.parametrize("purpose,expected", [
    ("Оплата по счету № 1767 от 29.10.2025 за метизы. Без налога (НДС)", "SUPPLIERS"),
    ("Оплата аренды нежилого помещения по договору № 94/26 за февраль 2025 г. Без налога (НДС)", "RENT"),
    ("Пополнение лицевого счета по договору № 3191 (топливные карты). Сумма 85000.00 В т.ч. НДС 20% 14166.67", "SUPPLIERS"),
    ("Оплата по счету № 519 от 23.04.2026 за услуги хранения и подработки. Без НДС", "SERVICES"),
    ("Единый налоговый платеж. НДС не облагается", "TAXES"),
])
def test_vat_tail_does_not_trigger_taxes(purpose, expected):
    assert rule_category("Списание", purpose, "ООО «Контрагент»")[0] == expected
