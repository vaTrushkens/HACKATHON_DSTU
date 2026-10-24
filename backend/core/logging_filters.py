"""Фильтр логов: маскирует чувствительные данные до записи.

Маскируются ИНН (10/12 цифр), номера счетов (20 цифр), телефоны, e-mail,
а также значения полей payment_purpose/counterparty_name/amount, если они
попали в сообщение целиком (например, при логировании dict'а операции).
"""
from __future__ import annotations

import logging
import re

_PATTERNS = [
    (re.compile(r"\b\d{20}\b"), "<счёт>"),
    (re.compile(r"\b\d{12}\b"), "<ИНН>"),
    (re.compile(r"\b\d{10}\b"), "<ИНН>"),
    (re.compile(r"\+?\d[\d\-\s()]{9,}\d"), "<телефон>"),
    (re.compile(r"[\w.+-]+@[\w-]+\.[\w.]+"), "<email>"),
    (re.compile(r"(['\"]?(payment_purpose|counterparty_name|counterparty_inn|amount|balance_eod)['\"]?\s*[:=]\s*)('[^']*'|\"[^\"]*\"|[\d.,-]+)"), r"\1<скрыто>"),
    (re.compile(r"(Bearer\s+)[A-Za-z0-9._-]+"), r"\1<token>"),
]


def mask(text: str) -> str:
    for pat, repl in _PATTERNS:
        text = pat.sub(repl, text)
    return text


class SensitiveDataFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        try:
            msg = record.getMessage()
        except Exception:  # noqa: BLE001
            return True
        record.msg = mask(msg)
        record.args = ()
        return True
