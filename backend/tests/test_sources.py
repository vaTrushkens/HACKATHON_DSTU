"""Внешние источники: разбор CommerceML, УПД, таблиц (CSV/XLSX, cp1251) и отчётов ОФД; импорт с дедупликацией и влиянием на прогноз;
коннекторы DaData/банк с подменённым HTTP; API импорта, ключей, синхронизации и проверки реестра."""
import datetime as dt
import io
from types import SimpleNamespace

import pandas as pd
import pytest
from django.contrib.auth import get_user_model
from rest_framework.test import APIClient

from apps.accounts.models import CompanyMembership
from apps.companies.models import Company
from apps.planning import connectors, importers, services
from apps.planning.integrations import RegistryAdapter
from apps.planning.models import CounterpartyCheck, IntegrationConfig, PlannedEvent
from cashflow_core.sources.base import detect_format, parse_amount, parse_date, read_table
from cashflow_core.sources.commerceml import parse_commerceml
from cashflow_core.sources.invoices_table import parse_invoices_table
from cashflow_core.sources.ofd import acquiring_credits, parse_ofd
from cashflow_core.sources.upd import parse_upd

from .test_api import _csv_bytes
from .test_core import _synthetic_ops

COMMERCEML = """<?xml version="1.0" encoding="UTF-8"?>
<КоммерческаяИнформация xmlns="urn:1C.ru:commerceml_2" ВерсияСхемы="2.08" ДатаФормирования="2026-07-01">
  <Документ>
    <Ид>b1</Ид><Номер>15</Номер><Дата>2026-07-01</Дата><ХозОперация>Счет на оплату</ХозОперация><Роль>Продавец</Роль>
    <Валюта>RUB</Валюта><Сумма>120 000.50</Сумма>
    <Контрагенты><Контрагент><Ид>c1</Ид><Наименование>ООО «Ромашка»</Наименование><ИНН>0012345678</ИНН><Роль>Покупатель</Роль></Контрагент></Контрагенты>
    <ЗначенияРеквизитов><ЗначениеРеквизита><Наименование>Срок оплаты</Наименование><Значение>20.07.2026</Значение></ЗначениеРеквизита></ЗначенияРеквизитов>
  </Документ>
  <Документ>
    <Ид>b2</Ид><Номер>16</Номер><Дата>2026-07-02</Дата><ХозОперация>Счет на оплату</ХозОперация><Роль>Покупатель</Роль><Сумма>50000</Сумма>
    <Контрагенты><Контрагент><Наименование>ИП Поставщиков</Наименование><ИНН>001234567890</ИНН></Контрагент></Контрагенты>
  </Документ>
  <Документ><Ид>b3</Ид><Номер>17</Номер><Дата>2026-07-02</Дата><Сумма>0</Сумма></Документ>
  <Документ><Ид>b4</Ид><Номер>18</Номер><Дата>2026-07-03</Дата><ХозОперация>Заказ товара</ХозОперация><Сумма>700</Сумма></Документ>
</КоммерческаяИнформация>"""

UPD = """<?xml version="1.0" encoding="windows-1251"?>
<Файл ИдФайл="ON_NSCHFDOPPR_x_y_20260701_1" ВерсФорм="5.03">
  <Документ КНД="1115131" Функция="СЧФДОП">
    <СвСчФакт НомерДок="УПД-7" ДатаДок="03.07.2026">
      <СвПрод><ИдСв><СвЮЛУч НаимОрг="ООО Тест" ИННЮЛ="0011112222" КПП="770101001"/></ИдСв></СвПрод>
      <СвПокуп><ИдСв><СвИП ИННФЛ="001234567890"><ФИО Фамилия="Иванов" Имя="Пётр"/></СвИП></ИдСв></СвПокуп>
    </СвСчФакт>
    <ТаблСчФакт>
      <СведТов НомСтр="1" НаимТов="Услуги" СтТовУчНал="36000.00"/>
      <ВсегоОпл СтТовБезНДСВсего="30000.00" СтТовУчНалВсего="36000.00"/>
    </ТаблСчФакт>
  </Документ>
</Файл>""".encode("cp1251")

