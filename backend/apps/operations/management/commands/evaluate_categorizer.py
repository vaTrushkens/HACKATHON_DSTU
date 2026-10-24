"""Оценка категоризации на вручную размеченной выборке.

    python manage.py evaluate_categorizer --sample ../data/labels/sample_300.csv --labels ../data/labels/labels_300.csv

Сравниваются три варианта: только правила; правила + контрагент; полный пайплайн
(правила + контрагент + ML). Результат сохраняется в последний BacktestResult
(поле categorizer_metrics["manual"]) и показывается на экране «Качество прогноза».
"""
from __future__ import annotations

import pandas as pd
from django.core.management.base import BaseCommand
from sklearn.metrics import accuracy_score, f1_score

from apps.forecasting.models import BacktestResult
from apps.operations import services as ops_services
from apps.operations.models import Operation
from cashflow_core.categories import LABELS
from cashflow_core.categorizer import Categorizer
from cashflow_core.categorizer.pipeline import apply_rules, propagate_by_counterparty


class Command(BaseCommand):
    def add_arguments(self, parser):
        parser.add_argument("--sample", required=True)
        parser.add_argument("--labels", required=True)

    def handle(self, *args, **opts):
        sample = pd.read_csv(opts["sample"], sep=";", encoding="utf-8-sig", dtype={"counterparty_inn": str, "id": int}).fillna("")
        labels = pd.read_csv(opts["labels"], sep=";", encoding="utf-8-sig")
        df = sample.merge(labels, on="n")
        y = df["label"]

        # для распространения по контрагенту нужна вся история компании — берём операции тех же компаний
        company_ids = df.company_id.unique().tolist()
        full = pd.DataFrame(list(Operation.objects.filter(company_id__in=company_ids).values("id", "direction", "counterparty_inn", "counterparty_name", "payment_purpose")))
        full["counterparty_inn"] = full["counterparty_inn"].fillna("")
        full["payment_purpose"] = full["payment_purpose"].fillna("")
        idx = full.set_index("id").index
        pos = idx.get_indexer(df["id"])

        rules = apply_rules(full)
        prop = propagate_by_counterparty(full, rules)
        cat = ops_services.get_categorizer()
        pipeline = cat.transform(full)
        has_ml = bool(cat.ml and cat.ml.models)

        results = {}
        variants = {"rules_only": rules, "rules_counterparty": prop, "full_pipeline": pipeline}
        for name, pred in variants.items():
            p = pred.iloc[pos]["category"].values
            results[name] = {
                "accuracy": round(float(accuracy_score(y, p)), 4),
                "macro_f1": round(float(f1_score(y, p, average="macro")), 4),
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
        results["sampling"] = "300 операций: 120 из «сложных» (слабое правило, пустое назначение, прочее) + 60 поступлений + 120 списаний случайно"

        rec = BacktestResult.objects.first()
        if rec:
            rec.categorizer_metrics = {**rec.categorizer_metrics, "manual": results}
            rec.save(update_fields=["categorizer_metrics"])
        for name in variants:
            r = results[name]
            self.stdout.write(f"{name:20s} acc={r['accuracy']:.3f} macroF1={r['macro_f1']:.3f} errors={r['errors']}")
        for e in results.get("error_examples", []):
            self.stdout.write(f"   ✗ {e['direction'][:4]} | {e['counterparty'][:30]:30s} | {e['purpose'][:60]:60s} | {e['label']} → {e['predicted']}")
