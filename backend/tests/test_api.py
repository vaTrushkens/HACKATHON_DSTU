"""Тесты API: аутентификация, изоляция компаний, загрузка выписки, прогноз, события, маскирование логов."""
import io
import logging

import pytest
from django.contrib.auth import get_user_model
from rest_framework.test import APIClient

from apps.accounts.models import CompanyMembership
from apps.companies.models import Company
from core.logging_filters import mask

from .test_core import _synthetic_ops


def _csv_bytes(df) -> bytes:
    out = df.copy()
    out.insert(0, "company_id", "T1")
    out["operation_date"] = out.operation_date.dt.strftime("%Y-%m-%d")
    return ("﻿" + out.to_csv(sep=";", index=False)).encode("utf-8")


@pytest.fixture
def client_and_company(db):
    User = get_user_model()
    user = User.objects.create_user("owner", password="pass12345")
    other = User.objects.create_user("stranger", password="pass12345")
    company = Company.objects.create(code="T1", name="Тест", legal_form="ООО", industry="Общепит (кофейня, кафе)")
    CompanyMembership.objects.create(user=user, company=company)
    c = APIClient()
    r = c.post("/api/auth/token/", {"username": "owner", "password": "pass12345"}, format="json")
    assert r.status_code == 200
    c.credentials(HTTP_AUTHORIZATION=f"Bearer {r.data['access']}")
    s = APIClient()
    r2 = s.post("/api/auth/token/", {"username": "stranger", "password": "pass12345"}, format="json")
    s.credentials(HTTP_AUTHORIZATION=f"Bearer {r2.data['access']}")
    return c, s, company


def test_requires_auth(db):
    assert APIClient().get("/api/companies/").status_code == 401


def test_company_isolation(client_and_company):
    c, s, company = client_and_company
    assert c.get(f"/api/companies/{company.id}/").status_code == 200
    assert s.get(f"/api/companies/{company.id}/").status_code == 403
    assert s.get("/api/companies/").data["count"] == 0


def test_upload_forecast_events_flow(client_and_company):
    c, _, company = client_and_company
    ops = _synthetic_ops(months=13)
    r = c.post(f"/api/companies/{company.id}/upload/", {"file": io.BytesIO(_csv_bytes(ops))}, format="multipart")
    assert r.status_code == 200, r.data
    assert r.data["report"]["rows_new"] == len(ops)
    # повторная загрузка того же файла не создаёт дублей
    r = c.post(f"/api/companies/{company.id}/upload/", {"file": io.BytesIO(_csv_bytes(ops))}, format="multipart")
    assert r.data["report"]["rows_new"] == 0

    ops_list = c.get(f"/api/companies/{company.id}/operations/", {"category": "SALARY"}).data
    assert ops_list["count"] > 0
    rec = c.get(f"/api/companies/{company.id}/recurring/").data
    assert any(p["category"] == "SALARY" for p in rec)

    fc = c.get(f"/api/companies/{company.id}/forecast/", {"horizon": 45}).data
    assert len(fc["forecast"]["daily"]) == 45
    assert fc["risk"]["level"] in ("low", "medium", "high")
    assert fc["run_id"]

    # ручное событие учитывается в прогнозе
    as_of = fc["forecast"]["as_of"]
    ev = c.post(f"/api/companies/{company.id}/events/", {"direction": "Списание", "amount": 50_000_000, "date": "2026-07-15", "label": "Покупка оборудования", "category": "OTHER_OUT"}, format="json")
    assert ev.status_code == 201, ev.data
    fc2 = c.get(f"/api/companies/{company.id}/forecast/", {"horizon": 45}).data
    assert fc2["risk"]["level"] == "high"
    assert any(e["source"] == "manual" for e in fc2["forecast"]["events"])
    assert as_of == fc2["forecast"]["as_of"]

    sc = c.post(f"/api/companies/{company.id}/scenario/", {"scenario": {"revenue_change_pct": -30}, "horizon": 45}, format="json").data
    assert sc["delta"]["end_balance_p50"] < 0

    ctx = c.get(f"/api/companies/{company.id}/assistant/context/").data
    assert ctx["enabled"] is False and len(ctx["context"]["tools"]) >= 4


def test_contract_generates_schedule_and_affects_forecast(client_and_company):
    c, _, company = client_and_company
    c.post(f"/api/companies/{company.id}/upload/", {"file": io.BytesIO(_csv_bytes(_synthetic_ops(months=13)))}, format="multipart")
    base = c.get(f"/api/companies/{company.id}/forecast/", {"horizon": 60}).data["forecast"]["components"]["det_in_total"]
    r = c.post(
        f"/api/companies/{company.id}/contracts/",
        {"counterparty_name": "ООО «Заказчик»", "direction": "Поступление", "total_amount": 1_000_000, "start_date": "2026-07-05", "end_date": "2026-08-10", "schedule_type": "prepay_final", "prepayment_pct": 40, "deferral_days": 5},
        format="json",
    )
    assert r.status_code == 201, r.data
    assert [e["amount"] for e in r.data["events"]] == [400_000, 600_000]
    after = c.get(f"/api/companies/{company.id}/forecast/", {"horizon": 60}).data["forecast"]
    assert after["components"]["det_in_total"] > base
    assert any(e["source"] == "contract" for e in after["events"])
    # расторжение убирает платежи из календаря
    assert c.delete(f"/api/companies/{company.id}/contracts/{r.data['id']}/").status_code == 204
    assert c.get(f"/api/companies/{company.id}/contracts/").data == []
    bad = c.post(f"/api/companies/{company.id}/contracts/", {"counterparty_name": "X", "direction": "Списание", "total_amount": 100, "start_date": "2026-07-05", "schedule_type": "postpay"}, format="json")
    assert bad.status_code == 400


def test_manual_category_change(client_and_company):
    c, _, company = client_and_company
    c.post(f"/api/companies/{company.id}/upload/", {"file": io.BytesIO(_csv_bytes(_synthetic_ops(months=3)))}, format="multipart")
    op = c.get(f"/api/companies/{company.id}/operations/", {"category": "SUPPLIERS"}).data["results"][0]
    r = c.patch(f"/api/companies/{company.id}/operations/{op['id']}/", {"category": "MARKETING"}, format="json")
    assert r.status_code == 200 and r.data["category"] == "MARKETING" and r.data["category_source"] == "manual"


def test_sensitive_data_masked_in_logs():
    msg = "op {'counterparty_inn': '0012345678', 'amount': 12345.67, 'payment_purpose': 'Оплата по счету'} inn=0012345678 acc 40702810000000000001"
    out = mask(msg)
    assert "0012345678" not in out and "40702810000000000001" not in out
    assert "12345.67" not in out and "Оплата по счету" not in out


def test_logging_filter_applied(caplog):
    logger = logging.getLogger("apps.test")
    from core.logging_filters import SensitiveDataFilter

    logger.addFilter(SensitiveDataFilter())
    with caplog.at_level(logging.INFO, logger="apps.test"):
        logger.info("ИНН контрагента %s", "007934320607")
    assert "007934320607" not in caplog.text