UPD_501_NO_TOTAL = """<?xml version="1.0" encoding="UTF-8"?>
<Файл ИдФайл="ON_NSCHFDOPPR_2" ВерсФорм="5.01">
  <Документ КНД="1115131" Функция="СЧФ">
    <СвСчФакт НомерСчФ="СФ-9" ДатаСчФ="05.07.2026">
      <СвПрод><ИдСв><СвЮЛУч НаимОрг="ООО Поставщик" ИННЮЛ="0099990001"/></ИдСв></СвПрод>
      <СвПокуп><ИдСв><СвЮЛУч НаимОрг="ООО Тест" ИННЮЛ="0011112222"/></ИдСв></СвПокуп>
    </СвСчФакт>
    <ТаблСчФакт>
      <СведТов НомСтр="1" СтТовУчНал="1200.00"/>
      <СведТов НомСтр="2" СтТовУчНал="800.50"/>
    </ТаблСчФакт>
  </Документ>
</Файл>"""

TABLE = "﻿Номер;Дата счёта;Срок оплаты;Контрагент;ИНН;Сумма с НДС;Направление\n21;01.07.2026;15.07.2026;ООО Клиент;0099;75 000,00;выставлен клиенту\n22;2026-07-02;;ООО Поставщик;0088;12 500;получен от поставщика\n"
OFD = "Дата смены;Наличные;Безналичные\n2026-07-03;10 000;45 000,50\n2026-07-04;0;30 000\n"
OFD_TYPED = "Дата чека;Тип оплаты;Сумма\n03.07.2026;Безналичными;100\n03.07.2026;Наличными;40\n03.07.2026;СБП;60\n"


def _xlsx(df: pd.DataFrame) -> bytes:
    buf = io.BytesIO()
    df.to_excel(buf, index=False)
    return buf.getvalue()


def _resp(status=200, payload=None, text=""):
    return SimpleNamespace(status_code=status, json=lambda: payload, text=text)


# ---------- разбор форматов ----------
def test_parse_helpers():
    assert parse_date("20.07.2026") == dt.date(2026, 7, 20) and parse_date("2026-07-20T00:00:00") == dt.date(2026, 7, 20) and parse_date("x") is None and parse_date("31.02.2026") is None
    assert parse_amount("120 000,50") == 120000.5 and parse_amount("1,234,567.89") == 1234567.89 and parse_amount("36000.00 руб.") == 36000.0 and parse_amount("") is None
    assert detect_format("a.xml", COMMERCEML.encode()) == "commerceml" and detect_format("b.xml", UPD) == "upd" and detect_format("c.csv", b"x;y") == "csv" and detect_format("d.xlsx", b"PK") == "xlsx"
    assert detect_format("e.xml", b"<other/>") == "xml"


def test_read_table_csv_cp1251_and_xlsx():
    df = read_table("t.csv", "Контрагент;Сумма\nООО Ъ;10".encode("cp1251"))
    assert list(df.columns) == ["контрагент", "сумма"] and df.iloc[0]["контрагент"] == "ООО Ъ"
    df = read_table("t.xlsx", _xlsx(pd.DataFrame({"Номер": [1], "Сумма": [55.5]})))
    assert df.iloc[0]["сумма"] == "55.5"


def test_commerceml_roles_due_date_and_skips():
    docs = parse_commerceml(COMMERCEML.encode())
    assert [d.number for d in docs] == ["15", "16", "18"]  # нулевая сумма пропущена
    assert docs[0].amount == 120000.5 and docs[0].counterparty_role == "Покупатель" and docs[0].due_date == dt.date(2026, 7, 20) and docs[0].counterparty_inn == "0012345678"
    assert docs[1].counterparty_role == "Продавец"  # роль контрагента выведена из нашей роли «Покупатель»
    assert docs[2].counterparty_name == "" and docs[2].counterparty_role == "" and docs[2].kind == "Заказ товара"


