"""Коннекторы к внешним API. Ключи — в ``IntegrationConfig`` компании или в переменных окружения (общие для всех).

* ``DaDataClient`` — ЕГРЮЛ/ЕГРИП через DaData (``findById/party``): статус, ликвидация, банкротство.
* ``BankClient``   — HTTP-контракт для API банка (кредиты, зарплатный проект, неисполненные платёжки).
                     У Банка Центр-инвест публичного API нет; контракт описан ниже, адрес задаётся настройкой.

Без ключа коннектор «не настроен»: сервис использует запасной режим (заглушка реестра, восстановление
графика банка из выписки) и честно показывает это в интерфейсе.
"""
from __future__ import annotations

import datetime as dt
import logging

import requests
from django.conf import settings

from cashflow_core.sources.base import parse_date

from .models import IntegrationConfig

log = logging.getLogger(__name__)
TIMEOUT = 25
HTTP_HINTS = {401: "ключ не принят (401) — проверьте токен", 403: "нет прав у ключа (403) — выдайте доступ к нужным данным", 404: "адрес не найден (404)", 429: "превышен лимит запросов (429) — повторите через минуту"}


class ConnectorError(Exception):
    pass


def _raise_for(r: requests.Response, who: str) -> None:
    if r.status_code >= 400:
        raise ConnectorError(f"{who}: {HTTP_HINTS.get(r.status_code, f'HTTP {r.status_code}')} {r.text[:160]}".strip())


def ping(client) -> dict:
    """Проверка ключей минимальным запросом к тому же эндпоинту, что и синхронизация: {"ok", "detail"}."""
    if not client.configured:
        return {"ok": False, "detail": "ключи не заданы"}
    try:
        detail = client.ping()
    except ConnectorError as e:
        return {"ok": False, "detail": str(e)}
    except requests.RequestException as e:
        return {"ok": False, "detail": f"нет соединения: {e}"}
    return {"ok": True, "detail": detail}


class DaDataClient:
    URL = "https://suggestions.dadata.ru/suggestions/api/4_1/rs/findById/party"
    STATUS = {
        "ACTIVE": ("действует", "low", 0.95),
        "REORGANIZING": ("реорганизация", "medium", 0.75),
        "LIQUIDATING": ("ликвидация", "high", 0.4),
        "LIQUIDATED": ("ликвидирована", "high", 0.1),
        "BANKRUPT": ("банкротство", "high", 0.2),
    }
    provider = "dadata"

    def __init__(self, api_key: str = ""):
        self.api_key = api_key

    @property
    def configured(self) -> bool:
        return bool(self.api_key)

    def ping(self) -> str:
        res = self.check("7707083893")  # ПАО Сбербанк — всегда есть в ЕГРЮЛ
        return f"ключ принят, тестовый ИНН найден: {res['name']}" if res.get("found") else "ключ принят, но тестовый ИНН не найден — проверьте тариф DaData"

    def check(self, inn: str, name: str = "") -> dict:
        r = requests.post(self.URL, json={"query": inn, "count": 1, "branch_type": "MAIN"}, headers={"Authorization": f"Token {self.api_key}", "Content-Type": "application/json", "Accept": "application/json"}, timeout=TIMEOUT)
        _raise_for(r, "DaData")
        items = r.json().get("suggestions") or []
        if not items:
            return {"inn": inn, "name": name, "status": "не найден в ЕГРЮЛ/ЕГРИП", "risk_level": "medium", "tax_debt": False, "pay_probability": 0.7, "provider": self.provider, "found": False}
        d = items[0].get("data") or {}
        state = d.get("state") or {}
        label, risk, prob = self.STATUS.get(state.get("status", "ACTIVE"), ("статус неизвестен", "medium", 0.75))
        liq = state.get("liquidation_date")
        return {
            "inn": inn, "name": (d.get("name") or {}).get("short_with_opf") or items[0].get("value") or name, "status": label, "risk_level": risk, "tax_debt": False, "pay_probability": prob,
            "provider": self.provider, "found": True, "type": d.get("type"), "ogrn": d.get("ogrn"), "okved": d.get("okved"),
            "registration_date": str(dt.datetime.fromtimestamp(state["registration_date"] / 1000).date()) if state.get("registration_date") else None,
            "liquidation_date": str(dt.datetime.fromtimestamp(liq / 1000).date()) if liq else None,
        }


