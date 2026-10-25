"""Оценка категоризации на вручную размеченной выборке.

    python manage.py evaluate_categorizer --sample ../data/labels/sample_300.csv --labels ../data/labels/labels_300.csv

Сравниваются три варианта: только правила; правила + контрагент; полный пайплайн
(правила + контрагент + ML). Результат сохраняется в последний BacktestResult
(поле categorizer_metrics["manual"]) и показывается на экране «Качество прогноза».
"""
from __future__ import annotations

import pandas as pd
import hashlib
import json
from pathlib import Path
from django.core.management.base import CommandError
from core.commands import LoggedCommand
from sklearn.metrics import accuracy_score, f1_score, precision_score, recall_score, confusion_matrix

from apps.forecasting.models import BacktestResult
from apps.operations import services as ops_services
from apps.operations.models import Operation
from cashflow_core.categories import LABELS
from cashflow_core.categorizer import Categorizer
from cashflow_core.categorizer.pipeline import apply_rules, propagate_by_counterparty


class Command(LoggedCommand):
    def add_arguments(self, parser):
        parser.add_argument("--sample", required=True)
        parser.add_argument("--labels", required=True)
        parser.add_argument("--output", help="Путь к JSON-отчёту")

    def handle(self, *args, **opts):
        sample = pd.read_csv(opts["sample"], sep=";", encoding="utf-8-sig", dtype={"counterparty_inn": str, "id": int}).fillna("")
        labels = pd.read_csv(opts["labels"], sep=";", encoding="utf-8-sig")
        try:
            df = sample.merge(labels, on="n", validate='one_to_one')
        except pd.errors.MergeError as exc:
            raise CommandError('Повторяются номера строк в разметке') from exc
        if len(df) != len(sample) or df.empty or not df.label.isin(LABELS).all():
            raise CommandError('Нужна корректная категория для каждой строки выборки')
        y = df["label"]

        # для распространения по контрагенту нужна вся история компании — берём операции тех же компаний
        company_ids = df.company_id.unique().tolist()
        full = pd.DataFrame(list(Operation.objects.filter(company_id__in=company_ids).values("id", "company_id", "direction", "counterparty_inn", "counterparty_name", "payment_purpose")))
        if full.empty:
            raise CommandError('Компании выборки отсутствуют в этой базе')
        full["counterparty_inn"] = full["counterparty_inn"].fillna("")
        full["payment_purpose"] = full["payment_purpose"].fillna("")
        idx = full.set_index("id").index
        pos = idx.get_indexer(df["id"])
        if (pos < 0).any():
            raise CommandError('ID выборки отсутствуют в этой базе. Оценка остановлена вместо подстановки чужих строк.')
        for col in ('company_id','direction','counterparty_inn','counterparty_name','payment_purpose'):
            if col in df and not (df[col].fillna('').astype(str).values == full.iloc[pos][col].fillna('').astype(str).values).all():
                raise CommandError(f'Выборка не соответствует текущим операциям: {col}')

        rules = apply_rules(full)
        prop = pd.concat([propagate_by_counterparty(group, rules.loc[group.index]) for _, group in full.groupby('company_id')]).sort_index()
        cat = ops_services.get_categorizer()
        pipeline = pd.concat([cat.transform(group) for _, group in full.groupby('company_id')]).sort_index()
        has_ml = bool(cat.ml and cat.ml.models)

        results = {}
        variants = {"rules_only": rules, "rules_counterparty": prop, "full_pipeline": pipeline}
        for name, pred in variants.items():
            p = pred.iloc[pos]["category"].values
            results[name] = {
                "accuracy": round(float(accuracy_score(y, p)), 4),
                "macro_f1": round(float(f1_score(y, p, average="macro")), 4),
                "macro_precision": round(float(precision_score(y,p,average='macro',zero_division=0)),4),
                "macro_recall": round(float(recall_score(y,p,average='macro',zero_division=0)),4),
                "confusion_labels": sorted(set(y) | set(p)),
                "confusion_matrix": confusion_matrix(y,p,labels=sorted(set(y) | set(p))).tolist(),
                "weighted_f1": round(float(f1_score(y, p, average="weighted")), 4),
                "errors": int((y.values != p).sum()),
            }
            if name == "full_pipeline":
                errs = df[y.values != p].assign(predicted=p[y.values != p])
                results["error_examples"] = [
                    {"direction": r.direction, "counterparty": r.counterparty_name, "purpose": (r.payment_purpose or "")[:90], "label": LABELS.get(r.label, r.label), "predicted": LABELS.get(r.predicted, r.predicted)}
                    for r in errs.head(25).itertuples()
                ]
                per_class = []
                for c in sorted(y.unique()):
                    m = y.values == c
                    per_class.append({"category": c, "label": LABELS.get(c, c), "n": int(m.sum()), "recall": round(float((p[m] == c).mean()), 3), "precision": round(float((y.values[p == c] == c).mean()), 3) if (p == c).any() else None})
                results["per_class"] = per_class
        results["n"] = int(len(df))
        results["has_ml"] = has_ml
        results["sampling"] = f"{len(df)} размеченных операций; распространение категорий отдельно по каждой компании"
        results['independent_holdout'] = False
        results['note'] = 'Независимость этой выборки от настройки правил и обучения не подтверждена'
        results['sample_sha256'] = hashlib.sha256(Path(opts['sample']).read_bytes()).hexdigest()
        results['labels_sha256'] = hashlib.sha256(Path(opts['labels']).read_bytes()).hexdigest()

        BacktestResult.objects.create(name='categorization_diagnostics', categorizer_metrics={'manual':results}, config={'kind':'categorization_diagnostics'})
        if opts['output']:
            Path(opts['output']).write_text(json.dumps(results,ensure_ascii=False,indent=2),encoding='utf-8')
        for name in variants:
            r = results[name]
            self.stdout.write(f"{name:20s} acc={r['accuracy']:.3f} macroF1={r['macro_f1']:.3f} errors={r['errors']}")
        for e in results.get("error_examples", []):
            self.stdout.write(f"   ✗ {e['direction'][:4]} | {e['counterparty'][:30]:30s} | {e['purpose'][:60]:60s} | {e['label']} → {e['predicted']}")