def test_upd_parties_total_and_fallback_total():
    (d,) = parse_upd(UPD)
    assert d.number == "УПД-7" and d.date == dt.date(2026, 7, 3) and d.amount == 36000.0
    assert d.seller_inn == "0011112222" and d.seller_name == "ООО Тест" and d.buyer_inn == "001234567890" and d.buyer_name == "ИП Иванов Пётр"
    (d,) = parse_upd(UPD_501_NO_TOTAL.encode())
    assert d.number == "СФ-9" and d.amount == 2000.5 and d.buyer_name == "ООО Тест" and d.kind == "СЧФ"  # 5.01: НомерСчФ/ДатаСчФ, итог из строк


def test_invoices_table_columns_direction_and_xlsx():
    rows = parse_invoices_table("invoices.csv", TABLE.encode("utf-8"))
    assert rows[0].direction == "Поступление" and rows[0].due_date == dt.date(2026, 7, 15) and rows[0].amount == 75000.0 and rows[0].counterparty_inn == "0099"
    assert rows[1].direction == "Списание" and rows[1].due_date is None and rows[1].date == dt.date(2026, 7, 2)
    rows = parse_invoices_table("inv.xlsx", _xlsx(pd.DataFrame({"Счет": ["A-1"], "Дата": ["2026-07-01"], "Итого": ["1 500"], "Клиент": ["ООО Х"]})))
    assert rows[0].number == "A-1" and rows[0].amount == 1500.0 and rows[0].direction == "" and rows[0].counterparty_name == "ООО Х"
    with pytest.raises(ValueError):
        parse_invoices_table("bad.csv", b"a;b\n1;2\n")


def test_ofd_layouts_t_plus_one_and_errors():
    rows = parse_ofd("ofd.csv", OFD.encode("utf-8"))
    assert rows == [{"date": dt.date(2026, 7, 3), "card": 45000.5, "cash": 10000.0}, {"date": dt.date(2026, 7, 4), "card": 30000.0, "cash": 0.0}]
    credits = acquiring_credits(rows, fee_pct=2.0)
    assert credits[0]["date"] == dt.date(2026, 7, 6) and credits[0]["amount"] == 44100.49  # 3 июля пятница → зачисление в понедельник
    assert parse_ofd("t.csv", OFD_TYPED.encode("utf-8")) == [{"date": dt.date(2026, 7, 3), "card": 160.0, "cash": 40.0}]  # «безналичными» ≠ наличные
    assert parse_ofd("t.xlsx", _xlsx(pd.DataFrame({"Дата": ["2026-07-07"], "Сумма": [500]}))) == [{"date": dt.date(2026, 7, 7), "card": 500.0, "cash": 0.0}]  # без типа — считаем безналом
    with pytest.raises(ValueError):
        parse_ofd("x.csv", "Сумма\n1\n".encode())


# ---------- импорт в календарь ----------
@pytest.fixture
def company(db):
    user = get_user_model().objects.create_user("owner", password="pass12345")
    c = Company.objects.create(code="T1", name="Тест", inn="0011112222", legal_form="ООО", industry="Общепит (кофейня, кафе)", as_of_override=dt.date(2026, 7, 1))
    CompanyMembership.objects.create(user=user, company=c)
    return c


@pytest.fixture
def client(company):
    c = APIClient()
    r = c.post("/api/auth/token/", {"username": "owner", "password": "pass12345"}, format="json")
    c.credentials(HTTP_AUTHORIZATION=f"Bearer {r.data['access']}")
    return c