class BankClient:
    """Контракт (JSON, Bearer-токен):
    GET {base}/companies/{inn}/loans           → {"loans": [{"contract": "КД-12", "schedule": [{"date": "2026-07-10", "amount": 125000.5}]}]}
    GET {base}/companies/{inn}/payroll         → {"payments": [{"date": "2026-07-10", "amount": 918495}]}
    GET {base}/companies/{inn}/payment-orders?status=pending → {"orders": [{"id": 7, "date": "2026-07-03", "amount": 50000, "counterparty": "…", "inn": "…", "purpose": "…"}]}
    """

    source = "bank"

    def __init__(self, base_url: str = "", token: str = ""):
        self.base_url, self.token = base_url.rstrip("/"), token

    @property
    def configured(self) -> bool:
        return bool(self.base_url)

    def ping(self) -> str:
        loans = self._get("/companies/ping/loans").get("loans")
        return f"API отвечает, кредитов в ответе: {len(loans or [])}"

    def _get(self, path: str, **params) -> dict:
        r = requests.get(f"{self.base_url}{path}", params=params, headers={"Authorization": f"Bearer {self.token}"} if self.token else {}, timeout=TIMEOUT)
        _raise_for(r, "API банка")
        return r.json()

    def events(self, inn: str, as_of: dt.date, horizon_end: dt.date) -> list[dict]:
        out: list[dict] = []
        in_range = lambda d: d is not None and as_of < d <= horizon_end  # noqa: E731
        for loan in self._get(f"/companies/{inn}/loans").get("loans") or []:
            for p in loan.get("schedule") or []:
                d = parse_date(p.get("date"))
                if in_range(d):
                    out.append({"kind": "bank_credit", "direction": "Списание", "amount": float(p["amount"]), "date": d, "category": "LOAN_OUT", "label": f"График кредита {loan.get('contract', '')}".strip(), "counterparty_name": "Банк", "source": "bank", "probability": 1.0, "external_ref": f"bank:loan:{loan.get('contract', '')}:{d}"})
        for p in self._get(f"/companies/{inn}/payroll").get("payments") or []:
            d = parse_date(p.get("date"))
            if in_range(d):
                out.append({"kind": "bank_payroll", "direction": "Списание", "amount": float(p["amount"]), "date": d, "category": "SALARY", "label": "Зарплатный проект", "counterparty_name": "Сотрудники", "source": "bank", "probability": 0.98, "external_ref": f"bank:payroll:{d}"})
        for o in self._get(f"/companies/{inn}/payment-orders", status="pending").get("orders") or []:
            d = parse_date(o.get("date"))
            if in_range(d):
                out.append({"kind": "bank_order", "direction": "Списание", "amount": float(o["amount"]), "date": d, "category": "SUPPLIERS", "label": (o.get("purpose") or "Платёжное поручение")[:255], "counterparty_name": (o.get("counterparty") or "")[:255], "counterparty_inn": (o.get("inn") or "")[:12], "source": "bank", "probability": 0.95, "external_ref": f"bank:order:{o.get('id', d)}"})
        return out


CONFIG_FIELDS = {
    "bank": [("base_url", "Адрес API банка"), ("token", "Токен")],
    "dadata": [("api_key", "API-ключ DaData")],
}


def config_for(company, kind: str) -> dict:
    """Настройки коннектора: из БД компании, иначе из переменных окружения."""
    rec = IntegrationConfig.objects.filter(company=company, kind=kind, enabled=True).first()
    if rec and any(rec.config.values()):
        return dict(rec.config)
    env = {"bank": {"base_url": settings.BANK_API_URL, "token": settings.BANK_API_TOKEN}, "dadata": {"api_key": settings.DADATA_API_KEY}}
    return env.get(kind, {})


def connector(company, kind: str):
    cfg = config_for(company, kind)
    if kind == "bank":
        return BankClient(cfg.get("base_url", ""), cfg.get("token", ""))
    if kind == "dadata":
        return DaDataClient(cfg.get("api_key", ""))
    raise ValueError(f"Неизвестный коннектор {kind}")
