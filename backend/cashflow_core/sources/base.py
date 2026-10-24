"""Общие типы и утилиты разбора: даты в двух форматах, суммы с пробелами и запятыми, XML без привязки к namespace."""
from __future__ import annotations

import csv
import datetime as dt
import io
import re
from dataclasses import dataclass

import pandas as pd

_DATE_ISO = re.compile(r"^(\d{4})-(\d{2})-(\d{2})")
_DATE_RU = re.compile(r"^(\d{2})\.(\d{2})\.(\d{4})")


def parse_date(value) -> dt.date | None:
    if value is None or (isinstance(value, float) and pd.isna(value)):
        return None
    if isinstance(value, dt.datetime):
        return value.date()
    if isinstance(value, dt.date):
        return value
    s = str(value).strip()
    try:
        if m := _DATE_ISO.match(s):
            return dt.date(int(m[1]), int(m[2]), int(m[3]))
        if m := _DATE_RU.match(s):
            return dt.date(int(m[3]), int(m[2]), int(m[1]))
    except ValueError:
        return None
    return None


def parse_amount(value) -> float | None:
    if value is None or (isinstance(value, float) and pd.isna(value)):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    s = re.sub(r"(руб\.?|р\.|₽|rub)", "", str(value).strip().lower())
    s = re.sub(r"[\s ']", "", s)
    if not s:
        return None
    s = s.replace(",", ".") if (s.count(",") == 1 and "." not in s) else s.replace(",", "")
    try:
        return float(s)
    except ValueError:
        return None


@dataclass
class SourceInvoice:
    """Счёт/документ из внешнего источника в нормализованном виде; направление может быть не определено."""

    number: str
    date: dt.date | None
    amount: float
    counterparty_name: str = ""
    counterparty_inn: str = ""
    counterparty_role: str = ""  # Покупатель | Продавец (роль контрагента в документе)
    seller_inn: str = ""
    seller_name: str = ""
    buyer_inn: str = ""
    buyer_name: str = ""
    due_date: dt.date | None = None
    direction: str = ""  # Поступление | Списание | ""
    label: str = ""
    external_id: str = ""
    kind: str = ""


def detect_format(filename: str, data: bytes) -> str:
    """commerceml | upd | xml | xlsx | csv — по имени и первым килобайтам содержимого."""
    name = (filename or "").lower()
    head = data[:6000].decode("utf-8", "ignore").lstrip("﻿").strip().lower()
    if name.endswith(".xml") or head.startswith("<?xml") or head.startswith("<"):
        if "коммерческаяинформация" in head or "commerceml" in head:
            return "commerceml"
        if "<файл" in head or "счфакт" in head or "nschf" in head:
            return "upd"
        return "xml"
    if name.endswith((".xlsx", ".xlsm", ".xls")):
        return "xlsx"
    return "csv"


def local(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def child(el, name: str):
    if el is None:
        return None
    return next((c for c in el if local(c.tag) == name), None)


def text(el, *path: str) -> str:
    cur = el
    for p in path:
        cur = child(cur, p)
        if cur is None:
            return ""
    return (cur.text or "").strip() if cur is not None else ""


def read_table(filename: str, data: bytes) -> pd.DataFrame:
    """CSV (разделитель и кодировка определяются сами) или Excel → DataFrame со строковыми колонками в нижнем регистре."""
    if (filename or "").lower().endswith((".xlsx", ".xlsm", ".xls")):
        df = pd.read_excel(io.BytesIO(data), dtype=str)
    else:
        try:
            txt = data.decode("utf-8-sig")
        except UnicodeDecodeError:
            txt = data.decode("cp1251")
        try:
            df = pd.read_csv(io.StringIO(txt), sep=None, engine="python", dtype=str)
        except (csv.Error, pd.errors.ParserError):  # одна колонка или экзотический разделитель — пробуем «;»
            try:
                df = pd.read_csv(io.StringIO(txt), sep=";", dtype=str)
            except (pd.errors.ParserError, pd.errors.EmptyDataError) as e:
                raise ValueError(f"не удалось разобрать CSV: {e}") from e
        except pd.errors.EmptyDataError as e:
            raise ValueError("файл пустой") from e
    df.columns = [str(c).strip().lower() for c in df.columns]
    return df.fillna("")


def find_col(df: pd.DataFrame, aliases: list[str], exclude: set[str] | None = None) -> str | None:
    exclude = exclude or set()
    for a in aliases:
        for c in df.columns:
            if c in exclude:
                continue
            if c == a or c.startswith(a):
                return c
    return None