def test_import_invoices_direction_dedupe_and_due(company):
    rep = importers.import_invoices(company, "docs.xml", COMMERCEML.encode(), deferral_days=10)
    assert rep["created"] == 3 and rep["incoming"] == 2 and rep["outgoing"] == 1 and rep["direction_default"] == 1 and rep["source"] == "1c"
    inc = PlannedEvent.objects.get(company=company, kind="invoice_in", label="Счет на оплату № 15")
    out = PlannedEvent.objects.get(company=company, kind="invoice_out")
    assert inc.due_date == dt.date(2026, 7, 20) and inc.counterparty_name == "ООО «Ромашка»" and inc.category == "REV_CLIENTS" and inc.meta["direction_by"] == "role"
    assert out.due_date == dt.date(2026, 7, 12) and out.probability == 1.0  # нет срока → дата + отсрочка
    assert importers.import_invoices(company, "docs.xml", COMMERCEML.encode())["duplicates"] == 3
    rep = importers.import_invoices(company, "upd.xml", UPD)
    e = PlannedEvent.objects.get(company=company, source="edo")
    assert rep["created"] == 1 and e.direction == "Поступление" and e.counterparty_name == "ИП Иванов Пётр" and e.meta["direction_by"] == "inn"
    rep = importers.import_invoices(company, "sf.xml", UPD_501_NO_TOTAL.encode())
    e = PlannedEvent.objects.get(company=company, label="УПД № СФ-9")
    assert rep["outgoing"] == 1 and e.counterparty_name == "ООО Поставщик" and e.meta["direction_by"] == "inn"  # наш ИНН у покупателя → платим мы
    foreign = UPD.replace('ИННЮЛ="0011112222"'.encode("cp1251"), 'ИННЮЛ="0099999999"'.encode("cp1251")).replace('НомерДок="УПД-7"'.encode("cp1251"), 'НомерДок="УПД-8"'.encode("cp1251"))
    assert importers.import_invoices(company, "upd2.xml", foreign, direction="out")["outgoing"] == 1
    with pytest.raises(ValueError):
        importers.import_invoices(company, "x.xml", b"<other/>")


def test_import_invoices_shifts_client_date_by_typical_delay(company, monkeypatch):
    delays = {"0099": (6.4, 3.0)}  # у клиента 0099 медианная задержка 6 дней, у остальных истории нет
    monkeypatch.setattr(importers, "client_delay", lambda company, inn, name: delays.get(inn, (None, None)))
    csv = "Номер;Дата;Срок оплаты;Контрагент;ИНН;Сумма;Направление\nD-1;2026-07-01;2026-07-15;ООО Клиент;0099;1000;выставлен клиенту\nD-2;2026-07-01;2026-07-15;ООО Новый;0077;1000;выставлен клиенту\nD-3;2026-07-01;2026-07-15;ООО Поставщик;0099;1000;получен от поставщика\n"
    importers.import_invoices(company, "d.csv", csv.encode("utf-8"))
    e1, e2, e3 = (PlannedEvent.objects.get(company=company, label=f"Счёт № D-{i}") for i in (1, 2, 3))
    assert e1.due_date == dt.date(2026, 7, 15) and e1.date == dt.date(2026, 7, 21) and e1.timing_std_days == 3.0  # срок + округлённая задержка клиента
    assert e2.date == e2.due_date and e2.timing_std_days == 0.0  # без истории — по сроку
    assert e3.date == e3.due_date  # наши платежи поставщику не сдвигаем


def test_import_ofd_skips_past_credits(company):
    rep = importers.import_ofd(company, "ofd.csv", OFD.encode("utf-8"), fee_pct=2.0)
    assert rep["created"] == 2 and rep["card_total"] == 75000.5 and rep["cash_total"] == 10000.0
    assert PlannedEvent.objects.filter(company=company, kind="ofd_acquiring", category="REV_ACQUIRING").count() == 2
    assert importers.import_ofd(company, "ofd.csv", OFD.encode("utf-8"))["duplicates"] == 2
    company.as_of_override = dt.date(2026, 7, 10)
    company.save()
    PlannedEvent.objects.filter(company=company).delete()
    assert importers.import_ofd(company, "ofd.csv", OFD.encode("utf-8"))["already_in_statement"] == 2


