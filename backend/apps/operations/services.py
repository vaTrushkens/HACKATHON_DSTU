"""Сервисы операций: импорт, категоризация, выгрузка в DataFrame."""
from __future__ import annotations

import logging
from decimal import Decimal
from functools import lru_cache

import numpy as np
import pandas as pd
from django.conf import settings
from django.db import transaction

from apps.companies.models import Company
from cashflow_core import ingest
from cashflow_core.categories import LABELS, BY_CODE
from cashflow_core.categorizer import Categorizer
from cashflow_core.categorizer.ml import MLCategorizer

from .models import IngestLog, Operation

log = logging.getLogger(__name__)
CATEGORIZER_PATH = settings.MODELS_DIR / "categorizer.joblib"

OPS_COLUMNS = ["operation_id", "operation_date", "amount", "direction", "counterparty_inn", "counterparty_name", "payment_purpose", "balance_eod", "category", "category_confidence", "category_source"]


@lru_cache(maxsize=1)
def get_categorizer() -> Categorizer:
    """Правила + обученная ML-доразметка (если модель уже обучена командой train_models)."""
    if CATEGORIZER_PATH.exists():
        try:
            return Categorizer(ml=MLCategorizer.load(str(CATEGORIZER_PATH)))
        except Exception as exc:  # noqa: BLE001
            log.warning("categorizer model load failed: %s", type(exc).__name__)
    return Categorizer()


def reset_categorizer_cache() -> None:
    get_categorizer.cache_clear()


def ops_frame(company: Company, until=None) -> pd.DataFrame:
    """Операции компании как DataFrame ядра (даты — Timestamp, суммы — float)."""
    qs = Operation.objects.filter(company=company).values(*OPS_COLUMNS)
    if until is not None:
        qs = qs.filter(operation_date__lte=until)
    df = pd.DataFrame(list(qs), columns=OPS_COLUMNS)
    if df.empty:
        return df
    df["operation_date"] = pd.to_datetime(df["operation_date"])
    df["amount"] = df["amount"].astype(float)
    df["balance_eod"] = df["balance_eod"].astype(float)
    df["counterparty_inn"] = df["counterparty_inn"].fillna("")
    df["payment_purpose"] = df["payment_purpose"].fillna("")
    return df.sort_values(["operation_date", "operation_id"]).reset_index(drop=True)


@transaction.atomic
def import_operations(company: Company, raw: pd.DataFrame, categorizer: Categorizer | None = None, source: str = "statement") -> ingest.IngestReport:
    """Очищает, категоризирует и сохраняет операции. Уже загруженные (по operation_id) пропускаются —
    так выписку можно догружать инкрементально."""
    # Reject mixed or unrelated companies before touching stored operations.
    if "company_id" in raw.columns:
        codes = set(raw.company_id.dropna().astype(str).str.strip()) - {""}
        expected = {company.code, getattr(company, "external_code", "")} - {""}
        if len(codes) > 1 or (codes and not codes.issubset(expected)):
            raise ValueError("Файл относится к другой компании или содержит несколько компаний. Используйте «Загрузить новый набор».")
    Company.objects.select_for_update().get(pk=company.pk)
    raw = raw.copy()
    raw['_balance_missing'] = pd.to_numeric(raw['balance_eod'], errors='coerce').isna() if 'balance_eod' in raw else True
    df, report = ingest.clean_operations(raw)
    if df.empty:
        IngestLog.objects.create(company=company, source=source, rows_total=report.rows_total, rows_new=0, report=report.as_dict())
        return report
    existing = {(str(d), op_id): (float(amount), direction, inn, purpose) for d, op_id, amount, direction, inn, purpose in Operation.objects.filter(company=company).values_list("operation_date", "operation_id", "amount", "direction", "counterparty_inn", "payment_purpose")}
    df["operation_id"] = df["operation_id"].astype(str)
    if existing:
        keep = []
        for r in df.itertuples():
            old = existing.get((str(r.operation_date.date()), r.operation_id))
            if old is not None and old != (float(r.amount), r.direction, r.counterparty_inn, r.payment_purpose):
                raise ValueError("Ранее загруженная операция с тем же ID и датой отличается от файла")
            keep.append(old is None)
        report.rows_duplicates += len(keep) - sum(keep)
        df = df.loc[keep]
    if len(df) and df['_balance_missing'].any() and existing:
        # Incremental statements must inherit known balances from earlier imports.
        history = ops_frame(company)
        new = df.copy()
        new.loc[new['_balance_missing'], 'balance_eod'] = np.nan
        joined = pd.concat([history, new], ignore_index=True).sort_values('operation_date')
        if (joined.groupby('operation_date').balance_eod.nunique() > 1).any():
            raise ValueError('Остаток в файле противоречит ранее загруженному остатку того же дня')
        restored = ingest._restore_balances(joined).groupby('operation_date').balance_eod.first()
        df = df.copy()
        df['balance_eod'] = df.operation_date.map(restored)
        report.warnings = [w for w in report.warnings if not w.startswith('Все остатки отсутствуют')]
        report.warnings.append('Пропущенные остатки восстановлены с учётом ранее загруженной истории')
    cat = categorizer or get_categorizer()
    cats = cat.transform(df) if len(df) else None
    objs = []
    for i, r in enumerate(df.itertuples(index=False)):
        c = cats.iloc[i]
        objs.append(
            Operation(
                company=company,
                operation_id=r.operation_id,
                operation_date=r.operation_date.date(),
                amount=Decimal(str(round(r.amount, 2))),
                direction=r.direction,
                counterparty_inn=r.counterparty_inn or "",
                counterparty_name=r.counterparty_name or "",
                payment_purpose=r.payment_purpose or "",
                balance_eod=Decimal(str(round(r.balance_eod, 2))),
                category=c["category"],
                category_confidence=float(c["category_confidence"]),
                category_source=c["category_source"],
                source=source,
            )
        )
    Operation.objects.bulk_create(objs, batch_size=2000)
    _refresh_company_stats(company)
    report.rows_new = len(objs)
    IngestLog.objects.create(company=company, source=source, rows_total=report.rows_total, rows_new=len(objs), report=report.as_dict())
    log.info("company %s: imported %d new operations", company.code, len(objs))
    return report


