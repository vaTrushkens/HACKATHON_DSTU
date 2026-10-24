"""Загрузка и очистка банковских выписок.

Поддерживаются два формата:

* CSV кейса (``;``, UTF-8 с BOM, поля company_id … balance_eod);
* текстовый обмен «1С:Клиент-банк» (``1CClientBankExchange``) — реальный
  формат, который выгружает любой российский банк и 1С.

На выходе всегда один и тот же DataFrame со стандартными колонками, см. ``COLUMNS``.
"""
from __future__ import annotations

import io
import hashlib
import json
import re
from dataclasses import dataclass, field

import numpy as np
import pandas as pd

COLUMNS = [
    "operation_id",
    "operation_date",
    "amount",
    "direction",
    "counterparty_inn",
    "counterparty_name",
    "payment_purpose",
    "balance_eod",
]
DIR_IN = "Поступление"
DIR_OUT = "Списание"

DEDUP_KEYS = ["operation_date", "amount", "direction", "counterparty_inn", "payment_purpose"]


@dataclass
class IngestReport:
    rows_total: int = 0
    rows_new: int = 0
    rows_duplicates: int = 0
    rows_bad: int = 0
    empty_inn: int = 0
    empty_purpose: int = 0
    gap_days: int = 0
    gaps: list[dict] = field(default_factory=list)
    first_date: str | None = None
    last_date: str | None = None
    history_days: int = 0
    warnings: list[str] = field(default_factory=list)

    def as_dict(self) -> dict:
        return self.__dict__.copy()


# ---------------------------------------------------------------------------
# CSV формата кейса
# ---------------------------------------------------------------------------
def read_statement_csv(path_or_buffer) -> pd.DataFrame:
    df = pd.read_csv(
        path_or_buffer,
        sep=None,
        engine="python",
        encoding="utf-8-sig",
        dtype={"company_id": str, "counterparty_inn": str, "company_inn": str, "operation_id": str},
    )
    df.columns = [c.strip().lstrip("﻿") for c in df.columns]
    return df


# ---------------------------------------------------------------------------
# 1С:Клиент-банк
# ---------------------------------------------------------------------------
_1C_DOC_RE = re.compile(r"СекцияДокумент=(.*?)\nКонецДокумента", re.S)


def read_1c_client_bank(text: str, own_account: str | None = None) -> pd.DataFrame:
    """Парсит выгрузку 1CClientBankExchange в стандартный DataFrame.

    Остаток на конец дня в формате 1С не передаётся по дням — есть только
    ``НачальныйОстаток``/``КонечныйОстаток`` секции счёта, поэтому ``balance_eod``
    восстанавливается кумулятивно от начального остатка.
    """
    if text.startswith("﻿"):
        text = text[1:]
    text = text.replace("\r\n", "\n")
    header = text.split("СекцияДокумент", 1)[0]
    kv = dict(_kv(line) for line in header.splitlines() if "=" in line)
    own_account = own_account or kv.get("РасчСчет") or ""
    opening = _to_float(kv.get("НачальныйОстаток"))

    rows = []
    for i, block in enumerate(_1C_DOC_RE.findall(text), start=1):
        d = dict(_kv(line) for line in block.splitlines() if "=" in line)
        payer_acc = d.get("ПлательщикСчет", "")
        is_out = own_account and payer_acc == own_account
        if not own_account:  # эвристика: если счёт не указан, считаем по полю ДатаСписано
            is_out = bool(d.get("ДатаСписано"))
        date = d.get("ДатаСписано") or d.get("ДатаПоступило") or d.get("Дата")
        rows.append(
            {
                "operation_id": ("1C-" + hashlib.sha256(json.dumps([d.get("Номер"), date, payer_acc, d.get("ПолучательСчет"), d.get("Сумма"), d.get("НазначениеПлатежа")], ensure_ascii=False).encode()).hexdigest()[:48]) if d.get("Номер") else None,
                "operation_date": pd.to_datetime(date, format="%d.%m.%Y", errors="coerce"),
                "amount": _to_float(d.get("Сумма")),
                "direction": DIR_OUT if is_out else DIR_IN,
                "counterparty_inn": (d.get("ПолучательИНН") if is_out else d.get("ПлательщикИНН")) or "",
                "counterparty_name": (d.get("Получатель1") or d.get("Получатель"))
                if is_out
                else (d.get("Плательщик1") or d.get("Плательщик")),
                "payment_purpose": d.get("НазначениеПлатежа", ""),
            }
        )
    df = pd.DataFrame(rows, columns=COLUMNS[:-1])
    if df.empty:
        df["balance_eod"] = []
        return df
    df = df.sort_values("operation_date").reset_index(drop=True)
    signed = np.where(df.direction == DIR_IN, df.amount, -df.amount)
    running = (opening or 0.0) + np.cumsum(signed)
    df["balance_eod"] = pd.Series(running).groupby(df.operation_date).transform("last").values
    return df