def test_imported_events_reach_the_forecast(company, client):
    client.post(f"/api/companies/{company.id}/upload/", {"file": io.BytesIO(_csv_bytes(_synthetic_ops(months=13)))}, format="multipart")
    base = client.get(f"/api/companies/{company.id}/forecast/", {"horizon": 30}).data
    as_of = dt.date.fromisoformat(base["forecast"]["as_of"])
    due = as_of + dt.timedelta(days=5)
    csv = f"Номер;Дата;Срок оплаты;Контрагент;Сумма;Направление\nБ-1;{as_of};{due};ООО Крупный;9000000;получен от поставщика\n"
    r = client.post(f"/api/companies/{company.id}/import/invoices/", {"file": io.BytesIO(csv.encode("utf-8"))}, format="multipart")
    assert r.status_code == 200 and r.data["outgoing"] == 1
    ofd = f"Дата смены;Безналичные\n{as_of};250000\n"
    r = client.post(f"/api/companies/{company.id}/import/ofd/", {"file": io.BytesIO(ofd.encode("utf-8")), "fee_pct": 1.5}, format="multipart")
    assert r.status_code == 200 and r.data["created"] == 1
    fc = client.get(f"/api/companies/{company.id}/forecast/", {"horizon": 30}).data
    sources = {e["source"] for e in fc["forecast"]["events"]}
    assert {"1c", "ofd"} <= sources
    assert fc["forecast"]["components"]["end_balance_p50"] < base["forecast"]["components"]["end_balance_p50"] - 8_000_000  # счёт поставщика на 9 млн сдвинул прогноз
    ev = client.get(f"/api/companies/{company.id}/events/").data
    assert any(e["source"] == "ofd" and e["kind_label"].startswith("Эквайринг") for e in ev)


# ---------- коннекторы ----------
def test_dadata_check_maps_status_and_caches(company, monkeypatch):
    IntegrationConfig.objects.create(company=company, kind="dadata", config={"api_key": "k"})
    calls = []
    payload = {"suggestions": [{"value": "ООО РОМАШКА", "data": {"state": {"status": "LIQUIDATING", "liquidation_date": 1767225600000}, "name": {"short_with_opf": "ООО «Ромашка»"}, "type": "LEGAL", "ogrn": "1"}}]}
    monkeypatch.setattr(connectors.requests, "post", lambda url, **kw: calls.append(kw["headers"]["Authorization"]) or _resp(200, payload))
    res = services.check_counterparty(company, "0012345678", "Ромашка")
    assert res["status"] == "ликвидация" and res["risk_level"] == "high" and res["provider"] == "dadata" and res["pay_probability"] == 0.4
    assert services.check_counterparty(company, "0012345678", "Ромашка")["provider"] == "dadata" and calls == ["Token k"]  # второй раз — из кэша
    assert CounterpartyCheck.objects.get(inn="0012345678").details["liquidation_date"] == "2026-01-01"
    monkeypatch.setattr(connectors.requests, "post", lambda url, **kw: _resp(200, {"suggestions": []}))
    assert services.check_counterparty(company, "0000000001", "X")["status"] == "не найден в ЕГРЮЛ/ЕГРИП"
    monkeypatch.setattr(connectors.requests, "post", lambda url, **kw: _resp(403, None, "forbidden"))
    assert services.check_counterparty(company, "0000000002", "Y")["provider"] == "mock"  # ошибка API → заглушка, чат и страницы не падают
    assert services.check_counterparty(company, "", "Без ИНН")["provider"] == "dadata" and services.check_counterparty(company, "", "Без ИНН")["status"] == "ИНН не указан"


def test_registry_falls_back_to_mock_without_key(company):
    res = services.check_counterparty(company, "0012345678", "Ромашка")
    assert res["provider"] == "mock" and res["status"]
    assert RegistryAdapter().check("0012345678") == RegistryAdapter().check("0012345678")  # детерминирована


def test_registry_lowers_invoice_probability(company):
    risky = next(inn for inn in (f"{i:010d}" for i in range(1, 200)) if RegistryAdapter().check(inn)["risk_level"] == "high")
    safe = next(inn for inn in (f"{i:010d}" for i in range(1, 200)) if RegistryAdapter().check(inn)["risk_level"] == "low")
    for inn, label in ((risky, "R"), (safe, "S")):
        PlannedEvent.objects.create(company=company, kind="invoice_in", direction="Поступление", amount=100, date=dt.date(2026, 7, 20), counterparty_inn=inn, counterparty_name=label, label=label, source="1c", probability=0.95)
    assert services.apply_registry_to_invoices(company) == 1
    assert PlannedEvent.objects.get(label="R").probability == 0.4 and PlannedEvent.objects.get(label="S").probability == 0.95
    assert PlannedEvent.objects.get(label="R").meta["registry"]["risk_level"] == "high"