def _refresh_company_stats(company: Company) -> None:
    agg = Operation.objects.filter(company=company).aggregate(first=models_min("operation_date"), last=models_max("operation_date"), n=models_count("id"))
    company.first_operation_date = agg["first"]
    company.last_operation_date = agg["last"]
    company.operations_count = agg["n"] or 0
    company.save(update_fields=["first_operation_date", "last_operation_date", "operations_count", "updated_at"])


def models_min(f):
    from django.db.models import Min

    return Min(f)


def models_max(f):
    from django.db.models import Max

    return Max(f)


def models_count(f):
    from django.db.models import Count

    return Count(f)


def recategorize(company: Company, categorizer: Categorizer | None = None, keep_manual: bool = True) -> int:
    """Пересчитывает категории (после переобучения модели). Ручные правки сохраняются."""
    df = ops_frame(company)
    if df.empty:
        return 0
    cat = categorizer or get_categorizer()
    cats = cat.transform(df)
    manual = set(Operation.objects.filter(company=company, category_source="manual").values_list("operation_date", "operation_id")) if keep_manual else set()
    objs = []
    for (date, op_id), (code, conf, src) in zip(zip(df.operation_date.dt.date, df.operation_id), cats[["category", "category_confidence", "category_source"]].itertuples(index=False)):
        if (date, op_id) in manual:
            continue
        objs.append(Operation(company=company, operation_date=date, operation_id=op_id, category=code, category_confidence=float(conf), category_source=src))
    # bulk_update требует pk — подтягиваем
    pk_map = {(d, op): pk for d, op, pk in Operation.objects.filter(company=company).values_list("operation_date", "operation_id", "id")}
    for o in objs:
        o.id = pk_map[(o.operation_date, o.operation_id)]
    Operation.objects.bulk_update(objs, ["category", "category_confidence", "category_source"], batch_size=2000)
    return len(objs)


def set_manual_category(op: Operation, category: str) -> Operation:
    if category not in LABELS:
        raise ValueError("unknown category")
    if BY_CODE[category].direction != ("in" if op.direction == "Поступление" else "out"):
        raise ValueError("Категория не соответствует направлению операции")
    op.category = category
    op.category_confidence = 1.0
    op.category_source = "manual"
    op.save(update_fields=["category", "category_confidence", "category_source"])
    return op


def category_summary(company: Company, as_of: pd.Timestamp, months: int = 3) -> dict:
    df = ops_frame(company)
    if df.empty:
        return {"inflow": [], "outflow": [], "months": months}
    start = as_of - pd.DateOffset(months=months)
    df = df[(df.operation_date > start) & (df.operation_date <= as_of)]
    out = {"months": months, "from": str(start.date()), "to": str(as_of.date())}
    for key, direction in (("inflow", "Поступление"), ("outflow", "Списание")):
        g = df[df.direction == direction].groupby("category").agg(total=("amount", "sum"), n=("amount", "size"), avg_conf=("category_confidence", "mean")).reset_index()
        g["label"] = g.category.map(LABELS)
        g["monthly_avg"] = g.total / months
        g = g.sort_values("total", ascending=False)
        out[key] = g.round(2).to_dict(orient="records")
    src = df.category_source.value_counts().to_dict()
    out["sources"] = {k: int(v) for k, v in src.items()}
    out["coverage"] = round(float((df.category_confidence >= 0.6).mean()), 3) if len(df) else None
    return out


def last_ingest_report(company: Company) -> dict | None:
    rec = IngestLog.objects.filter(company=company).first()
    return rec.report if rec else None
