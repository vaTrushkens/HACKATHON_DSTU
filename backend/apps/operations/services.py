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
from cashflow_core.categories import LABELS
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
    df, report = ingest.clean_operations(raw)
    if df.empty:
        IngestLog.objects.create(company=company, source=source, rows_total=report.rows_total, rows_new=0, report=report.as_dict())
        return report
    existing = set(Operation.objects.filter(company=company).values_list("operation_id", flat=True))
    df["operation_id"] = df["operation_id"].astype(str)
    if existing:
        # для файлов без стабильных id дополнительно отсекаем дубли по содержимому
        dup_content = set(Operation.objects.filter(company=company).values_list("operation_date", "amount", "direction", "payment_purpose"))
        content_key = list(zip(df.operation_date.dt.date, df.amount.map(lambda a: Decimal(str(round(a, 2)))), df.direction, df.payment_purpose))
        mask_new = ~df.operation_id.isin(existing).values & np.array([k not in dup_content for k in content_key], dtype=bool)
        df = df[mask_new]
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
    manual = set(Operation.objects.filter(company=company, category_source="manual").values_list("operation_id", flat=True)) if keep_manual else set()
    objs = []
    for op_id, (code, conf, src) in zip(df.operation_id, cats[["category", "category_confidence", "category_source"]].itertuples(index=False)):
        if op_id in manual:
            continue
        objs.append(Operation(company=company, operation_id=op_id, category=code, category_confidence=float(conf), category_source=src))
    # bulk_update требует pk — подтягиваем
    pk_map = dict(Operation.objects.filter(company=company).values_list("operation_id", "id"))
    for o in objs:
        o.id = pk_map[o.operation_id]
    Operation.objects.bulk_update(objs, ["category", "category_confidence", "category_source"], batch_size=2000)
    return len(objs)


def set_manual_category(op: Operation, category: str) -> Operation:
    if category not in LABELS:
        raise ValueError("unknown category")
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