def _kv(line: str) -> tuple[str, str]:
    k, _, v = line.partition("=")
    return k.strip(), v.strip()


def _to_float(v) -> float | None:
    if v is None or v == "":
        return None
    return float(str(v).replace(",", ".").replace(" ", ""))


# ---------------------------------------------------------------------------
# Очистка
# ---------------------------------------------------------------------------
def clean_operations(df: pd.DataFrame) -> tuple[pd.DataFrame, IngestReport]:
    """Нормализует типы, удаляет дубли и битые строки, считает отчёт о качестве."""
    rep = IngestReport(rows_total=len(df))
    df = df.copy()
    for col in COLUMNS:
        if col not in df.columns:
            df[col] = np.nan

    df["operation_date"] = pd.to_datetime(df["operation_date"], errors="coerce").dt.normalize()
    df["amount"] = pd.to_numeric(df["amount"], errors="coerce").abs()
    df["balance_eod"] = pd.to_numeric(df["balance_eod"], errors="coerce")
    df["direction"] = df["direction"].astype(str).str.strip().str.capitalize()
    df.loc[~df.direction.isin([DIR_IN, DIR_OUT]), "direction"] = np.nan
    df["counterparty_inn"] = df["counterparty_inn"].fillna("").astype(str).str.strip().replace({"nan": ""})
    df["counterparty_name"] = df["counterparty_name"].fillna("").astype(str).str.strip()
    df["payment_purpose"] = df["payment_purpose"].fillna("").astype(str).str.strip()

    bad = df.operation_date.isna() | ~np.isfinite(df.amount) | df.direction.isna() | (df.amount <= 0)
    rep.rows_bad = int(bad.sum())
    df = df[~bad].copy()
    df["balance_eod"] = df.balance_eod.where(np.isfinite(df.balance_eod))
    df["operation_id"] = df.operation_id.fillna("").astype(str).str.strip()
    missing_id = df.operation_id.eq("")
    if missing_id.any():
        # Identical rows without bank IDs are ambiguous: preserve their multiplicity.
        # The content hash plus occurrence number is stable when the file is reloaded.
        keys = df.loc[missing_id, DEDUP_KEYS + ["counterparty_name"]].astype(str).apply(lambda row: json.dumps(row.tolist(), ensure_ascii=False), axis=1)
        ordinal = keys.groupby(keys).cumcount()
        df.loc[missing_id, "operation_id"] = ["AUTO-" + hashlib.sha256(k.encode()).hexdigest()[:40] + f"-{n}" for k, n in zip(keys, ordinal)]
        rep.warnings.append("Нет банковских ID: использованы отпечатки содержимого; одинаковые платежи сохранены по числу строк. При частично пересекающихся файлах без ID проверьте их вручную.")
    if (df.operation_id.str.len() > 64).any():
        raise ValueError("Идентификатор операции длиннее 64 символов")
    id_keys = ["operation_date", "operation_id"]
    if "company_id" in df.columns:
        id_keys.insert(0, "company_id")
    repeated = df[df.duplicated(id_keys, keep=False)]
    if not repeated.empty and (repeated.groupby(id_keys)[DEDUP_KEYS[1:] + ["counterparty_name", "balance_eod"]].nunique(dropna=True) > 1).any().any():
        raise ValueError("Один ID операции в один день содержит противоречивые данные")
    dup = df.duplicated(subset=id_keys, keep="first")
    rep.rows_duplicates = int(dup.sum())
    df = df[~dup]

    rep.empty_inn = int((df.counterparty_inn == "").sum())
    rep.empty_purpose = int((df.payment_purpose == "").sum())

    df = df.sort_values(["operation_date", "operation_id"]).reset_index(drop=True)
    if df.empty:
        rep.warnings.append("В выписке нет корректных операций")
        return df, rep

    if (df.groupby("operation_date").balance_eod.nunique() > 1).any():
        raise ValueError("В один день указаны разные остатки на конец дня")
    # Остаток одинаков для всех операций дня; если он пропущен — восстанавливаем.
    if df.balance_eod.isna().any():
        if df.balance_eod.isna().all():
            rep.warnings.append("Все остатки отсутствуют: начальный остаток принят равным нулю. Проверьте остаток перед использованием прогноза.")
        df = _restore_balances(df)
        rep.warnings.append("Часть остатков отсутствовала и восстановлена кумулятивно")

    rep.first_date = str(df.operation_date.min().date())
    rep.last_date = str(df.operation_date.max().date())
    rep.history_days = int((df.operation_date.max() - df.operation_date.min()).days) + 1
    if rep.history_days < 90:
        rep.warnings.append("Короткая история (< 3 месяцев): сезонность ненадёжна, используется среднее по доступным данным")

    daily = daily_balance_check(df)
    gaps = daily[daily.gap_amount.abs() > 1.0]
    rep.gap_days = int(len(gaps))
    rep.gaps = [
        {"date": str(r.operation_date.date()), "gap_amount": round(float(r.gap_amount), 2), "days_since_prev": int(r.days_since_prev)}
        for r in gaps.itertuples()
    ]
    if rep.gap_days:
        rep.warnings.append(f"Обнаружено пропусков в выписке: {rep.gap_days} (остаток «скачет»)")
    return df, rep