def test_bank_client_contract_and_derived_fallback(company, client, monkeypatch):
    IntegrationConfig.objects.create(company=company, kind="bank", config={"base_url": "https://bank.example/api", "token": "t"})
    data = {
        "/companies/0011112222/loans": {"loans": [{"contract": "КД-1", "schedule": [{"date": "2026-07-10", "amount": 1000}, {"date": "2027-01-10", "amount": 1000}]}]},
        "/companies/0011112222/payroll": {"payments": [{"date": "2026-07-10", "amount": 500}]},
        "/companies/0011112222/payment-orders": {"orders": [{"id": 7, "date": "2026-07-03", "amount": 50, "counterparty": "ООО П", "inn": "1", "purpose": "за товар"}]},
    }
    monkeypatch.setattr(connectors.requests, "get", lambda url, **kw: _resp(200, data[url.replace("https://bank.example/api", "")]))
    rep = services.sync_bank(company, pd.Timestamp("2026-07-01"), 60)
    kinds = sorted(PlannedEvent.objects.filter(company=company, source="bank").values_list("kind", flat=True))
    assert rep == {"mode": "api", "events": 3} and kinds == ["bank_credit", "bank_order", "bank_payroll"]
    assert IntegrationConfig.objects.get(company=company, kind="bank").last_status == "ok"
    monkeypatch.setattr(connectors.requests, "get", lambda url, **kw: _resp(500, None, "boom"))
    with pytest.raises(connectors.ConnectorError):
        services.sync_bank(company, pd.Timestamp("2026-07-01"), 60)
    assert IntegrationConfig.objects.get(company=company, kind="bank").last_status == "error"  # статус ошибки не откатывается
    IntegrationConfig.objects.filter(company=company).delete()
    client.post(f"/api/companies/{company.id}/upload/", {"file": io.BytesIO(_csv_bytes(_synthetic_ops(months=13)))}, format="multipart")
    r = client.post(f"/api/companies/{company.id}/integrations/", {"source": "bank"}, format="json")
    assert r.status_code == 200 and r.data["mode"] == "derived" and r.data["events"] > 0  # без API — зарплатный проект и кредиты из выписки
    assert PlannedEvent.objects.filter(company=company, source="bank", kind="bank_payroll").exists()


def test_key_check_reports_http_hint(company, monkeypatch):
    IntegrationConfig.objects.create(company=company, kind="dadata", config={"api_key": "bad"})
    monkeypatch.setattr(connectors.requests, "post", lambda url, **kw: _resp(401, None, "unauthorized"))
    res = connectors.ping(connectors.connector(company, "dadata"))
    assert res["ok"] is False and "401" in res["detail"] and "токен" in res["detail"]
    monkeypatch.setattr(connectors.requests, "post", lambda url, **kw: _resp(200, {"suggestions": [{"value": "ПАО СБЕРБАНК", "data": {"state": {"status": "ACTIVE"}, "name": {"short_with_opf": "ПАО Сбербанк"}}}]}))
    assert connectors.ping(connectors.connector(company, "dadata")) == {"ok": True, "detail": "ключ принят, тестовый ИНН найден: ПАО Сбербанк"}
    assert connectors.ping(connectors.connector(company, "bank"))["detail"] == "ключи не заданы"
    monkeypatch.setattr(connectors.requests, "get", lambda url, **kw: (_ for _ in ()).throw(connectors.requests.ConnectionError("refused")))
    assert connectors.ping(connectors.BankClient("https://x", "t"))["detail"].startswith("нет соединения")


# ---------- API ----------
def test_import_api_validation_and_reports(company, client):
    r = client.post(f"/api/companies/{company.id}/import/invoices/", {"file": io.BytesIO(TABLE.encode("utf-8")), "direction": "auto"}, format="multipart")
    assert r.status_code == 200 and r.data["created"] == 2, r.data
    assert client.post(f"/api/companies/{company.id}/import/invoices/", {"file": io.BytesIO(b"<xml/>")}, format="multipart").status_code == 400
    assert client.post(f"/api/companies/{company.id}/import/invoices/", {}, format="multipart").status_code == 400
    assert client.post(f"/api/companies/{company.id}/import/ofd/", {"file": io.BytesIO("Сумма\n1\n".encode())}, format="multipart").status_code == 400
    r = client.post(f"/api/companies/{company.id}/import/ofd/", {"file": io.BytesIO(OFD.encode("utf-8")), "fee_pct": 2}, format="multipart")
    assert r.status_code == 200 and r.data["created"] == 2 and r.data["cash_total"] == 10000.0
    stranger = APIClient()
    get_user_model().objects.create_user("stranger", password="pass12345")
    stranger.credentials(HTTP_AUTHORIZATION=f"Bearer {stranger.post('/api/auth/token/', {'username': 'stranger', 'password': 'pass12345'}, format='json').data['access']}")
    assert stranger.post(f"/api/companies/{company.id}/import/ofd/", {"file": io.BytesIO(OFD.encode("utf-8"))}, format="multipart").status_code == 403


def test_config_api_masking_and_sources_list(company, client):
    r = client.put(f"/api/companies/{company.id}/integrations/bank/config/", {"config": {"base_url": "https://bank.example/api", "token": "secret-token", "junk": "x"}}, format="json")
    assert r.status_code == 200 and r.data["configured"] and r.data["values"] == {"base_url": "https://bank.example/api", "token": "…oken"}
    client.put(f"/api/companies/{company.id}/integrations/bank/config/", {"config": {"token": "…oken", "base_url": ""}}, format="json")
    assert IntegrationConfig.objects.get(company=company, kind="bank").config == {"base_url": "https://bank.example/api", "token": "secret-token"}  # маска и пустота не затирают ключ
    assert client.put(f"/api/companies/{company.id}/integrations/ozon/config/", {"config": {}}, format="json").status_code == 400
    r = client.get(f"/api/companies/{company.id}/integrations/")
    by = {s["source"]: s for s in r.data}
    assert set(by) == {"bank", "1c", "edo", "ofd", "registry", "manual", "contract", "tax_calendar"}
    assert by["bank"]["mode"] == "api" and by["bank"]["configured"] and by["bank"]["fields"][0]["name"] == "base_url"
    assert by["registry"]["mode"] == "mock" and by["1c"]["mode"] == "file" and by["manual"]["mode"] == "active"
    assert client.delete(f"/api/companies/{company.id}/integrations/bank/config/").status_code == 204
    assert {s["source"]: s["mode"] for s in client.get(f"/api/companies/{company.id}/integrations/").data}["bank"] == "derived"


def test_integrations_api_test_action_and_registry_sync(company, client, monkeypatch):
    assert client.post(f"/api/companies/{company.id}/integrations/", {"source": "1c", "action": "test"}, format="json").status_code == 400
    r = client.post(f"/api/companies/{company.id}/integrations/", {"source": "registry", "action": "test"}, format="json")
    assert r.status_code == 200 and r.data == {"source": "registry", "ok": False, "detail": "ключи не заданы"}
    monkeypatch.setattr(connectors.requests, "post", lambda url, **kw: _resp(200, {"suggestions": [{"value": "X", "data": {"state": {"status": "ACTIVE"}, "name": {"short_with_opf": "ПАО Сбербанк"}}}]}))
    client.put(f"/api/companies/{company.id}/integrations/dadata/config/", {"config": {"api_key": "k"}}, format="json")
    assert client.post(f"/api/companies/{company.id}/integrations/", {"source": "registry", "action": "test"}, format="json").data["ok"] is True
    r = client.post(f"/api/companies/{company.id}/integrations/", {"source": "registry"}, format="json")
    assert r.status_code == 200 and r.data["mode"] == "api" and r.data["updated_invoices"] == 0
    assert client.post(f"/api/companies/{company.id}/integrations/", {"source": "nope"}, format="json").status_code == 400
    r = client.get("/api/registry/check/", {"inn": "0012345678", "company": company.id})
    assert r.status_code == 200 and r.data["provider"] == "dadata" and r.data["status"] == "действует"
    assert client.get("/api/registry/check/", {"inn": "0012345678"}).data["provider"] == "mock"