def _restore_balances(df: pd.DataFrame) -> pd.DataFrame:
    """Fill only missing daily balances, preserving every observed daily anchor."""
    net = pd.Series(np.where(df.direction == DIR_IN, df.amount, -df.amount), index=df.operation_date).groupby(level=0).sum()
    known = df.groupby("operation_date").balance_eod.first().reindex(net.index)
    cumulative = net.cumsum()
    offsets = (known - cumulative).ffill().bfill().fillna(0.0)
    restored = known.fillna(cumulative + offsets)
    df = df.copy()
    df["balance_eod"] = df.balance_eod.fillna(df.operation_date.map(restored))
    return df


def daily_balance_check(df: pd.DataFrame) -> pd.DataFrame:
    """Сверка остатков: prev_balance + inflow − outflow должно равняться balance_eod.

    Расхождение означает пропуск операций в выписке за дни между соседними записями.
    """
    g = df.groupby("operation_date")
    daily = pd.DataFrame(
        {
            "inflow": g.apply(lambda x: x.loc[x.direction == DIR_IN, "amount"].sum(), include_groups=False),
            "outflow": g.apply(lambda x: x.loc[x.direction == DIR_OUT, "amount"].sum(), include_groups=False),
            "balance_eod": g.balance_eod.first(),
        }
    ).reset_index()
    daily["prev_balance"] = daily.balance_eod.shift()
    daily["days_since_prev"] = daily.operation_date.diff().dt.days.fillna(0)
    daily["gap_amount"] = (daily.balance_eod - (daily.prev_balance + daily.inflow - daily.outflow)).fillna(0.0).round(2)
    return daily


def opening_balance(df: pd.DataFrame) -> float:
    """Остаток на начало первого дня = balance_eod − поступления + списания этого дня."""
    if df.empty:
        return 0.0
    first = df[df.operation_date == df.operation_date.min()]
    signed = np.where(first.direction == DIR_IN, first.amount, -first.amount).sum()
    return float(first.balance_eod.iloc[0] - signed)


def to_standard_frame(raw: pd.DataFrame) -> pd.DataFrame:
    """Приводит произвольный CSV с похожими колонками к стандартным именам."""
    aliases = {
        "date": "operation_date",
        "дата": "operation_date",
        "дата операции": "operation_date",
        "сумма": "amount",
        "направление": "direction",
        "инн": "counterparty_inn",
        "инн контрагента": "counterparty_inn",
        "контрагент": "counterparty_name",
        "наименование контрагента": "counterparty_name",
        "назначение платежа": "payment_purpose",
        "назначение": "payment_purpose",
        "остаток": "balance_eod",
        "остаток на конец дня": "balance_eod",
    }
    df = raw.rename(columns={c: aliases.get(c.strip().lower(), c) for c in raw.columns})
    if "direction" not in df.columns and "debit" in df.columns and "credit" in df.columns:
        debit = pd.to_numeric(df["debit"], errors="coerce").fillna(0)
        credit = pd.to_numeric(df["credit"], errors="coerce").fillna(0)
        df["amount"] = np.where(credit > 0, credit, debit)
        df["direction"] = np.where(credit > 0, DIR_IN, DIR_OUT)
    if "operation_id" not in df.columns:
        df["operation_id"] = None
    return df


def load_any(filename: str, content: bytes) -> pd.DataFrame:
    """Определяет формат файла по содержимому и возвращает стандартный DataFrame."""
    head = content[:200].decode("utf-8", errors="ignore")
    if "1CClientBankExchange" in head:
        for enc in ("utf-8-sig", "cp1251"):
            try:
                return read_1c_client_bank(content.decode(enc))
            except UnicodeDecodeError:
                continue
    raw = read_statement_csv(io.BytesIO(content))
    return to_standard_frame(raw)
